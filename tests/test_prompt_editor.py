import json
from pathlib import Path
import shutil
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from postgrest import SyncPostgrestClient

from translation_app import create_app
from translation_app.jobs import process_one
from translation_app.services import projects
from translation_app.services.translation import translate_file, _request_data


class PromptEditorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name) / 'config'
        shutil.copytree(projects.CONFIG_ROOT, self.config)
        self.config_patch = patch.object(projects, 'CONFIG_ROOT', self.config)
        self.config_patch.start()
        self.app = create_app({'TESTING': True, 'AUTH_ENABLED': True, 'SECRET_KEY': 'test',
                               'STORAGE_ROOT': Path(self.tmp.name) / 'storage'})
        self.rows = {(project['id'], language, kind): {'content': content, 'version': 1}
                     for project in projects.list_projects() for language in project['languages']
                     for kind, content in projects.load_prompt_pair(project['id'], language).items()}
        self.db_error = False
        http = httpx.Client(transport=httpx.MockTransport(self.postgrest_request))
        self.addCleanup(http.close)
        db = SimpleNamespace(client=SyncPostgrestClient('https://example.invalid', http_client=http,
                                                        retry_enabled=False))
        engine_patch = patch('translation_app.web.api.engine', return_value=db)
        engine_patch.start()
        self.addCleanup(engine_patch.stop)
        self.client = self.app.test_client()
        self.client.get('/login')
        with self.client.session_transaction() as session:
            session['sid'] = self.app.extensions['sessions'].create(
                'team-user', 'team@example.invalid', 'test-token', 'refresh-token', time.time() + 3600)
            self.headers = {'X-CSRF-Token': session['csrf']}

    def tearDown(self):
        self.config_patch.stop()
        self.tmp.cleanup()

    def read(self, project='sawa', language='arabic'):
        response = self.client.get(f'/api/projects/{project}/prompts?language={language}')
        self.assertEqual(response.status_code, 200)
        return response.json['prompts']

    def save(self, project, language, kind, content, version):
        return self.client.post(f'/api/projects/{project}/prompts', headers=self.headers,
                                json={'language': language, 'kind': kind, 'content': content, 'version': version})

    def postgrest_request(self, request):
        self.assertEqual(request.url.path, '/prompt_configs')
        if self.db_error:
            return httpx.Response(503, json={'code': 'PGRST000', 'message': 'Database unavailable'})
        params = request.url.params
        project, language = (params[name].removeprefix('eq.') for name in ('project_id', 'target_language'))
        if request.method == 'PATCH':
            key = (project, language, params['kind'].removeprefix('eq.'))
            row = self.rows.get(key)
            if not row or params['version'] != f"eq.{row['version']}":
                return httpx.Response(200, json=[])
            body = json.loads(request.content)
            self.assertEqual(set(body), {'content'})
            row.update(content=body['content'], version=row['version'] + 1)
            return httpx.Response(200, json=[row])
        self.assertEqual(request.method, 'GET')
        return httpx.Response(200, json=[{'kind': kind, **row}
                              for (p, lang, kind), row in self.rows.items() if (p, lang) == (project, language)])

    def test_every_game_language_reads_two_supabase_rows(self):
        for project in projects.list_projects():
            for language in project['languages']:
                with self.subTest(project=project['id'], language=language):
                    prompts = self.read(project['id'], language)
                    self.assertEqual(set(prompts), {'system', 'instructions'})
                    folder = self.config / 'projects' / project['id'] / 'languages' / language
                    for kind, filename in (('system', 'system.md'), ('instructions', 'instructions.md')):
                        self.assertEqual(prompts[kind]['content'], (folder / filename).read_text())
                        self.assertEqual(prompts[kind]['version'], 1)
        self.assertFalse((self.config / 'base_prompt.md').exists())

    def test_both_prompts_are_saved_permanently_and_isolated(self):
        originals = {(game, language): projects.load_prompt_pair(game, language)
                     for game, language in (('sawa', 'arabic'), ('sawa', 'turkish'), ('tarbi3a_baloot', 'arabic'))}
        entries = self.read()
        for kind, filename in (('system', 'system.md'), ('instructions', 'instructions.md')):
            response = self.save('sawa', 'arabic', kind, 'New ' + kind, entries[kind]['version'])
            self.assertEqual(response.status_code, 200)
            path = self.config / 'projects/sawa/languages/arabic' / filename
            self.assertEqual(path.read_text(), originals['sawa', 'arabic'][kind])
            self.assertEqual(response.json['prompt']['version'], entries[kind]['version'] + 1)
            self.assertEqual(self.read()[kind]['content'], 'New ' + kind)
        for key in (('sawa', 'turkish'), ('tarbi3a_baloot', 'arabic')):
            self.assertEqual({kind: row['content'] for kind, row in self.read(*key).items()}, originals[key])

    def test_conflicting_team_edits_are_detected_and_repo_changes_are_ignored(self):
        entries = self.read()
        for kind in ('system', 'instructions'):
            self.assertEqual(self.save('sawa', 'arabic', kind, 'First edit', entries[kind]['version']).status_code, 200)
            self.assertEqual(self.save('sawa', 'arabic', kind, 'Stale edit', entries[kind]['version']).status_code, 409)
        latest = self.read()['system']
        path = self.config / 'projects/sawa/languages/arabic/system.md'
        path.write_text('Edited in IDE')
        self.assertEqual(self.read()['system']['content'], 'First edit')
        self.assertEqual(self.save('sawa', 'arabic', 'system', 'Browser edit', latest['version']).status_code, 200)
        self.assertEqual(path.read_text(), 'Edited in IDE')

    def test_dev_defaults_are_read_only(self):
        self.app.config['AUTH_ENABLED'] = False
        response = self.client.get('/api/projects/sawa/prompts?language=arabic')
        self.assertTrue(response.json['read_only'])
        self.assertEqual(self.save('sawa', 'arabic', 'system', 'Blocked', 1).status_code, 403)

    def test_missing_prompt_or_database_error_never_falls_back_in_production(self):
        self.rows.pop(('direct', 'english', 'instructions'))
        for self.db_error in (False, True):
            with self.subTest(db_error=self.db_error):
                response = self.client.get('/api/projects/direct/prompts?language=english')
                self.assertEqual(response.status_code, 503)
                response = self.client.post('/api/jobs', headers=self.headers, json={'project_id': 'direct',
                    'language': 'english', 'manual_rows': [{'source_text': 'hello'}]})
                self.assertEqual(response.status_code, 503)
        self.assertEqual(self.client.get('/api/jobs').json['jobs'], [])

    def test_only_two_kinds_are_writable_and_csrf_language_are_validated(self):
        entries = self.read()
        for kind in ('base', 'project', 'language', 'additional', '../system'):
            self.assertEqual(self.save('sawa', 'arabic', kind, 'bad', entries['system']['version']).status_code, 400)
        self.assertEqual(self.save('sawa', 'english', 'system', 'bad', entries['system']['version']).status_code, 400)
        self.assertEqual(self.client.post('/api/projects/s06/prompts', json={}).status_code, 403)

    def test_each_prompt_can_be_cleared_and_system_is_sent_verbatim(self):
        entries = self.read()
        for kind in ('system', 'instructions'):
            self.assertEqual(self.save('sawa', 'arabic', kind, '', entries[kind]['version']).status_code, 200)
        self.assertEqual({kind: row['content'] for kind, row in self.read().items()},
                         {'system': '', 'instructions': ''})
        data = _request_data([(7, 'Text {0}', 'button')], 'Custom system only', 'Arabic', 'term : مصطلح',
                             {'instructions': 'Custom instructions only'})
        self.assertEqual(data['messages'][0], {'role': 'system', 'content': 'Custom system only'})
        self.assertIn('Custom instructions only', data['messages'][1]['content'])
        self.assertIn('term : مصطلح', data['messages'][1]['content'])
        self.assertNotIn('Respond only', data['messages'][0]['content'])

    def test_queued_job_snapshots_both_supabase_prompts_and_retry_keeps_them(self):
        before = {'system': 'Saved in Supabase', 'instructions': ''}
        for kind, content in before.items():
            self.rows['s06', 'english', kind]['content'] = content
        response = self.client.post('/api/jobs', headers=self.headers, json={'project_id': 's06',
            'language': 'english', 'manual_rows': [{'source_text': '你好'}]})
        self.assertEqual(response.status_code, 202)
        store, job_id = self.app.extensions['jobs'], response.json['id']
        with store._connect() as db:
            settings = json.loads(db.execute('SELECT settings FROM jobs WHERE id=?', (job_id,)).fetchone()[0])
        self.assertEqual(settings['system_prompt'], before['system'])
        self.assertEqual(settings['instructions'], before['instructions'])
        self.assertEqual(settings['custom_instructions'], '')
        entries = self.read('s06', 'english')
        for kind in ('system', 'instructions'):
            self.save('s06', 'english', kind, 'Changed after submission', entries[kind]['version'])
        claimed = store.claim('first')
        store.fail(job_id, 'first')
        self.assertEqual(self.client.post(f'/api/jobs/{job_id}/retry', headers=self.headers).status_code, 202)
        received = []

        def provider(rows, system, language, context, config):
            received.append((system, config['instructions'], context))
            return [{'row_id': row[0], 'translated_text': 'Hello'} for row in rows]

        def translator(*args):
            return translate_file(*args, provider=provider, resource_loader=lambda *_: ({}, {}, 'IGNORED DATABASE STYLE'))

        with patch('translation_app.services.translation.load_prompt_pair', side_effect=AssertionError):
            process_one(store, translator)
        self.assertEqual(store.get(claimed['owner_id'], job_id)['status'], 'completed')
        self.assertEqual(received, [(before['system'], before['instructions'], '')])

    def test_temporary_instructions_apply_only_to_task_and_do_not_change_repo_files(self):
        project, language = 'direct', 'english'
        before = projects.load_prompt_pair(project, language)
        created = self.client.post('/api/jobs', headers=self.headers, json={
            'project_id': project, 'language': language, 'custom_instructions': 'Make this very short',
            'manual_rows': [{'source_text': 'hello'}]})
        self.assertEqual(created.status_code, 202)
        job_id, store = created.json['id'], self.app.extensions['jobs']
        store.claim('failed-worker')
        store.fail(job_id, 'failed-worker')
        self.assertEqual(self.client.post(f'/api/jobs/{job_id}/retry', headers=self.headers).status_code, 202)
        sent = []

        def provider(rows, system, target_language, context, settings):
            sent.append(_request_data(rows, system, target_language, context, settings))
            return [{'row_id': row[0], 'translated_text': 'Hello'} for row in rows]

        process_one(store, lambda *args: translate_file(*args, provider=provider))
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(sent[0]['messages']), 2)
        self.assertEqual(sent[0]['messages'][0]['content'], before['system'])
        self.assertIn(before['instructions'], sent[0]['messages'][1]['content'])
        self.assertIn('### TEMPORARY INSTRUCTIONS:\nMake this very short', sent[0]['messages'][1]['content'])
        self.assertEqual(projects.load_prompt_pair(project, language), before)
        next_job = self.client.post('/api/jobs', headers=self.headers, json={
            'project_id': project, 'language': language, 'manual_rows': [{'source_text': 'hello'}]})
        self.assertEqual(next_job.status_code, 202)
        claimed = store.claim('next-worker')
        self.assertEqual(claimed['settings']['custom_instructions'], '')
        self.assertEqual(claimed['settings']['instructions'], before['instructions'])

    def test_temporary_instruction_validation(self):
        for value in (None, [], 123, 'x' * 20001):
            with self.subTest(value_type=type(value).__name__):
                result = self.client.post('/api/jobs', headers=self.headers, json={
                    'project_id': 'direct', 'language': 'english', 'custom_instructions': value,
                    'manual_rows': [{'source_text': 'hello'}]})
                self.assertEqual(result.status_code, 400)

    def test_blank_temporary_instructions_add_no_prompt_section(self):
        data = _request_data([(0, 'hello', '')], 'Saved system', 'English', '',
                             {'instructions': 'Saved instructions', 'custom_instructions': '   '})
        self.assertNotIn('TEMPORARY INSTRUCTIONS', data['messages'][1]['content'])
        self.assertIn('Saved instructions', data['messages'][1]['content'])

    def test_prompt_paths_cannot_escape_or_alias_each_other(self):
        path = self.config / 'projects/sawa/languages/arabic/language.json'
        original = json.loads(path.read_text())
        for key in ('system_prompt', 'instructions'):
            path.write_text(json.dumps({**original, key: '../turkish/system.md'}))
            with self.assertRaises(ValueError):
                projects.load_prompt_pair('sawa', 'arabic')
        path.write_text(json.dumps({**original, 'system_prompt': 'instructions.md'}))
        with self.assertRaises(ValueError):
            projects.load_prompt_pair('sawa', 'arabic')

    def test_legacy_shared_files_cannot_change_the_effective_prompt_pair(self):
        before = projects.load_prompt_pair('sawa', 'arabic')
        (self.config / 'base_prompt.md').write_text('SHARED_RULE_SHOULD_NOT_APPEAR')
        legacy = self.config / 'projects/sawa/prompts'
        legacy.mkdir(exist_ok=True)
        (legacy / 'project.md').write_text('PROJECT_RULE_SHOULD_NOT_APPEAR')
        self.assertEqual(projects.load_prompt_pair('sawa', 'arabic'), before)
        page = self.client.get('/projects/sawa').text
        self.assertNotIn('共享基础规则', page)
        self.assertIn('id="instructions"', page)
        self.assertIn('仅本次，不保存', page)
        self.assertIn('全团队共用', page)


if __name__ == '__main__':
    unittest.main()
