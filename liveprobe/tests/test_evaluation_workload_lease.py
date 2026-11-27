from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import io
import json
from threading import Barrier

import pytest

from app import db, evalguard, evalrun
from app.config import AppConfig, Settings
from app.engine import Engine
from app.incidents import Signal
from app.models import Incident, Setting
from isolated_sqlite import isolated_k8s_db


def test_expiry_and_owner_fence_protect_a_replacement_lease(tmp_path, monkeypatch):
    stamp = datetime.now(timezone.utc)
    monkeypatch.setattr(evalguard, '_now', lambda: stamp)
    with isolated_k8s_db(tmp_path):
        old = evalguard.acquire_workload_lease('dev', 'inventory', 'first', 20)
        with pytest.raises(evalguard.WorkloadLeaseConflict):
            evalguard.acquire_workload_lease('dev', 'inventory', 'first', 20)
        assert not evalguard.release_workload_lease('dev', 'inventory', 'other', old['token'])
        assert not evalguard.release_workload_lease('dev', 'inventory', 'first', 'z' * 43)
        with db.session() as session:
            assert evalguard.workload_lease_active(session, 'dev', 'inventory')
            assert not evalguard.workload_lease_active(session, 'dev', 'order')
            assert not evalguard.workload_lease_active(session, 'prd', 'inventory')
        stamp += timedelta(seconds=20)
        assert not evalguard.workload_lease_owned('dev', 'inventory', 'first', old['token'])
        new = evalguard.acquire_workload_lease('dev', 'inventory', 'first', 20)
        assert not evalguard.release_workload_lease('dev', 'inventory', 'first', old['token'])
        assert evalguard.workload_lease_owned('dev', 'inventory', 'first', new['token'])
        evalguard.set_lock('legacy', 60)
        assert not evalguard.workload_lease_owned('dev', 'inventory', 'first', new['token'])
        assert evalguard.release_workload_lease('dev', 'inventory', 'first', new['token'])
        assert not evalguard.workload_lease_owned('dev', 'inventory', 'first', new['token'])
        with db.session() as session:
            assert session.get(Setting, evalguard.KEY).value['owner'] == 'legacy'


def test_acquire_serializes_two_concurrent_owners_and_rejects_an_open_incident(tmp_path):
    simultaneous = Barrier(2, timeout=5)

    def reserve(owner):
        simultaneous.wait()
        try:
            return evalguard.acquire_workload_lease('dev', 'inventory', owner)
        except evalguard.WorkloadLeaseConflict:
            return None

    with isolated_k8s_db(tmp_path), ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(reserve, ('first', 'second')))
        winner, = [receipt for receipt in receipts if receipt]
        assert evalguard.workload_lease_owned('dev', 'inventory', winner['owner'], winner['token'])

        with db.session() as session:
            session.add(Incident(namespace='dev', workload='order', fingerprint='dev/order'))
        with pytest.raises(evalguard.WorkloadLeaseConflict, match='active owner'):
            evalguard.acquire_workload_lease('dev', 'order', 'third')


def test_renew_only_extends_the_current_owner_and_never_revives_expiry(tmp_path, monkeypatch):
    stamp = datetime.now(timezone.utc)
    monkeypatch.setattr(evalguard, '_now', lambda: stamp)
    with isolated_k8s_db(tmp_path):
        lease = evalguard.acquire_workload_lease('dev', 'inventory', 'current', 60)
        original = lease['expires_at']
        stamp += timedelta(seconds=30)
        assert evalguard.renew_workload_lease('dev', 'inventory', 'current', lease['token'], 10) == original
        assert evalguard.renew_workload_lease('dev', 'inventory', 'other', lease['token'], 90) is None
        renewed = evalguard.renew_workload_lease('dev', 'inventory', 'current', lease['token'], 90)
        assert datetime.fromisoformat(renewed) == stamp + timedelta(seconds=90)
        stamp += timedelta(seconds=90)
        assert evalguard.renew_workload_lease('dev', 'inventory', 'current', lease['token'], 90) is None


def test_engine_still_accepts_signals_for_other_workloads_and_namespaces(tmp_path, monkeypatch):
    monkeypatch.setattr(evalguard, '_cache', (0.0, False))
    with isolated_k8s_db(tmp_path):
        lease = evalguard.acquire_workload_lease('dev', 'inventory', 'comparison')
        with db.session() as session:
            others = [Incident(namespace=ns, workload=workload, fingerprint=f'{ns}/{workload}', signal_count=1)
                      for ns, workload in (('dev', 'order'), ('prd', 'inventory'))]
            session.add_all(others)
            session.flush()
            ids = [incident.id for incident in others]
        engine = Engine(AppConfig(), Settings(background=False), None, None, None)
        for ns, workload in (('dev', 'inventory'), ('dev', 'order'), ('prd', 'inventory')):
            engine.on_signal(Signal(ns, 'Deployment', workload, 'ErrImagePull', 'pod_watch'))
        with db.session() as session:
            assert all(session.get(Incident, iid).signal_count == 2 for iid in ids)
            from sqlalchemy import select
            assert session.scalar(select(Incident).where(Incident.fingerprint == 'dev/inventory')) is None
        assert evalguard.release_workload_lease('dev', 'inventory', 'comparison', lease['token'])


def test_cli_requires_explicit_scope_and_preserves_legacy_protocol(tmp_path, monkeypatch, capsys):
    db_initializations = []
    monkeypatch.setattr(db, 'init_db', db_initializations.append)
    monkeypatch.setattr(evalguard, '_cache', (0.0, False))
    with isolated_k8s_db(tmp_path):
        for argv in (['lock', '--namespace', 'dev'], ['unlock', '--token-stdin'], ['lock-status'],
                     ['lock', '--namespace', 'prd'], ['lock', '--work', 'inventory']):
            with pytest.raises(SystemExit) as failure:
                evalrun.main(argv)
            assert failure.value.code == 2
        assert db_initializations == []
        capsys.readouterr()
        assert evalrun.main(['lock', '--ttl', '60']) == 0
        assert capsys.readouterr().out == 'evaluation lock locked\n'
        assert evalguard.evaluation_lock_active()
        scope = ['--namespace', 'dev', '--workload', 'inventory', '--owner', 'case-a']
        assert evalrun.main(['lock', *scope]) == 1
        assert 'legacy evaluation campaign' in json.loads(capsys.readouterr().out)['error']
        assert evalrun.main(['unlock']) == 0
        assert capsys.readouterr().out == 'evaluation lock unlocked\n'
        assert evalrun.main(['lock', *scope, '--ttl', '10']) == 0
        receipt = json.loads(capsys.readouterr().out)
        monkeypatch.setattr('sys.stdin', io.StringIO(receipt['token'] + '\n'))
        assert evalrun.main(['lock-status', *scope, '--token-stdin']) == 0
        assert json.loads(capsys.readouterr().out) == {'active': True}
        with db.session() as session:
            row = session.get(Setting, evalguard.validate_workload_lease('dev', 'inventory', 'case-a'))
            assert receipt['token'] not in json.dumps(row.value)
            assert not session.get(Setting, evalguard.KEY).value['owner']
        monkeypatch.setattr('sys.stdin', io.StringIO(receipt['token'] + '\n'))
        assert evalrun.main(['unlock', *scope, '--token-stdin']) == 0
        assert json.loads(capsys.readouterr().out) == {'released': True}
