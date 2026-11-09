import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from translation_app import create_app
from translation_app.services import projects


class PromptEditorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config = Path(self.tmp.name) / 'config'
        shutil.copytree(projects.CONFIG_ROOT, config)
        self.config_patch = patch.object(projects, 'CONFIG_ROOT', config)
        self.config_patch.start()
        self.app = create_app({'TESTING': True, 'AUTH_ENABLED': False, 'SECRET_KEY': 'test',
                               'STORAGE_ROOT': Path(self.tmp.name) / 'storage'})
        self.client = self.app.test_client()
        self.client.get('/projects')
        with self.client.session_transaction() as session:
            self.headers = {'X-CSRF-Token': session['csrf']}

    def tearDown(self):
        self.config_patch.stop()
        self.tmp.cleanup()

    def test_project_language_isolation_and_conflict(self):
        url = '/api/projects/sawa/prompts'
        data = self.client.get(url + '?language=arabic').json['prompts']
        other = self.client.get(url + '?language=turkish').json['prompts']['language']['content']
        body = {'language': 'arabic', 'kind': 'language', 'content': '新的阿拉伯语规则',
                'version': data['language']['version']}
        saved = self.client.post(url, json=body, headers=self.headers)
        self.assertEqual(saved.status_code, 200)
        self.assertIn('新的阿拉伯语规则', projects.compose_prompt('sawa', 'arabic'))
        self.assertEqual(other, self.client.get(url + '?language=turkish').json['prompts']['language']['content'])
        self.assertEqual(self.client.post(url, json=body, headers=self.headers).status_code, 409)
        self.assertEqual(self.client.post(url, json={**body, 'kind': 'base'}, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post(url, json={**body, 'language': 'english'}, headers=self.headers).status_code, 400)

    def test_direct_and_additional_rules(self):
        direct = self.client.get('/api/projects/direct/prompts?language=arabic').json['prompts']
        self.assertEqual(set(direct), {'base', 'language'})
        english = self.client.get('/api/projects/s06/prompts?language=english').json['prompts']
        self.assertIn('additional', english)
        self.assertEqual(self.client.post('/api/projects/s06/prompts', json={}, headers={}).status_code, 403)

    def test_queued_job_snapshots_prompt(self):
        before = projects.compose_prompt('s06', 'english')
        response = self.client.post('/api/jobs', headers=self.headers, json={'project_id':'s06',
            'language':'english', 'manual_rows':[{'source_text':'你好'}]})
        self.assertEqual(response.status_code, 202)
        with self.app.extensions['jobs']._connect() as db:
            settings = json.loads(db.execute('SELECT settings FROM jobs WHERE id=?', (response.json['id'],)).fetchone()[0])
        self.assertEqual(settings['system_prompt'], before)
        config = self.client.get('/api/projects/s06/prompts?language=english').json['prompts']
        self.client.post('/api/projects/s06/prompts', headers=self.headers, json={'language':'english', 'kind':'project',
            'content':'Changed after submission', 'version':config['project']['version']})
        self.assertNotEqual(settings['system_prompt'], projects.compose_prompt('s06', 'english'))
