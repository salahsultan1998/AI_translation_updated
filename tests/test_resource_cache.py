import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from translation_app.services import resources
from translation_app.services.database import ProjectDatabase


class ResourceCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {
            'SUPABASE_URL': 'https://example.test', 'SUPABASE_KEY': 'public-key',
            'LOCATOOL_STORAGE': self.temp.name, 'LOCATOOL_AUTH_ENABLED': 'false',
            'TRANSLATION_RESOURCE_CACHE_SECONDS': '30',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        with resources._LOCK:
            resources._CACHE.clear()
        self.project = {'direct': False, 'database_id': 'S06'}
        self.language = {'database_name': 'English'}
        self.created = 0
        self.requests = []
        self.term = 'Hero'
        self.after_load = None

        def create(*args, **kwargs):
            self.created += 1
            engine = MagicMock()

            def table(name):
                query = MagicMock()
                query.select.return_value = query
                query.eq.return_value = query
                query.order.return_value = query

                def page(start, end):
                    self.requests.append((name, start, end))
                    # Deliberately cap below page size to exercise pagination.
                    data = ([{'source_term': '英雄', 'target_term': self.term, 'note': 'noun'}]
                            if name == 'glossaries' else
                            [{'source_text': '你好', 'target_text': 'Hello'}])
                    if name == 'translation_memory' and start and self.after_load:
                        self.after_load()
                    query.execute.return_value.data = data if start == 0 else []
                    return query

                query.range.side_effect = page
                return query

            engine.client.table.side_effect = table

            return engine

        self.database = patch('translation_app.services.database.ProjectDatabase', side_effect=create)
        self.database.start()
        self.addCleanup(self.database.stop)

    def load(self, settings=None):
        return resources.load_resources(self.project, self.language, settings or {})

    @staticmethod
    def token(subject, expires=None):
        body = json.dumps({'sub': subject, 'exp': expires or time.time() + 3600}).encode()
        return 'header.' + base64.urlsafe_b64encode(body).decode().rstrip('=') + '.signature'

    def test_cache_returns_defensive_snapshots_and_reads_short_pages(self):
        first = self.load()
        first[0]['英雄']['target'] = 'mutated'
        second = self.load()
        self.assertEqual(second[0]['英雄']['target'], 'Hero')
        self.assertEqual(self.created, 1)
        self.assertEqual([(name, start) for name, start, _ in self.requests],
                         [('glossaries', 0), ('glossaries', 1),
                          ('translation_memory', 0), ('translation_memory', 1)])

    def test_auth_project_language_and_supabase_key_are_isolated(self):
        with patch.dict(os.environ, {'LOCATOOL_AUTH_ENABLED': 'true'}):
            alice, bob = self.token('alice'), self.token('bob')
            self.load({'access_token': alice})
            self.load({'access_token': alice})
            self.load({'access_token': bob})
            self.language = {'database_name': 'Arabic'}
            self.load({'access_token': alice})
            self.project = {'direct': False, 'database_id': 'other'}
            self.load({'access_token': alice})
            with patch.dict(os.environ, {'SUPABASE_KEY': 'other-key'}):
                self.load({'access_token': alice})
        self.assertEqual(self.created, 5)

    def test_ttl_disabled_and_expired(self):
        with patch.dict(os.environ, {'TRANSLATION_RESOURCE_CACHE_SECONDS': '0'}):
            self.load()
            self.load()
        self.assertEqual(self.created, 2)
        with patch('translation_app.services.resources.time.time', return_value=100):
            self.load()
        with patch('translation_app.services.resources.time.time', return_value=131):
            self.load()
        self.assertEqual(self.created, 4)

    def test_default_reuses_preloaded_snapshots(self):
        with patch.dict(os.environ):
            os.environ.pop('TRANSLATION_RESOURCE_CACHE_SECONDS', None)
            self.load()
            self.load()
        self.assertEqual(self.created, 1)
        self.assertTrue(resources._CACHE)

    def test_separate_worker_process_reuses_web_preload_without_database_calls(self):
        self.load()
        code = """
from unittest.mock import patch
from translation_app.services.resources import load_resources
with patch('translation_app.services.database.ProjectDatabase', side_effect=AssertionError('unexpected database call')):
    result = load_resources({'direct':False,'database_id':'S06'}, {'database_name':'English'}, {})
    assert result[0]['英雄']['target'] == 'Hero'
    assert result[1]['你好'] == 'Hello'
    assert result[2] == ''
"""
        subprocess.run([sys.executable, '-c', code], check=True, capture_output=True)
        path = resources._version_path('s06').parent.parent / 'resource-cache' / 'snapshots.sqlite3'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(b'public-key', path.read_bytes())

