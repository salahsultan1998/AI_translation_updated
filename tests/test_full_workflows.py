"""Flask workflow coverage with isolated files and mocked external services."""
import io
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

from openpyxl import Workbook, load_workbook

from translation_app import create_app
from translation_app.jobs import process_one
from translation_app.services.translation import translate_file


class FlaskWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'test-key',
                               'STORAGE_ROOT': self.temp.name, 'AUTH_ENABLED': False})
        self.client = self.app.test_client()
        self.client.get('/projects')
        with self.client.session_transaction() as session:
            self.csrf = session['csrf']
        self.headers = {'X-CSRF-Token': self.csrf}

    def tearDown(self):
        self.temp.cleanup()

    def test_database_style_is_not_an_extra_prompt_editor(self):
        database = MagicMock()
        with patch('translation_app.web.api.engine', return_value=database):
            self.assertEqual(self.client.get('/api/projects/tarbi3a_baloot/profile').status_code, 404)
            page = self.client.get('/projects/tarbi3a_baloot').text
            self.assertNotIn('id="profile"', page)
            self.assertNotIn('共享基础规则', page)
        database.get_game_profile.assert_not_called()

    def test_glossary_check_conflicts_then_save_chosen_values(self):
        database, query = MagicMock(), MagicMock()
        database.client.table.return_value.select.return_value = query
        query.eq.return_value = query
        query.in_.return_value = query
        query.execute.return_value.data = [{'source_term': '已有', 'target_term': 'قديم', 'note': '备注'}]
        database.upsert_terms.return_value = 2
        pairs = [{'source': '已有', 'target': 'جديد', 'note': '新'},
                 {'source': '新词', 'target': 'مصطلح', 'note': ''}]
        with patch('translation_app.web.api.engine', return_value=database):
            checked = self.client.post('/api/projects/tarbi3a_baloot/glossary/check',
                                       json={'language': 'arabic', 'pairs': pairs}, headers=self.headers)
            self.assertEqual(checked.status_code, 200)
            self.assertEqual(checked.get_json()['clean_pairs'], [pairs[1]])
            self.assertEqual(checked.get_json()['conflicts'], [{'source': '已有', 'existing_target': 'قديم',
                'existing_note': '备注', 'new_target': 'جديد', 'new_note': '新'}])
            self.assertEqual(query.eq.call_args_list, [call('game_id', 'tarbi3a_baloot'), call('target_language', 'arabic')])
            query.in_.assert_called_once_with('source_term', ['已有', '新词'])
            database.upsert_terms.assert_not_called()

            saved = self.client.post('/api/projects/tarbi3a_baloot/glossary/save',
                                     json={'language': 'arabic', 'pairs': pairs}, headers=self.headers)
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.get_json()['count'], 2)
            database.upsert_terms.assert_called_once_with('Tarbi3a_Baloot', 'Arabic', pairs)
            unsupported = self.client.post('/api/projects/tarbi3a_baloot/glossary/check',
                json={'language': 'english', 'pairs': pairs}, headers=self.headers)
            self.assertEqual(unsupported.status_code, 400)
            self.assertEqual(database.upsert_terms.call_count, 1)

    def test_multi_sheet_preview_submit_and_result_download(self):
        workbook = Workbook()
        first = workbook.active
        first.title = 'UI'
        first.append(['原文', '译文'])
        first.append(['开始', '旧值'])
        second = workbook.create_sheet('Dialogue')
        second.append(['Text', 'Context', 'Target'])
        second.append(['你好', '问候', 'old'])
        other = workbook.create_sheet('Unchanged')
        other.append(['value'])
        other.append(['preserve'])
        file_bytes = io.BytesIO()
        workbook.save(file_bytes)
        workbook.close()
        file_bytes.seek(0)
        preview = self.client.post('/api/uploads/preview', data={'file': (file_bytes, 'test.xlsx')}, headers=self.headers)
        self.assertEqual(preview.status_code, 200)
        data = preview.get_json()
        self.assertEqual([item['name'] for item in data['sheets']], ['UI', 'Dialogue', 'Unchanged'])
        self.assertEqual(data['sheets'][1]['rows'][0], ['你好', '问候', 'old'])
        body = {'project_id': 'tarbi3a_baloot', 'language': 'arabic', 'upload_id': data['upload_id'],
                'sheets': [{'name': 'UI', 'source': '原文', 'context': None, 'target': '译文'},
                           {'name': 'Dialogue', 'source': 'Text', 'context': 'Context', 'target': 'Target'}]}
        created = self.client.post('/api/jobs', json=body, headers=self.headers)
        self.assertEqual(created.status_code, 202, created.get_json())
        job_id = created.get_json()['id']
        def translator(*args):
            return translate_file(*args, resource_loader=lambda *_: ({}, {}, ''),
                                  provider=lambda rows, *_: [{'row_id': row[0], 'translated_text': 'ترجمة'} for row in rows])
        self.assertTrue(process_one(self.app.extensions['jobs'], translator))
        status = self.client.get('/api/jobs/' + job_id).get_json()
        self.assertEqual(status['status'], 'completed')
        rendered = self.client.get('/api/jobs/' + job_id + '/preview').get_json()
        self.assertEqual(rendered['sheets'][0]['rows'][0][-1], 'ترجمة')
        response = self.client.get(status['download_url'])
        self.assertEqual(response.status_code, 200)
        result = load_workbook(io.BytesIO(response.data))
        try:
            self.assertEqual(result['UI']['B2'].value, 'ترجمة')
            self.assertEqual(result['Dialogue']['C2'].value, 'ترجمة')
            self.assertEqual(result['Unchanged']['A2'].value, 'preserve')
        finally:
            result.close()
            response.close()

    def test_invalid_uploads_and_malformed_job_requests(self):
        for name, content in [('sample.txt', b'hello'), ('broken.xlsx', b'not an xlsx'), ('empty.csv', b'')]:
            with self.subTest(name=name):
                result = self.client.post('/api/uploads/preview', data={'file': (io.BytesIO(content), name)}, headers=self.headers)
                self.assertEqual(result.status_code, 400)
        cases = [{'project_id': 'tarbi3a_baloot', 'language': 'english', 'manual_rows': [{'source_text': 'a'}]},
                 {'project_id': 'tarbi3a_baloot', 'language': 'arabic', 'manual_rows': []},
                 {'project_id': 'tarbi3a_baloot', 'language': 'arabic', 'manual_rows': [{'source_text': ' '}]}]
        for body in cases:
            with self.subTest(body=body):
                self.assertEqual(self.client.post('/api/jobs', json=body, headers=self.headers).status_code, 400)
        malformed = self.client.post('/api/jobs', data='not-json', content_type='application/json', headers=self.headers)
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(self.client.post('/api/uploads/preview', data={}).status_code, 403)

    def test_reference_file_import_for_glossary_and_translation_memory(self):
        database = MagicMock()
        database.upsert_terms.return_value = 1
        database.upsert_tm.return_value = 1
        content = '原文,译文,备注\n你好,مرحبا,问候\n'.encode('utf-8')
        pair = {'source': '你好', 'target': 'مرحبا', 'note': '问候'}
        with patch('translation_app.web.api.engine', return_value=database):
            for kind in ('terms', 'translation_memory'):
                with self.subTest(kind=kind):
                    form = {'file': (io.BytesIO(content), 'reference.csv'), 'kind': kind, 'language': 'arabic',
                            'sheet': 'CSV_Sheet', 'source': '原文', 'target': '译文', 'note': '备注'}
                    result = self.client.post('/api/projects/tarbi3a_baloot/knowledge/upload', data=form, headers=self.headers)
                    self.assertEqual(result.status_code, 200, result.get_json())
                    self.assertEqual(result.get_json()['count'], 1)
        database.upsert_terms.assert_called_once_with('Tarbi3a_Baloot', 'Arabic', [pair])
        database.upsert_tm.assert_called_once_with('Tarbi3a_Baloot', 'Arabic', [pair])


if __name__ == '__main__':
    unittest.main()
