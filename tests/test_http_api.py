import base64
import json
import os
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from postgrest import SyncPostgrestClient

from translation_app import create_app, http_api
from translation_app.jobs import process_one
from translation_app.services import translation


class TranslationApiTests(unittest.TestCase):
    def setUp(self):
        storage = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            'LOCATOOL_STORAGE': storage, 'JOB_STORAGE_ROOT': os.path.join(storage, 'jobs'),
            'LOCATOOL_AUTH_ENABLED': 'true', 'LOCATOOL_API_KEY': 'test-key',
            'LOCATOOL_API_SUPABASE_EMAIL': 'api@example.invalid', 'LOCATOOL_API_SUPABASE_PASSWORD': 'test-password',
            'SUPABASE_URL': 'https://example.invalid', 'SUPABASE_KEY': 'test-public-key',
            'TRANSLATION_RESOURCE_CACHE_SECONDS': '300',
        }))
        self.enterContext(patch.object(http_api, '_service_session', None))
        expiry = time.time() + 3600
        claims = base64.urlsafe_b64encode(json.dumps({'exp': expiry}).encode()).decode().rstrip('=')
        api_token = f'api.{claims}.signature'
        self.enterContext(patch.object(http_api.SupabaseAuth, 'login', return_value={
            'access_token': api_token, 'expires_at': expiry,
        }))
        self.backend = self.enterContext(httpx.Client(transport=httpx.MockTransport(self.database_request)))

        def database_client(*args):
            postgrest = SyncPostgrestClient('https://example.invalid', http_client=self.backend,
                                           retry_enabled=False)
            return SimpleNamespace(postgrest=postgrest, table=postgrest.from_, rpc=postgrest.rpc)

        self.enterContext(patch('supabase.create_client', side_effect=database_client))
        self.prompts = {'system': {'content': 'Old prompt', 'version': 1},
                        'instructions': {'content': 'Project instructions', 'version': 1}}
        self.term = {'id': '20000000-0000-0000-0000-000000000001', 'kind': 'terms',
                     'source': '领取', 'target': 'Claim', 'note': '', 'scene': '', 'string_key': '', 'verified': False}
        self.reference_reads = 0
        self.api = self.enterContext(TestClient(http_api.app))
        self.web = create_app({'TESTING': True, 'SECRET_KEY': 'test', 'STORAGE_ROOT': storage})
        self.browser = self.web.test_client()
        self.browser.get('/login')
        with self.browser.session_transaction() as session:
            session['sid'] = self.web.extensions['sessions'].create(
                'team-member', 'team@example.invalid', f'web.{claims}.signature', 'refresh', expiry)
            self.csrf = {'X-CSRF-Token': session['csrf']}
        self.calls = []

        def model(rows, prompt, language, references, settings):
            self.calls.append((prompt, language, references))
            return [{'row_id': row_id, 'translated_text': 'Claim {1}'}
                    for row_id, _, _ in rows]

        self.enterContext(patch.object(translation, '_call_model', side_effect=model))

    def database_request(self, request):
        method, path = request.method, request.url.path
        if path == '/prompt_configs':
            self.assertEqual(request.url.params['project_id'], 'eq.s06')
            self.assertEqual(request.url.params['target_language'], 'eq.english')
            if method == 'PATCH':
                self.prompts['system'] = {'content': json.loads(request.content)['content'], 'version': 2}
                return httpx.Response(200, json=[self.prompts['system']])
            return httpx.Response(200, json=[{'kind': kind, **row} for kind, row in self.prompts.items()])
        if path == '/knowledge_project_members':
            return httpx.Response(200, json=[{'can_edit': True}])
        if path == '/rpc/knowledge_mutate':
            data = json.loads(request.content)
            self.assertEqual((data['p_game'], data['p_language'], data['p_kind'], data['p_action']),
                             ('s06', 'english', 'terms', 'edit'))
            self.term.update(data['p_rows'][0])
            return httpx.Response(200, json=1)
        self.assertEqual(path, '/knowledge_entries')
        self.assertEqual(request.url.params['status'], 'eq.active')
        if request.url.params.get('offset') in (None, '0'):
            self.reference_reads += 1
            return httpx.Response(200, json=[self.term])
        return httpx.Response(200, json=[])

    def translate(self, *rows, key='test-key'):
        return self.api.post('/v1/translate', headers={'X-API-Key': key}, json={
            'game_id': 'S06', 'target_language': 'English', 'texts': list(rows),
            'batch_size': 10, 'use_smart_budget': True, 'validate': True,
        })

    def test_api_preserves_old_contract_and_uses_latest_web_configuration(self):
        row = {'id': 'svn_12', 'source_text': '领取{1}', 'context': 'Button'}
        response = self.translate(row)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['translations'][0], {
            **row, 'translated_text': 'Claim {1}', 'issues': [],
        })
        self.assertEqual(self.calls[-1][:2], ('Old prompt', 'English'))
        self.assertIn('领取 : Claim', self.calls[-1][2])
        self.assertEqual(self.translate(row).status_code, 200)
        self.assertEqual(self.reference_reads, 1)

        saved = self.browser.post('/api/projects/s06/prompts', headers=self.csrf, json={
            'language': 'english', 'kind': 'system', 'content': 'Updated prompt', 'version': 1})
        self.assertEqual(saved.status_code, 200, saved.text)
        saved = self.browser.patch('/api/projects/s06/knowledge/terms/entries/' + self.term['id'],
                                   headers=self.csrf, json={'language': 'english', 'version': 1, 'status': 'active',
                                                           'entry': {'source': '领取', 'target': 'Collect'}})
        self.assertEqual(saved.status_code, 200, saved.text)

        batch = self.translate(*[{**row, 'id': f'svn_{i}', 'context': f'Button {i}'} for i in range(10)])
        self.assertEqual(batch.status_code, 200, batch.text)
        self.assertEqual([item['id'] for item in batch.json()['translations']], [f'svn_{i}' for i in range(10)])
        self.assertEqual(self.reference_reads, 2)
        api_configuration = self.calls[-1]
        self.assertEqual(api_configuration[:2], ('Updated prompt', 'English'))
        self.assertIn('领取 : Collect', api_configuration[2])

        job = self.browser.post('/api/jobs', headers=self.csrf, json={
            'project_id': 's06', 'language': 'english', 'manual_rows': [row],
        })
        self.assertEqual(job.status_code, 202, job.text)
        process_one(self.web.extensions['jobs'])
        self.assertEqual(self.browser.get('/api/jobs/' + job.json['id']).json['status'], 'completed')
        self.assertEqual(self.calls[-1][:2], api_configuration[:2])
        self.assertIn('[rows 0] 领取 : Collect', self.calls[-1][2])

    def test_deadline_signals_cancellation_and_returns_504(self):
        observed = threading.Event()

        def wait_for_cancellation(request, key, auth, cancelled):
            if cancelled.wait(1):
                observed.set()

        with patch.object(http_api, '_translate', side_effect=wait_for_cancellation) as work:
            with patch.object(http_api, 'TIMEOUT_SECONDS', .05):
                response = self.translate({'source_text': '领取{1}'})
            work.assert_called_once()
            self.assertEqual(response.status_code, 504)
        self.assertTrue(observed.wait(1))

    def test_missing_auth_and_invalid_model_output_are_errors(self):
        self.assertEqual(self.translate({'source_text': '领取{1}'}, key='wrong').status_code, 401)
        with patch.object(translation, '_call_model', return_value=[{'row_id': 0, 'translated_text': 'Claim'}]):
            self.assertEqual(self.translate({'source_text': '领取{1}'}).status_code, 502)
