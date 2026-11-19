"""QUI (autobrr/qui) provisioning via REST API.

Target: QUI v1.35.0 (Go single-binary).

API surface
  GET  /api/auth/check-setup                 setup needed?
  POST /api/auth/setup                       create admin
  POST /api/auth/login                       session cookie
  GET  /api/instances                        list qBit instances
  POST /api/instances                        add instance
  PUT  /api/instances/{id}                   update (hardlinks etc.)
  GET  /api/client-api-keys                  list proxy keys
  POST /api/client-api-keys                  create proxy key
  DELETE /api/client-api-keys/{id}           revoke key
  PUT  /api/cross-seed/completion/{id}       per-instance completion
  PATCH /api/cross-seed/settings             global automation
  GET  /api/cross-seed/settings              read automation
  POST /api/torznab/indexers/discover        Prowlarr import
  GET  /api/torznab/indexers                 list indexers
  POST /api/torznab/indexers                 add indexer

Hardlink fields on instance (PR #849, #1056): useHardlinks,
hardlinkBaseDir, hardlinkDirPreset, fallbackToRegularMode.
"""
from __future__ import annotations

from urllib.parse import urlparse

from mediaflow_lib.common import ProvisionError

_CLIENT_NAME = 'mediaflow'
_HARDLINK_DIR = '/data/torrents/cross-seeds'


def configure_qui(ctx) -> None:
    """Bootstrap QUI: admin, qBit instance with hardlinks, proxy key,
    cross-seed completion, Prowlarr indexers."""
    cfg = ctx.cfg
    admin = cfg['admin']
    managed = cfg.setdefault('managed', {}).setdefault('qui', {})

    api = ctx.api('qui', headers={'X-Requested-With': 'XMLHttpRequest'})
    ctx.note('Waiting for QUI…')
    api.wait('/api/auth/check-setup')

    # -- admin setup (first boot only) --
    setup = api.request('GET', '/api/auth/check-setup')
    if setup.get('setupRequired', True):
        if managed.get('setup_done'):
            raise ProvisionError(
                'QUI reports setup required but managed state says done; '
                'possible config mismatch')
        ctx.note('Creating QUI admin account…')
        api.request('POST', '/api/auth/setup', {
            'username': admin['username'],
            'password': admin['password'],
        })
        managed['setup_done'] = True
        managed['_owner'] = 'mediaflow-bootstrap'
        ctx.save()
    else:
        if not managed.get('setup_done'):
            # Setup completed externally — do NOT claim ownership.
            managed['setup_done'] = True
            ctx.save()

    # -- session login --
    api.request('POST', '/api/auth/login', {
        'username': admin['username'],
        'password': admin['password'],
    })

    # -- qBittorrent instance with hardlink settings --
    instance_id = _ensure_instance(api, ctx, cfg, managed)

    # -- client proxy key --
    proxy_url = _ensure_proxy_key(api, ctx, managed, instance_id)

    managed['instance_id'] = instance_id
    managed['proxy_url'] = proxy_url
    managed['username'] = admin['username']
    managed['password'] = admin['password']
    ctx.save()

    # -- per-instance cross-seed completion --
    _configure_completion(api, ctx, managed, instance_id)

    # -- global cross-seed automation --
    _configure_cross_seed(api, ctx, managed)

    # -- Prowlarr indexer discovery --
    _discover_indexers(api, ctx, cfg, managed)

    ctx.save()
    ctx.note('QUI configured.')


# -- instance --------------------------------------------------------------

