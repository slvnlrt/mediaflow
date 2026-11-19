"""Provision qBittorrent, Radarr, Sonarr, Prowlarr and Recyclarr.

Public API
----------
configure_qbittorrent(ctx)   -> None
provision_arr(ctx)           -> {'radarr_profile_id': int, 'sonarr_profile_id': int}
configure_recyclarr(ctx)     -> None

All functions are idempotent: re-runs update managed entities without
duplicating them and preserve user-added data (accounts, indexers, extra
custom formats).  Ambiguous collisions raise ProvisionError with a
sanitised message (no credentials, no response bodies).
"""
from __future__ import annotations

import hashlib
import json as _json
import os
import time
import urllib.parse

from .common import Api, Context, CONTAINER_PORTS, ProvisionError, write_private
from .presets.fr_1080p import (
    PROFILE_NAME,
    RADARR_CATEGORY,
    RADARR_DL_PATH,
    RADARR_ROOT,
    RADARR_SYNC_CATEGORIES,
    SONARR_CATEGORY,
    SONARR_DL_PATH,
    SONARR_ROOT,
    SONARR_SYNC_CATEGORIES,
)
from .quality_policy import (
    ensure_custom_formats as _ensure_custom_formats_policy,
    ensure_profiles as _ensure_profiles_policy,
    ensure_quality_definitions as _ensure_quality_definitions_policy,
    POLICY,
    get_naming,
)


# ── Managed-state helpers ────────────────────────────────────────────────

def _mget(ctx: Context, key: str, default: object = None) -> object:
    return ctx.cfg.get("managed", {}).get(key, default)


def _mset(ctx: Context, key: str, value: object) -> None:
    ctx.cfg.setdefault("managed", {})[key] = value
    ctx.save()


# ── qBittorrent ─────────────────────────────────────────────────────────


def configure_qbittorrent(ctx: Context) -> None:
    """Authenticate to qBittorrent, create categories, set preferences.

    When VPN is active qBit shares the Gluetun namespace; binds traffic
    to tun0 so nothing leaks if the tunnel drops.  Explicit autoTMM,
    save-path and temp-path settings are applied and verified.
    """
    api = _qbit_api(ctx)
    _qbit_login(ctx, api)

    _qbit_ensure_category(api, RADARR_CATEGORY, RADARR_DL_PATH)
    _qbit_ensure_category(api, SONARR_CATEGORY, SONARR_DL_PATH)
    ctx.note("qBittorrent: categories configured")

    # Automatic management applies category paths; all data stays on one filesystem.
    iface = "tun0" if ctx.cfg["vpn"]["mode"] != "none" else ""
    prefs = {
        "save_path": "/data/torrents",
        "temp_path_enabled": False,
        "temp_path": "/data/torrents/incomplete",
        "auto_tmm_enabled": True,
        "current_network_interface": iface,
    }
    _qbit_set_preferences(api, prefs)

    # Re-read and verify critical preferences were applied.
    actual = api.request("GET", "/api/v2/app/preferences")
    if actual is None:
        raise ProvisionError("qBittorrent: cannot read preferences")
    for key in prefs:
        if actual.get(key) != prefs[key]:
            raise ProvisionError(
                f"qBittorrent: preference '{key}' not applied"
            )
    ctx.note("qBittorrent: preferences verified")
    if iface:
        ctx.note("qBittorrent: bound to tun0 (VPN)")


def _qbit_api(ctx: Context) -> Api:
    api = ctx.api("qbittorrent")
    # qBittorrent ≥4.5 requires Origin/Referer matching the target host.
    api.headers["Referer"] = api.base_url + "/"
    return api


def _qbit_login(ctx: Context, api: Api) -> None:
    """Wait for the socket, then verify the authenticated qBittorrent 5.3 session."""
    deadline = time.monotonic() + 180
    while True:
        try:
            api.request("POST", "/api/v2/auth/login",
                        form={"username": ctx.cfg["admin"]["username"], "password": ctx.cfg["admin"]["password"]},
                        allowed=(204,))
            break
        except ProvisionError as error:
            if error.status is not None:
                raise
            if time.monotonic() >= deadline:
                raise ProvisionError("qBittorrent: service not reachable after 180s") from None
            time.sleep(2)
    version = api.request("GET", "/api/v2/app/version")
    ctx.note(f"qBittorrent: authenticated ({version})")


