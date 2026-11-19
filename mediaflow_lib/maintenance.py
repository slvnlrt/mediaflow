"""Coherent private backups, staged restores and data-plus-image rollback."""
from __future__ import annotations

import copy
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import shutil
import subprocess
import tarfile
import tempfile
import time

from .common import Context, ProvisionError, write_private
from .compose import ensure_network, write_compose
from .config import prepare_storage, validate
from .image_updates import UpdatePlan, merge_overrides

_TOPOLOGY_KEYS = ("project_name", "data_path", "bind_ip", "uid", "gid", "ports", "timezone")


def _should_exclude(path: str) -> bool:
    return (path == "apps/jellyfin-cache" or path.startswith("apps/jellyfin-cache/")
            or "__pycache__" in PurePosixPath(path).parts or path == "operation.lock")


def _running_services(ctx) -> list[str]:
    if not (ctx.state / "compose.json").exists():
        return []
    result = ctx.compose("ps", "--services", "--filter", "status=running", capture=True)
    return result.stdout.split()


def _docker(*args: str) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ProvisionError("Docker inspection failed; no application state changed")
    return result.stdout


def snapshot_images(ctx) -> tuple[dict[str, str], dict[str, str]]:
    """Read actual container image IDs and portable registry digests, without env."""
    ids = ctx.compose("ps", "--all", "--quiet", capture=True).stdout.split()
    if not ids:
        return {}, {}
    rows = _docker("inspect", "--format",
                   '{{index .Config.Labels "com.docker.compose.service"}} {{.Image}} {{.Config.Image}}', *ids)
    local, portable = {}, {}
    for row in rows.splitlines():
        service, image_id, original_reference = row.split()
        digests = json.loads(_docker("image", "inspect", "--format", "{{json .RepoDigests}}", image_id))
        if not digests:
            raise ProvisionError(f"{service}: image has no portable registry digest for backup")
        reference = original_reference if "@sha256:" in original_reference else digests[0]
        local[service], portable[service] = image_id, reference
    return local, portable


def _check_member_safety(member: tarfile.TarInfo) -> None:
    name = member.name.rstrip("/")
    parts = PurePosixPath(name).parts
    if not name or name.startswith("/") or ".." in parts or "\\" in name:
        raise ProvisionError("Unsafe archive path")
    if name != "_backup_meta.json" and not name.startswith("state/"):
        raise ProvisionError("Archive entry outside state/")
    if member.islnk() or not (member.isfile() or member.isdir() or member.issym()):
        raise ProvisionError("Unsupported archive entry type")
    if name == "_backup_meta.json" and not member.isfile():
        raise ProvisionError("Archive metadata must be a regular file")
    if member.issym():
        target = member.linkname
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
        if target.startswith("/") or "\\" in target or not (resolved == "state" or resolved.startswith("state/")):
            raise ProvisionError("Archive symlink escapes state/")


def _members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = tar.getmembers()
    names, links = set(), set()
    for member in members:
        _check_member_safety(member)
        name = member.name.rstrip("/")
        if name in names:
            raise ProvisionError("Duplicate archive entry")
        names.add(name)
        if member.issym():
            links.add(name)
    for member in members:
        if any(str(parent) in links for parent in PurePosixPath(member.name).parents):
            raise ProvisionError("Archive writes through a symlink")
    return members


def _json_member(tar: tarfile.TarFile, name: str, limit: int) -> dict:
    try:
        member = tar.getmember(name)
    except KeyError:
        raise ProvisionError(f"Archive missing {name}") from None
    if not member.isfile() or member.size > limit:
        raise ProvisionError(f"Invalid or oversized {name}")
    with tar.extractfile(member) as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ProvisionError(f"Invalid {name}")
    return value


def _validate_archive(path: Path) -> dict:
    try:
        with tarfile.open(path, "r:gz") as tar:
            _members(tar)
            metadata = _json_member(tar, "_backup_meta.json", 1024 * 1024)
    except (tarfile.TarError, OSError, ValueError):
        raise ProvisionError("Unreadable or invalid backup archive") from None
    if metadata.get("schema") != 1 or not metadata.get("project_name"):
        raise ProvisionError("Unsupported or incomplete backup schema")
    return metadata


def inspect_backup(archive: Path) -> dict:
    metadata = _validate_archive(archive)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            config = _json_member(tar, "state/config.json", 10 * 1024 * 1024)
    except (tarfile.TarError, OSError, ValueError):
        raise ProvisionError("Invalid private configuration in backup") from None
    return {"metadata": metadata, "config": config}


def _add_json(tar: tarfile.TarFile, name: str, data: dict) -> None:
    raw = json.dumps(data, indent=2).encode()
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(raw), 0o600, int(time.time())
    info.uid, info.gid = os.getuid(), os.getgid()
    tar.addfile(info, BytesIO(raw))


