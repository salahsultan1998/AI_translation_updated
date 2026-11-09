import multiprocessing
import os
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from translation_app.services import provider_runtime as runtime


def _contend(root, active, peak, mutex, release, messages):
    os.environ['TRANSLATION_GLOBAL_CONCURRENCY'] = '2'
    with runtime.request_slot({'_limiter_root': root}):
        with mutex:
            active.value += 1
            peak.value = max(peak.value, active.value)
        messages.put('entered')
        release.wait(10)
        with mutex:
            active.value -= 1


def _hold_slot(root, messages):
    os.environ['TRANSLATION_GLOBAL_CONCURRENCY'] = '1'
    with runtime.request_slot({'_limiter_root': root}):
        messages.put('entered')
        threading.Event().wait(30)


class ProviderRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.settings = {'_limiter_root': self.folder.name}

    def test_limit_is_shared_across_processes(self):
        context = multiprocessing.get_context('spawn')
        active, peak = context.Value('i', 0), context.Value('i', 0)
        mutex, release, messages = context.Lock(), context.Event(), context.Queue()
        processes = [context.Process(target=_contend, args=(
            self.folder.name, active, peak, mutex, release, messages)) for _ in range(4)]
        try:
            for process in processes:
                process.start()
            self.assertEqual(messages.get(timeout=8), 'entered')
            self.assertEqual(messages.get(timeout=8), 'entered')
            with self.assertRaises(queue.Empty):
                messages.get(timeout=0.15)
            release.set()
            for process in processes:
                process.join(8)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(peak.value, 2)
            self.assertEqual(active.value, 0)
        finally:
            release.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(2)
            messages.close()

    def test_process_exit_releases_slot(self):
        context = multiprocessing.get_context('spawn')
        messages = context.Queue()
        process = context.Process(target=_hold_slot, args=(self.folder.name, messages))
        process.start()
        try:
            self.assertEqual(messages.get(timeout=8), 'entered')
            process.terminate()
            process.join(3)
            acquired = threading.Event()

            def acquire():
                with runtime.request_slot(self.settings):
                    acquired.set()

            with patch.dict(os.environ, {'TRANSLATION_GLOBAL_CONCURRENCY': '1'}):
                thread = threading.Thread(target=acquire, daemon=True)
                thread.start()
                self.assertTrue(acquired.wait(2))
                thread.join(2)
        finally:
            if process.is_alive():
                process.terminate()
            process.join(2)
            messages.close()

    def test_cancellation_while_waiting_and_release_on_exception(self):
        cancel, waiting, finished = threading.Event(), threading.Event(), threading.Event()
        errors = []

        def acquire():
            waiting.set()
            try:
                with runtime.request_slot({**self.settings, '_cancel_event': cancel}):
                    errors.append('unexpected acquisition')
            except RuntimeError as error:
                errors.append(str(error))
            finally:
                finished.set()

        with patch.dict(os.environ, {'TRANSLATION_GLOBAL_CONCURRENCY': '1'}):
            with self.assertRaisesRegex(ValueError, 'test'):
                with runtime.request_slot(self.settings):
                    thread = threading.Thread(target=acquire, daemon=True)
                    thread.start()
                    self.assertTrue(waiting.wait(1))
                    cancel.set()
                    self.assertTrue(finished.wait(1))
                    raise ValueError('test')
            thread.join(1)
            self.assertEqual(errors, ['翻译任务已停止'])
            with runtime.request_slot(self.settings):
                pass
        self.assertEqual(Path(self.folder.name, '0.lock').stat().st_mode & 0o777, 0o600)

    def test_configuration_and_default_root(self):
        for value in ('0', '129', 'bad', ''):
            with patch.dict(os.environ, {'TRANSLATION_GLOBAL_CONCURRENCY': value}):
                with self.assertRaisesRegex(ValueError, 'TRANSLATION_GLOBAL_CONCURRENCY'):
                    with runtime.request_slot(self.settings):
                        pass
        with patch.dict(os.environ, {'JOB_STORAGE_ROOT': '/private/jobs', 'LOCATOOL_STORAGE': '/other'}):
            self.assertEqual(runtime._slot_root({}), Path('/private/jobs/provider-slots'))
        with patch.dict(os.environ, {'LOCATOOL_STORAGE': '/private/data'}, clear=True):
            self.assertEqual(runtime._slot_root({}), Path('/private/data/jobs/provider-slots'))

    def test_connections_are_reused_per_thread_and_reset_after_fork(self):
        sessions = []

        def create():
            session = MagicMock()
            sessions.append(session)
            return session

        with patch.object(runtime, '_LOCAL', threading.local()), \
             patch.object(runtime.requests, 'Session', side_effect=create), \
             patch.object(runtime.os, 'getpid', return_value=10) as getpid:
            runtime.post('https://example.test', timeout=1)
            runtime.post('https://example.test', timeout=1)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].post.call_count, 2)
            thread = threading.Thread(target=lambda: runtime.post('https://example.test'))
            thread.start()
            thread.join(1)
            self.assertEqual(len(sessions), 2)
            getpid.return_value = 11
            runtime.post('https://example.test')
            self.assertEqual(len(sessions), 3)
            sessions[0].close.assert_called_once()
            self.assertEqual(sessions[0].mount.call_args_list[0].args[1].max_retries.total, 0)


if __name__ == '__main__':
    unittest.main()