    def test_simultaneous_preload_and_translation_load_only_once(self):
        from concurrent.futures import ThreadPoolExecutor
        self.after_load = lambda: time.sleep(.03)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.load) for _ in range(2)]
            results = [future.result(timeout=3) for future in futures]
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.created, 1)

    def test_preload_reports_remaining_lifetime_instead_of_extending_old_cache(self):
        with patch('translation_app.services.resources.time.time', return_value=100):
            self.load()
        with patch('translation_app.services.resources.time.time', return_value=125):
            result = resources.preload_resources({**self.project, 'id': 's06'},
                                                {**self.language, 'id': 'english'}, {})
        self.assertEqual(result['expires_in'], 5)
        self.assertEqual(self.created, 1)

    def test_expired_jwt_cannot_use_previously_cached_snapshot(self):
        token = self.token('alice', time.time() + 10)
        with patch.dict(os.environ, {'LOCATOOL_AUTH_ENABLED': 'true'}):
            self.load({'access_token': token})
            with patch('translation_app.services.resources.time.time', return_value=time.time() + 11):
                with self.assertRaisesRegex(ValueError, '登录已过期'):
                    self.load({'access_token': token})
        self.assertEqual(self.created, 1)

    def test_opaque_tokens_are_not_cached_and_anonymous_is_rejected_in_production(self):
        self.load({'access_token': 'opaque'})
        self.load({'access_token': 'opaque'})
        self.assertEqual(self.created, 2)
        with patch.dict(os.environ, {'LOCATOOL_AUTH_ENABLED': 'true'}):
            with self.assertRaisesRegex(ValueError, '登录已过期'):
                self.load()

    def test_game_generation_invalidates_all_languages_and_has_no_credentials(self):
        self.load()
        self.term = 'Updated Hero'
        resources.invalidate_resources('s06')
        self.assertEqual(self.load()[0]['英雄']['target'], 'Updated Hero')
        self.assertEqual(self.created, 2)
        marker = resources._version_path('s06')
        self.assertTrue(marker.is_file())
        self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('public-key', marker.read_text())

    def test_worker_and_web_storage_roots_use_same_generation(self):
        settings = {'_limiter_root': str(Path(self.temp.name) / 'jobs' / 'provider-slots')}
        self.load(settings)
        resources.invalidate_resources('s06', storage_root=self.temp.name)
        self.load(settings)
        self.assertEqual(self.created, 2)

    def test_another_process_invalidates_an_existing_snapshot(self):
        self.load()
        subprocess.run([sys.executable, '-c',
                        "from translation_app.services.resources import invalidate_resources; "
                        "invalidate_resources('s06')"], check=True, capture_output=True)
        self.load()
        self.assertEqual(self.created, 2)

    def test_invalidation_during_load_does_not_cache_mixed_snapshot(self):
        self.after_load = lambda: resources.invalidate_resources('s06')
        self.load()
        self.load()
        self.assertEqual(self.created, 2)
        self.assertFalse(resources._CACHE)

    def test_cache_is_bounded_and_direct_mode_never_loads_database(self):
        for i in range(resources._MAX_ENTRIES + 3):
            self.project['database_id'] = f'game{i}'
            self.load()
        self.assertEqual(len(resources._CACHE), resources._MAX_ENTRIES)
        count = self.created
        self.assertEqual(resources.load_resources({'direct': True}, {}, {}), ({}, {}, ''))
        self.assertEqual(self.created, count)


class ResourceInvalidationTests(unittest.TestCase):
    def test_partial_bulk_write_invalidates_after_failure(self):
        engine = object.__new__(ProjectDatabase)
        engine.storage_root = '/unused'
        engine.client = MagicMock()
        engine.client.table.return_value.upsert.return_value.execute.side_effect = [None, RuntimeError('failed')]
        rows = [{'game_id': 'game', 'source_term': str(i)} for i in range(3)]
        with patch('translation_app.services.resources.invalidate_resources') as invalidate:
            with self.assertRaisesRegex(RuntimeError, 'failed'):
                engine._upsert('glossaries', rows, 'source_term', 2)
        invalidate.assert_called_once_with('game', storage_root='/unused')

    def test_profile_write_also_invalidates_after_uncertain_error(self):
        engine = object.__new__(ProjectDatabase)
        engine.storage_root = '/unused'
        engine.client = MagicMock()
        engine.client.table.return_value.upsert.return_value.execute.side_effect = RuntimeError('failed')
        with patch('translation_app.services.resources.invalidate_resources') as invalidate:
            with self.assertRaisesRegex(RuntimeError, 'failed'):
                engine.upsert_game_profile('game', 'style')
        invalidate.assert_called_once_with('game', storage_root='/unused')


if __name__ == '__main__':
    unittest.main()
