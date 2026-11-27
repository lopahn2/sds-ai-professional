from sqlalchemy import select

from app import db
from app.app_errors import AppErrorWatcher
from app.config import AppConfig, Settings
from app.engine import Engine
from app.incidents import Signal, ingest
from app.main import alert_signal
from app.models import Incident, WorkflowRun
from app.overview import assemble
from isolated_sqlite import isolated_k8s_db


def test_dev_only_suppresses_new_prd_signals_and_does_not_resume_old_prd_work(tmp_path):
    cfg = AppConfig(target_namespaces=['dev'], historical_namespaces=['stg', 'prd'])
    assert cfg.history_namespaces == ['dev', 'stg', 'prd']
    with isolated_k8s_db(tmp_path):
        engine = Engine(cfg, Settings(background=False), None, None, None)
        with db.session() as session:
            dev = Incident(namespace='dev', workload='order', fingerprint='dev/order')
            prd = Incident(namespace='prd', workload='order', fingerprint='prd/order', status='analyzing')
            session.add_all([dev, prd])
            session.flush()
            session.add(WorkflowRun(incident_id=prd.id, status='running', phase='analysis', snapshot={}))
            dev_id, prd_id = dev.id, prd.id
            signal = Signal('prd', 'Deployment', 'order', 'NoEndpoints', 'endpoint_poller')
            assert ingest(session, cfg, signal) == (None, 'suppressed')
        engine.recover_on_startup()
        engine.schedule(prd_id)
        assert set(engine._scheduled) == {dev_id}
        assert engine._process(prd_id) is False
        with db.session() as session:
            assert session.get(Incident, prd_id).status == 'analyzing'
            assert session.scalar(select(WorkflowRun.status).where(WorkflowRun.incident_id == prd_id)) == 'running'


def test_dev_only_overview_retains_shared_warnings_and_excludes_prd():
    cfg = AppConfig(target_namespaces=['dev'], historical_namespaces=['stg', 'prd'], app_error_threshold=1)
    emitted = []
    watcher = AppErrorWatcher(cfg, Settings(), emitted.append)
    event = lambda ns: {'metadata': {'namespace': ns}, 'reason': 'Unhealthy'}
    alert = lambda ns: {'labels': {'namespace': ns, 'app': 'order', 'alertname': 'TargetHigh5xxRate'}}
    assert alert_signal(alert('prd'), cfg, None) is None
    assert alert_signal(alert('dev'), cfg, None).namespace == 'dev'
    assert watcher.process([{'namespace': ns, 'app': 'order', 'timestamp': 100, 'exception': 'ExampleError',
                             'message': 'failure', 'frames': [], 'sample': 'ExampleError: failure'}
                            for ns in ('prd', 'dev')], now=100) == 1
    assert [signal.namespace for signal in emitted] == ['dev']

    from app.flows import FlowRec, FlowStore
    import time

    flow = FlowStore()
    flow.add([FlowRec(time.time(), 'prd/pod/stopped', 'dev/pod/client', 'DROPPED', 'TCP', 8080)])
    inventory = {'namespaces': [{'metadata': {'name': ns}} for ns in ('dev', 'prd', 'kube-system')],
                 'deployments': [{'metadata': {'name': 'order', 'namespace': ns, 'labels': {'app.kubernetes.io/part-of': 'samsung-trade'}},
                                  'spec': {'replicas': 0}} for ns in ('prd', 'dev')]}
    old = [{'id': 1, 'namespace': 'prd', 'workload': 'order', 'status': 'open'}]
    result = assemble(inventory, {}, cfg=cfg, store=flow, events=[event('prd'), event('kube-system'), event('dev')], incidents=old)
    assert [row['name'] for row in result['cluster']['environments']] == ['dev']
    assert {row['name'] for row in result['cluster']['namespaces']} == {'dev', 'kube-system'}
    assert {row['namespace'] for row in result['events']} == {'dev', 'kube-system'}
    assert result['incidents']['open'] == result['topology']['incidents'] == []
    assert not any(node['id'].startswith('prd/') for node in result['topology']['nodes'])

