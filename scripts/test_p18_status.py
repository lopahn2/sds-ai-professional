"""p18-status.py 빠른 상태 조회 회귀. 네트워크·kubectl·DB를 쓰지 않는다."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


def module():
    path = Path(__file__).with_name('p18-status.py')
    spec = importlib.util.spec_from_file_location('p18_status', path)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class StatusHelpers(unittest.TestCase):
    def setUp(self):
        self.m = module()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_tail_reads_only_end_of_large_log(self):
        log = self.root / 'big.log'
        with log.open('w') as handle:
            for i in range(400_000):
                handle.write(f'line {i} ' + 'x' * 20 + '\n')
            handle.write('마지막 줄 결과\n')
        started = time.monotonic()
        rows = self.m.tail_lines(log, 2)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(rows[-1], '마지막 줄 결과')
        self.assertTrue(rows[0].startswith('line 399999'))

    def test_tail_drops_partial_first_line(self):
        log = self.root / 'a.log'
        log.write_text('A' * 5000 + '\nok\n')
        self.assertEqual(self.m.tail_lines(log, 5), ['ok'])

    def test_redacts_connection_strings_and_secret_values(self):
        text = self.m.redact('dsn=postgresql+psycopg://u:pw@127.0.0.1:55432/db password=abc token: t1')
        self.assertNotIn('pw@', text)
        self.assertNotIn('abc', text)
        self.assertNotIn('t1', text)

    def write(self, folder, name, data):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(json.dumps(data))

    def test_run_states_from_receipts_and_pid(self):
        done = self.root / 'done'
        self.write(done, 'launch.json', {'started_at': 's', 'parent_pid': os.getpid()})
        self.write(done, 'child-return.json', {'actual_exit': 1, 'finished_at': 'f'})
        record = self.m.run_record(done)
        self.assertEqual((record['state'], record['actual_exit']), ('종료', 1))

        running = self.root / 'running'
        self.write(running, 'launch.json', {'started_at': 's', 'parent_pid': os.getpid()})
        (running / 'pytest.log').write_text('progress 50%\n')
        record = self.m.run_record(running)
        self.assertEqual(record['state'], '실행중')
        self.assertEqual(record['log_tail'], ['progress 50%'])

        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        orphan = self.root / 'orphan'
        self.write(orphan, 'launch.json', {'started_at': 's', 'parent_pid': dead.pid})
        self.assertEqual(self.m.run_record(orphan)['state'], '종료영수증없음')

    def test_failed_verdict_is_not_reported_as_pass(self):
        folder = self.root / 'pg'
        self.write(folder, 'verdict.json', {'실제종료코드': 2, '시험수': 23, '실패수': 1})
        record = self.m.run_record(folder)
        self.assertEqual((record['state'], record['actual_exit'], record['failures']), ('종료', 2, 1))

    def test_korean_review_verdict_is_visible_without_inventing_exit(self):
        for field in ('판정', 'gate', 'verdict'):
            for verdict in ('PASS', 'BLOCK', 'HOLD'):
                with self.subTest(field=field, verdict=verdict):
                    folder = self.root / (field + verdict)
                    date_field = '기록시각' if field == '판정' else 'recorded_at_utc'
                    self.write(folder, 'verdict.json', {field: verdict, '검사항목수': 12,
                               date_field: '2026-10-27T12:36:05+00:00'})
                    record = self.m.run_record(folder)
                    self.assertEqual(record['state'], '판정기록')
                    self.assertEqual(record['verdict'], verdict)
                    self.assertEqual(record['review_checks'], 12)
                    self.assertNotIn('actual_exit', record)
                    rendered = self.m.render({'checked_at': '점검시각', 'elapsed_s': {}, 'runs': [record]})
                    self.assertIn(verdict, rendered)
                    self.assertIn("'review_checks': 12", rendered)

    def test_live_pid_with_review_date_is_not_declared_exited(self):
        folder = self.root / 'review_running'
        self.write(folder, 'launch.json', {'parent_pid': os.getpid()})
        self.write(folder, 'verdict.json', {'gate': 'PASS', 'recorded_at_utc': '2026-10-27T12:36:05+00:00'})
        record = self.m.run_record(folder)
        self.assertEqual(record['state'], '실행중')
        self.assertNotIn('actual_exit', record)

    def test_recent_runs_orders_by_receipt_time_and_limits(self):
        for index in range(4):
            folder = self.root / f'r{index}'
            self.write(folder, 'receipt.json', {'actual_exit': 0, 'finished_at': str(index)})
            os.utime(folder / 'receipt.json', (1000 + index, 1000 + index))
        runs = self.m.recent_runs(self.root, 2)
        self.assertEqual([Path(r['path']).name for r in runs], ['r3', 'r2'])

    def test_runner_lock_detects_holder(self):
        lock = self.root / '.lock'
        self.assertIn('잠금파일없음', self.m.runner_lock(lock))
        with lock.open('w') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIn('잠금보유', self.m.runner_lock(lock))
        self.assertIn('잠금없음', self.m.runner_lock(lock))

    def test_offline_collect_finishes_fast(self):
        args = type('A', (), {'root': str(self.root), 'runs': 3, 'timeout': 1.0,
                              'no_api': True, 'no_kube': True})()
        started = time.monotonic()
        report = self.m.collect(args)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn('campaign', report)
        self.assertIn('runs', report)

    def test_render_survives_unreachable_api_and_kube(self):
        error = '확인불가:URLError'
        report = {'checked_at': '2026-10-31T00:00:00+00:00', 'elapsed_s': {}, 'campaign': {},
                  'api': {'counts': {'open': error}, 'readyz': error, 'analyzing_items': error},
                  'kube': {'error': '연결 거부'}, 'processes': [], 'runs': []}
        text = self.m.render(report)
        self.assertIn('analyzing 목록 확인불가', text)
        self.assertIn(error, text)


if __name__ == '__main__':
    unittest.main()
