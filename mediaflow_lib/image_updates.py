"""Resolve upstream tracking tags to immutable digests for update planning."""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .common import ProvisionError
from .compose import SOURCE_ROOT


def parse_pinned(ref: str) -> tuple[str, str | None]:
    """Split 'repo:tag@sha256:…' into ('repo:tag', 'sha256:…' or None)."""
    at = ref.find("@")
    if at >= 0:
        return ref[:at], ref[at + 1:]
    return ref, None


def load_tracking() -> tuple[dict[str, str], dict[str, str]]:
    """Return (base_images, tracking_ref_per_service) from images.json."""
    path = SOURCE_ROOT / "images.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        raise ProvisionError("Cannot read or parse images.json") from None
    base = data.get("images")
    if not isinstance(base, dict) or not base:
        raise ProvisionError("images.json has no valid 'images' map")
    explicit = data.get("tracking", {})
    tracking: dict[str, str] = {}
    for service, ref in base.items():
        if service in explicit:
            tracking[service] = explicit[service]
        else:
            name, _ = parse_pinned(ref)
            tracking[service] = name
    return base, tracking


def active_services(cfg: dict) -> list[str]:
    """Services present in the composed stack that use tracked images."""
    services = ["qbittorrent", "radarr", "sonarr", "prowlarr", "jellyfin", "seerr", "recyclarr"]
    for name, value in cfg["options"].items():
        if value:
            services.append(name)
    if cfg["options"].get("wud"):
        services.append("socket-proxy")
    if cfg["vpn"]["mode"] != "none":
        services.append("gluetun")
        if cfg["vpn"].get("port_forwarding"):
            services.append("port-sync")
    return services


def check_buildx() -> None:
    """Verify that docker buildx is available."""
    try:
        result = subprocess.run(["docker", "buildx", "version"],
                                capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise ProvisionError("Docker is not installed or not in PATH") from None
    except subprocess.TimeoutExpired:
        raise ProvisionError("Docker buildx version check timed out") from None
    if result.returncode:
        raise ProvisionError(
            "Docker buildx is required for image updates; "
            "install docker-buildx-plugin and retry")


def resolve_digest(tracking_ref: str) -> str:
    """Query the registry for the current manifest digest of a tracking reference."""
    try:
        result = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect",
             "--format", "{{json .Manifest}}", tracking_ref],
            capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise ProvisionError(f"Registry query timed out for {tracking_ref}") from None
    if result.returncode:
        raise ProvisionError(f"Cannot resolve upstream digest for {tracking_ref} (Docker exit {result.returncode})")
    try:
        descriptor = json.loads(result.stdout.strip())
    except (json.JSONDecodeError, ValueError):
        raise ProvisionError(
            f"Unexpected registry response for {tracking_ref}") from None
    digest = descriptor.get("digest") if isinstance(descriptor, dict) else None
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ProvisionError(
            f"No valid sha256 digest in registry response for {tracking_ref}")
    return digest


@dataclass
class UpdatePlan:
    """Per-service resolution result comparing current pin to upstream tracking tag."""
    service: str
    tracking_ref: str
    current_ref: str
    current_digest: str | None
    new_digest: str
    new_ref: str
    changed: bool


def plan_updates(cfg: dict, services: list[str]) -> list[UpdatePlan]:
    """Resolve upstream digests for the given services.

    All resolutions must succeed; partial failure raises with every error collected.
    """
    check_buildx()
    base, tracking = load_tracking()
    overrides = cfg.get("image_overrides", {})
    missing = [s for s in services if s not in base]
    if missing:
        raise ProvisionError(f"No image defined for: {', '.join(missing)}")
    errors: list[str] = []
    plans: list[UpdatePlan] = []
    for service in services:
        track = tracking[service]
        effective = overrides.get(service, base[service])
        _, current_digest = parse_pinned(effective)
        try:
            new_digest = resolve_digest(track)
        except ProvisionError as exc:
            errors.append(str(exc))
            continue
        new_ref = f"{track}@{new_digest}"
        plans.append(UpdatePlan(
            service=service, tracking_ref=track,
            current_ref=effective, current_digest=current_digest,
            new_digest=new_digest, new_ref=new_ref,
            changed=(current_digest != new_digest)))
    if errors:
        raise ProvisionError(
            "Image resolution failed:\n  " + "\n  ".join(errors))
    return plans


def merge_overrides(current: dict[str, str], plans: list[UpdatePlan]) -> dict[str, str]:
    """Build new overrides: keep inactive selections, add changed resolved refs."""
    result = dict(current)
    for plan in plans:
        if plan.changed:
            result[plan.service] = plan.new_ref
    return result


def display_plan(plans: list[UpdatePlan]) -> bool:
    """Print per-service comparison. Return True if any service has an available update."""
    has_updates = False
    for plan in plans:
        if plan.changed:
            has_updates = True
            print(f"  {plan.service} ({plan.tracking_ref})")
            print(f"    actuel : {plan.current_ref}")
            print(f"    proposé : {plan.new_ref}")
        else:
            print(f"  {plan.service} : à jour ({plan.current_digest})")
    return has_updates


def ensure_pins(cfg: dict, active: list[str], container_refs: dict[str, str]) -> bool:
    """Ensure every active service has a durable pin in image_overrides.

    Uses container_refs (portable registry digests) for services with running
    containers, falls back to images.json base for newly enabled services.
    Returns True if any pin was added (caller should save config).
    """
    base, _ = load_tracking()
    overrides = cfg.setdefault("image_overrides", {})
    added = False
    for service in active:
        if service in overrides:
            continue
        if service in container_refs:
            overrides[service] = container_refs[service]
        elif service in base:
            overrides[service] = base[service]
        else:
            raise ProvisionError(f"No starter image for active service: {service}")
        added = True
    return added
