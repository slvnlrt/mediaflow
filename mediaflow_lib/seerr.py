"""Jellyseerr (Seerr) provisioning via /api/v1 endpoints.

Target: seerr-team/seerr v3.5.2.

API surface
  GET  /api/v1/settings/public              initialized flag
  POST /api/v1/auth/jellyfin                admin login → owner
  POST /api/v1/settings/initialize          finalize setup
  POST /api/v1/settings/jellyfin/library/sync   sync JF libraries
  GET  /api/v1/settings/jellyfin/library    list libraries (id,name,enabled)
  PUT  /api/v1/settings/jellyfin/library/{id}  enable/disable library
  GET  /api/v1/settings/radarr              list services
  POST /api/v1/settings/radarr/test         validate connection
  POST /api/v1/settings/radarr              create service
  PUT  /api/v1/settings/radarr/{id}         update service
  GET  /api/v1/settings/sonarr              (same pattern)
  POST /api/v1/settings/sonarr/test
  POST /api/v1/settings/sonarr
  PUT  /api/v1/settings/sonarr/{id}
  GET  /api/v1/settings/main                read defaults
  POST /api/v1/settings/main                update permissions

Permissions (server/lib/permissions.ts):
  REQUEST=32  REQUEST_MOVIE=262144  REQUEST_TV=524288
"""
from __future__ import annotations

from urllib.parse import urlparse

from mediaflow_lib.common import ProvisionError

_DEFAULT_PERMS = 32 | 262144 | 524288  # REQUEST | REQUEST_MOVIE | REQUEST_TV


def configure_seerr(ctx, profiles: dict) -> None:
    """Wire Seerr to Jellyfin + Radarr + Sonarr; requests need approval."""
    cfg = ctx.cfg
    admin = cfg['admin']
    managed = cfg.setdefault('managed', {}).setdefault('seerr', {})

    api = ctx.api('seerr')
    ctx.note('Waiting for Seerr…')
    api.wait('/api/v1/settings/public')

    pub = api.request('GET', '/api/v1/settings/public')
    if not pub.get('initialized'):
        _initialize(api, ctx, admin, managed)
    else:
        _login(api, ctx, admin)
        if not managed.get('initialized'):
            # Initialized externally — do NOT claim ownership.
            managed['initialized'] = True
            ctx.save()

    _sync_jellyfin_libraries(api, ctx, managed)
    _ensure_radarr(api, ctx, cfg, profiles, managed)
    _ensure_sonarr(api, ctx, cfg, profiles, managed)
    _set_default_permissions(api, ctx)
    ctx.save()
    ctx.note('Seerr configured.')


# -- init ------------------------------------------------------------------

def _login(api, ctx, admin):
    public = api.request('GET', '/api/v1/settings/public')
    payload = {
        'username': admin['username'],
        'password': admin['password'],
        'email': admin['email'],
        'serverType': 2,
    }
    # Once linked, Seerr explicitly rejects a hostname in subsequent logins.
    if public.get('mediaServerType') == 4:
        jf_host, jf_port = _parse(ctx.internal('jellyfin'))
        payload.update(hostname=jf_host, port=jf_port, useSsl=False, urlBase='')
    elif public.get('mediaServerType') != 2:
        raise ProvisionError('Seerr is already linked to a different media server type')
    api.request('POST', '/api/v1/auth/jellyfin', payload)


def _initialize(api, ctx, admin, managed):
    ctx.note('Initializing Seerr…')
    _login(api, ctx, admin)
    api.request('POST', '/api/v1/settings/initialize')
    managed['initialized'] = True
    managed['_owner'] = 'mediaflow-bootstrap'
    ctx.save()
    ctx.note('Seerr initialized.')


# -- Jellyfin library sync ------------------------------------------------

def _sync_jellyfin_libraries(api, ctx, managed):
    """Sync Jellyfin libraries and enable Films + Séries."""
    ctx.note('Syncing Jellyfin libraries in Seerr…')
    api.request('POST', '/api/v1/settings/jellyfin/library/sync')

    libs = api.request('GET', '/api/v1/settings/jellyfin/library') or []
    wanted = {'Films', 'Séries'}
    enabled_ids = set()

    for lib in libs:
        name = lib.get('name', '')
        lib_id = lib.get('id')
        if name in wanted:
            if not lib.get('enabled'):
                api.request('PUT',
                            f'/api/v1/settings/jellyfin/library/{lib_id}',
                            {'enabled': True})
                ctx.note(f'Enabled Jellyfin library {name} in Seerr.')
            enabled_ids.add(name)

    missing = wanted - enabled_ids
    if missing:
        raise ProvisionError(
            f'Seerr: Jellyfin libraries not found after sync: {missing}')

    managed['jellyfin_libraries_synced'] = True
    ctx.save()


# -- helpers ---------------------------------------------------------------

def _parse(url):
    p = urlparse(url)
    return p.hostname, p.port


def _desired_radarr(hostname, port, api_key, profile_id):
    return {
        'name': 'Radarr',
        'hostname': hostname,
        'port': port,
        'apiKey': api_key,
        'useSsl': False,
        'baseUrl': '',
        'activeProfileId': profile_id,
        'activeDirectory': '/data/media/movies',
        'is4k': False,
        'minimumAvailability': 'released',
        'isDefault': True,
        'tags': [],
        'externalUrl': '',
        'syncEnabled': True,
        'preventSearch': False,
        'tagRequests': False,
    }