def _destination(ctx, destination: Path) -> Path:
    destination = destination.resolve()
    if destination.is_relative_to(ctx.state.resolve()):
        raise ProvisionError("Backup destination must be outside .state")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    return destination


def _create_archive(ctx, destination: Path, portable_images: dict) -> Path:
    fd, name = tempfile.mkstemp(prefix=time.strftime("mediaflow-%Y%m%d-%H%M%S-"),
                                suffix=".tar.gz", dir=destination)
    archive = Path(name)
    snapshot = copy.deepcopy(ctx.cfg)
    if portable_images:
        snapshot["image_overrides"] = {**snapshot.get("image_overrides", {}), **portable_images}
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output, tarfile.open(fileobj=output, mode="w:gz", dereference=False) as tar:
            _add_json(tar, "_backup_meta.json", {"schema": 1, "project_name": ctx.cfg["project_name"]})
            _add_json(tar, "state/config.json", snapshot)
            for base, dirs, files in os.walk(ctx.state, followlinks=False):
                rel = Path(base).relative_to(ctx.state)
                dirs[:] = sorted(d for d in dirs if not _should_exclude((rel / d).as_posix()))
                for name in [*dirs, *sorted(files)]:
                    path = Path(base) / name
                    relative = path.relative_to(ctx.state).as_posix()
                    if relative == "config.json" or _should_exclude(relative):
                        continue
                    info = tar.gettarinfo(str(path), arcname=f"state/{relative}")
                    if info is None:
                        raise ProvisionError("A runtime special file could not be backed up")
                    _check_member_safety(info)
                    if info.issym() and not path.resolve().is_relative_to(ctx.state.resolve()):
                        raise ProvisionError("A runtime symlink points outside private state")
                    info.uname = info.gname = ""
                    if info.isfile():
                        with path.open("rb") as source:
                            tar.addfile(info, source)
                    else:
                        tar.addfile(info)
        return archive
    except BaseException:
        archive.unlink(missing_ok=True)
        raise


def _stop(ctx, running: list[str]) -> None:
    try:
        ctx.compose("stop")
    except BaseException as original:
        if running:
            try:
                ctx.compose("start", *running)
            except ProvisionError:
                raise ProvisionError("Stop failed and the previous service set could not be restored") from original
        raise


def backup(ctx, destination: Path) -> Path:
    destination = _destination(ctx, destination)
    running = _running_services(ctx)
    _, portable = snapshot_images(ctx)
    _stop(ctx, running)
    try:
        archive = _create_archive(ctx, destination, portable)
    except BaseException as original:
        if running:
            try:
                ctx.compose("start", *running)
            except ProvisionError:
                raise ProvisionError("Backup failed and services could not be restarted") from original
        raise
    if running:
        try:
            ctx.compose("start", *running)
        except ProvisionError:
            raise ProvisionError(f"Backup saved at {archive}, but service restart failed") from None
    ctx.note(f"Private backup saved: {archive}")
    return archive


def _owner(member, relative: str, source_cfg: dict, target_cfg: dict) -> tuple[int, int]:
    if relative == "apps/seerr" or relative.startswith("apps/seerr/"):
        return 1000, 1000
    uid = target_cfg["uid"] if member.uid == source_cfg["uid"] else member.uid
    gid = target_cfg["gid"] if member.gid == source_cfg["gid"] else member.gid
    return uid, gid


