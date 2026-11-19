"""Jellyfin first-boot provisioning via official Startup and Library APIs.

Target: Jellyfin 12.4.  Startup routes deprecated (PR #16757) but
backward-compatible.  PR #17369: POST /Startup/User returns 403 if
admin already has a password — partial resume handled explicitly.

API surface
  GET  /System/Info/Public               StartupWizardCompleted flag
  POST /Startup/Configuration            locale
  GET  /Startup/User                     prime placeholder (mandatory)
  POST /Startup/User                     set admin (403 if already set)
  POST /Startup/RemoteAccess             LAN, no UPnP
  POST /Startup/Complete                 finish wizard
  POST /Users/AuthenticateByName         access token
  POST /Library/VirtualFolders?…         create library
  GET  /Library/VirtualFolders           list libraries
  POST /Auth/Keys?app=…                  persistent API key (204)
  GET  /Auth/Keys                        retrieve key
"""
from __future__ import annotations

from urllib.parse import quote

from mediaflow_lib.common import ProvisionError


def configure_jellyfin(ctx) -> None:
    """Provision Jellyfin: French admin, Films + Séries libraries, API key."""
    cfg = ctx.cfg
    admin = cfg['admin']
    managed = cfg.setdefault('managed', {}).setdefault('jellyfin', {})

    api = ctx.api('jellyfin')
    ctx.note('Waiting for Jellyfin…')
    api.wait('/health')

    if not managed.get('wizard_completed'):
        _run_wizard(api, admin, managed, ctx)

    token, user_id = _authenticate(api, admin)
    managed['admin_user_id'] = user_id

    jf = ctx.api('jellyfin', headers={
        'Authorization': f'MediaBrowser Token="{token}"',
    })

    if not managed.get('api_key'):
        _ensure_api_key(jf, managed)

    _ensure_libraries(jf, managed, ctx)

    ctx.save()
    ctx.note('Jellyfin configured.')


# -- wizard ----------------------------------------------------------------

def _run_wizard(api, admin, managed, ctx):
    info = api.request('GET', '/System/Info/Public')
    if info.get('StartupWizardCompleted'):
        # Completed outside our control — preserve, do NOT claim ownership.
        managed['wizard_completed'] = True
        ctx.save()
        ctx.note('Jellyfin wizard already completed externally.')
        return

    ctx.note('Running Jellyfin first-time setup (fr)…')

    api.request('POST', '/Startup/Configuration', {
        'UICulture': 'fr',
        'MetadataCountryCode': 'FR',
        'PreferredMetadataLanguage': 'fr',
        'ServerName': 'MediaFlow',
    })

    # Prime placeholder user — mandatory; failure is fatal.
    api.request('GET', '/Startup/User')

    # Set admin credentials.  403 ⇒ password already set in a previous
    # partial run; skip and resume remaining steps.
    try:
        api.request('POST', '/Startup/User', {
            'Name': admin['username'],
            'Password': admin['password'],
        })
    except ProvisionError as exc:
        if 'HTTP 403' not in str(exc):
            raise
        ctx.note('Admin password already set (partial resume).')

    api.request('POST', '/Startup/RemoteAccess', {
        'EnableRemoteAccess': True,
        'EnableAutomaticPortMapping': False,
    })

    api.request('POST', '/Startup/Complete')

    managed['wizard_completed'] = True
    managed['_owner'] = 'mediaflow-bootstrap'
    ctx.save()
    ctx.note('Jellyfin wizard completed.')


# -- auth ------------------------------------------------------------------

_AUTH_DEVICE = (
    'MediaBrowser Client="MediaFlow", Device="Bootstrap", '
    'DeviceId="mediaflow-bootstrap", Version="1.0"'
)


def _authenticate(api, admin):
    result = api.request('POST', '/Users/AuthenticateByName', {
        'Username': admin['username'],
        'Pw': admin['password'],
    }, headers={'Authorization': _AUTH_DEVICE})
    return result['AccessToken'], result['User']['Id']


# -- API key ---------------------------------------------------------------

_APP = 'MediaFlow'