def _qbit_ensure_category(api: Api, name: str, save_path: str) -> None:
    categories = api.request("GET", "/api/v2/torrents/categories") or {}
    if name in categories:
        existing = categories[name].get("savePath", "")
        if existing.rstrip("/") != save_path.rstrip("/"):
            api.request(
                "POST",
                "/api/v2/torrents/editCategory",
                form={"category": name, "savePath": save_path},
                allowed=(200,),
            )
    else:
        api.request(
            "POST",
            "/api/v2/torrents/createCategory",
            form={"category": name, "savePath": save_path},
            allowed=(200,),
        )


def _qbit_set_preferences(api: Api, prefs: dict) -> None:
    api.request(
        "POST",
        "/api/v2/app/setPreferences",
        form={"json": _json.dumps(prefs)},
        headers={"Accept": "*/*"},
        allowed=(200,),
    )


# ── Shared *arr helpers ──────────────────────────────────────────────────


def _arr_api(ctx: Context, service: str) -> Api:
    """Return an authenticated API handle for a Servarr service."""
    return ctx.api(service, headers={"X-Api-Key": ctx.cfg["secrets"][service]})


def _set_field(fields: list[dict], name: str, value: object) -> None:
    """Update a named field inside a provider schema's *fields* list."""
    for f in fields:
        if f.get("name") == name:
            f["value"] = value
            return


def _find_by(items: list[dict], key: str, value: object) -> dict | None:
    for item in items:
        if item.get(key) == value:
            return item
    return None


# ── *arr authentication ──────────────────────────────────────────────────


def _configure_arr_auth(
    ctx: Context, api: Api, service: str, api_version: str = "v3",
) -> None:
    """Create admin account for an *arr service if not yet configured.

    Parent seeds config.xml with API key + Forms + Enabled but no
    username/password.  This sets the account via the official host-config
    API.  On re-run, an existing matching username is left untouched
    (password NOT reset).  A different username without our managed marker
    is treated as a collision.
    """
    prefix = f"/api/{api_version}/config/host"
    current = api.request("GET", prefix)
    if current is None:
        raise ProvisionError(f"{service}: host config unavailable")

    managed_key = f"{service}_auth"
    is_managed = _mget(ctx, managed_key, False)
    target_user = ctx.cfg["admin"]["username"]
    current_user = (current.get("username") or "").strip()

    if current_user == target_user:
        # Already configured with our username — do not touch password.
        if not is_managed:
            _mset(ctx, managed_key, True)
        return

    if current_user and not is_managed:
        raise ProvisionError(
            f"{service}: authentication already configured with a different account"
        )

    # First run (empty username) or managed re-run with changed target user.
    updated = {**current}
    updated["authenticationMethod"] = "forms"
    updated["authenticationRequired"] = "enabled"
    updated["username"] = target_user
    updated["password"] = ctx.cfg["admin"]["password"]
    updated["passwordConfirmation"] = ctx.cfg["admin"]["password"]
    api.request("PUT", f"{prefix}/{current['id']}", updated)

    _mset(ctx, managed_key, True)
    ctx.note(f"{service}: admin account configured")


# ── Download client ──────────────────────────────────────────────────────


