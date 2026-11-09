"""Explicit retries preserve frozen settings and fence expired workers."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from translation_app import create_app
from translation_app.jobs import JobStore, process_one
from translation_app.services.translation import translate_file


class JobRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = JobStore(self.temp.name)

    def create(self, owner='alice'):
        return self.store.create(owner, 's06', 'english', 'test.csv', b'source\nhello\n',
                                 {'CSV_Sheet': {'source': 'source', 'target': 'target'}},
                                 {'model': 'frozen-model', 'system_prompt': 'frozen prompt', 'batch_size': 30,
                                  'custom_instructions': 'frozen instructions', 'access_token': 'old-token'})

    def test_explicit_retry_keeps_snapshot_and_refreshes_credentials(self):
        job = self.create()
        snapshot = (self.store.files / job['id'] / 'retry.json').read_text()
        self.assertNotIn('old-token', snapshot)
        self.store.claim('first')
        self.store.progress(job['id'], 'first', 2, 4)
        self.assertTrue(self.store.fail(job['id'], 'first'))
        self.assertIsNone(self.store.retry('bob', job['id'], 'other-token'))
        retried = self.store.retry('alice', job['id'], 'fresh-token')
        self.assertEqual(retried['status'], 'queued')
        self.assertNotIn('fresh-token', json.dumps(retried))
        second = self.store.claim('second')
        self.assertEqual(second['settings']['access_token'], 'fresh-token')
        self.assertEqual(second['settings']['model'], 'frozen-model')
        self.assertEqual(second['settings']['system_prompt'], 'frozen prompt')
        self.assertEqual(second['settings']['custom_instructions'], 'frozen instructions')
        self.assertEqual(second['output_path'].parent.name, job['id'])
        self.assertFalse(self.store.progress(job['id'], 'first', 4, 4))
        self.assertFalse(self.store.complete(job['id'], 'first', {}))
        self.assertFalse(self.store.fail(job['id'], 'first'))

    def test_running_job_cannot_retry_but_expired_lease_can(self):
        job = self.create()
        self.store.claim('first')
        with self.assertRaisesRegex(ValueError, '只有失败'):
            self.store.retry('alice', job['id'])
        with closing(sqlite3.connect(self.store.database)) as db, db:
            db.execute('UPDATE jobs SET lease_until=? WHERE id=?', (time.time()-1, job['id']))
        self.assertEqual(self.store.retry('alice', job['id'])['status'], 'queued')
        with self.assertRaisesRegex(ValueError, '只有失败'):
            self.store.retry('alice', job['id'])

    def test_retry_respects_active_job_limit(self):
        job = self.create()
        self.store.claim('first')
        self.store.fail(job['id'], 'first')
        for _ in range(3):
            self.create()
        with self.assertRaisesRegex(ValueError, '最多提交三个'):
            self.store.retry('alice', job['id'])

    def test_phase_progress_and_queue_metrics(self):
        job = self.create()
        def translator(source, target, project, language, mappings, settings, progress):
            for phase, message in [('resources', '正在加载项目知识库'), ('translating', '正在翻译'),
                                   ('writing', '正在生成结果文件')]:
                settings['_stage_callback'](phase)
                self.assertEqual(self.store.get('alice', job['id'])['message'], message)
            self.assertFalse(settings['_cancel_event'].is_set())
            self.assertEqual(settings['_limiter_root'], str(self.store.root / 'provider-slots'))
            progress(1, 1)
            Path(target).write_text('source,target\nhello,Hello\n')
            return {'rows': 1}
        process_one(self.store, translator, worker_id='worker')
        result = self.store.get('alice', job['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertGreaterEqual(result['queue_seconds'], 0)
        self.assertGreaterEqual(result['started_at'], result['created_at'])
        self.assertFalse(result['can_retry'])
        with self.assertRaisesRegex(ValueError, '只有失败'):
            self.store.retry('alice', job['id'])

    def test_lost_lease_cancels_translation_and_rejects_phase_update(self):
        job = self.create()
        def translator(source, target, project, language, mappings, settings, progress):
            with closing(sqlite3.connect(self.store.database)) as db, db:
                db.execute('UPDATE jobs SET lease_until=?', (time.time()-1,))
            with self.assertRaisesRegex(RuntimeError, 'lease lost'):
                settings['_stage_callback']('writing')
            self.assertTrue(settings['_cancel_event'].is_set())
            raise RuntimeError('lease lost')
        process_one(self.store, translator, worker_id='worker')
        self.assertEqual(self.store.get('alice', job['id'])['status'], 'interrupted')


class RetryApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'test', 'AUTH_ENABLED': False,
                               'STORAGE_ROOT': self.temp.name})
        self.client = self.app.test_client()
        self.client.get('/projects')
        with self.client.session_transaction() as session:
            self.headers = {'X-CSRF-Token': session['csrf']}
        self.store = self.app.extensions['jobs']

    def test_retry_api_requires_csrf_and_owner_and_uses_original_settings(self):
        body = {'project_id': 'direct', 'language': 'english', 'manual_rows': [{'source_text': 'hello'}]}
        created = self.client.post('/api/jobs', json=body, headers=self.headers)
        self.assertEqual(created.status_code, 202)
        job_id = created.get_json()['id']
        self.store.claim('worker')
        self.store.fail(job_id, 'worker')
        self.assertEqual(self.client.post(f'/api/jobs/{job_id}/retry').status_code, 403)
        other = self.app.test_client()
        other.get('/projects')
        with other.session_transaction() as session:
            other_headers = {'X-CSRF-Token': session['csrf']}
        self.assertEqual(other.post(f'/api/jobs/{job_id}/retry', headers=other_headers).status_code, 404)
        result = self.client.post(f'/api/jobs/{job_id}/retry', headers=self.headers)
        self.assertEqual(result.status_code, 202)
        self.assertEqual(result.get_json()['id'], job_id)
        claimed = self.store.claim('replacement')
        from translation_app.services.projects import load_prompt_pair
        self.assertEqual(claimed['settings']['instructions'], load_prompt_pair('direct', 'english')['instructions'])
        self.assertEqual(claimed['settings']['batch_size'], 10)
        self.assertFalse(claimed['settings']['use_exact_memory'])
        self.assertEqual(self.client.post(f'/api/jobs/{job_id}/retry', headers=self.headers).status_code, 400)
        page = self.client.get('/direct').text
        self.assertIn('retry-job', page)
        self.assertIn('可能重复计费', page)

    def test_real_pipeline_resumes_after_timeout_without_resending_successful_rows(self):
        self.check_pipeline_resume(RuntimeError('翻译服务响应超时，请检查网络后重新提交'))

    def test_real_pipeline_does_not_reuse_invalid_model_output(self):
        self.check_pipeline_resume([{'row_id': 1, 'translated_text': 'Missing placeholder'}])

    def check_pipeline_resume(self, failure):
        body = {'project_id': 'direct', 'language': 'english', 'batch_size': 1,
                'manual_rows': [{'source_text': text} for text in ('第一行 {0}', '第二行 {1}', '第三行', '第一行 {0}')]}
        created = self.client.post('/api/jobs', json=body, headers=self.headers)
        self.assertEqual(created.status_code, 202)
        job_id = created.get_json()['id']
        sent_before, sent_after = [], []

        def initial_provider(rows, *_):
            self.assertEqual(len(rows), 1)
            row_id, source, _ = rows[0]
            sent_before.append(source)
            if source == '第二行 {1}':
                if isinstance(failure, Exception):
                    raise failure
                return failure
            return [{'row_id': row_id, 'translated_text': 'First {0}'}]

        def resumed_provider(rows, *_):
            self.assertEqual(len(rows), 1)
            row_id, source, _ = rows[0]
            sent_after.append(source)
            return [{'row_id': row_id, 'translated_text': {'第二行 {1}': 'Second {1}', '第三行': 'Third'}[source]}]

        with patch.dict('os.environ', {'TRANSLATION_CONCURRENCY': '1'}):
            process_one(self.store, lambda *args: translate_file(*args, provider=initial_provider))
            failed = self.client.get(f'/api/jobs/{job_id}').get_json()
            self.assertEqual(failed['status'], 'failed')
            self.assertTrue(failed['can_retry'])
            self.assertIsNone(failed['download_url'])
            self.assertEqual(self.client.get(f'/api/jobs/{job_id}/manual-result').status_code, 404)
            self.assertEqual(sent_before, ['第一行 {0}', '第二行 {1}'])
            response = self.client.post(f'/api/jobs/{job_id}/retry', headers=self.headers)
            self.assertEqual(response.status_code, 202, response.get_json())
            self.assertEqual(response.get_json()['id'], job_id)
            process_one(self.store, lambda *args: translate_file(*args, provider=resumed_provider))

        self.assertEqual(sent_after, ['第二行 {1}', '第三行'])
        result = self.client.get(f'/api/jobs/{job_id}').get_json()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['summary']['restored_rows'], 1)
        self.assertEqual(result['summary']['deduplicated_rows'], 1)
        rows = self.client.get(f'/api/jobs/{job_id}/manual-result').get_json()['rows']
        self.assertEqual([row['translated_text'] for row in rows], ['First {0}', 'Second {1}', 'Third', 'First {0}'])


if __name__ == '__main__':
    unittest.main()
