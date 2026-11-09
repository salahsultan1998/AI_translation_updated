import io
import tempfile
import unittest

from translation_app import create_app
from translation_app.jobs import process_one
from translation_app.services.translation import translate_file


class ManualResultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'test-key',
                               'STORAGE_ROOT': self.temp.name, 'AUTH_ENABLED': False})
        self.client = self.app.test_client()
        self.client.get('/projects')
        with self.client.session_transaction() as session:
            self.headers = {'X-CSRF-Token': session['csrf']}

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def translator(*args):
        return translate_file(*args, provider=lambda batch, *_: [
            {'row_id': row[0], 'translated_text': f'翻译 {row[0]}\t第二列\n下一行'} for row in batch
        ])

    def test_all_rows_multiline_and_owner_scope(self):
        rows = [{'source_text': f'原文 {i}', 'context': ''} for i in range(16)]
        rows[0] = {'source_text': '第一行\n第二行\t带制表符', 'context': '备注\n上下文'}
        response = self.client.post('/api/jobs', headers=self.headers, json={
            'project_id': 'direct', 'language': 'english', 'manual_rows': rows, 'manual_target': '英语'
        })
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json['input_mode'], 'manual')
        job_id = response.json['id']
        self.assertEqual(self.client.get(f'/api/jobs/{job_id}/manual-result').status_code, 404)
        self.assertTrue(process_one(self.app.extensions['jobs'], self.translator))
        result = self.client.get(f'/api/jobs/{job_id}/manual-result')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.json['rows']), 16)
        for i, row in enumerate(result.json['rows']):
            self.assertEqual(row['source_text'], rows[i]['source_text'])
            self.assertEqual(row['context'], rows[i]['context'])
            self.assertEqual(row['translated_text'], f'翻译 {i}\t第二列\n下一行')
        stranger = self.app.test_client()
        stranger.get('/projects')
        self.assertEqual(stranger.get(f'/api/jobs/{job_id}/manual-result').status_code, 404)
        self.assertEqual(stranger.get(f'/api/jobs/{job_id}/download').status_code, 404)

    def test_uploaded_file_named_manual_cannot_spoof_manual_mode(self):
        upload = self.client.post('/api/uploads/preview', headers=self.headers, data={
            'file': (io.BytesIO('源文本,上下文\n你好,问候\n'.encode()), 'manual_input.csv')
        })
        self.assertEqual(upload.status_code, 200)
        created = self.client.post('/api/jobs', headers=self.headers, json={
            'project_id': 'direct', 'language': 'english', 'upload_id': upload.json['upload_id'],
            'sheets': [{'name': 'CSV_Sheet', 'source': '源文本', 'context': '上下文', 'target': '英语',
                        '_manual_entry': True}]
        })
        self.assertEqual(created.status_code, 202)
        self.assertEqual(created.json['input_mode'], 'file')
        self.assertTrue(process_one(self.app.extensions['jobs'], self.translator))
        self.assertEqual(self.client.get(f"/api/jobs/{created.json['id']}/manual-result").status_code, 404)

    def test_manual_target_cannot_replace_input_columns(self):
        for target in ('源文本', '上下文'):
            response = self.client.post('/api/jobs', headers=self.headers, json={
                'project_id': 'direct', 'language': 'english',
                'manual_rows': [{'source_text': '你好', 'context': ''}], 'manual_target': target
            })
            self.assertEqual(response.status_code, 400)

    def test_blank_rows_keep_edited_translation_and_row_alignment(self):
        rows = [
            {'source_text': '第一行', 'context': '', 'translated_text': 'old A'},
            {'source_text': '', 'context': '备注', 'translated_text': '保留此内容\n下一行'},
            {'source_text': '第三行', 'context': '测试', 'translated_text': 'old B'},
        ]
        created = self.client.post('/api/jobs', headers=self.headers, json={
            'project_id': 'direct', 'language': 'english', 'manual_rows': rows, 'manual_target': '英语'
        })
        self.assertEqual(created.status_code, 202)
        self.assertTrue(process_one(self.app.extensions['jobs'], self.translator))
        result = self.client.get(f"/api/jobs/{created.json['id']}/manual-result")
        self.assertEqual(result.status_code, 200)
        translated = result.json['rows']
        self.assertEqual(len(translated), 3)
        self.assertEqual(translated[0]['translated_text'], '翻译 0\t第二列\n下一行')
        self.assertEqual(translated[1], rows[1])
        self.assertEqual(translated[2]['translated_text'], '翻译 2\t第二列\n下一行')

    def test_manual_existing_translation_must_be_text(self):
        created = self.client.post('/api/jobs', headers=self.headers, json={
            'project_id': 'direct', 'language': 'english',
            'manual_rows': [{'source_text': '你好', 'context': '', 'translated_text': None}]
        })
        self.assertEqual(created.status_code, 400)


if __name__ == '__main__':
    unittest.main()