def _ensure_instance(api, ctx, cfg, managed):
    """Create or find qBit instance; configure hardlinks; return ID."""
    instances = api.request('GET', '/api/instances') or []

    qbit_url = ctx.internal('qbittorrent')
    parsed = urlparse(qbit_url)
    qbit_host = f'{parsed.scheme}://{parsed.hostname}:{parsed.port}'

    instance_id = next((item['id'] for item in instances if item['id'] == managed.get('instance_id')), None)
    for inst in instances:
        if (inst.get('host') or '').rstrip('/') == qbit_host.rstrip('/'):
            if managed.get('instance_id') and managed['instance_id'] != inst['id']:
                raise ProvisionError(
                    'QUI: qBit instance exists but ID does not match managed '
                    'state; possible collision')
            instance_id = inst['id']
            break

    if instance_id is None:
        ctx.note('Adding qBittorrent instance to QUI…')
        result = api.request('POST', '/api/instances', {
            'name': 'qBittorrent',
            'host': qbit_host,
            'username': cfg['admin']['username'],
            'password': cfg['admin']['password'],
        })
        instance_id = result['id']

    # Update instance with hardlink and filesystem settings
    ctx.note('Configuring instance hardlink settings…')
    api.request('PUT', f'/api/instances/{instance_id}', {
        'name': 'qBittorrent',
        'host': qbit_host,
        'username': cfg['admin']['username'],
        'password': cfg['admin']['password'],
        'hasLocalFilesystemAccess': True,
        'useHardlinks': True,
        'hardlinkBaseDir': _HARDLINK_DIR,
        'hardlinkDirPreset': 'flat',
        'fallbackToRegularMode': False,
    })

    # Verify hardlink settings applied
    all_inst = api.request('GET', '/api/instances') or []
    found = None
    for inst in all_inst:
        if inst.get('id') == instance_id:
            found = inst
            break
    if not found:
        raise ProvisionError('QUI: instance not found after creation')
    if not found.get('useHardlinks'):
        raise ProvisionError(
            'QUI: useHardlinks not set after update — '
            'check QUI version supports hardlink mode')
    if found.get('hardlinkBaseDir') != _HARDLINK_DIR:
        raise ProvisionError(
            f'QUI: hardlinkBaseDir mismatch — expected {_HARDLINK_DIR}, '
            f'got {found.get("hardlinkBaseDir")!r}')
    if not found.get('hasLocalFilesystemAccess') or found.get('fallbackToRegularMode'):
        raise ProvisionError('QUI: local filesystem access or no-fallback policy was not applied')

    return instance_id


# -- proxy key -------------------------------------------------------------

def _ensure_proxy_key(api, ctx, managed, instance_id):
    """Create or reuse a client proxy key; return full internal URL."""
    if managed.get('proxy_url') and managed.get('proxy_key_id'):
        # Verify key still exists
        keys = api.request('GET', '/api/client-api-keys') or []
        for k in keys:
            if k.get('id') == managed['proxy_key_id']:
                return managed['proxy_url']
        # Key gone — will recreate below

    keys = api.request('GET', '/api/client-api-keys') or []
    for k in keys:
        if (k.get('clientName') == _CLIENT_NAME
                and k.get('instanceId') == instance_id):
            if managed.get('proxy_key_id') == k.get('id'):
                # Our key from previous run, raw key unreadable; delete, recreate
                api.request('DELETE', f'/api/client-api-keys/{k["id"]}')
                break
            # Foreign key with our name — collision
            raise ProvisionError(
                f'QUI: client API key "{_CLIENT_NAME}" exists (id={k.get("id")}) '
                'but was not created by this bootstrap instance')

    ctx.note('Creating QUI proxy key for arr…')
    result = api.request('POST', '/api/client-api-keys', {
        'clientName': _CLIENT_NAME,
        'instanceId': instance_id,
    })

    raw_key = result.get('key', '')
    if not raw_key:
        raise ProvisionError('QUI: proxy key creation returned no key')

    base = ctx.internal('qui')
    proxy_url = f'{base}/proxy/{raw_key}'
    managed['proxy_key_id'] = result.get('clientApiKey', {}).get('id')
    return proxy_url


# -- cross-seed completion (per-instance) ----------------------------------

def _configure_completion(api, ctx, managed, instance_id):
    """Enable completion-based cross-seed for this instance."""
    ctx.note('Configuring cross-seed completion for instance…')
    api.request('PUT', f'/api/cross-seed/completion/{instance_id}', {
        'enabled': True,
        'categories': [],
        'tags': [],
        'excludeCategories': [],
        'excludeTags': [],
    })
    # Verify
    settings = api.request('GET', f'/api/cross-seed/completion/{instance_id}')
    if not settings.get('enabled'):
        raise ProvisionError(
            'QUI: cross-seed completion not enabled after update')
    managed['completion_configured'] = True
    ctx.save()


