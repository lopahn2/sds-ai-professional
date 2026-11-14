"""F3: persistence is the commit point for requested fault changes."""
from test_contract import HttpCase, app
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch


class PersistenceTests(HttpCase):
    def seed_resources(self, directory):
        runtime = self.server.runtime
        runtime.state_dir = directory
        for fault in ('not_ready', 'crash', 'background_job_failure', 'memory_leak',
                      'worker_exhaustion', 'cpu_spin'):
            runtime.set_fault({'fault': fault, 'params': {'threads': 1} if fault == 'cpu_spin' else {}})
        runtime.next_job, runtime.crash_at = 123.0, 456.0
        runtime.allocations.append(bytearray(16))
        return (deepcopy(runtime.faults), (Path(directory) / 'faults.json').read_bytes(),
                runtime.allocations[0], runtime.holder_stop, runtime.spin_stop,
                list(runtime.holders), list(runtime.spinners))

    def assert_resources_unchanged(self, before, directory):
        runtime = self.server.runtime
        faults, saved, allocation, holder_stop, spin_stop, holders, spinners = before
        self.assertEqual(runtime.faults, faults)
        self.assertEqual((runtime.next_job, runtime.crash_at), (123.0, 456.0))
        self.assertIs(runtime.allocations[0], allocation)
        self.assertIs(runtime.holder_stop, holder_stop)
        self.assertIs(runtime.spin_stop, spin_stop)
        self.assertFalse(holder_stop.is_set())
        self.assertFalse(spin_stop.is_set())
        self.assertEqual(runtime.holders, holders)
        self.assertEqual(runtime.spinners, spinners)
        self.assertEqual((Path(directory) / 'faults.json').read_bytes(), saved)

    def test_clear_failure_preserves_state_timers_allocations_and_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            before = self.seed_resources(directory)
            with patch.object(Path, 'unlink', side_effect=PermissionError('injected')):
                status, body, _ = self.request('/api/chaos', 'DELETE')
                self.assertEqual(status, 503)
                self.assertIn('error', body)
                self.assert_resources_unchanged(before, directory)
            with patch.dict(os.environ, {'FAULT_STATE_DIR': directory}):
                restarted = app.create_server('127.0.0.1', 0, background=False)
            try:
                self.assertEqual(restarted.runtime.faults, before[0])
            finally:
                restarted.runtime.stop.set()
                restarted.server_close()

    def test_off_failure_preserves_state_timers_allocations_and_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            before = self.seed_resources(directory)
            for fault in before[0]:
                with self.subTest(fault=fault), patch('runtime.os.replace', side_effect=OSError('injected')):
                    status, _, _ = self.request('/api/chaos', 'POST', {'fault': fault, 'off': True})
                    self.assertEqual(status, 503)
                    self.assert_resources_unchanged(before, directory)

    def test_single_fault_off_unlink_failure_remains_active(self):
        runtime = self.server.runtime
        with tempfile.TemporaryDirectory() as directory:
            runtime.state_dir = directory
            runtime.set_fault({'fault': 'not_ready'})
            before = deepcopy(runtime.faults)
            with patch.object(Path, 'unlink', side_effect=PermissionError('injected')):
                self.assertEqual(self.chaos('not_ready', False)[0], 503)
                self.assertEqual(runtime.faults, before)
                self.assertEqual(self.request('/healthz/ready')[0], 503)

    def test_expiry_persistence_failure_releases_resources_and_background_continues(self):
        runtime = self.server.runtime
        with tempfile.TemporaryDirectory() as directory:
            before = self.seed_resources(directory)
            for entry in runtime.faults.values():
                entry['expires_at'] = time.time() - 1
            runtime._persist()
            path = Path(directory) / 'faults.json'
            saved = path.read_bytes()
            reached = threading.Event()
            with patch.object(Path, 'unlink', side_effect=PermissionError('injected')), \
                    patch.object(runtime.business, 'tick', side_effect=reached.set):
                thread = threading.Thread(target=runtime.background_tick)
                thread.start()
                try:
                    self.assertTrue(reached.wait(2), 'expiry write failure aborted background tick')
                    self.assertTrue(thread.is_alive())
                    self.assertEqual(runtime.faults, {})
                    self.assertEqual(runtime.allocations, [])
                    self.assertTrue(before[3].is_set())
                    self.assertTrue(before[4].is_set())
                    self.assertEqual(path.read_bytes(), saved)
                    self.assertEqual(runtime.errors['RequestError'], 0)
                    self.assertIn('chaos_expiry_persist_failed', self.output.getvalue())
                    self.assertIn('"reason": "expiry"', self.output.getvalue())
                finally:
                    runtime.stop.set()
                    thread.join(2)
            for worker in before[5] + before[6]:
                worker.join(1)
                self.assertFalse(worker.is_alive())
            with patch.dict(os.environ, {'FAULT_STATE_DIR': directory}):
                restarted = app.create_server('127.0.0.1', 0, background=False)
            try:
                self.assertEqual(restarted.runtime.chaos()['active'], [])
            finally:
                restarted.runtime.stop.set()
                restarted.server_close()

    def test_startup_fault_persistence_failure_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'not-a-directory'
            path.write_text('unchanged')
            with patch.dict(os.environ, {'FAULT_STATE_DIR': str(path), 'FAULT_NOT_READY': '1'}):
                with self.assertRaises(app.RequestError) as raised:
                    app.create_server('127.0.0.1', 0, background=False)
            self.assertEqual(raised.exception.status, 503)
            self.assertIn('"reason": "startup"', self.output.getvalue())

    def test_set_failure_preserves_state_timers_allocations_and_workers(self):
        runtime = self.server.runtime
        with tempfile.TemporaryDirectory() as directory:
            runtime.state_dir = directory
            runtime.set_fault({'fault': 'not_ready'})
            runtime.next_job, runtime.crash_at = 123.0, 456.0
            allocation = bytearray(16)
            runtime.allocations.append(allocation)
            path = Path(directory) / 'faults.json'
            before, saved = deepcopy(runtime.faults), path.read_bytes()
            holders, spinners = runtime.holder_stop, runtime.spin_stop
            for fault in ('not_ready', 'crash', 'background_job_failure', 'memory_leak',
                          'worker_exhaustion', 'cpu_spin'):
                with self.subTest(fault=fault), patch('runtime.os.replace', side_effect=OSError('injected')):
                    status, body, _ = self.request('/api/chaos', 'POST', {'fault': fault})
                    self.assertEqual(status, 503)
                    self.assertIn('error', body)
                    self.assertEqual(runtime.faults, before)
                    self.assertEqual((runtime.next_job, runtime.crash_at), (123.0, 456.0))
                    self.assertIs(runtime.allocations[0], allocation)
                    self.assertIs(runtime.holder_stop, holders)
                    self.assertIs(runtime.spin_stop, spinners)
                    self.assertEqual(runtime.holders, [])
                    self.assertEqual(runtime.spinners, [])
                    self.assertEqual(path.read_bytes(), saved)
            self.assertIn('chaos_state_write_failed', self.output.getvalue())

    def test_regular_file_state_directory_rejects_set_without_activation(self):
        runtime = self.server.runtime
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'not-a-directory'
            path.write_text('unchanged')
            runtime.state_dir = str(path)
            status, _, _ = self.chaos('not_ready')
            self.assertEqual(status, 503)
            self.assertFalse(runtime.enabled('not_ready'))
            self.assertEqual(path.read_text(), 'unchanged')
            with patch.dict(os.environ, {'FAULT_STATE_DIR': str(path)}):
                restarted = app.create_server('127.0.0.1', 0, background=False)
            try:
                self.assertFalse(restarted.runtime.enabled('not_ready'))
            finally:
                restarted.runtime.stop.set()
                restarted.server_close()
