"""최초·반복 감지 근거의 정제 이력 회귀. DB 연결 없이 실제 ingest를 실행한다."""
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from app.config import AppConfig
from app.incidents import Signal, ingest
from app.models import AuditLog, Incident


class _Rows:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class _Session:
    def __init__(self, incident=None):
        self.incident = incident
        self.rows = []

    def execute(self, *args, **kwargs):
        return None

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def get(self, model, key):
        return None

    def scalars(self, *args, **kwargs):
        return _Rows(self.incident)

    def add(self, row):
        if isinstance(row, Incident) and row.id is None:
            row.id = 21
        self.rows.append(row)

    def flush(self):
        return None


class SignalProvenanceTests(unittest.TestCase):
    def test_same_reason_repeat_keeps_its_own_observed_sample(self):
        now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
        inc = Incident(id=21, namespace='dev', workload='payment', status='open',
                       fingerprint='dev/payment', signal_count=1,
                       signal_reasons=['ApplicationException'], created_at=now)
        session = _Session(inc)
        signal = Signal('dev', 'Deployment', 'payment', 'ApplicationException',
                        'app_log', {'sample': '합성 결제 예외 원문', 'timestamp': 1793534399})
        _, outcome = ingest(session, AppConfig(), signal, now=now)
        observations = [r for r in session.rows if isinstance(r, AuditLog)
                        and r.event == 'signal_observed']
        self.assertEqual(outcome, 'merged')
        self.assertEqual(len(observations), 1)
        snapshot = observations[0].detail['signal']
        self.assertEqual(snapshot['detail']['sample'], '합성 결제 예외 원문')
        self.assertEqual(snapshot['received_at'], now.isoformat())
        self.assertEqual(snapshot['source'], 'app_log')
        self.assertEqual(len(snapshot['sha256']), 64)

    def test_created_and_repeated_samples_are_append_only_sanitized_and_hashed(self):
        from copy import deepcopy
        from hashlib import sha256
        import json
        from app.redact import redact_obj
        now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
        session = _Session()
        detail = {'sample': '합성 예외', 'authorization': 'synthetic-private',
                  'nested': {'password': 'synthetic-password'}, 'timestamp': 123}
        signal = Signal('dev', 'Deployment', 'payment', 'ApplicationException', 'app_log', detail)
        inc, outcome = ingest(session, AppConfig(), signal, now=now)
        self.assertEqual(outcome, 'created')
        observations = [r for r in session.rows if isinstance(r, AuditLog) and r.event == 'signal_observed']
        self.assertEqual(len(observations), 1)
        original = deepcopy(observations[0].detail)
        session.incident = inc
        signal.detail = {'sample': '다른 사유 표본'}
        signal.reason = 'CrashLoopBackOff'
        ingest(session, AppConfig(), signal, now=now)
        signal.detail = {'sample': '같은 사유의 다음 표본'}
        ingest(session, AppConfig(), signal, now=now)
        observations = [r for r in session.rows if isinstance(r, AuditLog) and r.event == 'signal_observed']
        self.assertEqual(len(observations), 3)
        self.assertEqual(observations[0].detail, original)
        self.assertEqual(original['signal']['detail'], redact_obj(detail))
        self.assertNotIn('synthetic-private', json.dumps(original))
        self.assertNotIn('synthetic-password', json.dumps(original))
        expected = sha256(json.dumps(redact_obj(detail), sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(original['signal']['sha256'], expected)
        self.assertEqual(observations[2].detail['signal']['detail']['sample'], '같은 사유의 다음 표본')
        self.assertEqual(inc.signal_count, 3)


if __name__ == '__main__':
    unittest.main()
