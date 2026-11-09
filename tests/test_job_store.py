import csv
import json
import io
import sqlite3
from contextlib import closing
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from translation_app.jobs import JobStore, process_one, run_worker


class DurableJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = JobStore(self.temp.name)

    def create(self, owner="alice"):
        return self.store.create(owner, "s06", "english", "source.csv", b"source\nhello\n",
                                 {"CSV_Sheet": {"source": "source", "target": "english"}},
                                 {"access_token": "sensitive", "model": "test"})

    def test_claim_once_and_enforce_owner(self):
        job = self.create()
        barrier = threading.Barrier(3)
        claimed = []

        def worker(name):
            barrier.wait()
            claimed.append(self.store.claim(name))

        threads = [threading.Thread(target=worker, args=(name,)) for name in ("one", "two")]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(item is not None for item in claimed), 1)
        self.assertIsNone(self.store.get("bob", job["id"]))
        self.assertIsNone(self.store.result_path("bob", job["id"]))
        self.assertNotIn("settings", self.store.get("alice", job["id"]))

    def test_completed_job_progress_and_credentials_scrubbed(self):
        job = self.create()

        def translator(source, target, project, lang, mapping, settings, progress):
            self.assertEqual(settings["access_token"], "sensitive")
            self.assertEqual((project, lang), ("s06", "english"))
            self.assertTrue(Path(source).is_file())
            progress(1, 1)
            Path(target).write_bytes(b"source,english\nhello,hello\n")
            return {"rows": 1}

        self.assertTrue(process_one(self.store, translator, worker_id="worker"))
        item = self.store.get("alice", job["id"])
        self.assertEqual((item["status"], item["completed"], item["total"]), ("completed", 1, 1))
        self.assertEqual(item["summary"], {"rows": 1})
        self.assertTrue(self.store.result_path("alice", job["id"]).is_file())
        with closing(sqlite3.connect(self.store.database)) as db, db:
            self.assertEqual(json.loads(db.execute("SELECT settings FROM jobs").fetchone()[0]), {})

    def test_interrupted_jobs_are_not_automatically_replayed(self):
        job = self.create()
        self.store.claim("lost", 1)
        with closing(sqlite3.connect(self.store.database)) as db, db:
            db.execute("UPDATE jobs SET lease_until=?", (time.time() - 1,))
        self.assertIsNone(self.store.claim("replacement"))
        self.assertEqual(self.store.get("alice", job["id"])["status"], "interrupted")
        self.assertFalse(self.store.complete(job["id"], "lost", {}))
        with closing(sqlite3.connect(self.store.database)) as db, db:
            self.assertEqual(json.loads(db.execute("SELECT settings FROM jobs").fetchone()[0]), {})

    def test_queued_job_reports_fresh_and_stale_worker_without_changing_job(self):
        job = self.create()
        self.assertFalse(self.store.get('alice', job['id'])['worker_online'])
        self.store.touch_worker('worker')
        self.assertTrue(self.store.get('alice', job['id'])['worker_online'])
        with closing(sqlite3.connect(self.store.database)) as db, db:
            db.execute('UPDATE workers SET heartbeat_at=?', (time.time() - 60,))
        self.assertFalse(self.store.get('alice', job['id'])['worker_online'])
        self.assertEqual(self.store.get('alice', job['id'])['status'], 'queued')
        self.assertEqual(self.store.list('alice')[0]['status'], 'queued')

    def test_expired_running_lease_is_visible_without_restarting_a_worker(self):
        job = self.create()
        self.store.claim('lost', 1)
        with closing(sqlite3.connect(self.store.database)) as db, db:
            db.execute('UPDATE jobs SET lease_until=?', (time.time() - 1,))
        self.assertEqual(self.store.get('alice', job['id'])['status'], 'interrupted')
        self.assertEqual(self.store.list('alice')[0]['status'], 'interrupted')
        with closing(sqlite3.connect(self.store.database)) as db:
            self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0], 'running')

    def test_worker_registers_and_cleans_up_presence(self):
        def stop_after_check(store, **kwargs):
            self.assertTrue(store.worker_available())
            raise RuntimeError('stop worker for test')

        with patch('translation_app.jobs.process_one', side_effect=stop_after_check):
            with self.assertRaisesRegex(RuntimeError, 'stop worker'):
                run_worker(self.store)
        self.assertFalse(self.store.worker_available())

    def test_failure_never_exposes_partial_output(self):
        job = self.create()

        def translator(source, target, project, lang, mapping, settings, progress):
            Path(target).write_bytes(b"partial")
            raise ValueError("private content")

        process_one(self.store, translator)
        item = self.store.get("alice", job["id"])
        self.assertEqual(item["status"], "failed")
        self.assertNotIn("private content", item["message"])
        self.assertIsNone(self.store.result_path("alice", job["id"]))
        with closing(sqlite3.connect(self.store.database)) as db, db:
            self.assertEqual(json.loads(db.execute("SELECT settings FROM jobs").fetchone()[0]), {})

    def test_expired_login_shows_actionable_error(self):
        job = self.create()

        def translator(*args):
            raise ValueError("登录已过期，请重新登录")

        process_one(self.store, translator)
        self.assertIn("重新登录", self.store.get("alice", job["id"])["message"])

    def test_worker_with_translation_pipeline(self):
        from translation_app.services.translation import translate_file
        job = self.store.create("alice", "direct", "english", "demo.csv", b"source\nhello\n",
                                {"CSV_Sheet": {"source": "source", "context": None, "target": "English"}},
                                {"batch_size": 10})

        def provider(rows, prompt, language, context, settings):
            return [{"row_id": row[0], "translated_text": "Hello"} for row in rows]

        def translator(*args):
            return translate_file(*args, provider=provider)

        process_one(self.store, translator)
        self.assertEqual(self.store.get("alice", job["id"])["status"], "completed")
        result = self.store.result_path("alice", job["id"]).read_text(encoding="utf-8-sig")
        self.assertEqual(list(csv.reader(io.StringIO(result)))[1], ["hello", "Hello"])


if __name__ == "__main__":
    unittest.main()