def _upsert_download_client(
    ctx: Context, api: Api, service: str,
    category_field: str, category: str,
) -> None:
    """Create or update qBittorrent download client for an *arr service.

    QUI proxy is used only when the ``qui`` option is active AND
    ``managed.qui`` carries valid proxy credentials (set by media.py).
    """
    # QUI proxy: require both the option AND valid managed credentials.
    qui_active = ctx.cfg["options"].get("qui", False)
    qui_data = _mget(ctx, "qui") if qui_active else None
    if qui_data and not isinstance(qui_data, dict):
        qui_data = None
    if qui_data and not qui_data.get("proxy_url"):
        qui_data = None

    if qui_data:
        parsed = urllib.parse.urlsplit(qui_data["proxy_url"])
        host = parsed.hostname
        port = parsed.port or CONTAINER_PORTS["qui"]
        url_base = parsed.path.rstrip("/") if parsed.path not in ("", "/") else ""
        username = qui_data["username"]
        password = qui_data["password"]
    else:
        internal = ctx.internal("qbittorrent")
        parsed = urllib.parse.urlsplit(internal)
        host = parsed.hostname
        port = parsed.port or CONTAINER_PORTS["qbittorrent"]
        url_base = ""
        username = ctx.cfg["admin"]["username"]
        password = ctx.cfg["admin"]["password"]

    # Grab the provider schema template for QBittorrent.
    schemas = api.request("GET", "/api/v3/downloadclient/schema") or []
    template = _find_by(schemas, "implementation", "QBittorrent")
    if template is None:
        raise ProvisionError(f"{service}: QBittorrent provider not available")

    fields = list(template.get("fields", []))
    _set_field(fields, "host", host)
    _set_field(fields, "port", port)
    _set_field(fields, "useSsl", False)
    _set_field(fields, "urlBase", url_base)
    _set_field(fields, "username", username)
    _set_field(fields, "password", password)
    _set_field(fields, category_field, category)
    _set_field(fields, "initialState", 0)       # Start
    _set_field(fields, "sequentialOrder", False)
    _set_field(fields, "firstAndLast", False)

    payload: dict = {
        "enable": True,
        "protocol": "torrent",
        "priority": 1,
        "removeCompletedDownloads": False,   # preserve seeding
        "removeFailedDownloads": False,      # keep for investigation
        "name": "qBittorrent",
        "fields": fields,
        "implementation": "QBittorrent",
        "configContract": "QBittorrentSettings",
        "implementationName": "qBittorrent",
    }

    # Ownership: check managed ID first, then search by name.
    managed_key = f"{service}_dl_id"
    managed_id = _mget(ctx, managed_key)

    existing_list = api.request("GET", "/api/v3/downloadclient") or []
    found = None
    if managed_id is not None:
        found = _find_by(existing_list, "id", managed_id)
    if found is None:
        found = _find_by(existing_list, "name", "qBittorrent")
        if found and found.get("implementation") != "QBittorrent":
            raise ProvisionError(
                f"{service}: entity 'qBittorrent' exists with unexpected type"
            )
        # Fresh default or previous bootstrap residue — adopt.

    if found:
        payload["id"] = found["id"]
        api.request("PUT", f"/api/v3/downloadclient/{found['id']}", payload)
        _mset(ctx, managed_key, found["id"])
    else:
        result = api.request("POST", "/api/v3/downloadclient", payload)
        _mset(ctx, managed_key, result["id"])

    ctx.note(f"{service}: download client configured")


# ── Root folder ──────────────────────────────────────────────────────────


def _ensure_root_folder(api: Api, path: str) -> None:
    folders = api.request("GET", "/api/v3/rootfolder") or []
    for f in folders:
        if f.get("path", "").rstrip("/") == path.rstrip("/"):
            return
    api.request("POST", "/api/v3/rootfolder", {"path": path})


# ── Naming ───────────────────────────────────────────────────────────────


def _configure_naming(api: Api, naming: dict) -> None:
    current = api.request("GET", "/api/v3/config/naming")
    if current is None:
        raise ProvisionError("Naming config unavailable")
    updated = {**current, **naming}
    api.request("PUT", f"/api/v3/config/naming/{current['id']}", updated)


# ── Media management (hardlinks) ─────────────────────────────────────────


def _configure_media_management(api: Api) -> None:
    current = api.request("GET", "/api/v3/config/mediamanagement")
    if current is None:
        raise ProvisionError("Media management config unavailable")
    updates = {
        "copyUsingHardlinks": True,
        "importExtraFiles": True,
        "extraFileExtensions": "srt,nfo",
    }
    updated = {**current, **updates}
    api.request("PUT", f"/api/v3/config/mediamanagement/{current['id']}", updated)


