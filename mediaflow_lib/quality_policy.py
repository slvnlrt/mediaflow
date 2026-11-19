"""Complete quality policy for Radarr and Sonarr.

Loads the bundled policy from presets/quality_data.json and
provides idempotent provisioning functions that create or update:

- Custom formats (26 Radarr / 15 Sonarr)
- Quality profiles (6 each, all defaults: Any, SD, HD-720p, HD-1080p,
  Ultra-HD, HD - 720p/1080p)
- Custom-format scores per profile (French audio preference VFF 1000 …
  VF2 500, rejection CFs at −10000, FR Remux Tier 02 at +100)
- Quality definitions (size limits)

All entities are resolved by **name**, never by database ID, so the
policy is portable across fresh installs and database resets.

Radarr HD-1080p allows Remux-1080p; Sonarr HD-1080p does not.
x265 (HD) is penalised (−10000) on 1080p profiles to reject HEVC at
non-4K resolutions.  French audio scores express a *preference*
(minFormatScore=0), not an exclusivity filter.

Custom-format definitions are derived from TRaSH Guides (MIT; see LICENSE).
"""
from __future__ import annotations

import json as _json
import pathlib as _pathlib
from typing import Any

__all__ = [
    "POLICY",
    "ensure_custom_formats",
    "ensure_profiles",
    "ensure_quality_definitions",
    "default_profile_name",
]

# ── Load static policy data ──────────────────────────────────────────────

_DATA_PATH = _pathlib.Path(__file__).with_name("presets") / "quality_data.json"

with open(_DATA_PATH, encoding="utf-8") as _f:
    POLICY: dict[str, Any] = _json.load(_f)

# ── Public constants ─────────────────────────────────────────────────────


def default_profile_name(service: str) -> str:
    """Return the default quality profile name for Seerr integration."""
    return POLICY["defaults"][service]


# ── Custom format provisioning ───────────────────────────────────────────


def ensure_custom_formats(api: Any, service: str) -> dict[str, int]:
    """Create or update all managed custom formats; return name→id map.

    Existing CFs matched by name are updated in-place.  User-added CFs
    with non-colliding names are preserved.  Idempotent.
    """
    policy_cfs = POLICY[service]["customFormats"]
    existing = api.request("GET", "/api/v3/customformat") or []
    existing_by_name: dict[str, dict] = {cf["name"]: cf for cf in existing}

    name_to_id: dict[str, int] = {}

    for cf_def in policy_cfs:
        name = cf_def["name"]
        payload = _build_cf_payload(cf_def, service)
        match = existing_by_name.get(name)

        if match:
            payload["id"] = match["id"]
            api.request("PUT", f"/api/v3/customformat/{match['id']}", payload)
            name_to_id[name] = match["id"]
        else:
            result = api.request("POST", "/api/v3/customformat", payload)
            name_to_id[name] = result["id"]

    return name_to_id


def _build_cf_payload(cf_def: dict, service: str) -> dict:
    """Build a Servarr REST API custom-format payload from policy data."""
    info_link = (
        f"https://wiki.servarr.com/{service}/settings#custom-formats-2"
    )
    specs = []
    for spec in cf_def["specifications"]:
        api_spec: dict[str, Any] = {
            "name": spec["name"],
            "implementation": spec["implementation"],
            "negate": spec["negate"],
            "required": spec["required"],
            "fields": [
                {"name": f["name"], "value": f["value"]}
                for f in spec["fields"]
            ],
        }
        # implementationName and infoLink are optional but expected
        impl_names = {
            "ReleaseTitleSpecification": "Release Title",
            "ReleaseGroupSpecification": "Release Group",
            "SourceSpecification": "Source",
            "ResolutionSpecification": "Resolution",
            "QualityModifierSpecification": "Quality Modifier",
        }
        api_spec["implementationName"] = impl_names.get(
            spec["implementation"], spec["implementation"]
        )
        api_spec["infoLink"] = info_link
        specs.append(api_spec)

    return {
        "name": cf_def["name"],
        "includeCustomFormatWhenRenaming": cf_def["includeCustomFormatWhenRenaming"],
        "specifications": specs,
    }


# ── Profile provisioning ────────────────────────────────────────────────


def ensure_profiles(
    api: Any,
    service: str,
    cf_name_to_id: dict[str, int],
) -> dict[str, int]:
    """Create or update all six quality profiles; return name→id map.

    *cf_name_to_id* maps custom-format names to their runtime IDs
    (from ``ensure_custom_formats``).

    Radarr profiles carry a language field; Sonarr profiles do not.

    Existing profiles matched by name are updated.  User-created profiles
    with non-colliding names are preserved.  Idempotent.
    """
    policy_profiles = POLICY[service]["profiles"]
    existing = api.request("GET", "/api/v3/qualityprofile") or []
    if not existing:
        raise _PolicyError(
            f"{service}: no quality profiles — service not initialised"
        )

    # Use first profile as template for quality item structure
    template = existing[0]
    existing_by_name: dict[str, dict] = {p["name"]: p for p in existing}

    profile_ids: dict[str, int] = {}

    for prof_def in policy_profiles:
        name = prof_def["name"]
        items = _build_items_from_template(
            template.get("items", []), prof_def,
        )
        cutoff_id = _resolve_cutoff(items, prof_def["cutoff"])
        if cutoff_id is None:
            raise _PolicyError(
                f"{service}: cutoff '{prof_def['cutoff']}' not found "
                f"in profile '{name}'"
            )

        format_items = _build_format_items(prof_def, cf_name_to_id)

        payload: dict[str, Any] = {
            "name": name,
            "upgradeAllowed": prof_def["upgradeAllowed"],
            "cutoff": cutoff_id,
            "items": items,
            "minFormatScore": prof_def["minFormatScore"],
            "cutoffFormatScore": prof_def["cutoffFormatScore"],
            "minUpgradeFormatScore": prof_def["minUpgradeFormatScore"],
            "formatItems": format_items,
        }

        # Language (Radarr only); an unavailable policy value is not optional.
        lang_name = prof_def.get("language")
        if lang_name:
            payload["language"] = resolve_language(api, lang_name)

        match = existing_by_name.get(name)
        if match:
            managed_ids = set(cf_name_to_id.values())
            payload["formatItems"].extend(
                item for item in match.get("formatItems", [])
                if item["format"] not in managed_ids
            )
            payload["id"] = match["id"]
            api.request(
                "PUT", f"/api/v3/qualityprofile/{match['id']}", payload,
            )
            profile_ids[name] = match["id"]
        else:
            result = api.request(
                "POST", "/api/v3/qualityprofile", payload,
            )
            profile_ids[name] = result["id"]

    return profile_ids


