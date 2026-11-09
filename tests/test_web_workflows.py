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

    def test_glossary_conflict_lookup_errors_do_not_allow_save(self):
        csrf = self.login()
        db = unittest.mock.MagicMock()
        db.client.table.side_effect = RuntimeError('database failed')
        with patch('translation_app.web.api.engine', return_value=db):
            result = self.client.post('/api/projects/sawa/glossary/check',
                                      json={'language': 'arabic', 'pairs': [{'source': 'a', 'target': 'b'}]},
                                      headers={'X-CSRF-Token': csrf})
        self.assertEqual(result.status_code, 500)
        self.assertNotIn('database failed', result.text)
        db.upsert_terms.assert_not_called()

    def test_knowledge_upload_uses_project_and_language_database_ids(self):
        csrf = self.login()
        db = unittest.mock.MagicMock()
        db.upsert_tm.return_value = 1
        form = {'file': (io.BytesIO('source,target\n你好,مرحبا\n'.encode()), 'terms.csv'),
                'language': 'arabic', 'kind': 'translation_memory', 'sheet': 'CSV_Sheet',
                'source': 'source', 'target': 'target', 'note': ''}
        with patch('translation_app.web.api.engine', return_value=db):
            response = self.client.post('/api/projects/sawa/knowledge/upload', data=form,
                                        headers={'X-CSRF-Token': csrf})
        self.assertEqual(response.status_code, 200, response.get_json())
        db.upsert_tm.assert_called_once_with('sawa', 'Arabic', [{'source': '你好', 'target': 'مرحبا', 'note': ''}])

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

    def test_project_job_loads_anonymous_context_when_disabled(self):
        self.client.get('/projects')
        payload = {'project_id': 's06', 'language': 'english',
                   'manual_rows': [{'source_text': '你好', 'context': ''}]}
        created = self.client.post('/api/jobs', json=payload, headers={'X-CSRF-Token': self.csrf(self.client)})
        self.assertEqual(created.status_code, 202)
        job_id = created.get_json()['id']
        database = unittest.mock.MagicMock()
        query = database.client.table.return_value.select.return_value.eq.return_value.eq.return_value.order.return_value.range.return_value.execute.return_value
        query.data = []
        database.get_game_profile.return_value = ''
        def translator(*args):
            return translate_file(*args, provider=lambda rows, *_: [
                {'row_id': row[0], 'translated_text': 'Hello'} for row in rows])
        with patch.dict('os.environ', {'LOCATOOL_AUTH_ENABLED': 'false'}), \
             patch('translation_app.services.database.ProjectDatabase', return_value=database) as database_class:
            self.assertTrue(process_one(self.app.extensions['jobs'], translator))
        database_class.assert_called_once_with(None, allow_anonymous=True)
        self.assertEqual(self.client.get('/api/jobs/' + job_id).get_json()['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