# ── Download client config (completed-import handling) ───────────────────


def _configure_download_client_config(api: Api) -> None:
    current = api.request("GET", "/api/v3/config/downloadclient")
    if current is None:
        raise ProvisionError("Download client config unavailable")
    updates = {
        "enableCompletedDownloadHandling": True,
        "autoRedownloadFailed": True,
    }
    updated = {**current, **updates}
    api.request("PUT", f"/api/v3/config/downloadclient/{current['id']}", updated)


# ── Quality policy (profiles, custom formats, quality definitions) ───────


def _apply_quality_policy(ctx: Context, api: Api, service: str) -> int:
    """Apply complete quality policy; return the default profile id."""
    # 1. Create/update all custom formats — returns name→id map.
    cf_ids = _ensure_custom_formats_policy(api, service)
    ctx.note(f"{service}: {len(cf_ids)} custom formats applied")

    # 2. Create/update all six quality profiles with correct scores.
    profile_ids = _ensure_profiles_policy(
        api, service, cf_ids,
    )
    ctx.note(f"{service}: {len(profile_ids)} quality profiles applied")

    # 3. Update quality definitions (size limits).
    _ensure_quality_definitions_policy(api, service)
    ctx.note(f"{service}: quality definitions applied")

    # Return the default profile ID for Seerr.
    default_name = PROFILE_NAME
    default_id = profile_ids.get(default_name)
    if default_id is None:
        raise ProvisionError(
            f"{service}: default profile '{default_name}' not found after apply"
        )
    return default_id


# ── Single *arr service provisioning ─────────────────────────────────────


def _provision_arr_service(
    ctx: Context,
    service: str,
    category_field: str,
    category: str,
    root_path: str,
) -> int:
    """Fully provision one *arr service; return its quality-profile id."""
    api = _arr_api(ctx, service)
    api.wait("/api/v3/system/status")
    ctx.note(f"{service}: service ready")

    _configure_arr_auth(ctx, api, service)
    _upsert_download_client(ctx, api, service, category_field, category)
    _ensure_root_folder(api, root_path)
    _configure_naming(api, get_naming(service))
    _configure_media_management(api)
    _configure_download_client_config(api)
    profile_id = _apply_quality_policy(ctx, api, service)
    ctx.note(
        f"{service}: provisioned (profile {PROFILE_NAME} id={profile_id})"
    )
    return profile_id


# ── Prowlarr ─────────────────────────────────────────────────────────────


def _configure_prowlarr(ctx: Context) -> None:
    api = _arr_api(ctx, "prowlarr")
    api.wait("/api/v1/system/status")
    ctx.note("Prowlarr: service ready")

    _configure_arr_auth(ctx, api, "prowlarr", api_version="v1")
    _ensure_prowlarr_download_client(ctx, api)

    # Register Radarr.
    _ensure_prowlarr_app(
        ctx, api,
        name="Radarr",
        implementation="Radarr",
        config_contract="RadarrSettings",
        api_key=ctx.cfg["secrets"]["radarr"],
        base_url=ctx.internal("radarr"),
        categories=RADARR_SYNC_CATEGORIES,
        managed_key="prowlarr_radarr_id",
    )
    # Register Sonarr.
    _ensure_prowlarr_app(
        ctx, api,
        name="Sonarr",
        implementation="Sonarr",
        config_contract="SonarrSettings",
        api_key=ctx.cfg["secrets"]["sonarr"],
        base_url=ctx.internal("sonarr"),
        categories=SONARR_SYNC_CATEGORIES,
        managed_key="prowlarr_sonarr_id",
    )
    ctx.note("Prowlarr: applications configured")

    # Byparr: provision when enabled, clean up when disabled.
    if ctx.cfg["options"].get("byparr"):
        _ensure_byparr_proxy(ctx, api)
    else:
        _cleanup_byparr_proxy(ctx, api)


