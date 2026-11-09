"""Durable single-server translation jobs shared by web and worker processes.

SQLite WAL coordinates processes on one host; artifacts live on a private shared
local disk. This is not a distributed queue and must not use a network drive.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable

LOG = logging.getLogger(__name__)
WORKER_HEARTBEAT_INTERVAL = 5
WORKER_HEARTBEAT_TTL = 20
STAGE_MESSAGES = {'reading': '正在读取文件', 'resources': '正在加载项目知识库',
                  'planning': '正在准备翻译批次', 'translating': '正在翻译', 'writing': '正在生成结果文件'}


class JobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.files = self.root / "files"
        self.files.mkdir(exist_ok=True, mode=0o700)
        self.database = self.root / "jobs.sqlite3"
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                project_id TEXT NOT NULL, language TEXT NOT NULL,
                filename TEXT NOT NULL, extension TEXT NOT NULL,
                mappings TEXT NOT NULL, settings TEXT NOT NULL,
                status TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
                total INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',
                summary TEXT, worker_id TEXT, lease_until REAL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_owner ON jobs(owner_id, created_at)")
            db.execute("CREATE TABLE IF NOT EXISTS workers (id TEXT PRIMARY KEY, heartbeat_at REAL NOT NULL)")
            db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
            for name in ('queued_at', 'started_at', 'queue_seconds'):
                if name not in columns:
                    db.execute(f'ALTER TABLE jobs ADD COLUMN {name} REAL')
        try:
            os.chmod(self.database, 0o600)
        except OSError:
            LOG.warning("Could not restrict job database permissions")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _directory(self, job_id: str) -> Path:
        if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise ValueError("Invalid job id")
        return self.files / job_id

    @staticmethod
    def _public(row: sqlite3.Row, worker_available: bool) -> dict[str, Any]:
        status, message = row["status"], row["message"]
        # Surface a dead worker's expired lease even if no replacement worker
        # has arrived to perform the durable interruption update in claim().
        if status in {'running', 'cancelling'} and row['lease_until'] is not None and row['lease_until'] < time.time():
            status, message = 'interrupted', '处理已中断，可继续未完成的翻译。'
        mapping = json.loads(row['mappings']).get('CSV_Sheet', {})
        input_mode = 'manual' if isinstance(mapping, dict) and mapping.get('_manual_entry') is True else 'file'
        return {"id": row["id"], "project_id": row["project_id"],
                "language": row["language"], "filename": row["filename"],
                "input_mode": input_mode,
                "status": status, "completed": row["completed"],
                "total": row["total"], "message": message,
                "worker_online": worker_available,
                "summary": json.loads(row["summary"]) if row["summary"] else None,
                "started_at": row['started_at'], "queue_seconds": row['queue_seconds'],
                "can_retry": status in {'failed', 'interrupted', 'cancelled'},
                "can_cancel": status in {'queued', 'running'},
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def touch_worker(self, worker_id: str) -> None:
        if not worker_id:
            raise ValueError("Invalid worker id")
        with self._connect() as db:
            db.execute("INSERT INTO workers (id, heartbeat_at) VALUES (?, ?) "
                       "ON CONFLICT(id) DO UPDATE SET heartbeat_at=excluded.heartbeat_at",
                       (worker_id, time.time()))
            db.execute("DELETE FROM workers WHERE heartbeat_at < ?", (time.time() - 86400,))

    def forget_worker(self, worker_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM workers WHERE id=?", (worker_id,))

    def worker_available(self) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT 1 FROM workers WHERE heartbeat_at >= ? LIMIT 1",
                             (time.time() - WORKER_HEARTBEAT_TTL,)).fetchone()
        return row is not None

    def create(self, owner_id: str, project_id: str, language: str, filename: str,
               file_bytes: bytes, mappings: dict, settings: dict) -> dict:
        if not owner_id or not project_id or not language:
            raise ValueError("Owner, project and language are required")
        filename = Path(filename.replace("\\", "/")).name
        if not filename or any(ord(char) < 32 for char in filename):
            raise ValueError("Invalid file name")
        ext = Path(filename).suffix.lower()
        if ext not in {".csv", ".xlsx"} or not file_bytes:
            raise ValueError("A nonempty CSV or XLSX file is required")
        # Serialize before creating files, so invalid payloads leave no artifacts.
        mapped, opts = json.dumps(mappings, ensure_ascii=False), json.dumps(settings, ensure_ascii=False)
        job_id, now = uuid.uuid4().hex, time.time()
        folder = self._directory(job_id)
        folder.mkdir(mode=0o700)
        try:
            input_path = folder / ("input" + ext)
            with input_path.open("xb") as fh:
                os.chmod(input_path, 0o600)
                fh.write(file_bytes)
            retry_settings = {key: settings[key] for key in ('model', 'batch_size', 'use_smart_budget',
                              'instructions', 'custom_instructions', 'system_prompt', 'use_exact_memory') if key in settings}
            with (folder / 'retry.json').open('x', encoding='utf-8') as fh:
                os.chmod(fh.name, 0o600)
                json.dump(retry_settings, fh, ensure_ascii=False)
            with self._connect() as db:
                db.execute("""INSERT INTO jobs
                    (id, owner_id, project_id, language, filename, extension, mappings,
                     settings, status, message, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)""",
                    (job_id, owner_id, project_id, language, filename, ext, mapped, opts,
                     "等待处理", now, now))
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return self.get(owner_id, job_id)

    def retry(self, owner_id: str, job_id: str, access_token=None) -> dict | None:
        """Explicitly resume one failed job; validated checkpoints remain private."""
        folder = self._directory(job_id)
        now = time.time()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM jobs WHERE id=? AND owner_id=?', (job_id, owner_id)).fetchone()
            if not row:
                return None
            expired = row['status'] in {'running', 'cancelling'} and row['lease_until'] is not None and row['lease_until'] < now
            if row['status'] not in {'failed', 'interrupted', 'cancelled'} and not expired:
                raise ValueError('只有失败、中断或已停止的任务可以继续')
            active = db.execute("SELECT count(*) FROM jobs WHERE owner_id=? AND id<>? "
                                "AND (status='queued' OR (status IN ('running','cancelling') AND lease_until>=?))",
                                (owner_id, job_id, now)).fetchone()[0]
            if active >= 3:
                raise ValueError('同时最多提交三个任务，请等待已有任务完成')
            try:
                settings = json.loads((folder / 'retry.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                raise ValueError('此任务无法继续，请重新提交') from None
            if not (folder / ('input' + row['extension'])).is_file():
                raise ValueError('输入文件不存在，请重新提交')
            if row['project_id'] != 'direct':
                settings['access_token'] = access_token
            db.execute("UPDATE jobs SET status='queued', settings=?, message=?, worker_id=NULL, lease_until=NULL, "
                       'summary=NULL, queued_at=?, started_at=NULL, queue_seconds=NULL, updated_at=? WHERE id=?',
                       (json.dumps(settings, ensure_ascii=False), '等待继续处理', now, now, job_id))
        return self.get(owner_id, job_id)

    def cancel(self, owner_id: str, job_id: str) -> dict | None:
        """Cancel queued work immediately; let a running worker drain sent calls."""
        self._directory(job_id)
        now = time.time()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status, lease_until FROM jobs WHERE id=? AND owner_id=?',
                             (job_id, owner_id)).fetchone()
            if row is None:
                return None
            if row['status'] == 'running' and row['lease_until'] is not None and row['lease_until'] >= now:
                db.execute("UPDATE jobs SET status='cancelling', message=?, updated_at=? WHERE id=?",
                           ('正在停止，等待已发送的请求结束…', now, job_id))
            elif row['status'] in {'queued', 'running'}:
                db.execute("UPDATE jobs SET status='cancelled', message=?, settings='{}', "
                           'worker_id=NULL, lease_until=NULL, updated_at=? WHERE id=?',
                           ('翻译已停止，可继续未完成的翻译。', now, job_id))
        return self.get(owner_id, job_id)

    def should_stop(self, job_id: str, worker_id: str) -> bool:
        with self._connect() as db:
            row = db.execute('SELECT status, worker_id, lease_until FROM jobs WHERE id=?', (job_id,)).fetchone()
        return (row is None or row['status'] != 'running' or row['worker_id'] != worker_id
                or row['lease_until'] is None or row['lease_until'] < time.time())

    def finish_cancel(self, job_id: str, worker_id: str) -> bool:
        now = time.time()
        with self._connect() as db:
            result = db.execute("UPDATE jobs SET status='cancelled', message=?, settings='{}', "
                                'worker_id=NULL, lease_until=NULL, updated_at=? '
                                "WHERE id=? AND worker_id=? AND status='cancelling' AND lease_until>=?",
                                ('翻译已停止，可继续未完成的翻译。', now, job_id, worker_id, now))
            return result.rowcount == 1

    def get(self, owner_id: str, job_id: str) -> dict | None:
        self._directory(job_id)
        with self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=? AND owner_id=?",
                             (job_id, owner_id)).fetchone()
        return self._public(row, self.worker_available()) if row else None

    def manual_mapping(self, owner_id: str, job_id: str) -> dict | None:
        """Return the trusted manual mapping only to the owner of that job."""
        self._directory(job_id)
        with self._connect() as db:
            row = db.execute("SELECT mappings FROM jobs WHERE id=? AND owner_id=?",
                             (job_id, owner_id)).fetchone()
        if not row:
            return None
        mapping = json.loads(row['mappings']).get('CSV_Sheet', {})
        return mapping if isinstance(mapping, dict) and mapping.get('_manual_entry') is True else None

    def clear_project(self, owner_id: str, project_id: str) -> None:
        """Discard this browser/user's jobs for a workspace on a fresh visit."""
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT id FROM jobs WHERE owner_id=? AND project_id=?',
                              (owner_id, project_id)).fetchall()
            db.execute('DELETE FROM jobs WHERE owner_id=? AND project_id=?', (owner_id, project_id))
        for row in rows:
            shutil.rmtree(self._directory(row['id']), ignore_errors=True)

    def list(self, owner_id: str, project_id: str | None = None, limit: int = 25) -> list[dict]:
        limit = max(1, min(int(limit), 100))
        query = "SELECT * FROM jobs WHERE owner_id=?"
        params: list[Any] = [owner_id]
        if project_id is not None:
            query += " AND project_id=?"
            params.append(project_id)
        with self._connect() as db:
            rows = db.execute(query + " ORDER BY created_at DESC LIMIT ?", (*params, limit)).fetchall()
        available = self.worker_available()
        return [self._public(row, available) for row in rows]

    def result_path(self, owner_id: str, job_id: str) -> Path | None:
        self._directory(job_id)
        with self._connect() as db:
            row = db.execute("SELECT extension FROM jobs WHERE id=? AND owner_id=? AND status='completed'",
                             (job_id, owner_id)).fetchone()
        if not row:
            return None
        result = self._directory(job_id) / ("result" + row["extension"])
        return result if result.is_file() else None

    def claim(self, worker_id: str, lease_seconds: int = 120) -> dict | None:
        if not worker_id or lease_seconds < 1:
            raise ValueError("Invalid worker or lease duration")
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Do not retry automatically: a model request may already have been charged.
            db.execute("""UPDATE jobs SET status='interrupted', message=?, worker_id=NULL,
                       lease_until=NULL, settings='{}', updated_at=?
                       WHERE status IN ('running','cancelling') AND lease_until < ?""",
                       ("处理已中断，可继续未完成的翻译。", now, now))
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at, id LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("""UPDATE jobs SET status='running', worker_id=?, lease_until=?,
                       updated_at=?, message=?, started_at=?, queue_seconds=? WHERE id=? AND status='queued'""",
                       (worker_id, now + lease_seconds, now, "正在读取文件", now,
                        max(0, now - (row['queued_at'] or row['created_at'])), row["id"]))
        job = dict(row)
        job["mappings"] = json.loads(job["mappings"])
        job["settings"] = json.loads(job["settings"])
        folder = self._directory(job["id"])
        job["input_path"] = folder / ("input" + job["extension"])
        job["output_path"] = folder / ("result" + job["extension"])
        return job

    def heartbeat(self, job_id: str, worker_id: str, lease_seconds: int = 120) -> bool:
        now = time.time()
        with self._connect() as db:
            result = db.execute("""UPDATE jobs SET lease_until=?, updated_at=?
                WHERE id=? AND worker_id=? AND status IN ('running','cancelling') AND lease_until >= ?""",
                (now + lease_seconds, now, job_id, worker_id, now))
            return result.rowcount == 1

    def progress(self, job_id: str, worker_id: str, completed: int, total: int) -> bool:
        completed, total = int(completed), int(total)
        if completed < 0 or total < 0 or completed > total:
            raise ValueError("Invalid progress")
        now = time.time()
        with self._connect() as db:
            result = db.execute("""UPDATE jobs SET completed=?, total=?, updated_at=?
                WHERE id=? AND worker_id=? AND status='running' AND lease_until >= ?""",
                (completed, total, now, job_id, worker_id, now))
            return result.rowcount == 1

    def stage(self, job_id: str, worker_id: str, stage: str) -> bool:
        now = time.time()
        with self._connect() as db:
            result = db.execute("UPDATE jobs SET message=?, updated_at=? WHERE id=? AND worker_id=? "
                                "AND status='running' AND lease_until>=?",
                                (STAGE_MESSAGES[stage], now, job_id, worker_id, now))
            return result.rowcount == 1

    def complete(self, job_id: str, worker_id: str, summary: dict) -> bool:
        now = time.time()
        encoded = json.dumps(summary, ensure_ascii=False)
        with self._connect() as db:
            result = db.execute("""UPDATE jobs SET status='completed', summary=?, message=?,
                worker_id=NULL, lease_until=NULL, settings='{}', updated_at=?
                WHERE id=? AND worker_id=? AND status='running' AND lease_until >= ?""",
                (encoded, "翻译完成", now, job_id, worker_id, now))
            return result.rowcount == 1

    def fail(self, job_id: str, worker_id: str, message: str = "翻译失败，请检查输入或稍后重试。") -> bool:
        now = time.time()
        with self._connect() as db:
            result = db.execute("""UPDATE jobs SET status='failed', message=?, worker_id=NULL,
                lease_until=NULL, settings='{}', updated_at=? WHERE id=? AND worker_id=? AND status='running'
                AND lease_until >= ?""", (message, now, job_id, worker_id, now))
            return result.rowcount == 1


