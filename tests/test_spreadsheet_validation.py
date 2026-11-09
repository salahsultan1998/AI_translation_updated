from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from openpyxl import Workbook

from translation_app.services.spreadsheets import CSV_SHEET, has_mapped_rows, inspect_file


class MappedInputValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def test_csv_finds_source_after_blank_rows_and_handles_short_rows(self):
        path = self.folder / 'input.csv'
        path.write_text('id,source,context\n1\n2,   \n3,hello,hint\n', encoding='utf-8')
        inspect_file(path)
        self.assertTrue(has_mapped_rows(path, {CSV_SHEET: {
            'source': 'source', 'context': 'context', 'target': 'target'}}))

    def test_csv_all_blank_sources_and_invalid_mapping(self):
        path = self.folder / 'input.csv'
        path.write_text('source,context\n,hint\n   ,another\n', encoding='utf-8')
        inspect_file(path)
        self.assertFalse(has_mapped_rows(path, {CSV_SHEET: {'source': 'source', 'target': 'out'}}))
        for mapping in ({}, {'Other': {'source': 'source', 'target': 'out'}},
                        {CSV_SHEET: {'source': 'missing', 'target': 'out'}},
                        {CSV_SHEET: {'source': 'source', 'context': 'missing', 'target': 'out'}},
                        {CSV_SHEET: {'source': 'source', 'target': ''}}):
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                has_mapped_rows(path, mapping)

    def test_csv_stops_after_first_source_without_materializing_rest(self):
        @contextmanager
        def rows(path):
            def data():
                yield 0, ['hello']
                raise AssertionError('Unnecessary scan after finding a source')
            yield ['source'], ['source'], data()

        with patch('translation_app.services.spreadsheets._csv_rows', rows):
            self.assertTrue(has_mapped_rows(self.folder / 'input.csv', {
                CSV_SHEET: {'source': 'source', 'target': 'target'}}))

    def workbook(self, first_rows, second_rows):
        path = self.folder / 'input.xlsx'
        book = Workbook()
        book.active.title = 'First'
        for row in first_rows:
            book.active.append(row)
        second = book.create_sheet('Second')
        for row in second_rows:
            second.append(row)
        book.save(path)
        book.close()
        inspect_file(path)
        return path

    def test_xlsx_validates_later_mapping_after_earlier_source_found(self):
        path = self.workbook([['source'], ['hello']], [['text'], ['world']])
        mapping = {'First': {'source': 'source', 'target': 'out'},
                   'Second': {'source': 'text', 'target': 'out'}}
        self.assertTrue(has_mapped_rows(path, mapping))
        for invalid in ({'source': 'missing', 'target': 'out'},
                        {'source': 'text', 'context': 'missing', 'target': 'out'},
                        {'source': 'text', 'target': ''}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                has_mapped_rows(path, {**mapping, 'Second': invalid})
        with self.assertRaisesRegex(ValueError, '工作表不存在'):
            has_mapped_rows(path, {**mapping, 'Missing': {'source': 'source', 'target': 'out'}})

    def test_xlsx_blank_first_sheet_can_find_source_on_second(self):
        path = self.workbook([['source'], ['   ']], [['text'], [None], ['hello']])
        self.assertTrue(has_mapped_rows(path, {
            'First': {'source': 'source', 'target': 'out'},
            'Second': {'source': 'text', 'target': 'out'}}))

    def test_xlsx_stops_data_scan_but_reads_all_selected_headers(self):
        def first_rows(**kwargs):
            yield ('source',)
            yield ('hello',)
            raise AssertionError('First sheet scanned after source found')

        def second_rows(**kwargs):
            yield ('text',)
            raise AssertionError('Second sheet data scanned unnecessarily')

        first, second, book = MagicMock(), MagicMock(), MagicMock()
        for sheet, reader in ((first, first_rows), (second, second_rows)):
            sheet.max_column, sheet.max_row = 1, 100
            sheet.iter_rows.side_effect = reader
        book.sheetnames = ['First', 'Second']
        book.__getitem__.side_effect = {'First': first, 'Second': second}.__getitem__
        with patch('translation_app.services.spreadsheets._open_workbook', return_value=book):
            self.assertTrue(has_mapped_rows(self.folder / 'input.xlsx', {
                'First': {'source': 'source', 'target': 'out'},
                'Second': {'source': 'text', 'target': 'out'}}))
        second.iter_rows.assert_called_once_with(values_only=True)
        book.close.assert_called_once()

    def test_xlsx_all_blank_sources(self):
        path = self.workbook([['source'], ['   ']], [['text'], [' ']])
        self.assertFalse(has_mapped_rows(path, {
            'First': {'source': 'source', 'target': 'out'},
            'Second': {'source': 'text', 'target': 'out'}}))

    def test_only_supported_file_types(self):
        with self.assertRaisesRegex(ValueError, '仅支持 CSV 和 XLSX'):
            has_mapped_rows(self.folder / 'input.xls', {'Sheet': {'source': 'source', 'target': 'out'}})


if __name__ == '__main__':
    unittest.main()