def _ensure_prowlarr_download_client(ctx: Context, api: Api) -> None:
    """Manual grabs in Prowlarr go directly to qBittorrent, even with QUI enabled."""
    path = "/api/v1/downloadclient"
    endpoint = urllib.parse.urlsplit(ctx.internal("qbittorrent"))
    name = "MediaFlow - qBittorrent"
    clients = api.request("GET", path)
    managed_id = _mget(ctx, "prowlarr_dl_id")
    existing = _find_by(clients, "id", managed_id) if managed_id is not None else None
    if existing and existing["implementation"] != "QBittorrent":
        raise ProvisionError("Prowlarr: managed client ID is now used by another provider")
    if not existing:
        for client in clients:
            fields = {f["name"]: f.get("value") for f in client["fields"]}
            if (client["implementation"] == "QBittorrent" and client.get("enable")
                    and not client.get("categories") and not fields.get("category")
                    and fields.get("host") == endpoint.hostname and fields.get("port") == endpoint.port
                    and not fields.get("useSsl") and not fields.get("urlBase")
                    and fields.get("username") == ctx.cfg["admin"]["username"]):
                ctx.note("Prowlarr: existing direct qBittorrent client retained")
                return
        if _find_by(clients, "name", name):
            raise ProvisionError("Prowlarr: a different personal client already uses the MediaFlow client name")

    schema = _find_by(api.request("GET", path + "/schema"), "implementation", "QBittorrent")
    if not schema:
        raise ProvisionError("Prowlarr: qBittorrent client provider unavailable")
    payload = dict(schema, name=name, enable=True, priority=1, categories=[])
    for key, value in {
        "host": endpoint.hostname, "port": endpoint.port, "useSsl": False, "urlBase": "",
        "username": ctx.cfg["admin"]["username"], "password": ctx.cfg["admin"]["password"],
        "category": "", "initialState": 0, "sequentialOrder": False, "firstAndLast": False,
    }.items():
        _set_field(payload["fields"], key, value)
    if existing:
        payload["id"] = existing["id"]
        api.request("PUT", f"{path}/{existing['id']}", payload)
    else:
        created = api.request("POST", path, payload)
        _mset(ctx, "prowlarr_dl_id", created["id"])
    ctx.note("Prowlarr: direct qBittorrent client configured")


def _ensure_prowlarr_app(
    ctx: Context,
    api: Api,
    *,
    name: str,
    implementation: str,
    config_contract: str,
    api_key: str,
    base_url: str,
    categories: list[int],
    managed_key: str,
) -> None:
    prowlarr_url = ctx.internal("prowlarr")

    managed_id = _mget(ctx, managed_key)
    apps = api.request("GET", "/api/v1/applications") or []

    existing = None
    if managed_id is not None:
        existing = _find_by(apps, "id", managed_id)
    if existing is None:
        existing = _find_by(apps, "name", name)
        if existing and existing.get("implementation") != implementation:
            raise ProvisionError(
                f"Prowlarr: application '{name}' exists with unexpected type "
                f"'{existing.get('implementation')}'"
            )

    payload: dict = {
        "name": name,
        "implementation": implementation,
        "configContract": config_contract,
        "syncLevel": "fullSync",
        "fields": [
            {"name": "prowlarrUrl", "value": prowlarr_url},
            {"name": "baseUrl", "value": base_url},
            {"name": "apiKey", "value": api_key},
            {"name": "syncCategories", "value": categories},
        ],
        "tags": [],
    }
    if implementation == "Sonarr":
        payload["fields"].extend([
            {"name": "animeSyncCategories", "value": [5070]},
            {"name": "syncAnimeStandardFormatSearch", "value": True},
        ])

    if existing:
        payload["id"] = existing["id"]
        api.request("PUT", f"/api/v1/applications/{existing['id']}", payload)
        _mset(ctx, managed_key, existing["id"])
    else:
        result = api.request("POST", "/api/v1/applications", payload)
        _mset(ctx, managed_key, result["id"])


