"""Private, job-local checkpoints; only validated translations are committed."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time


def check_cancelled(settings):
    event = settings.get('_cancel_event')
    if event is not None and event.is_set():
        raise RuntimeError('翻译任务已停止')


@contextmanager
def output_lock(output_path, settings):
    # Fence overlapping retries of the same job, including a worker with a lost
    # lease that is still waiting for an external response. flock dies with it.
    path = Path(output_path).with_suffix('.lock')
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
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


class Checkpoint:
    def __init__(self, output_path, fingerprint):
        path = Path(output_path).with_suffix('.checkpoint.sqlite3')
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path)
        with self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS metadata (fingerprint TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS translations (id INTEGER PRIMARY KEY, text TEXT NOT NULL)')
            previous = self.db.execute('SELECT fingerprint FROM metadata').fetchone()
            if previous != (fingerprint,):
                self.db.execute('DELETE FROM translations')
                self.db.execute('DELETE FROM metadata')
                self.db.execute('INSERT INTO metadata VALUES (?)', (fingerprint,))

    def read(self):
        return dict(self.db.execute('SELECT id, text FROM translations'))

    def save(self, translations):
        with self.db:
            self.db.executemany('INSERT OR REPLACE INTO translations VALUES (?, ?)', translations.items())

    def close(self):
        self.db.close()


def fingerprint(value):
    from hashlib import sha256
    digest = sha256()
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    for chunk in encoder.iterencode(value):
        digest.update(chunk.encode())
    return digest.hexdigest()
