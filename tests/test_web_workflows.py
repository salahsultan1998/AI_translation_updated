import io
import tempfile
import time
import unittest
from unittest.mock import patch

from translation_app import create_app
from translation_app.jobs import process_one
from translation_app.services.translation import translate_file


class FakeAuth:
    def login(self, email, password):
        if password != 'correct':
            raise ValueError('bad password')
        return {'user_id': email, 'email': email, 'access_token': 'private-token',
                'refresh_token': 'private-refresh', 'expires_at': time.time() + 3600}

    def refresh(self, token):
        raise AssertionError('unexpected refresh')


class WebWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'test-secret',
                               'STORAGE_ROOT': self.temp.name, 'AUTH_PROVIDER': FakeAuth(),
                               'AUTH_ENABLED': True})
        # Prompt storage has its own request-level tests in test_prompt_editor.
        prompt_patch = patch('translation_app.web.api.read_prompts', return_value={
            'system': {'content': 'Translate the text', 'version': 1},
            'instructions': {'content': '', 'version': 1},
        })
        prompt_patch.start()
        self.addCleanup(prompt_patch.stop)
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def login(self, client=None, email='a@example.com'):
        client = client or self.client
        client.get('/login')
        with client.session_transaction() as sess:
            csrf = sess['csrf']
        result = client.post('/login', data={'email': email, 'password': 'correct', 'csrf_token': csrf})
        self.assertEqual(result.status_code, 302)
        client.get('/projects')
        with client.session_transaction() as sess:
            return sess['csrf']

    def test_login_csrf_and_project_language_isolation(self):
        self.assertEqual(self.client.get('/projects').status_code, 302)
        self.assertEqual(self.client.post('/login', data={}).status_code, 403)
        csrf = self.login()
        page = self.client.get('/projects')
        self.assertEqual(page.status_code, 200)
        self.assertIn('直接翻译', page.text)
        self.assertNotIn('DM_03', page.text)
        project = self.client.get('/projects/s06').text
        self.assertIn('英语', project)
        self.assertNotIn('土耳其语', project)
        invalid = self.client.post('/api/jobs', json={'project_id': 's06', 'language': 'arabic'}, headers={'X-CSRF-Token': csrf})
        self.assertEqual(invalid.status_code, 400)

    def test_public_static_cache_does_not_extend_to_pages_or_api(self):
        path = '/static/css/app.css'
        with self.client.get(path) as asset:
            self.assertEqual(asset.status_code, 200)
            self.assertEqual(asset.headers['Cache-Control'], 'public, max-age=86400')
            self.assertNotIn('Set-Cookie', asset.headers)
            etag = asset.headers['ETag']
        with self.client.get(path, headers={'If-None-Match': etag}) as cached:
            self.assertEqual(cached.status_code, 304)
            self.assertEqual(cached.headers['Cache-Control'], 'public, max-age=86400')
        with self.client.get('/static/missing.webp') as missing:
            self.assertEqual(missing.headers['Cache-Control'], 'no-store')
        self.login()
        self.assertEqual(self.client.get('/direct').headers['Cache-Control'], 'no-store')
        self.assertEqual(self.client.get('/api/jobs').headers['Cache-Control'], 'no-store')

    def test_upload_submit_and_owner_scope(self):
        csrf = self.login()
        headers = {'X-CSRF-Token': csrf}
        preview = self.client.post('/api/uploads/preview', data={'file': (io.BytesIO('源文,上下文\n你好,问候\n'.encode()), 'sample.csv')}, headers=headers)
        self.assertEqual(preview.status_code, 200)
        upload = preview.get_json()
        self.assertEqual(upload['sheets'][0]['rows'][0], ['你好', '问候'])
        request = {'project_id': 'direct', 'language': 'english', 'upload_id': upload['upload_id'],
                   'sheets': [{'name': 'CSV_Sheet', 'source': '源文', 'context': '上下文', 'target': '译文'}]}
        created = self.client.post('/api/jobs', json=request, headers=headers)
        self.assertEqual(created.status_code, 202, created.get_json())
        job_id = created.get_json()['id']
        status = self.client.get('/api/jobs/' + job_id)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.get_json()['status'], 'queued')
        self.assertNotIn('private-token', status.text)
        other = self.app.test_client()
        other_csrf = self.login(other, 'b@example.com')
        self.assertEqual(other.get('/api/jobs/' + job_id).status_code, 404)
        other_upload = other.post('/api/jobs', json=request, headers={'X-CSRF-Token': other_csrf})
        self.assertEqual(other_upload.status_code, 400)

    def test_logout_invalidates_session(self):
        csrf = self.login()
        self.assertEqual(self.client.post('/logout', data={'csrf_token': csrf}).status_code, 302)
        self.assertEqual(self.client.get('/api/jobs').status_code, 401)

    def test_manual_to_completed_preview_and_download(self):
        csrf = self.login()
        body = {'project_id': 'direct', 'language': 'english',
                'manual_rows': [{'source_text': '你好 {0}', 'context': '问候'}], 'manual_target': '英语'}
        result = self.client.post('/api/jobs', json=body, headers={'X-CSRF-Token': csrf})
        self.assertEqual(result.status_code, 202, result.get_json())
        job_id = result.get_json()['id']

        def translator(*args):
            return translate_file(*args, provider=lambda rows, *_: [
                {'row_id': row[0], 'translated_text': 'Hello {0}'} for row in rows])

        self.assertTrue(process_one(self.app.extensions['jobs'], translator))
        status = self.client.get('/api/jobs/' + job_id).get_json()
        self.assertEqual(status['status'], 'completed')
        self.assertIsNotNone(status['download_url'])
        preview = self.client.get('/api/jobs/' + job_id + '/preview').get_json()
        self.assertEqual(preview['sheets'][0]['rows'][0][-1], 'Hello {0}')
        download = self.client.get(status['download_url'])
        self.assertEqual(download.status_code, 200)
        self.assertIn(b'Hello {0}', download.data)
        download.close()


class DevelopmentModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'test-secret',
                               'STORAGE_ROOT': self.temp.name, 'AUTH_PROVIDER': FakeAuth(),
                               'AUTH_ENABLED': False})
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def csrf(self, client):
        with client.session_transaction() as sess:
            return sess['csrf']

    def test_no_login_and_session_scoped_jobs(self):
        page = self.client.get('/projects')
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('退出登录', page.text)
        self.assertEqual(self.client.get('/login').status_code, 302)
        self.assertEqual(self.client.post('/api/jobs', json={}).status_code, 403)
        payload = {'project_id': 'direct', 'language': 'english',
                   'manual_rows': [{'source_text': '你好', 'context': ''}]}
        created = self.client.post('/api/jobs', json=payload, headers={'X-CSRF-Token': self.csrf(self.client)})
        self.assertEqual(created.status_code, 202)
        job_id = created.get_json()['id']
        self.assertEqual(self.client.get('/api/jobs/' + job_id).status_code, 200)
        other = self.app.test_client()
        other.get('/projects')
        self.assertEqual(other.get('/api/jobs/' + job_id).status_code, 404)

    def test_refresh_cleanup_discards_only_current_users_project_jobs(self):
        self.client.get('/projects')
        headers = {'X-CSRF-Token': self.csrf(self.client)}
        body = {'project_id': 'direct', 'language': 'english', 'manual_rows': [{'source_text': '你好'}]}
        job = self.client.post('/api/jobs', json=body, headers=headers).get_json()
        other = self.app.test_client()
        other.get('/projects')
        other_job = other.post('/api/jobs', json=body, headers={'X-CSRF-Token': self.csrf(other)}).get_json()
        response = self.client.post('/api/jobs/clear', json={'project_id': 'direct'}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/api/jobs/' + job['id']).status_code, 404)
        self.assertEqual(other.get('/api/jobs/' + other_job['id']).status_code, 200)

    def test_project_job_cannot_use_anonymous_knowledge_when_login_disabled(self):
        self.client.get('/projects')
        payload = {'project_id': 's06', 'language': 'english',
                   'manual_rows': [{'source_text': '你好', 'context': ''}]}
        created = self.client.post('/api/jobs', json=payload, headers={'X-CSRF-Token': self.csrf(self.client)})
        self.assertEqual(created.status_code, 202)
        job_id = created.get_json()['id']
        def translator(*args):
            return translate_file(*args, provider=lambda *_: self.fail('anonymous job reached the model'))
        with patch.dict('os.environ', {'LOCATOOL_AUTH_ENABLED': 'false'}), \
             patch('translation_app.services.database.ProjectDatabase') as database_class:
            self.assertTrue(process_one(self.app.extensions['jobs'], translator))
        database_class.assert_not_called()
        result = self.client.get('/api/jobs/' + job_id).get_json()
        self.assertEqual(result['status'], 'failed')
        self.assertIn('请登录有项目权限', result['message'])


if __name__ == '__main__':
    unittest.main()
