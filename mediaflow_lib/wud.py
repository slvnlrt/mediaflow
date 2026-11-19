"""What's Up Docker (WUD) 9.4.1 provisioning.

WUD 9.x bootstrap admin via env vars (parent sets before start):
  WUD_AUTH_ADMIN_USER        cfg.admin.username
  WUD_AUTH_ADMIN_PASSWORD    cfg.secrets.wud_password
  WUD_AUTH_SESSION_SECRET    cfg.secrets.wud_session  (≥32 chars)

Socket-proxy (read-only Docker API, parent-managed):
  WUD_WATCHER_LOCAL_HOST=socket-proxy  PORT=2375

Monitor-only: no WUD_TRIGGER_* env vars.
Image: getwud/wud:9.4.1   Volume: /store   Port: 3000

Auth: passport-http Basic strategy (BasicStrategy.ts). POST /auth/login
behind requireAuthentication middleware; send Authorization: Basic header
to authenticate and create session.

API surface
  GET  /health               unauthenticated readiness
  POST /auth/login           Basic auth → session + user object
  GET  /api/containers       authenticated endpoint (verify access)
"""
from __future__ import annotations

import base64

from mediaflow_lib.common import ProvisionError


def configure_wud(ctx) -> None:
    """Verify WUD health and authenticate as admin."""
    cfg = ctx.cfg
    managed = cfg.setdefault('managed', {}).setdefault('wud', {})

    api = ctx.api('wud')
    ctx.note('Waiting for WUD…')
    api.wait('/health')

    _authenticate(ctx, cfg)

    managed['configured'] = True
    managed['_owner'] = 'mediaflow-bootstrap'
    ctx.save()
    ctx.note('WUD healthy, authenticated, monitoring (no auto-update).')


def _authenticate(ctx, cfg):
    """Login via HTTP Basic auth and verify a protected endpoint."""
    admin = cfg['admin']
    creds = base64.b64encode(
        f'{admin["username"]}:{cfg["secrets"]["wud_password"]}'.encode()
    ).decode()

    authed = ctx.api('wud', headers={'Authorization': f'Basic {creds}'})

    # POST /auth/login — passport Basic strategy authenticates from header,
    # creates session, returns user object.
    user = authed.request('POST', '/auth/login')
    if not isinstance(user, dict) or not user.get('username'):
        raise ProvisionError(
            'WUD: login did not return a valid user object')
    ctx.note(f'WUD admin login verified (user: {user["username"]}).')

    # Hit a protected endpoint to confirm session grants access.
    result = authed.request('GET', '/api/containers')
    if result is None:
        # 204 is acceptable (no containers yet)
        pass
    ctx.note('WUD API access confirmed.')
