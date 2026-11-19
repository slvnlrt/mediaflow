"""Media service provisioning — public API.

Exports
  configure_jellyfin(ctx)              -> None
  configure_seerr(ctx, profiles:dict)  -> None
  configure_qui(ctx)                   -> None
  configure_wud(ctx)                   -> None
"""
from __future__ import annotations

from mediaflow_lib.jellyfin import configure_jellyfin
from mediaflow_lib.seerr import configure_seerr
from mediaflow_lib.qui import configure_qui
from mediaflow_lib.wud import configure_wud

__all__ = [
    'configure_jellyfin',
    'configure_seerr',
    'configure_qui',
    'configure_wud',
]