# -- cross-seed automation (global) ----------------------------------------

def _configure_cross_seed(api, ctx, managed):
    """Configure global cross-seed automation: operational but no mass scan."""
    ctx.note('Configuring cross-seed automation…')
    api.request('PATCH', '/api/cross-seed/settings', {
        'enabled': True,
        'runIntervalMinutes': 60,
        'startPaused': False,
    })
    # Verify
    settings = api.request('GET', '/api/cross-seed/settings')
    if not settings.get('enabled'):
        raise ProvisionError(
            'QUI: cross-seed automation not enabled after update')
    managed['cross_seed_configured'] = True
    ctx.save()
    ctx.note('Cross-seed automation configured (60 min interval).')


# -- Prowlarr indexer discovery --------------------------------------------

def _enabled_prowlarr_torrents(ctx):
    api = ctx.api('prowlarr', headers={'X-Api-Key': ctx.cfg['secrets']['prowlarr']})
    return {str(row['id']) for row in api.request('GET', '/api/v1/indexer')
            if row['enable'] and row['protocol'] == 'torrent'}


def _local_indexers(entries, base):
    return [row for row in entries or []
            if row['backend'] == 'prowlarr' and row['base_url'].rstrip('/') == base.rstrip('/')]


def diagnose_indexers(ctx, api):
    """Compare local IDs, not tracker names, totals or private URLs. Read-only."""
    upstream = _enabled_prowlarr_torrents(ctx)
    rows = _local_indexers(api.request('GET', '/api/torznab/indexers'), ctx.internal('prowlarr'))
    linked = {str(row['indexer_id']) for row in rows}
    enabled = {str(row['indexer_id']) for row in rows if row['enabled']}
    for message, ids in [
        ('actifs dans Prowlarr mais absents de QUI', upstream - linked),
        ('actifs dans Prowlarr mais désactivés dans QUI', (upstream & linked) - enabled),
        ('actifs dans QUI mais absents ou inactifs dans Prowlarr', enabled - upstream),
    ]:
        if ids:
            # Numeric Prowlarr IDs are safe; never print arbitrary user-supplied identifiers.
            numbers = sorted(int(i) for i in ids if i.isascii() and i.isdecimal() and len(i) <= 10)
            ctx.note(f"Attention : {len(ids)} indexeur(s) {message}. IDs numériques : {numbers}")


def _discover_indexers(api, ctx, cfg, managed):
    """Reconcile the current Prowlarr indexers on every invocation."""
    prowlarr_url = ctx.internal('prowlarr')
    key = cfg['secrets']['prowlarr']
    enabled = _enabled_prowlarr_torrents(ctx)
    if not enabled:
        ctx.note('Aucun indexeur torrent actif dans Prowlarr ; aucun ajout dans QUI.')
        return
    response = api.request('POST', '/api/torznab/indexers/discover',
                           {'base_url': prowlarr_url, 'api_key': key})
    if not isinstance(response, dict) or not isinstance(response.get('indexers'), list):
        raise ProvisionError('QUI returned an invalid indexer discovery response')
    if response.get('warnings'):
        raise ProvisionError('QUI: some indexer capabilities could not be discovered')
    discovered = [item for item in response['indexers'] if str(item['id']) in enabled]
    if not discovered:
        ctx.note('Attention : aucun des indexeurs torrent actifs de Prowlarr n’a été découvert par QUI.')
        return
    existing = api.request('GET', '/api/torznab/indexers')
    identities = {str(item['indexer_id']) for item in _local_indexers(existing, prowlarr_url)}
    for item in discovered:
        identity = str(item['id'])
        if identity in identities:
            continue
        result = api.request('POST', '/api/torznab/indexers', {
            'name': item['name'], 'base_url': prowlarr_url, 'indexer_id': str(item['id']),
            'api_key': key, 'backend': item['backend'], 'enabled': True,
            'capabilities': item.get('caps', []), 'categories': item.get('categories', []),
        })
        if result.get('warnings'):
            raise ProvisionError('QUI: indexer added but its capabilities require attention')
        identities.add(identity)
    ctx.note(f'QUI: {len(discovered)} Prowlarr indexer(s) linked.')
