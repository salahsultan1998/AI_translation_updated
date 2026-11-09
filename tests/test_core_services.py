import csv
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

from translation_app.services import projects
from translation_app.services.projects import load_prompt_pair, get_project, list_languages, list_projects, validate_selection
from translation_app.services.spreadsheets import CSV_SHEET, inspect_file, read_mapped_rows
from translation_app.services.translation import _prepare_batches, translate_file


def prompt_text(project, language):
    return '\n\n'.join(load_prompt_pair(project, language).values())


class ProjectConfigurationTests(unittest.TestCase):
    def test_project_languages_and_database_ids(self):
        self.assertEqual({p['id'] for p in list_projects()}, {'direct', 'tarbi3a_baloot', 'baloot_challenges', 'sawa', 's06'})
        self.assertEqual(get_project('tarbi3a_baloot')['database_id'], 'Tarbi3a_Baloot')
        for project, language in [('direct', 'turkish'), ('s06', 'english'), ('sawa', 'arabic'), ('sawa', 'turkish')]:
            validate_selection(project, language)
        for project, language in [('s06', 'arabic'), ('sawa', 'english'), ('tarbi3a_baloot', 'english')]:
            with self.assertRaises(ValueError):
                validate_selection(project, language)

    def test_project_specific_prompt_does_not_leak(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'config'
            shutil.copytree(projects.CONFIG_ROOT, config)
            (config / 'projects/s06/languages/english/instructions.md').write_text('S06_ONLY_RULE')
            with patch.object(projects, 'CONFIG_ROOT', config):
                self.assertIn('S06_ONLY_RULE', prompt_text('s06', 'english'))
                self.assertNotIn('S06_ONLY_RULE', prompt_text('direct', 'english'))
                self.assertNotIn('S06_ONLY_RULE', prompt_text('baloot_challenges', 'arabic'))

    def test_language_changes_are_private_to_the_project(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'config'
            shutil.copytree(projects.CONFIG_ROOT, config)
            with patch.object(projects, 'CONFIG_ROOT', config):
                original = prompt_text('tarbi3a_baloot', 'arabic')
                direct = prompt_text('direct', 'arabic')
                rules = config / 'projects/sawa/languages/arabic/instructions.md'
                rules.write_text(rules.read_text(encoding='utf-8') + '\nSAWA_ONLY_RULE', encoding='utf-8')
                metadata_path = rules.parent / 'language.json'
                metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
                metadata['name'] = '项目专属阿拉伯语'
                metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding='utf-8')
                self.assertIn('SAWA_ONLY_RULE', prompt_text('sawa', 'arabic'))
                self.assertEqual(original, prompt_text('tarbi3a_baloot', 'arabic'))
                self.assertEqual(direct, prompt_text('direct', 'arabic'))
                self.assertEqual(list_languages('sawa')[0]['name'], '项目专属阿拉伯语')
                self.assertNotEqual(list_languages('direct')[0]['name'], '项目专属阿拉伯语')
                from translation_app.web.pages import project_view
                self.assertEqual(project_view(get_project('sawa'))['languages'][0]['name'], '项目专属阿拉伯语')

    def test_add_language_to_one_project_without_global_registration(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'config'
            shutil.copytree(projects.CONFIG_ROOT, config)
            project_dir = config / 'projects/s06'
            french = project_dir / 'languages/french'
            french.mkdir()
            (french / 'language.json').write_text(json.dumps({
                'id': 'french', 'name': '法语', 'database_name': 'French', 'system_prompt': 'system.md', 'instructions': 'instructions.md'
            }, ensure_ascii=False), encoding='utf-8')
            (french / 'system.md').write_text('Translate into French', encoding='utf-8')
            (french / 'instructions.md').write_text('S06 法语规则', encoding='utf-8')
            config_path = project_dir / 'project.json'
            project = json.loads(config_path.read_text(encoding='utf-8'))
            project['languages'].append('french')
            config_path.write_text(json.dumps(project), encoding='utf-8')
            with patch.object(projects, 'CONFIG_ROOT', config):
                self.assertIn('S06 法语规则', prompt_text('s06', 'french'))
                self.assertEqual(validate_selection('s06', 'french')[1]['database_name'], 'French')
                with self.assertRaises(ValueError):
                    validate_selection('direct', 'french')
                with self.assertRaises(ValueError):
                    validate_selection('sawa', 'french')

    def test_language_instruction_paths_cannot_escape_the_language_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'config'
            shutil.copytree(projects.CONFIG_ROOT, config)
            path = config / 'projects/sawa/languages/arabic/language.json'
            metadata = json.loads(path.read_text(encoding='utf-8'))
            metadata['instructions'] = '../turkish/instructions.md'
            path.write_text(json.dumps(metadata), encoding='utf-8')
            with patch.object(projects, 'CONFIG_ROOT', config), self.assertRaises(ValueError):
                validate_selection('sawa', 'arabic')


class SpreadsheetTranslationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_context_deduplication_and_batch_limit(self):
        rows = [(0, 'Open', 'verb'), (1, 'Open', 'adjective'), (2, 'Open', 'verb')]
        batches, aliases = _prepare_batches(rows, 1, False)
        self.assertEqual([len(batch) for batch in batches], [1, 1])
        self.assertEqual(aliases, {2: 0})

    def test_csv_export_preserves_untranslated_existing_targets(self):
        source, output = self.folder/'source.csv', self.folder/'output.csv'
        with source.open('w', newline='', encoding='utf-8') as stream:
            csv.writer(stream).writerows([['source','context','target'], ['你好','','old'], ['', '', 'keep']])
        mapping = {CSV_SHEET: {'source': 'source', 'context': 'context', 'target': 'target'}}
        self.assertEqual(inspect_file(source)['sheets'][0]['name'], CSV_SHEET)
        result = translate_file(source, output, 'direct', 'english', mapping, {}, provider=lambda rows, *args: [
            {'row_id': row[0], 'translated_text': 'Hello'} for row in rows])
        with output.open(encoding='utf-8-sig', newline='') as stream:
            records = list(csv.reader(stream))
        self.assertEqual(records[1][2], 'Hello')
        self.assertEqual(records[2][2], 'keep')
        self.assertEqual(result['rows'], 1)

    def test_preview_keeps_blank_header_cells_aligned(self):
        path = self.folder / 'blank-header.csv'
        path.write_text('source,,target\nhello,ignored,world\n', encoding='utf-8')
        sheet = inspect_file(path)['sheets'][0]
        self.assertEqual(sheet['columns'], ['source', 'target'])
        self.assertEqual(sheet['preview'], [['hello', 'world']])

    def test_csv_row_and_column_limits_are_enforced_on_preview_and_read(self):
        path = self.folder / 'bounded.csv'
        path.write_text('source,target\na,1\nb,2\nc,3\n', encoding='utf-8')
        mapping = {CSV_SHEET: {'source': 'source', 'target': 'target'}}
        with patch('translation_app.services.spreadsheets.MAX_ROWS', 2):
            with self.assertRaisesRegex(ValueError, '不可超过 2 行'):
                inspect_file(path)
            with self.assertRaisesRegex(ValueError, '不可超过 2 行'):
                read_mapped_rows(path, mapping)
        path.write_text('source,target,extra\na,1,x\n', encoding='utf-8')
        with patch('translation_app.services.spreadsheets.MAX_COLUMNS', 2):
            with self.assertRaisesRegex(ValueError, '不可超过 2 列'):
                inspect_file(path)
            with self.assertRaisesRegex(ValueError, '不可超过 2 列'):
                read_mapped_rows(path, mapping)

    def test_xlsx_limits_apply_across_sheets(self):
        path = self.folder / 'bounded.xlsx'
        book = Workbook()
        book.active.title = 'First'; book.active.append(['source']); book.active.append(['one'])
        sheet = book.create_sheet('Second'); sheet.append(['source']); sheet.append(['two'])
        book.save(path); book.close()
        mapping = {name: {'source': 'source', 'target': 'target'} for name in ('First', 'Second')}
        with patch('translation_app.services.spreadsheets.MAX_ROWS', 1):
            with self.assertRaisesRegex(ValueError, '不可超过 1 行'):
                inspect_file(path)
            with self.assertRaisesRegex(ValueError, '不可超过 1 行'):
                read_mapped_rows(path, mapping)

    def test_xlsx_multi_sheet_mapping_and_unselected_sheet_preserved(self):
        source, output = self.folder/'source.xlsx', self.folder/'output.xlsx'
        book = Workbook(); first = book.active; first.title = 'First'
        first.append(['source', 'target']); first.append(['你好', 'old']); first.append(['', 'keep'])
        second = book.create_sheet('Second'); second.append(['text', 'out']); second.append(['世界', 'old2'])
        other = book.create_sheet('Other'); other.append(['keep']); other.append(['unchanged'])
        book.save(source); book.close()
        mapping = {'First': {'source':'source', 'target':'target'}, 'Second': {'source':'text','target':'out'}}
        translate_file(source, output, 'direct', 'english', mapping, {}, provider=lambda rows, *args: [
            {'row_id':row[0], 'translated_text':'translated'} for row in rows])
        book = load_workbook(output)
        self.assertEqual(book['First']['B2'].value, 'translated')
        self.assertEqual(book['First']['B3'].value, 'keep')
        self.assertEqual(book['Second']['B2'].value, 'translated')
        self.assertEqual(book['Other']['A2'].value, 'unchanged')
        book.close()

    def test_missing_columns_and_partial_model_results_fail_without_output(self):
        source, output = self.folder/'source.csv', self.folder/'output.csv'
        source.write_text('source,target\nHello {0},old\nWorld,old2\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            read_mapped_rows(source, {CSV_SHEET: {'source':'wrong','target':'target'}})
        with self.assertRaises(ValueError):
            translate_file(source, output, 'direct', 'english', {CSV_SHEET:{'source':'source','target':'target'}}, {},
                           provider=lambda rows, *args: [{'row_id':0,'translated_text':'Hello'}])
        self.assertFalse(output.exists())

    def test_project_context_and_project_language_isolation(self):
        source, output = self.folder/'source.csv', self.folder/'output.csv'
        source.write_text('source\n英雄 {0}\n', encoding='utf-8')
        received = []
        def resources(project, language, settings):
            self.assertEqual(project['database_id'], 'S06')
            self.assertEqual(language['database_name'], 'English')
            return {'英雄': {'target': 'Hero', 'note': '专用名词'}}, {}, 'Game tone'
        def provider(rows, system, language, context, settings):
            received.append((system, context, settings['instructions']))
            return [{'row_id':rows[0][0], 'translated_text':'Hero {0}'}]
        translate_file(source, output, 's06', 'english', {CSV_SHEET:{'source':'source','target':'target'}}, {'instructions': 'S06 English instructions'},
                       provider=provider, resource_loader=resources)
        self.assertNotIn('GAME TONE', received[0][1])
        self.assertNotIn('Game tone', received[0][1])
        self.assertIn('英雄 : Hero', received[0][1])
        self.assertEqual('S06 English instructions', received[0][2])

    def test_progress_is_visible_before_loading_project_context(self):
        source, output = self.folder/'source.csv', self.folder/'output.csv'
        source.write_text('source\n你好\n', encoding='utf-8')
        events = []
        def resources(*args):
            events.append('resources')
            return {}, {}, ''
        def progress(done, total):
            events.append((done, total))
        translate_file(source, output, 's06', 'english', {CSV_SHEET: {'source': 'source', 'target': 'target'}}, {},
                       progress, provider=lambda rows, *_: [{'row_id': rows[0][0], 'translated_text': 'Hello'}],
                       resource_loader=resources)
        self.assertEqual(events[:2], [(0, 1), 'resources'])


if __name__ == '__main__':
    unittest.main()