def _ensure_byparr_proxy(ctx: Context, api: Api) -> None:
    """Add Byparr as a FlareSolverr-compatible indexer proxy with tag."""
    # Ensure 'byparr' tag exists.
    tags = api.request("GET", "/api/v1/tag") or []
    tag = _find_by(tags, "label", "byparr")
    if tag is None:
        tag = api.request("POST", "/api/v1/tag", {"label": "byparr"})
    tag_id: int = tag["id"]
    _mset(ctx, "byparr_tag_id", tag_id)

    # Determine Byparr URL as seen from Prowlarr.
    # In VPN mode both share the gluetun network namespace → localhost.
    if ctx.cfg["vpn"]["mode"] != "none":
        byparr_host = f"http://localhost:{CONTAINER_PORTS['byparr']}"
    else:
        byparr_host = ctx.internal("byparr")

    managed_id = _mget(ctx, "byparr_proxy_id")
    proxies = api.request("GET", "/api/v1/indexerproxy") or []

    existing = None
    if managed_id is not None:
        existing = _find_by(proxies, "id", managed_id)
    if existing is None:
        existing = _find_by(proxies, "name", "Byparr")

    payload: dict = {
        "name": "Byparr",
        "implementation": "FlareSolverr",
        "configContract": "FlareSolverrSettings",
        "fields": [
            {"name": "host", "value": byparr_host},
            {"name": "requestTimeout", "value": 60},
        ],
        "tags": [tag_id],
    }

    if existing:
        payload["id"] = existing["id"]
        api.request("PUT", f"/api/v1/indexerproxy/{existing['id']}", payload)
        _mset(ctx, "byparr_proxy_id", existing["id"])
    else:
        result = api.request("POST", "/api/v1/indexerproxy", payload)
        _mset(ctx, "byparr_proxy_id", result["id"])

    ctx.note("Prowlarr: Byparr proxy configured")


def _cleanup_byparr_proxy(ctx: Context, api: Api) -> None:
    """Remove managed Byparr proxy and untag indexers when option disabled."""
    proxy_id = _mget(ctx, "byparr_proxy_id")
    tag_id = _mget(ctx, "byparr_tag_id")
    if proxy_id is None and tag_id is None:
        return  # nothing to clean up

    # Remove tag from indexers first (avoid broken proxy reference).
    if tag_id is not None:
        try:
            indexers = api.request("GET", "/api/v1/indexer") or []
            for idx in indexers:
                idx_tags = idx.get("tags", [])
                if tag_id in idx_tags:
                    idx["tags"] = [t for t in idx_tags if t != tag_id]
                    api.request(
                        "PUT", f"/api/v1/indexer/{idx['id']}", idx
                    )
        except ProvisionError:
            pass  # best-effort; indexers may have been removed

    # Delete the proxy itself.
    if proxy_id is not None:
        try:
            api.request(
                "DELETE",
                f"/api/v1/indexerproxy/{proxy_id}",
                allowed=(200, 202, 204, 404),
            )
        except ProvisionError:
            pass  # already gone

    _mset(ctx, "byparr_proxy_id", None)
    _mset(ctx, "byparr_tag_id", None)
    ctx.note("Prowlarr: Byparr proxy removed")


# ── provision_arr (public) ───────────────────────────────────────────────


def provision_arr(ctx: Context) -> dict:
    """Provision Radarr, Sonarr and Prowlarr; return quality-profile ids.

    Returns ``{'radarr_profile_id': int, 'sonarr_profile_id': int}``.
    """
    radarr_id = _provision_arr_service(
        ctx,
        service="radarr",
        category_field="movieCategory",
        category=RADARR_CATEGORY,
        root_path=RADARR_ROOT,
    )
    sonarr_id = _provision_arr_service(
        ctx,
        service="sonarr",
        category_field="tvCategory",
        category=SONARR_CATEGORY,
        root_path=SONARR_ROOT,
    )

    _configure_prowlarr(ctx)

    # Top-level keys consumed by configure_seerr.
    _mset(ctx, "radarr_profile_id", radarr_id)
    _mset(ctx, "sonarr_profile_id", sonarr_id)

    return {"radarr_profile_id": radarr_id, "sonarr_profile_id": sonarr_id}


# ── Recyclarr ────────────────────────────────────────────────────────────


