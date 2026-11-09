"""Offline end-to-end comparison using a simulated model; no API keys or network."""
import argparse
import csv
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from translation_app.services.translation import translate_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=int, default=240)
    parser.add_argument('--request-delay', type=float, default=.03)
    parser.add_argument('--row-delay', type=float, default=.001)
    args = parser.parse_args()
    if args.rows < 1 or min(args.request_delay, args.row_delay) < 0:
        parser.error('rows must be positive and delays nonnegative')
    results = []
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = root / 'source.csv'
        with source.open('w', newline='', encoding='utf-8') as handle:
            writer = csv.writer(handle)
            writer.writerow(['source'])
            writer.writerows([[f'文案 {i:05d}'] for i in range(args.rows)])

        def provider(rows, *_):
            time.sleep(args.request_delay + args.row_delay * len(rows))
            return [{'row_id': row_id, 'translated_text': f'Text {text[-5:]}'}
                    for row_id, text, _ in rows]

        for concurrency, batch_size in ((1, 10), (3, 10), (1, 30), (3, 30)):
            output = root / f'out-{concurrency}-{batch_size}.csv'
            with patch.dict(os.environ, {'TRANSLATION_CONCURRENCY': str(concurrency),
                                         'TRANSLATION_INPUT_TOKEN_BUDGET': '6000',
                                         'TRANSLATION_OUTPUT_TOKEN_BUDGET': '4000'}):
                summary = translate_file(source, output, 'direct', 'english',
                                         {'CSV_Sheet': {'source': 'source', 'target': 'target'}},
                                         {'batch_size': batch_size}, provider=provider)
            with output.open(encoding='utf-8-sig', newline='') as handle:
                actual = [row['target'] for row in csv.DictReader(handle)]
            assert actual == [f'Text {i:05d}' for i in range(args.rows)], 'row alignment failed'
            results.append({'concurrency': concurrency, 'batch_size': batch_size,
                            'batches': summary['batches'], 'seconds': summary['duration_seconds']})
    for result in results:
        result['relative_speed'] = round(results[0]['seconds'] / result['seconds'], 2)
    print(json.dumps({'simulation_only': True, 'rows': args.rows,
                      'request_delay': args.request_delay, 'row_delay': args.row_delay,
                      'results': results}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