def _desired_sonarr(hostname, port, api_key, profile_id):
    return {
        'name': 'Sonarr',
        'hostname': hostname,
        'port': port,
        'apiKey': api_key,
        'useSsl': False,
        'baseUrl': '',
        'activeProfileId': profile_id,
        'activeDirectory': '/data/media/tv',
        'is4k': False,
        'isDefault': True,
        'enableSeasonFolders': True,
        'tags': [],
        'externalUrl': '',
        'syncEnabled': True,
        'preventSearch': False,
        'tagRequests': False,
    }


_CMP_KEYS = (
    'hostname', 'port', 'apiKey', 'activeProfileId', 'activeDirectory',
    'is4k', 'minimumAvailability', 'isDefault', 'syncEnabled', 'preventSearch',
)


def _needs_update(existing, desired):
    """True if any significant field differs."""
    for k in _CMP_KEYS:
        if k in desired and existing.get(k) != desired[k]:
            return True
    return False


def _find(entries, hostname, port):
    for e in entries:
        if e.get('hostname') == hostname and e.get('port') == port:
            return e
    return None


def _validate(test, profile_id, root, label):
    ids = {p['id'] for p in test.get('profiles', [])}
    if profile_id not in ids:
        raise ProvisionError(
            f'{label} profile {profile_id} not found (available: {sorted(ids)})')
    dirs = {rf['path'] for rf in test.get('rootFolders', [])}
    if root not in dirs:
        raise ProvisionError(
            f'{label} root {root} not found (available: {dirs})')
    return next(p['name'] for p in test['profiles'] if p['id'] == profile_id)


# -- Radarr ----------------------------------------------------------------

def _ensure_radarr(api, ctx, cfg, profiles, managed):
    hostname, port = _parse(ctx.internal('radarr'))
    api_key = cfg['secrets']['radarr']
    profile_id = profiles['radarr_profile_id']
    desired = _desired_radarr(hostname, port, api_key, profile_id)

    existing_list = api.request('GET', '/api/v1/settings/radarr') or []
    match = _find(existing_list, hostname, port)

    if match:
        # Collision check: if match exists but we don't own it, preserve it
        # only if it already matches our desired state.
        if managed.get('radarr_id') != match['id']:
            if not _needs_update(match, desired):
                ctx.note('Radarr already linked in Seerr (foreign, compatible).')
                return
            raise ProvisionError(
                'Seerr: Radarr service exists with different settings and '
                'was not created by this bootstrap')

        if not _needs_update(match, desired):
            ctx.note('Radarr already linked in Seerr.')
            return

    test = api.request('POST', '/api/v1/settings/radarr/test', {
        'hostname': hostname, 'port': port,
        'apiKey': api_key, 'useSsl': False, 'baseUrl': '',
    })
    desired['activeProfileName'] = _validate(test, profile_id, '/data/media/movies', 'Radarr')

    if match:
        ctx.note('Updating Radarr in Seerr…')
        api.request('PUT', f'/api/v1/settings/radarr/{match["id"]}', desired)
        managed['radarr_id'] = match['id']
    else:
        ctx.note('Adding Radarr to Seerr…')
        result = api.request('POST', '/api/v1/settings/radarr', desired)
        managed['radarr_id'] = result['id']
    managed['_owner'] = 'mediaflow-bootstrap'
    ctx.save()


# -- Sonarr ----------------------------------------------------------------

def _ensure_sonarr(api, ctx, cfg, profiles, managed):
    hostname, port = _parse(ctx.internal('sonarr'))
    api_key = cfg['secrets']['sonarr']
    profile_id = profiles['sonarr_profile_id']
    desired = _desired_sonarr(hostname, port, api_key, profile_id)

    existing_list = api.request('GET', '/api/v1/settings/sonarr') or []
    match = _find(existing_list, hostname, port)

    if match:
        if managed.get('sonarr_id') != match['id']:
            if not _needs_update(match, desired):
                ctx.note('Sonarr already linked in Seerr (foreign, compatible).')
                return
            raise ProvisionError(
                'Seerr: Sonarr service exists with different settings and '
                'was not created by this bootstrap')

        if not _needs_update(match, desired):
            ctx.note('Sonarr already linked in Seerr.')
            return

    test = api.request('POST', '/api/v1/settings/sonarr/test', {
        'hostname': hostname, 'port': port,
        'apiKey': api_key, 'useSsl': False, 'baseUrl': '',
    })
    desired['activeProfileName'] = _validate(test, profile_id, '/data/media/tv', 'Sonarr')

    if match:
        ctx.note('Updating Sonarr in Seerr…')
        api.request('PUT', f'/api/v1/settings/sonarr/{match["id"]}', desired)
        managed['sonarr_id'] = match['id']
    else:
        ctx.note('Adding Sonarr to Seerr…')
        result = api.request('POST', '/api/v1/settings/sonarr', desired)
        managed['sonarr_id'] = result['id']
    managed['_owner'] = 'mediaflow-bootstrap'
    ctx.save()


# -- permissions -----------------------------------------------------------

def _set_default_permissions(api, ctx):
    main = api.request('GET', '/api/v1/settings/main')
    if main.get('defaultPermissions') == _DEFAULT_PERMS:
        return
    ctx.note('Setting default permissions (requests need approval)…')
    api.request('POST', '/api/v1/settings/main', {
        'defaultPermissions': _DEFAULT_PERMS,
    })