def process_one(store: JobStore, translator: Callable | None = None, *,
                worker_id: str | None = None, lease_seconds: int = 120) -> bool:
    """Claim and run one job. Returns False when the queue is empty."""
    if translator is None:
        from translation_app.services.translation import translate_file
        translator = translate_file
    worker_id = worker_id or uuid.uuid4().hex
    job = store.claim(worker_id, lease_seconds)
    if not job:
        return False
    stopped = threading.Event()
    cancelled = threading.Event()

    def check_stop():
        if store.should_stop(job['id'], worker_id):
            cancelled.set()

    def keep_alive():
        next_heartbeat = time.monotonic() + lease_seconds / 3
        while not stopped.wait(min(.5, lease_seconds / 3)):
            try:
                check_stop()
                if time.monotonic() >= next_heartbeat:
                    if not store.heartbeat(job["id"], worker_id, lease_seconds):
                        cancelled.set()
                        return
                    next_heartbeat = time.monotonic() + lease_seconds / 3
            except Exception:
                LOG.exception("Job heartbeat failed: %s", job["id"])

    heartbeat = threading.Thread(target=keep_alive, daemon=True)
    heartbeat.start()
    try:
        def progress(done, total):
            if not store.progress(job["id"], worker_id, done, total):
                cancelled.set()
                raise RuntimeError("Job lease lost")

        def stage(value):
            if not store.stage(job['id'], worker_id, value):
                cancelled.set()
                raise RuntimeError('Job lease lost')

        job['settings'].update(_stage_callback=stage, _cancel_event=cancelled,
                               _cancel_check=check_stop,
                               _limiter_root=str(store.root / 'provider-slots'), _job_id=job['id'])

        summary = translator(job["input_path"], job["output_path"], job["project_id"],
                             job["language"], job["mappings"], job["settings"],
                             progress)
        if store.finish_cancel(job['id'], worker_id):
            return True
        if not job["output_path"].is_file():
            raise RuntimeError("Translation did not produce an output file")
        if not store.complete(job["id"], worker_id, summary or {}):
            if not store.finish_cancel(job['id'], worker_id):
                LOG.warning("Job finished after its lease expired: %s", job["id"])
    except Exception as error:
        if store.finish_cancel(job['id'], worker_id):
            LOG.info('Translation job stopped: %s', job['id'])
            return True
        # Provider errors can contain user text or credentials; never put them in logs.
        LOG.warning("Translation job failed: %s", job["id"])
        message = "翻译失败，请检查输入或稍后重试。"
        safe_messages = {"未配置翻译服务密钥", "翻译服务返回的格式无效", "翻译结果格式无效",
                         "翻译结果行号无效", "翻译结果缺失、重复或为空", "翻译结果缺少行",
                         "没有可翻译的源文本", "翻译服务请求失败，请稍后重试",
                         "无法连接翻译服务，请稍后重试", "翻译服务响应超时，请检查网络后重新提交",
                         "翻译服务连接中断，请检查网络后重新提交", "翻译性能配置无效", "翻译任务已停止",
                         "翻译输入超过 Token 预算，请缩短原文、上下文或补充要求"}
        detail = str(error)
        if isinstance(error, ValueError) and detail == "登录已过期，请重新登录":
            message = "登录已过期，请重新登录后重新提交。"
        elif (detail in safe_messages or re.fullmatch(r"第 \d+ 行的变量或标签不匹配", detail)
              or re.fullmatch(r"翻译服务拒绝请求（HTTP [45]\d\d），请检查模型和服务配置", detail)
              or re.fullmatch(r"翻译服务暂不可用（HTTP [45]\d\d），请稍后重试", detail)):
            message = detail
        if not store.fail(job["id"], worker_id, message):
            # A stop request may race the failure update after the check above.
            store.finish_cancel(job['id'], worker_id)
    finally:
        stopped.set()
        heartbeat.join()
    return True


def run_worker(store: JobStore, *, poll_seconds: float = 1.0) -> None:
    """Run one worker process; start more processes to control concurrency."""
    worker_id = uuid.uuid4().hex
    stopped = threading.Event()
    store.touch_worker(worker_id)

    def announce():
        while not stopped.wait(WORKER_HEARTBEAT_INTERVAL):
            try:
                store.touch_worker(worker_id)
            except Exception:
                LOG.exception("Worker heartbeat failed")

    heartbeat = threading.Thread(target=announce, daemon=True)
    heartbeat.start()
    try:
        while True:
            if not process_one(store, worker_id=worker_id):
                time.sleep(poll_seconds)
    finally:
        stopped.set()
        heartbeat.join()
        store.forget_worker(worker_id)


if __name__ == "__main__":
    from dotenv import load_dotenv

    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")
    private_root = Path(os.getenv("LOCATOOL_STORAGE", str(project_root / "instance")))
    job_root = os.getenv("JOB_STORAGE_ROOT", str(private_root / "jobs"))
    logging.basicConfig(level=logging.INFO)
    run_worker(JobStore(job_root))