def _extract(archive: Path, stage: Path, source_cfg: dict, target_cfg: dict) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        members = _members(tar)
        total = sum(m.size for m in members if m.isfile())
        if shutil.disk_usage(stage).free < total + 16 * 1024**2:
            raise ProvisionError("Insufficient free space to stage the restored data")
        links, owners = [], []
        for member in members:
            if not member.name.startswith("state/"):
                continue
            relative = member.name.removeprefix("state/")
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if member.issym():
                links.append((target, member.linkname))
                continue
            if member.isdir():
                target.mkdir(exist_ok=True, mode=0o700)
            else:
                with tar.extractfile(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
            owners.append((target, *_owner(member, relative, source_cfg, target_cfg), member.mode & 0o777))
        for target, link in links:
            target.symlink_to(link)
        for target, _ in links:
            if not target.resolve().is_relative_to(stage.resolve()):
                raise ProvisionError("Restored symlink escapes private state")
        for target, uid, gid, mode in sorted(owners, key=lambda entry: len(entry[0].parts), reverse=True):
            if os.getuid() == 0:
                os.chown(target, uid, gid)
            elif target.stat().st_uid != uid or target.stat().st_gid != gid:
                raise ProvisionError("Restore requires root to reproduce application ownership; use explicit non-root uid/gid")
            target.chmod(mode)


def restore(ctx, archive: Path, *, overrides: dict | None = None) -> list[str]:
    info = inspect_backup(archive)
    source = info["config"]
    target = copy.deepcopy(source)
    destination = overrides if overrides is not None else {k: ctx.cfg[k] for k in _TOPOLOGY_KEYS if k in ctx.cfg}
    if set(destination) - set(_TOPOLOGY_KEYS):
        raise ProvisionError("Unsupported restore destination setting")
    target.update(destination)
    validate(target, ctx.root)
    target["phase"] = "restored"
    target.setdefault("managed", {})["published_services"] = []
    state = ctx.state
    stage = Path(tempfile.mkdtemp(prefix=".state-restore-", dir=ctx.root))
    old_cfg = copy.deepcopy(ctx.cfg)
    previous = None
    running = []
    swapped = False
    try:
        _extract(archive, stage, source, target)
        prepare_storage(Context(ctx.root, target))
        write_private(stage / "config.json", json.dumps(target, indent=2) + "\n")
        cache = stage / "apps/jellyfin-cache"
        cache.mkdir(parents=True, exist_ok=True, mode=0o750)
        if os.getuid() == 0:
            os.chown(cache, target["uid"], target["gid"])
        if (state / "compose.json").exists():
            running = _running_services(ctx)
            _stop(ctx, running)
        else:
            existing = _docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={target['project_name']}")
            if existing.strip():
                raise ProvisionError("Fresh restore would overwrite an existing Docker project")
        if state.exists():
            previous = Path(tempfile.mkdtemp(prefix=".state-previous-", dir=ctx.root))
            previous.rmdir()
            state.rename(previous)
        stage.rename(state)
        swapped = True
        ctx.cfg.clear()
        ctx.cfg.update(target)
        ensure_network(ctx)
        write_compose(ctx)
    except BaseException as original:
        if swapped and state.exists():
            shutil.rmtree(state)
        if previous is not None and previous.exists():
            previous.rename(state)
        ctx.cfg.clear()
        ctx.cfg.update(old_cfg)
        if running:
            try:
                ctx.compose("start", *running)
            except ProvisionError:
                raise ProvisionError("Restore failed; original data recovered but services could not restart") from original
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    if previous is not None:
        shutil.rmtree(previous)
    ctx.note("Configuration restored. Services remain stopped; review paths before start.")
    return running


def update(ctx, plans, *, verify=None) -> None:
    """Pull resolved images, backup, deploy and verify atomically with rollback."""
    if ctx.cfg.get("phase") != "ready":
        raise ProvisionError("Complete installation before updating")
    changed = [p for p in plans if p.changed]
    if not changed:
        ctx.note("All images are already current.")
        return
    # Verify rollback is possible before changing anything
    running = _running_services(ctx)
    _, portable = snapshot_images(ctx)
    if not portable:
        raise ProvisionError("No existing containers for safe rollback")
    original_cfg = copy.deepcopy(ctx.cfg)
    # Pull resolved images while services are still running
    for plan in changed:
        ctx.note(f"Pulling {plan.service}: {plan.new_ref}")
        try:
            result = subprocess.run(["docker", "pull", plan.new_ref],
                                    capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired):
            raise ProvisionError(
                f"Image pull failed or timed out for {plan.service}; running applications are unchanged") from None
        if result.returncode:
            raise ProvisionError(
                f"Image pull failed for {plan.service}; running applications are unchanged")
    # Stop and backup
    destination = _destination(ctx, ctx.root / "backups")
    _stop(ctx, running)
    try:
        archive = _create_archive(ctx, destination, portable)
    except BaseException as original:
        if running:
            try:
                ctx.compose("start", *running)
            except ProvisionError:
                raise ProvisionError(
                    "Pre-update backup failed and services could not restart") from original
        raise
    # Deploy with new overrides and verify inside transaction
    try:
        ctx.cfg["phase"] = "updating"
        ctx.cfg["image_overrides"] = merge_overrides(
            {**ctx.cfg.get("image_overrides", {}), **portable}, plans)
        ctx.save()
        write_compose(ctx)
        ctx.compose("up", "-d", "--remove-orphans", "--wait", "--wait-timeout", "240")
        if verify is not None:
            verify(ctx)
    except BaseException as original:
        try:
            restore(ctx, archive)
            ctx.cfg["image_overrides"] = {**original_cfg.get("image_overrides", {}), **portable}
            ctx.cfg["phase"] = "restored"
            ctx.cfg["managed"]["published_services"] = original_cfg["managed"]["published_services"]
            ctx.save()
            write_compose(ctx)
            ctx.compose("up", "-d", "--remove-orphans", "--wait", "--wait-timeout", "240")
            if verify is not None:
                verify(ctx)
            ctx.cfg["phase"] = "ready"
            ctx.save()
        except BaseException:
            ctx.cfg["phase"] = "rollback-failed"
            ctx.save()
            raise ProvisionError(
                f"Update and rollback failed; private pre-update backup: {archive}") from original
        raise ProvisionError(
            f"Update failed; previous data and images restored. Backup: {archive}") from original
    ctx.cfg["phase"] = "ready"
    ctx.save()
    ctx.note(f"Update complete; private pre-update backup: {archive}")
