"""Bounded, permission-scoped knowledge snapshots with cross-process invalidation."""
import base64
from collections import OrderedDict
from copy import deepcopy
from contextlib import contextmanager
import fcntl
import logging
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .translation_state import check_cancelled

_CACHE = OrderedDict()
_LOCK = threading.Lock()
_MAX_ENTRIES = 2
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
        ttl = int(os.getenv('TRANSLATION_RESOURCE_CACHE_SECONDS', '300'))
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


@contextmanager
def _disk_cache(path):
    root = path.parent.parent / 'resource-cache'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    database = root / 'snapshots.sqlite3'
    fd = os.open(database, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    db = sqlite3.connect(database, timeout=30)
    try:
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS snapshots '
                       '(key TEXT PRIMARY KEY, version TEXT, expires REAL, data TEXT)')
            yield db
    finally:
        db.close()


@contextmanager
def _population_lock(path, key, settings):
    root = path.parent.parent / 'resource-cache'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / (key + '.lock'), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while True:
            check_cancelled(settings)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(.05)
        yield
    finally:
        os.close(fd)


def _remember(key, expires, snapshot):
    with _LOCK:
        _CACHE[key] = (expires, deepcopy(snapshot))
        _CACHE.move_to_end(key)
        while len(_CACHE) > _MAX_ENTRIES:
            _CACHE.popitem(last=False)


def _load_rows(project, language, settings, *, client=None, refresh=False):
    check_cancelled(settings)
    settings['_resource_expires_at'] = 0
    if project['direct']:
        return []
    from translation_app.auth_config import auth_enabled

    token = settings.get('access_token')
    anonymous = not auth_enabled()
    if not token and not anonymous:
        raise ValueError('登录已过期，请重新登录')
    ttl = _cache_lifetime(token, anonymous)
    game = project['database_id'].lower().strip()
    lang = language['database_name'].lower().strip()
    path = _version_path(game, settings)
    scope = (str(path), lang, anonymous, _fingerprint(token or ''), 'knowledge-v3')
    disk_key = _fingerprint(json.dumps(scope))
    if not ttl:
        with _LOCK:
            _CACHE.clear()
        return _fetch_rows(game, lang, token, anonymous, settings, client)

    # A per-scope lock coalesces page preload and worker requests, also across
    # processes. Other projects/languages/auth scopes can load independently.
    with _population_lock(path, disk_key, settings):
        version = _version(path)
        key = scope + (version,)
        now = time.time()
        with _LOCK:
            for old_key in [k for k, (expires, _) in _CACHE.items() if expires <= now]:
                del _CACHE[old_key]
            cached = _CACHE.get(key)
            if cached and not refresh:
                _CACHE.move_to_end(key)
                settings['_resource_expires_at'] = cached[0]
                logging.getLogger(__name__).info('Knowledge cache hit: memory')
                return deepcopy(cached[1])
        row = None
        if not refresh:
            with _disk_cache(path) as db:
                row = db.execute('SELECT version, expires, data FROM snapshots WHERE key=?', (disk_key,)).fetchone()
        if row and row[0] == version and row[1] > now:
            try:
                snapshot = json.loads(row[2])
                if not isinstance(snapshot, list):
                    raise ValueError('Invalid snapshot')
            except (ValueError, TypeError):
                pass  # A damaged cache is reloaded from the authoritative source.
            else:
                _remember(key, row[1], snapshot)
                settings['_resource_expires_at'] = row[1]
                logging.getLogger(__name__).info('Knowledge cache hit: shared disk')
                return deepcopy(snapshot)
        logging.getLogger(__name__).info('Knowledge cache miss: loading Supabase')
        snapshot = _fetch_rows(game, lang, token, anonymous, settings, client)
        # Edits during a load must never publish a mixed snapshot for later jobs.
        if _version(path) == version:
            expires = now + ttl
            with _disk_cache(path) as db:
                db.execute('DELETE FROM snapshots WHERE expires<=?', (time.time(),))
                db.execute('INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?)',
                           (disk_key, version, expires, json.dumps(snapshot, ensure_ascii=False)))
                db.execute('DELETE FROM snapshots WHERE key IN '
                           '(SELECT key FROM snapshots ORDER BY expires DESC LIMIT -1 OFFSET 32)')
            _remember(key, expires, snapshot)
            settings['_resource_expires_at'] = expires
        return snapshot


def load_resources(project, language, settings):
    entries = {'terms': {}, 'memory': {}}
    for row in _load_rows(project, language, settings):
        if row['status'] == 'active':
            entries[row['kind']].setdefault(row['source'], []).append({key: row[key] for key in
                                                                       ('target', 'note', 'scene', 'string_key', 'verified')})
    return entries['terms'], entries['memory'], ''


def preload_resources(project, language, settings, *, include_lists=False, refresh=False):
    started = time.perf_counter()
    client = None
    if include_lists:
        from .database import ProjectDatabase
        from .knowledge import KnowledgeError, PAGE_SIZE, access
        token = settings.get('access_token')
        if not token:
            raise KnowledgeError('请登录有项目权限的账号后使用知识库。', 403)
        client = ProjectDatabase(token).client
        # Management reads must recheck membership even on a shared cache hit.
        editable = access(client, project['database_id'].lower().strip())
    rows = _load_rows(project, language, settings, client=client, refresh=refresh)
    counts = {'terms': 0, 'memory': 0}
    libraries = {'terms': [], 'memory': []} if include_lists else None
    for row in rows:
        if row['status'] == 'active':
            counts[row['kind']] += 1
        if libraries is not None:
            libraries[row['kind']].append(row)
    result = {**counts,
              'expires_in': max(0, settings.get('_resource_expires_at', 0) - time.time()),
              'seconds': round(time.perf_counter() - started, 4)}
    logging.getLogger(__name__).info('Knowledge preload completed: project=%s language=%s %s',
                                   project['id'], language['id'], result)
    if include_lists:
        result.update(libraries=libraries, editable=editable, page_size=PAGE_SIZE)
    return result


def _fetch_rows(game, lang, token, anonymous, settings, client=None):
    if client is None:
        from .database import ProjectDatabase
        from .knowledge import KnowledgeError, access
        if anonymous and not token:
            raise KnowledgeError('请登录有项目权限的账号后使用知识库。', 403)
        client = ProjectDatabase(token, allow_anonymous=anonymous).client
        access(client, game)

    rows, page_size = [], 1000
    while True:
        check_cancelled(settings)
        offset = len(rows)
        response = (client.table('knowledge_entries')
                    .select('id,kind,source,target,note,scene,string_key,status,origin,verified,version,updated_at')
                    .eq('game_id', game).eq('target_language', lang)
                    .order('id').range(offset, offset + page_size - 1).execute())
        page = response.data or []
        if not page:
            return rows
        # The server may cap pages below 1000: only an empty page ends the scan.
        rows.extend(page)
