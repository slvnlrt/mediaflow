"""Shared names and paths for application interconnections.

The complete audiovisual policy lives in quality_data.json.
"""

PROFILE_NAME = "HD-1080p"

# ── Filesystem paths (inside containers, all under the shared /data bind)

RADARR_ROOT = "/data/media/movies"
SONARR_ROOT = "/data/media/tv"
RADARR_CATEGORY = "radarr"
SONARR_CATEGORY = "sonarr"
RADARR_DL_PATH = "/data/torrents/movies"
SONARR_DL_PATH = "/data/torrents/tv"

# ── Prowlarr — Newznab standard sync categories ─────────────────────────

RADARR_SYNC_CATEGORIES: list[int] = [
    2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060, 2070, 2080, 2090,
]
SONARR_SYNC_CATEGORIES: list[int] = [
    5000, 5010, 5020, 5030, 5040, 5045, 5050, 5090,
]
