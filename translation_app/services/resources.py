"""Bounded, permission-scoped knowledge snapshots with cross-process invalidation."""
import base64
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid

_CACHE = OrderedDict()
_LOCK = threading.Lock()
_MAX_ENTRIES = 16
_ROOT = Path(__file__).resolve().parents[2]


def _fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _version_path(project_id, settings=None, storage_root=None):
    settings = settings or {}
    if settings.get('_limiter_root'):
        jobs_root = Path(settings['_limiter_root']).parent
    else:
        private_root = Path(storage_root or os.getenv('LOCATOOL_STORAGE', str(_ROOT / 'instance')))
        jobs_root = Path(os.getenv('JOB_STORAGE_ROOT', str(private_root / 'jobs')))
    scope = '\0'.join((os.getenv('SUPABASE_URL', ''), os.getenv('SUPABASE_KEY', ''),
                       project_id.lower().strip()))
    return jobs_root.resolve() / 'resource-versions' / _fingerprint(scope)


def _version(path):
    try:
        return path.read_text(encoding='ascii')
    except FileNotFoundError:
        return ''


def invalidate_resources(project_id, *, storage_root=None):
    """Invalidate every language/auth scope of this game after a database mutation."""
    path = _version_path(project_id, storage_root=storage_root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('x', encoding='ascii') as handle:
            os.chmod(temp, 0o600)
            handle.write(uuid.uuid4().hex)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _cache_lifetime(token, anonymous):
    try:
        ttl = int(os.getenv('TRANSLATION_RESOURCE_CACHE_SECONDS', '30'))
    except ValueError:
        raise ValueError('TRANSLATION_RESOURCE_CACHE_SECONDS 必须是 0 到 3600 之间的整数') from None
    if not 0 <= ttl <= 3600:
        raise ValueError('TRANSLATION_RESOURCE_CACHE_SECONDS 必须是 0 到 3600 之间的整数')
    if token:
        # JWT claims are used only to shorten cache lifetime, never to authorize.
        # Opaque/malformed tokens always go back to Supabase for verification.
        try:
            payload = token.split('.')[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
            expires = float(claims['exp'])
        except (ValueError, TypeError, KeyError, IndexError):
            return 0
        if not math.isfinite(expires):
            return 0
        remaining = expires - time.time()
        if remaining <= 0:
            raise ValueError('登录已过期，请重新登录')
        ttl = min(ttl, remaining)
    elif not anonymous:
        raise ValueError('登录已过期，请重新登录')
    return ttl


def load_resources(project, language, settings):
    if project['direct']:
        return {}, {}, ''
    from translation_app.auth_config import auth_enabled
    from .database import ProjectDatabase

    token = settings.get('access_token')
    anonymous = not auth_enabled()
    if not token and not anonymous:
        raise ValueError('登录已过期，请重新登录')
    ttl = _cache_lifetime(token, anonymous)
    game = project['database_id'].lower().strip()
    lang = language['database_name'].lower().strip()
    path = _version_path(game, settings)
    version = _version(path)
    key = (str(path), version, lang, anonymous, _fingerprint(token or ''))
    now = time.monotonic()
    with _LOCK:
        expired = [k for k, (expires, _) in _CACHE.items() if expires <= now]
        for old_key in expired:
            del _CACHE[old_key]
        cached = _CACHE.get(key)
        if ttl and cached:
            _CACHE.move_to_end(key)
            return deepcopy(cached[1])

    # Each miss uses a fresh client; mutable authentication is never shared.
    engine = ProjectDatabase(token, allow_anonymous=anonymous)

    def fetch_all(table, columns, order):
        rows, offset, page_size = [], 0, 1000
        while True:
            response = (engine.client.table(table).select(columns).eq('game_id', game)
                        .eq('target_language', lang).order(order)
                        .range(offset, offset + page_size - 1).execute())
            page = response.data or []
            if not page:
                return rows
            rows.extend(page)
            # The server may cap pages below 1000: only an empty page ends the scan.
            offset += len(page)

    glossary = fetch_all('glossaries', 'source_term,target_term,note', 'source_term')
    memory = fetch_all('translation_memory', 'source_text,target_text', 'source_text')
    snapshot = ({row['source_term']: {'target': row['target_term'], 'note': row.get('note') or ''}
                 for row in glossary},
                {row['source_text']: row['target_text'] for row in memory},
                engine.get_game_profile(game))
    # Loading can race an edit. Do not retain a snapshot spanning an invalidation.
    if ttl and _version(path) == version:
        with _LOCK:
            _CACHE[key] = (now + ttl, deepcopy(snapshot))
            _CACHE.move_to_end(key)
            while len(_CACHE) > _MAX_ENTRIES:
                _CACHE.popitem(last=False)
    return snapshot
