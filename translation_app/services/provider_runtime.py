"""Connection reuse and a single-host request limit shared by worker processes."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import threading
import time

import requests
from requests.adapters import HTTPAdapter


_LOCAL = threading.local()


def int_setting(name, default, minimum, maximum):
    """Read bounded server configuration, rejecting typos rather than ignoring them."""
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是 {minimum} 到 {maximum} 之间的整数") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须是 {minimum} 到 {maximum} 之间的整数")
    return value


def _slot_root(settings):
    if settings.get('_limiter_root'):
        return Path(settings['_limiter_root'])
    project_root = Path(__file__).resolve().parents[2]
    storage = Path(os.getenv('LOCATOOL_STORAGE', str(project_root / 'instance')))
    return Path(os.getenv('JOB_STORAGE_ROOT', str(storage / 'jobs'))) / 'provider-slots'


def _check_cancelled(event):
    if event is not None and event.is_set():
        raise RuntimeError('翻译任务已停止')


@contextmanager
def request_slot(settings):
    """Hold one advisory file lock for the duration of one actual HTTP request.

    Every worker on the host must share the directory and configured limit. Locks
    belong to open file descriptions, so threads also contend, and process exit
    releases them without a lease timeout. Never unlink live slot files.
    """
    limit = int_setting('TRANSLATION_GLOBAL_CONCURRENCY', 4, 1, 32)
    event = settings.get('_cancel_event')
    root = _slot_root(settings)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    held = None
    try:
        while held is None:
            _check_cancelled(event)
            for slot in range(limit):
                fd = os.open(root / f'{slot}.lock', os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                    continue
                except BaseException:
                    os.close(fd)
                    raise
                held = fd
                break
            if held is None:
                if event is None:
                    time.sleep(0.05)
                else:
                    event.wait(0.05)
        _check_cancelled(event)
        yield
    finally:
        if held is not None:
            try:
                fcntl.flock(held, fcntl.LOCK_UN)
            finally:
                os.close(held)


def post(*args, **kwargs):
    """Reuse connections within a thread without sharing mutable Session state."""
    pid = os.getpid()
    if getattr(_LOCAL, 'pid', None) != pid:
        previous = getattr(_LOCAL, 'session', None)
        if previous is not None:
            previous.close()
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=1, max_retries=0, pool_block=True)
        session.mount('https://', adapter)
        session.mount('http://', adapter)
        _LOCAL.pid, _LOCAL.session = pid, session
    return _LOCAL.session.post(*args, **kwargs)
