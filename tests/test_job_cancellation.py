import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from translation_app import create_app
from translation_app.jobs import process_one
from translation_app.services.translation import translate_file


class JobCancellationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'cancel-test', 'AUTH_ENABLED': False,
                               'STORAGE_ROOT': self.temp.name})
        self.client = self.app.test_client()
        self.client.get('/direct')
        with self.client.session_transaction() as session:
            self.headers = {'X-CSRF-Token': session['csrf']}
        self.store = self.app.extensions['jobs']

    def create(self):
        result = self.client.post('/api/jobs', headers=self.headers, json={
            'project_id': 'direct', 'language': 'english', 'batch_size': 1,
            'manual_rows': [{'source_text': text} for text in ('First {0}', 'Second {1}', 'Third') ]})
        self.assertEqual(result.status_code, 202)
        return result.json['id']

    def cancel(self, job_id):
        return self.client.post(f'/api/jobs/{job_id}/cancel', headers=self.headers)

    def status(self, job_id):
        return self.client.get('/api/jobs/' + job_id).json

    def test_queued_cancel_is_immediate_idempotent_and_not_claimed(self):
        job_id = self.create()
        self.assertTrue(self.status(job_id)['can_cancel'])
        response = self.cancel(job_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['status'], 'cancelled')
        self.assertTrue(response.json['can_retry'])
        self.assertFalse(response.json['can_cancel'])
        self.assertIsNone(self.store.claim('worker'))
        self.assertEqual(self.cancel(job_id).json['status'], 'cancelled')
        with self.store._connect() as db:
            self.assertEqual(json.loads(db.execute('SELECT settings FROM jobs WHERE id=?', (job_id,)).fetchone()[0]), {})
        self.assertEqual(self.client.get(f'/api/jobs/{job_id}/download').status_code, 404)
        self.assertEqual(self.client.post(f'/api/jobs/{job_id}/retry', headers=self.headers).status_code, 202)
        self.assertEqual(self.status(job_id)['status'], 'queued')

    def test_cancel_requires_csrf_and_owner(self):
        job_id = self.create()
        self.assertEqual(self.client.post(f'/api/jobs/{job_id}/cancel').status_code, 403)
        other = self.app.test_client()
        other.get('/direct')
        with other.session_transaction() as session:
            headers = {'X-CSRF-Token': session['csrf']}
        self.assertEqual(other.post(f'/api/jobs/{job_id}/cancel', headers=headers).status_code, 404)
        self.assertEqual(self.status(job_id)['status'], 'queued')

    def test_stop_drains_sent_calls_then_resume_reuses_validated_rows(self):
        job_id = self.create()
        sent, release = threading.Event(), threading.Event()
        calls, cancel_signals = [], []

        def provider(rows, prompt, language, context, settings):
            calls.extend(row[0] for row in rows)
            if rows[0][0] == 1:
                cancel_signals.append(settings['_cancel_event'])
                sent.set()
                if not release.wait(5):
                    raise RuntimeError('Test release timeout')
            return [{'row_id': row[0], 'translated_text': row[1]} for row in rows]

        def translator(*args):
            return translate_file(*args, provider=provider)

        with patch.dict('os.environ', {'TRANSLATION_CONCURRENCY': '1'}):
            worker = threading.Thread(target=lambda: process_one(self.store, translator, worker_id='worker'))
            worker.start()
            try:
                self.assertTrue(sent.wait(3))
                response = self.cancel(job_id)
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.json['status'], 'cancelling')
                self.assertFalse(response.json['can_retry'])
                self.assertTrue(cancel_signals[0].wait(2))
                self.assertEqual(self.client.post(f'/api/jobs/{job_id}/retry', headers=self.headers).status_code, 400)
                self.assertEqual(self.client.get(f'/api/jobs/{job_id}/manual-result').status_code, 404)
            finally:
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(calls, [0, 1])
            self.assertEqual(self.status(job_id)['status'], 'cancelled')
            self.assertTrue(self.status(job_id)['can_retry'])
            self.assertEqual(self.client.get(f'/api/jobs/{job_id}/download').status_code, 404)
            self.assertEqual(self.client.post(f'/api/jobs/{job_id}/retry', headers=self.headers).status_code, 202)
            process_one(self.store, translator, worker_id='resumed')
        self.assertEqual(calls, [0, 1, 2])
        result = self.status(job_id)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['summary']['restored_rows'], 2)
        self.assertEqual(result['summary']['batches'], 1)

    def test_cancellation_wins_over_late_worker_completion(self):
        job_id = self.create()

        def translator(source, target, project, lang, mappings, settings, progress):
            self.cancel(job_id)
            Path(target).write_text('late result')
            return {'rows': 3}

        process_one(self.store, translator, worker_id='worker')
        self.assertEqual(self.status(job_id)['status'], 'cancelled')
        self.assertEqual(self.client.get(f'/api/jobs/{job_id}/download').status_code, 404)

    def test_cancel_racing_failure_is_finalized_as_stopped(self):
        job_id = self.create()
        original = self.store.fail

        def race(job, worker, message):
            self.cancel(job)
            return original(job, worker, message)

        def translator(*args):
            raise ValueError('invalid result')

        with patch.object(self.store, 'fail', side_effect=race):
            process_one(self.store, translator, worker_id='worker')
        self.assertEqual(self.status(job_id)['status'], 'cancelled')

    def test_parallel_sent_batches_are_saved_and_no_next_batch_is_started(self):
        job_id = self.create()
        both_sent = threading.Barrier(3)
        release = threading.Event()
        calls = []

        def provider(rows, *args):
            calls.append(rows[0][0])
            both_sent.wait(3)
            if not release.wait(5):
                raise RuntimeError('test timeout')
            return [{'row_id': row[0], 'translated_text': row[1]} for row in rows]

        def translator(*args):
            return translate_file(*args, provider=provider)

        with patch.dict('os.environ', {'TRANSLATION_CONCURRENCY': '2'}):
            worker = threading.Thread(target=lambda: process_one(self.store, translator, worker_id='parallel'))
            worker.start()
            try:
                both_sent.wait(3)
                self.assertEqual(self.cancel(job_id).json['status'], 'cancelling')
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertCountEqual(calls, [0, 1])
        self.assertEqual(self.status(job_id)['status'], 'cancelled')

    def test_completed_job_is_not_changed_by_late_cancel(self):
        job_id = self.create()

        def translator(source, target, project, lang, mappings, settings, progress):
            Path(target).write_text('complete result')
            return {'rows': 3}

        process_one(self.store, translator, worker_id='worker')
        self.assertEqual(self.cancel(job_id).json['status'], 'completed')
        self.assertIsNotNone(self.status(job_id)['download_url'])

    def test_expired_stopping_worker_is_recoverable_and_fenced(self):
        job_id = self.create()
        self.store.claim('lost')
        self.assertEqual(self.cancel(job_id).json['status'], 'cancelling')
        self.assertTrue(self.store.heartbeat(job_id, 'lost'))
        with self.store._connect() as db:
            db.execute('UPDATE jobs SET lease_until=? WHERE id=?', (time.time() - 1, job_id))
        self.assertEqual(self.status(job_id)['status'], 'interrupted')
        self.assertEqual(self.client.post(f'/api/jobs/{job_id}/retry', headers=self.headers).status_code, 202)
        self.assertFalse(self.store.finish_cancel(job_id, 'lost'))
        self.assertFalse(self.store.complete(job_id, 'lost', {}))
        self.assertEqual(self.status(job_id)['status'], 'queued')

    def test_ui_contains_stop_button_and_stopping_state(self):
        page = self.client.get('/direct').text
        self.assertIn('id="stop-job" hidden', page)
        self.assertIn('停止翻译', page)


if __name__ == '__main__':
    unittest.main()
