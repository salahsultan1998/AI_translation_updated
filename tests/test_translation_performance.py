import csv
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

from translation_app.services.translation import translate_file


class TranslationPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.output = self.root / 'in.csv', self.root / 'out.csv'
        self.mapping = {'CSV_Sheet': {'source': 'source', 'context': 'context', 'target': 'target'}}

    def write(self, rows):
        with self.source.open('w', encoding='utf-8', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(['source', 'context', 'target'])
            writer.writerows(rows)

    def translate(self, settings=None, provider=None, resources=None):
        return translate_file(self.source, self.output, 'direct', 'english', self.mapping,
                              settings or {}, provider=provider or self.provider,
                              resource_loader=resources)

    @staticmethod
    def provider(rows, *_):
        return [{'row_id': row_id, 'translated_text': 'translated ' + text} for row_id, text, _ in rows]

    def test_bounded_parallel_requests_preserve_out_of_order_rows(self):
        self.write([(f'row {i}', '', '') for i in range(6)])
        barrier = threading.Barrier(3)
        lock = threading.Lock()
        running, maximum, calls = 0, 0, []

        def provider(rows, *_):
            nonlocal running, maximum
            with lock:
                running += 1
                maximum = max(maximum, running)
                calls.append(rows[0][0])
                first = len(calls) <= 3
            if first:
                barrier.wait(timeout=3)
            time.sleep(.005 * (6 - rows[0][0]))
            with lock:
                running -= 1
            return self.provider(rows)

        with patch.dict('os.environ', {'TRANSLATION_CONCURRENCY': '3'}):
            summary = self.translate({'batch_size': 1}, provider)
        self.assertEqual(maximum, 3)
        self.assertEqual(summary['batches'], 6)
        with self.output.open(encoding='utf-8-sig', newline='') as handle:
            self.assertEqual([row['target'] for row in csv.DictReader(handle)],
                             [f'translated row {i}' for i in range(6)])

    def test_failure_drains_and_checkpoints_already_sent_success(self):
        self.write([('bad', '', ''), ('good {0}', '', ''), ('later', '', '')])
        barrier = threading.Barrier(2)
        calls = []

        def failing(rows, *_):
            calls.append(rows[0][1])
            barrier.wait(timeout=3)
            if rows[0][1] == 'bad':
                raise RuntimeError('failure')
            time.sleep(.03)
            return self.provider(rows)

        with patch.dict('os.environ', {'TRANSLATION_CONCURRENCY': '2'}):
            with self.assertRaisesRegex(RuntimeError, 'failure'):
                self.translate({'batch_size': 1}, failing)
            self.assertCountEqual(calls, ['bad', 'good {0}'])
            self.assertFalse(self.output.exists())
            resumed = []

            def provider(rows, *_):
                resumed.extend(row[1] for row in rows)
                return self.provider(rows)

            result = self.translate({'batch_size': 1}, provider)
        self.assertEqual(result['restored_rows'], 1)
        self.assertCountEqual(resumed, ['bad', 'later'])

    def test_checkpoint_reused_only_with_same_effective_inputs(self):
        self.write([('hello', '', '')])
        settings = {'system_prompt': 'original', 'model': 'one'}
        first = self.translate(settings)
        with patch.object(self, 'provider', side_effect=AssertionError('must restore')):
            restored = self.translate(settings)
        self.assertEqual((first['restored_rows'], restored['restored_rows']), (0, 1))
        for changed in ({'system_prompt': 'changed', 'model': 'one'},
                        {'system_prompt': 'changed', 'model': 'two'},
                        {'system_prompt': 'changed', 'model': 'two', 'custom_instructions': 'formal'}):
            self.assertEqual(self.translate(changed)['restored_rows'], 0)
        resources = lambda *_: ({}, {'hello': 'memory'}, '')
        self.assertEqual(self.translate(changed, resources=resources)['restored_rows'], 0)

    def test_exact_memory_requires_opt_in_empty_context_and_safe_tags(self):
        self.write([('hello {0}', '', ''), ('world', 'button', ''), ('broken {0}', '', '')])
        resources = lambda *_: ({}, {'hello {0}': 'Hello {0}', 'world': 'World', 'broken {0}': 'Broken'}, '')
        seen = []

        def provider(rows, *_):
            seen.extend(row[1] for row in rows)
            return self.provider(rows)

        result = self.translate({'use_exact_memory': True}, provider, resources)
        self.assertEqual(result['memory_rows'], 1)
        self.assertCountEqual(seen, ['world', 'broken {0}'])
        seen.clear()
        result = self.translate({'use_exact_memory': True, 'custom_instructions': 'formal'}, provider, resources)
        self.assertEqual(result['memory_rows'], 0)
        self.assertEqual(len(seen), 3)
        result = self.translate({}, provider, resources)
        self.assertEqual(result['memory_rows'], 0)

    def test_context_and_output_budget_split_even_in_manual_mode(self):
        self.write([('a' * 160 + str(i), '', '') for i in range(4)])
        resources = lambda *_: ({}, {}, 'profile ' * 10)
        lengths = []

        def provider(rows, *_):
            lengths.append(len(rows))
            return self.provider(rows)

        with patch.dict('os.environ', {'TRANSLATION_INPUT_TOKEN_BUDGET': '400',
                                       'TRANSLATION_OUTPUT_TOKEN_BUDGET': '256'}):
            result = self.translate({'batch_size': 50, 'use_smart_budget': False,
                                     'system_prompt': 'Translate'}, provider, resources)
        self.assertGreater(result['batches'], 1)
        self.assertEqual(sum(lengths), 4)

    def test_oversized_single_row_fails_before_model_request(self):
        self.write([('字' * 1000, '', '')])
        with patch.dict('os.environ', {'TRANSLATION_OUTPUT_TOKEN_BUDGET': '256'}), \
             patch.object(self, 'provider', side_effect=AssertionError('should not call')):
            with self.assertRaisesRegex(ValueError, 'Token 预算'):
                self.translate()
        self.assertFalse(self.output.exists())

    def test_cross_sheet_dedup_preserves_context_and_original_positions(self):
        source, output = self.root / 'in.xlsx', self.root / 'out.xlsx'
        book = Workbook()
        first = book.active
        first.title = 'first'
        first.append(['source', 'context', 'target'])
        first.append(['Open {0}', 'verb', 'old'])
        first.append(['', '', 'keep'])
        second = book.create_sheet('second')
        second.append(['source', 'context', 'target'])
        second.append(['Open {0}', 'verb', 'old'])
        second.append(['Open {0}', 'adjective', 'old'])
        book.save(source)
        book.close()
        mappings = {name: {'source': 'source', 'context': 'context', 'target': 'target'}
                    for name in ('first', 'second')}
        seen = []

        def provider(rows, *_):
            seen.extend(rows)
            return [{'row_id': i, 'translated_text': f'{context} {{0}}'} for i, _, context in rows]

        result = translate_file(source, output, 'direct', 'english', mappings, {}, provider=provider)
        self.assertEqual((result['rows'], result['unique_rows'], len(seen)), (3, 2, 2))
        book = load_workbook(output)
        try:
            self.assertEqual(book['first']['C2'].value, 'verb {0}')
            self.assertEqual(book['first']['C3'].value, 'keep')
            self.assertEqual(book['second']['C2'].value, 'verb {0}')
            self.assertEqual(book['second']['C3'].value, 'adjective {0}')
        finally:
            book.close()

    def test_metric_logs_contain_no_source_or_instruction(self):
        self.write([('private source', '', '')])
        with self.assertLogs('translation_app.services.translation', level='INFO') as logs:
            result = self.translate({'custom_instructions': 'private instruction'})
        self.assertNotIn('private source', '\n'.join(logs.output))
        self.assertNotIn('private instruction', '\n'.join(logs.output))
        self.assertIn('resources_seconds', result['timings'])
        self.assertEqual(result['requests']['attempts'], 0)  # injected providers do not fake HTTP metrics
