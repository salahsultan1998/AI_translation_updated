import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class LocalStartupTests(unittest.TestCase):
    def test_python_entrypoint_starts_one_worker(self):
        with tempfile.TemporaryDirectory() as storage, \
             patch.dict('os.environ', {'LOCATOOL_STORAGE': storage}), \
             patch('threading.Thread') as thread, \
             patch('flask.Flask.run') as flask_run:
            runpy.run_path(str(Path(__file__).resolve().parents[1] / 'app.py'), run_name='__main__')
        thread.assert_called_once()
        self.assertTrue(thread.call_args.kwargs['daemon'])
        self.assertEqual(thread.call_args.kwargs['name'], 'locatool-local-worker')
        thread.return_value.start.assert_called_once()
        flask_run.assert_called_once()

    def test_wsgi_import_does_not_start_local_worker(self):
        with tempfile.TemporaryDirectory() as storage, \
             patch.dict('os.environ', {'LOCATOOL_STORAGE': storage}), \
             patch('threading.Thread') as thread, \
             patch('flask.Flask.run') as flask_run:
            runpy.run_path(str(Path(__file__).resolve().parents[1] / 'app.py'), run_name='wsgi_import')
        thread.assert_not_called()
        flask_run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