def _resource_id(service: str, name: str) -> str:
    return hashlib.sha256(f"mediaflow:{service}:{name}".encode()).hexdigest()[:32]


def configure_recyclarr(ctx: Context) -> None:
    """Sync the complete, local CF catalogue without an upstream policy drift."""
    config_dir = ctx.state / "apps" / "recyclarr"
    resources = config_dir / "policy"
    uid, gid = ctx.cfg["uid"], ctx.cfg["gid"]

    def save(path, value):
        write_private(path, _json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        if os.getuid() == 0:
            os.chown(path, uid, gid)
        elif path.stat().st_uid != uid:
            raise ProvisionError("Recyclarr config owned by wrong user")

    for directory in [resources, resources / "radarr", resources / "sonarr"]:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.getuid() == 0:
            os.chown(directory, uid, gid)

    metadata, instances = {"json_paths": {}}, {}
    for service in ("radarr", "sonarr"):
        policy = POLICY[service]
        metadata["json_paths"][service] = {"custom_formats": [service]}
        assignments = []
        for cf in policy["customFormats"]:
            resource_id = _resource_id(service, cf["name"])
            resource = dict(cf, trash_id=resource_id)
            resource["specifications"] = [
                dict(spec, fields={field["name"]: field["value"] for field in spec["fields"]})
                for spec in cf["specifications"]
            ]
            save(resources / service / f"{resource_id}.json", resource)
            assignments.append({
                "trash_ids": [resource_id],
                "assign_scores_to": [
                    {"name": profile["name"], "score": profile["formatScores"].get(cf["name"], 0)}
                    for profile in policy["profiles"]
                ],
            })
        instances[service] = {f"mediaflow-{service}": {
            "base_url": ctx.internal(service), "api_key": ctx.cfg["secrets"][service],
            "delete_old_custom_formats": False, "replace_existing_custom_formats": True,
            "custom_formats": assignments,
        }}
    save(resources / "metadata.json", metadata)
    save(config_dir / "settings.yml", {"resource_providers": [{
        "name": "mediaflow-policy", "type": "trash-guides",
        "path": "/config/policy", "replace_default": True,
    }]})
    # JSON is a YAML subset: using its serializer avoids interpolation/quoting errors.
    save(config_dir / "recyclarr.yml", instances)
    ctx.note("Recyclarr: local catalogue and all profile scores configured")
    ctx.compose("run", "--rm", "recyclarr", "sync")
    for service in ("radarr", "sonarr"):
        _verify_recyclarr(ctx, service)
    ctx.note("Recyclarr: all custom formats and profile scores verified")


def _verify_recyclarr(ctx: Context, service: str) -> None:
    api = _arr_api(ctx, service)
    actual_formats = {cf["name"]: cf for cf in api.request("GET", "/api/v3/customformat")}
    actual_profiles = {p["name"]: p for p in api.request("GET", "/api/v3/qualityprofile")}

    def conditions(cf):
        return sorted([
            {key: spec[key] for key in ("name", "implementation", "negate", "required")}
            | {"fields": {field["name"]: field.get("value") for field in spec["fields"]}}
            for spec in cf["specifications"]
        ], key=lambda spec: spec["name"])

    for expected in POLICY[service]["customFormats"]:
        actual = actual_formats.get(expected["name"])
        if (not actual or actual["includeCustomFormatWhenRenaming"] != expected["includeCustomFormatWhenRenaming"]
                or conditions(actual) != conditions(expected)):
            raise ProvisionError(f"Recyclarr/{service}: custom format differs: {expected['name']}")
    for expected in POLICY[service]["profiles"]:
        actual = actual_profiles.get(expected["name"])
        if not actual:
            raise ProvisionError(f"Recyclarr/{service}: missing profile: {expected['name']}")
        scores = {item["name"]: item["score"] for item in actual["formatItems"]}
        for cf in POLICY[service]["customFormats"]:
            name = cf["name"]
            if scores.get(name) != expected["formatScores"].get(name, 0):
                raise ProvisionError(f"Recyclarr/{service}: score differs: {expected['name']}/{name}")