def _build_items_from_template(
    template_items: list[dict],
    prof_def: dict,
) -> list[dict]:
    """Rebuild exact ordering/groups while resolving the server's quality IDs."""
    qualities, groups = {}, {}

    def collect(items):
        for item in items:
            if item.get("items"):
                groups[item["name"]] = item["id"]
                collect(item["items"])
            elif isinstance(item.get("quality"), dict):
                quality = item["quality"]
                qualities[quality["name"]] = quality

    collect(template_items)
    next_group_id = max([999, *groups.values(), *(q["id"] for q in qualities.values())]) + 1
    used = set()

    def leaf(name, allowed):
        if name not in qualities:
            raise _PolicyError(f"Required quality is unavailable: {name}")
        used.add(name)
        return {"quality": qualities[name], "items": [], "allowed": allowed}

    result = []
    for entry in prof_def["items"]:
        allowed = entry["allowed"]
        if "group" in entry:
            name = entry["group"]
            if name not in groups:
                groups[name] = next_group_id
                next_group_id += 1
            result.append({
                "id": groups[name], "name": name, "allowed": allowed,
                "items": [leaf(member, allowed) for member in entry["members"]],
            })
        else:
            result.append(leaf(entry["quality"], allowed))
    # New upstream qualities are not implicitly accepted by an existing policy.
    result.extend(leaf(name, False) for name in qualities if name not in used)
    return result


def _resolve_cutoff(items: list[dict], cutoff_name: str) -> int | None:
    """Resolve a quality or group name to its ID for cutoff."""
    for item in items:
        # Check group name
        if item.get("name") == cutoff_name and "id" in item:
            return item["id"]
        # Check individual quality
        q = item.get("quality")
        if isinstance(q, dict) and q.get("name") == cutoff_name:
            return q["id"]
        # Check children
        for child in item.get("items", []):
            cq = child.get("quality")
            if isinstance(cq, dict) and cq.get("name") == cutoff_name:
                return cq["id"]
    return None


def _build_format_items(
    prof_def: dict,
    cf_name_to_id: dict[str, int],
) -> list[dict]:
    """Build formatItems list from policy scores and runtime CF IDs."""
    format_items = []
    scores = prof_def.get("formatScores", {})
    for cf_name, cf_id in cf_name_to_id.items():
        score = scores.get(cf_name, 0)
        format_items.append({"format": cf_id, "name": cf_name, "score": score})
    return format_items


# ── Quality definitions ──────────────────────────────────────────────────


def ensure_quality_definitions(api: Any, service: str) -> None:
    """Update quality definitions (size limits) to match policy.

    Only updates definitions that differ from the policy; preserves
    any definitions not in the policy (future qualities).
    """
    policy_defs = POLICY[service]["qualityDefinitions"]
    policy_by_name = {d["quality"]: d for d in policy_defs}

    current = api.request("GET", "/api/v3/qualitydefinition") or []
    current_names = {q["quality"]["name"] for q in current}
    missing = set(policy_by_name) - current_names
    if missing:
        raise _PolicyError("Required quality definitions unavailable: " + ", ".join(sorted(missing)))
    for qdef in current:
        policy = policy_by_name.get(qdef["quality"]["name"])
        if policy is None:
            continue

        needs_update = False
        if qdef.get("minSize") != policy["minSize"]:
            qdef["minSize"] = policy["minSize"]
            needs_update = True
        if qdef.get("maxSize") != policy["maxSize"]:
            qdef["maxSize"] = policy["maxSize"]
            needs_update = True
        if qdef.get("preferredSize") != policy["preferredSize"]:
            qdef["preferredSize"] = policy["preferredSize"]
            needs_update = True

        if needs_update:
            api.request(
                "PUT", f"/api/v3/qualitydefinition/{qdef['id']}", qdef,
            )


# ── Language resolution ──────────────────────────────────────────────────


def resolve_language(api: Any, name: str) -> dict | None:
    """Resolve an exact language name; fail rather than changing the policy."""
    languages = api.request("GET", "/api/v3/language") or []
    for lang in languages:
        if lang.get("name", "").casefold() == name.casefold():
            return {"id": lang["id"], "name": lang["name"]}
    raise _PolicyError(f"Required profile language is unavailable: {name}")


# ── Naming ───────────────────────────────────────────────────────────────


def get_naming(service: str) -> dict:
    """Return the naming configuration for the given service."""
    return dict(POLICY[service]["naming"])


# ── Error ────────────────────────────────────────────────────────────────

# Import ProvisionError so policy errors propagate correctly through arr.py.
from .common import ProvisionError as _ProvisionError


class _PolicyError(_ProvisionError):
    """Raised when quality policy cannot be applied."""
