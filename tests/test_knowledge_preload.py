import base64
import json
import os
import time
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from translation_app import create_app
from translation_app.jobs import process_one
from translation_app.services.translation import translate_file


class KnowledgePreloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'LOCATOOL_AUTH_ENABLED': 'true',
                                      'TRANSLATION_RESOURCE_CACHE_SECONDS': '300',
                                      'LOCATOOL_STORAGE': self.temp.name})
        env.start()
        self.addCleanup(env.stop)
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'test',
                               'STORAGE_ROOT': self.temp.name, 'AUTH_ENABLED': True})
        self.client = self.app.test_client()
        self.client.get('/login')
        expiry = time.time() + 3600
        claims = base64.urlsafe_b64encode(json.dumps({'exp': expiry}).encode()).decode().rstrip('=')
        with self.client.session_transaction() as session:
            session['sid'] = self.app.extensions['sessions'].create(
                'member', 'member@example.invalid', f'header.{claims}.signature', 'refresh', expiry)
            self.headers = {'X-CSRF-Token': session['csrf']}
        self.engine = MagicMock(storage_root=self.temp.name)
        self.enterContext(patch('translation_app.web.api.engine', return_value=self.engine))
        self.enterContext(patch('translation_app.web.api.read_prompts', return_value={
            'system': {'content': 'Translate', 'version': 1},
            'instructions': {'content': '', 'version': 1},
        }))
        self.members = [{'can_edit': True}]
        self.rows = [{'kind': kind, 'source': '举报', 'target': 'إبلاغ', 'note': '',
                      'scene': '', 'string_key': '', 'verified': False, 'status': 'active'}
                     for kind in ('terms', 'memory')]
        self.rows.append({**self.rows[0], 'source': '草稿词', 'target': 'مسودة', 'status': 'pending'})
        self.row_scans = 0

        def table(name):
            query = MagicMock()
            query.select.return_value = query
            query.eq.return_value = query
            query.order.return_value = query

            if name == 'knowledge_project_members':
                query.limit.return_value = query
                query.execute.return_value.data = self.members
                return query

            def page(start, end):
                if start == 0:
                    self.row_scans += 1
                query.execute.return_value.data = self.rows if start == 0 else []
                return query

            query.range.side_effect = page
            return query

        self.engine.client.table.side_effect = table
        self.engine.get_game_profile.return_value = 'Friendly game'
        self.factory = patch('translation_app.services.database.ProjectDatabase', return_value=self.engine)
        self.create_database = self.factory.start()
        self.addCleanup(self.factory.stop)

    def preload(self, project='tarbi3a_baloot', language='arabic', **options):
        return self.client.post(f'/api/projects/{project}/knowledge/preload',
                                json={'language': language, **options}, headers=self.headers)

    def test_preload_is_reused_by_actual_translation_job(self):
        response = self.preload(include_lists=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json['terms'], response.json['memory']), (1, 1))
        self.assertGreater(response.json['expires_in'], 0)
        self.assertEqual([row['status'] for row in response.json['libraries']['terms']], ['active', 'pending'])
        self.assertEqual(len(response.json['libraries']['memory']), 1)
        self.assertTrue(response.json['editable'])
        self.assertNotIn('Friendly game', response.text)
        self.assertEqual(self.create_database.call_count, 1)
        created = self.client.post('/api/jobs', json={
            'project_id': 'tarbi3a_baloot', 'language': 'arabic',
            'manual_rows': [{'source_text': '点击举报 草稿词'}]}, headers=self.headers)
        self.assertEqual(created.status_code, 202)

        def provider(rows, prompt, language, context, settings):
            self.assertNotIn('Friendly game', context)
            self.assertIn('إبلاغ', context)
            self.assertNotIn('مسودة', context)
            return [{'row_id': row[0], 'translated_text': 'اضغط للإبلاغ'} for row in rows]

        def translator(*args):
            return translate_file(*args, provider=provider)

        process_one(self.app.extensions['jobs'], translator)
        result = self.client.get('/api/jobs/' + created.json['id']).json
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.create_database.call_count, 1)
        # Clearing browser jobs does not discard the shared project cache.
        self.client.post('/api/jobs/clear', json={'project_id': 'tarbi3a_baloot'}, headers=self.headers)
        self.assertEqual(self.preload().status_code, 200)
        self.assertEqual(self.create_database.call_count, 1)

    def test_language_and_project_scope_validation_and_csrf(self):
        self.assertEqual(self.preload('s06', 'arabic').status_code, 400)
        self.assertEqual(self.preload('direct', 'arabic').status_code, 400)
        self.assertEqual(self.preload('missing', 'arabic').status_code, 400)
        response = self.client.post('/api/projects/tarbi3a_baloot/knowledge/preload',
                                    json={'language': 'arabic'})
        self.assertEqual(response.status_code, 403)
        self.create_database.assert_not_called()

    def test_authenticated_workspace_requires_login(self):
        response = self.app.test_client().get('/api/projects/tarbi3a_baloot/knowledge/terms/entries?language=arabic')
        self.assertEqual(response.status_code, 401)
        self.create_database.assert_not_called()

    def test_mutation_invalidates_shared_snapshot_before_next_preload(self):
        self.assertEqual(self.preload(include_lists=True).status_code, 200)
        self.engine.client.rpc.return_value.execute.return_value.data = 1
        self.rows.append({**self.rows[0], 'source': '检举', 'target': 'تبليغ'})
        created = self.client.post('/api/projects/tarbi3a_baloot/knowledge/terms/entries',
                                   json={'language': 'arabic', 'entry': {'source': '检举', 'target': 'تبليغ'}},
                                   headers=self.headers)
        self.assertEqual(created.status_code, 201, created.text)
        refreshed = self.preload(include_lists=True)
        self.assertEqual(refreshed.json['terms'], 2)
        self.assertIn('检举', [row['source'] for row in refreshed.json['libraries']['terms']])

        job = self.client.post('/api/jobs', json={'project_id': 'tarbi3a_baloot', 'language': 'arabic',
                               'manual_rows': [{'source_text': '点击检举'}]}, headers=self.headers)
        self.assertEqual(job.status_code, 202, job.text)

        def provider(rows, _prompt, _language, context, _settings):
            self.assertIn('تبليغ', context)
            return [{'row_id': row[0], 'translated_text': 'اضغط للتبليغ'} for row in rows]

        process_one(self.app.extensions['jobs'], lambda *args: translate_file(*args, provider=provider))
        self.assertEqual(self.client.get('/api/jobs/' + job.json['id']).json['status'], 'completed')
        self.assertEqual(self.row_scans, 2)

    def test_management_preload_rechecks_permissions_and_can_force_refresh(self):
        self.assertEqual(self.preload(include_lists=True).json['terms'], 1)
        self.rows[0]['status'] = 'archived'
        self.assertEqual(self.preload(include_lists=True).json['terms'], 1)
        refreshed = self.preload(include_lists=True, refresh=True)
        self.assertEqual(refreshed.json['terms'], 0)
        self.assertEqual(refreshed.json['libraries']['terms'][0]['status'], 'archived')
        self.assertEqual(self.preload().json['terms'], 0)
        self.members.clear()
        self.assertEqual(self.preload(include_lists=True).status_code, 403)
        self.assertEqual(self.row_scans, 2)

    def test_failed_preload_returns_error_and_can_be_retried(self):
        original = self.engine.client.table.side_effect
        self.engine.client.table.side_effect = RuntimeError('private database details')
        failed = self.preload()
        self.assertEqual(failed.status_code, 500)
        self.assertNotIn('private database details', failed.text)
        self.engine.client.table.side_effect = original
        self.assertEqual(self.preload().status_code, 200)
        self.assertEqual(self.create_database.call_count, 2)


if __name__ == '__main__':
    unittest.main()
