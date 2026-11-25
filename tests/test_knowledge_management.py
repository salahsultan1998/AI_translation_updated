import io
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from openpyxl import load_workbook
from postgrest.exceptions import APIError

from translation_app import create_app
from translation_app.services.translation import _ContextIndex


ENTRY_ID = '20000000-0000-0000-0000-000000000001'
PREFIX = '/api/projects/tarbi3a_baloot/knowledge/'


class KnowledgeManagementTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'test', 'STORAGE_ROOT': root,
                               'AUTH_ENABLED': True})
        self.client, self.csrf = self.member('editor')
        self.db = MagicMock()
        self.db.storage_root = root
        self.members = self.db.client.table.return_value.select.return_value.eq.return_value.limit.return_value
        self.members.execute.return_value.data = [{'can_edit': True}]
        self.calls = []
        self.responses = {'knowledge_list': {'rows': [], 'total': 0, 'scenes': []},
                          'knowledge_import_matches': [], 'knowledge_mutate': 1}

        def rpc(name, arguments):
            self.calls.append((name, arguments))
            result = self.responses[name]
            if isinstance(result, Exception):
                raise result
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=result))

        self.db.client.rpc.side_effect = rpc
        self.engine = self.enterContext(patch('translation_app.web.api.engine', return_value=self.db))

    def member(self, name):
        client = self.app.test_client()
        client.get('/login')
        with client.session_transaction() as session:
            session['sid'] = self.app.extensions['sessions'].create(
                name, name + '@example.invalid', 'test-token', 'refresh', time.time() + 3600)
            return client, {'X-CSRF-Token': session['csrf']}

    def api(self, path, body=None, method='POST', client=None, headers=None):
        return (client or self.client).open(PREFIX + path, method=method,
            json={'language': 'arabic', **(body or {})}, headers=self.csrf if headers is None else headers)

    def test_project_permission_and_csrf(self):
        url = PREFIX + 'terms/entries?language=arabic'
        self.assertEqual(self.app.test_client().get(url).status_code, 401)
        entry = {'entry': {'source': '金币', 'target': 'الذهب'}}
        self.assertEqual(self.api('terms/entries', entry, headers={}).status_code, 403)
        self.members.execute.return_value.data = []
        self.assertEqual(self.client.get(url).status_code, 403)
        self.members.execute.return_value.data = [{'can_edit': False}]
        self.assertEqual(self.api('terms/entries', entry).status_code, 403)
        self.assertFalse(self.client.get(url).json['editable'])
        self.assertEqual([name for name, _ in self.calls], ['knowledge_list'])
        self.assertEqual(self.client.get(PREFIX + 'terms/entries?language=english').status_code, 400)

    def test_glossary_snapshot_checks_access_and_reads_past_short_pages(self):
        url = PREFIX + 'terms/snapshot?language=arabic'
        self.members.execute.return_value.data = []
        self.assertEqual(self.client.get(url).status_code, 403)
        self.members.execute.return_value.data = [{'can_edit': False}]
        table = self.db.client.table.return_value
        page = table.select.return_value.eq.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value
        page.execute.return_value.data = [{'id': 'a', 'status': 'active'}]
        following = page.gt.return_value
        following.execute.side_effect = [SimpleNamespace(data=[{'id': 'b', 'status': 'archived'}]), SimpleNamespace(data=[])]
        result = self.client.get(url)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(result.json['editable'])
        self.assertEqual([row['id'] for row in result.json['rows']], ['a', 'b'])
        self.assertEqual([call.args for call in page.gt.call_args_list], [('id', 'a'), ('id', 'b')])

    def test_history_snapshot_is_project_scoped_and_includes_inactive_rows_and_keys(self):
        url = PREFIX + 'memory/snapshot?language=arabic'
        self.members.execute.return_value.data = []
        self.assertEqual(self.client.get(url).status_code, 403)
        self.members.execute.return_value.data = [{'can_edit': False}]
        table = self.db.client.table.return_value
        scoped = table.select.return_value.eq.return_value.eq.return_value
        page = scoped.eq.return_value.order.return_value.limit.return_value
        item = {'id': 'a', 'status': 'archived', 'string_key': 'inventory.favorite'}
        page.execute.return_value.data = [item]
        page.gt.return_value.execute.return_value.data = []
        result = self.client.get(url)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(result.json['editable'])
        self.assertEqual(result.json['rows'], [item])
        scoped.eq.assert_called_with('kind', 'memory')
        self.assertEqual(self.client.get(PREFIX + 'memory/snapshot?language=english').status_code, 400)

    def test_create_edit_and_bulk_operations_use_scope_and_versions(self):
        entry = {'source': ' 收藏 ', 'target': 'المفضلة', 'scene': '背包', 'note': '标题', 'string_key': 'inventory.favorite'}
        with patch('translation_app.services.knowledge.invalidate_resources') as invalidate:
            self.assertEqual(self.api('memory/entries', {'entry': entry}).status_code, 201)
            refs = [{'id': ENTRY_ID, 'version': 2}]
            self.assertEqual(self.api('memory/entries/' + ENTRY_ID, {'entry': entry, 'version': 1,
                'status': 'pending'}, 'PATCH').status_code, 200)
            self.assertEqual(self.api('memory/bulk', {'entries': refs, 'status': 'archived', 'action': 'status'}).json['count'], 1)
            self.assertEqual(self.api('memory/bulk', {'entries': refs, 'action': 'delete'}).json['count'], 1)
            self.assertEqual(invalidate.call_count, 4)
        _, created = self.calls[0]
        self.assertEqual((created['p_game'], created['p_language'], created['p_kind']), ('tarbi3a_baloot', 'arabic', 'memory'))
        self.assertEqual(created['p_rows'], [{**entry, 'source': '收藏'}])
        self.assertEqual(created['p_status'], 'active')
        self.assertEqual(self.calls[-1][1]['p_refs'], refs)
        self.assertEqual(self.api('memory/bulk', {'entries': refs * 2, 'action': 'delete'}).status_code, 400)

    def test_conflicting_database_version_does_not_invalidate_or_report_success(self):
        self.responses['knowledge_mutate'] = APIError({'message': 'internal', 'code': 'PT409'})
        with patch('translation_app.services.knowledge.invalidate_resources') as invalidate:
            response = self.api('terms/bulk', {'entries': [{'id': ENTRY_ID, 'version': 1}], 'action': 'delete'})
        self.assertEqual(response.status_code, 409)
        self.assertNotIn('internal', response.text)
        invalidate.assert_not_called()

    def test_import_preview_keeps_existing_rows_and_is_bound_to_user_and_library(self):
        self.responses['knowledge_import_matches'] = [{'line': 2, 'target': 'قديمة', 'note': '', 'scene': ''}]
        result = self.api('memory/import/preview', {
            'paste': '原文\t译文\n冲突\tجديدة\n新词\tمصطلح\t商店\t说明\tshop.term\n无效\n新词\tمصطلح\t商店\t说明\tshop.term'
        })
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json['counts'], {'new': 1, 'duplicate': 1, 'conflict': 1, 'invalid': 1})
        self.assertEqual([name for name, _ in self.calls], ['knowledge_import_matches'])
        plan = {'plan_id': result.json['plan_id']}
        other, headers = self.member('another')
        self.assertEqual(self.api('memory/import', plan, client=other, headers=headers).status_code, 400)
        self.assertEqual(self.api('terms/import', plan).status_code, 400)
        self.assertEqual(self.api('memory/import', plan).json, {'count': 1, 'skipped': 0})
        data = self.calls[-1][1]
        self.assertEqual(data['p_rows'], [{'source': '新词', 'target': 'مصطلح', 'scene': '商店',
                                           'note': '说明', 'string_key': 'shop.term'}])
        self.assertEqual((data['p_status'], data['p_origin']), ('pending', 'paste'))

    def test_import_lookup_failure_does_not_produce_a_write_plan(self):
        self.responses['knowledge_import_matches'] = RuntimeError('private database details')
        response = self.api('terms/import/preview', {'paste': '词\tكلمة'})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('private database details', response.text)
        self.assertEqual([name for name, _ in self.calls], ['knowledge_import_matches'])

    def test_file_column_mapping_and_filtered_export_preserve_text(self):
        file = {'file': (io.BytesIO('原文,译文,场景,备注\n=1+2,نص,商店,说明\n'.encode()), 'words.csv')}
        upload = self.client.post('/api/uploads/preview', data=file, headers=self.csrf).json
        preview = self.api('terms/import/preview', {'upload_id': upload['upload_id'], 'sheet': 'CSV_Sheet',
            'mapping': {'source': '原文', 'target': '译文', 'scene': '场景', 'note': '备注'}})
        self.assertEqual(preview.json['counts'], {'new': 1})
        self.assertEqual(self.api('terms/import', {'plan_id': preview.json['plan_id']}).status_code, 200)
        row = self.calls[-1][1]['p_rows'][0]
        self.responses['knowledge_list'] = {'rows': [{**row, 'status': 'pending'}], 'total': 1, 'scenes': ['商店']}
        download = self.client.get(PREFIX + 'terms/export?language=arabic&status=pending&scene=商店')
        self.assertEqual(download.status_code, 200)
        self.assertEqual(self.calls[-1][1]['p_scene'], '商店')
        workbook = load_workbook(io.BytesIO(download.data))
        worksheet = workbook.active
        self.assertEqual((worksheet['A2'].value, worksheet['A2'].data_type), ('=1+2', 's'))
        self.assertEqual(worksheet['D2'].value, '说明')
        workbook.close()
        source = {'file': (io.BytesIO(download.data), 'export.xlsx')}
        upload = self.client.post('/api/uploads/preview', data=source, headers=self.csrf).json
        preview = self.api('terms/import/preview', {'upload_id': upload['upload_id'], 'sheet': '知识库',
            'mapping': {'source': '原文', 'target': '译文', 'scene': '适用场景', 'note': '备注'}})
        self.assertEqual(preview.json['rows'][0]['source'], '=1+2')

    def test_contextual_matches_and_legacy_memory_are_safe_to_reuse(self):
        memory = {
            '收藏': [{'target': 'المفضلة', 'scene': '背包', 'string_key': 'inventory.favorite', 'verified': True},
                   {'target': 'احفظ', 'scene': '礼物', 'string_key': 'gift.favorite', 'verified': True}],
            '欢迎': [{'target': 'أهلاً', 'verified': False}],
            '金币': [{'target': 'ذهب', 'verified': True}],
            '退出': [{'target': 'خروج', 'verified': True}],
        }
        index = _ContextIndex({'金币': [{'target': 'الذهب'}]}, memory)
        self.assertEqual(index.references('收藏')[1], set())
        references = index.build([(7, '收藏', 'gift.favorite'), (8, '收藏', '背包')])
        self.assertIn('[rows 7] 收藏 -> احفظ', references)
        self.assertIn('[rows 8] 收藏 -> المفضلة', references)
        self.assertIsNone(index.exact_target('欢迎'))
        self.assertIsNone(index.exact_target('金币'))
        self.assertEqual(index.exact_target('退出'), 'خروج')
