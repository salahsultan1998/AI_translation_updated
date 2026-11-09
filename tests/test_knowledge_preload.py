import os
from pathlib import Path
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
        env = patch.dict(os.environ, {'LOCATOOL_AUTH_ENABLED': 'false',
                                      'TRANSLATION_RESOURCE_CACHE_SECONDS': '300',
                                      'LOCATOOL_STORAGE': self.temp.name})
        env.start()
        self.addCleanup(env.stop)
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'test',
                               'STORAGE_ROOT': self.temp.name, 'AUTH_ENABLED': False})
        self.client = self.app.test_client()
        self.client.get('/projects/tarbi3a_baloot')
        with self.client.session_transaction() as session:
            self.headers = {'X-CSRF-Token': session['csrf']}
        self.engine = MagicMock()

        def table(name):
            query = MagicMock()
            query.select.return_value = query
            query.eq.return_value = query
            query.order.return_value = query

            def page(start, end):
                items = ([{'source_term': '举报', 'target_term': 'إبلاغ', 'note': ''}]
                         if name == 'glossaries' else
                         [{'source_text': '举报', 'target_text': 'إبلاغ'}])
                query.execute.return_value.data = items if start == 0 else []
                return query

            query.range.side_effect = page
            return query

        self.engine.client.table.side_effect = table
        self.engine.get_game_profile.return_value = 'Friendly game'
        self.factory = patch('translation_app.services.database.ProjectDatabase', return_value=self.engine)
        self.create_database = self.factory.start()
        self.addCleanup(self.factory.stop)

    def preload(self, project='tarbi3a_baloot', language='arabic'):
        return self.client.post(f'/api/projects/{project}/knowledge/preload',
                                json={'language': language}, headers=self.headers)

    def test_preload_is_reused_by_actual_translation_job(self):
        response = self.preload()
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json['terms'], response.json['memory']), (1, 1))
        self.assertGreater(response.json['expires_in'], 0)
        self.assertNotIn('Friendly game', response.text)
        self.assertEqual(self.create_database.call_count, 1)
        created = self.client.post('/api/jobs', json={
            'project_id': 'tarbi3a_baloot', 'language': 'arabic',
            'manual_rows': [{'source_text': '点击举报'}]}, headers=self.headers)
        self.assertEqual(created.status_code, 202)

        def provider(rows, prompt, language, context, settings):
            self.assertIn('Friendly game', context)
            self.assertIn('إبلاغ', context)
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
        self.app.config['AUTH_ENABLED'] = True
        response = self.preload()
        self.assertEqual(response.status_code, 401)
        self.create_database.assert_not_called()

    def test_mutation_invalidates_shared_snapshot_before_next_preload(self):
        self.preload()
        from translation_app.services.resources import invalidate_resources
        self.engine.get_game_profile.return_value = 'New game style'
        invalidate_resources('tarbi3a_baloot', storage_root=Path(self.temp.name))
        self.assertEqual(self.preload().status_code, 200)
        self.assertEqual(self.create_database.call_count, 2)

    def test_failed_preload_returns_error_and_can_be_retried(self):
        self.engine.get_game_profile.side_effect = RuntimeError('private database details')
        failed = self.preload()
        self.assertEqual(failed.status_code, 500)
        self.assertNotIn('private database details', failed.text)
        self.engine.get_game_profile.side_effect = None
        self.assertEqual(self.preload().status_code, 200)
        self.assertEqual(self.create_database.call_count, 2)


if __name__ == '__main__':
    unittest.main()