def _ensure_api_key(jf, managed):
    for entry in _list_keys(jf):
        if entry.get('AppName') == _APP:
            managed['api_key'] = entry['AccessToken']
            return

    result = jf.request('POST', f'/Auth/Keys?app={_APP}')
    if isinstance(result, dict) and 'AccessToken' in result:
        managed['api_key'] = result['AccessToken']
        return

    for entry in _list_keys(jf):
        if entry.get('AppName') == _APP:
            managed['api_key'] = entry['AccessToken']
            return

    raise ProvisionError('Jellyfin: failed to create API key')


def _list_keys(jf):
    r = jf.request('GET', '/Auth/Keys')
    return r.get('Items', []) if isinstance(r, dict) else []


# -- libraries -------------------------------------------------------------

_MOVIES = 'Films'
_TV = 'Séries'


def _ensure_libraries(jf, managed, ctx):
    existing = jf.request('GET', '/Library/VirtualFolders') or []
    names = {lib['Name'] for lib in existing}
    libs = managed.setdefault('libraries', {})

    if _MOVIES not in names:
        ctx.note(f'Creating library {_MOVIES}…')
        jf.request(
            'POST',
            '/Library/VirtualFolders'
            f'?collectionType=movies&refreshLibrary=false&name={quote(_MOVIES)}',
            {'LibraryOptions': _opts('/data/media/movies', 'movie')},
        )
        libs['movies'] = _MOVIES

    if _TV not in names:
        ctx.note(f'Creating library {_TV}…')
        jf.request(
            'POST',
            '/Library/VirtualFolders'
            f'?collectionType=tvshows&refreshLibrary=false&name={quote(_TV)}',
            {'LibraryOptions': _opts('/data/media/tv', 'tv')},
        )
        libs['tv'] = _TV

    # Re-read to confirm
    after = jf.request('GET', '/Library/VirtualFolders') or []
    after_names = {lib['Name'] for lib in after}
    for expected in (_MOVIES, _TV):
        if expected not in after_names:
            raise ProvisionError(f'Jellyfin: library {expected} not found after creation')


def _opts(path, kind):
    base = {
        'EnableArchiveMediaFiles': False,
        'EnablePhotos': False,
        'EnableRealtimeMonitor': True,
        'ExtractChapterImagesDuringLibraryScan': False,
        'EnableChapterImageExtraction': False,
        'DownloadImagesInAdvance': False,
        'EnableInternetProviders': True,
        'ImportMissingEpisodes': False,
        'SaveLocalMetadata': False,
        'EnableAutomaticSeriesGrouping': False,
        'PreferredMetadataLanguage': 'fr',
        'MetadataCountryCode': 'FR',
        'SeasonZeroDisplayName': 'Specials',
        'AutomaticRefreshIntervalDays': 0,
        'EnableEmbeddedTitles': False,
        'EnableEmbeddedEpisodeInfos': False,
        'SkipSubtitlesIfEmbeddedSubtitlesPresent': False,
        'SkipSubtitlesIfAudioTrackMatches': False,
        'SaveSubtitlesWithMedia': True,
        'RequirePerfectSubtitleMatch': True,
        'MetadataSavers': [],
        'LocalMetadataReaderOrder': ['Nfo'],
        'SubtitleDownloadLanguages': [],
        'DisabledSubtitleFetchers': [],
        'SubtitleFetcherOrder': [],
        'PathInfos': [{'Path': path}],
    }
    if kind == 'movie':
        base['TypeOptions'] = [_t(
            'Movie',
            f=['TheMovieDb', 'The Open Movie Database'],
            i=['TheMovieDb', 'The Open Movie Database', 'Screen Grabber'],
        )]
    elif kind == 'tv':
        base['TypeOptions'] = [
            _t('Series',
               f=['TheMovieDb', 'The Open Movie Database'],
               i=['TheMovieDb']),
            _t('Season', f=[], i=['TheMovieDb'],
               fo=['TheMovieDb']),
            _t('Episode',
               f=['TheMovieDb', 'The Open Movie Database'],
               i=['TheMovieDb', 'Screen Grabber']),
        ]
    return base


def _t(name, *, f, i, fo=None, io=None):
    return {
        'Type': name,
        'MetadataFetchers': f,
        'MetadataFetcherOrder': fo or list(f),
        'ImageFetchers': i,
        'ImageFetcherOrder': io or list(i),
    }
