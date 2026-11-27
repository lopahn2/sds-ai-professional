"""Premature router handoff correction; all DB tests use the UUID-isolated fixture."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from conftest import needs_db
from test_api import client  # noqa: F401
from app import db
from app.config import AppConfig, Settings
from app.engine import Engine
from app.evidence import Bundle
from app.models import Action, AuditLog, Evidence, Incident, RoutingDecision, WorkflowRun
from app.observed_recovery import recovery_proof
from app.router import decide, extract_features


def healthy(age=2):
    stamp = datetime.now(timezone.utc) - timedelta(seconds=age)
    b = Bundle('dev', 'delivery')
    b.deployment = {'replicas': 1, 'ready_replicas': 1, 'available_replicas': 1, 'unavailable_replicas': 0,
                    'images': ['img'], 'revisions': [], 'current_revision': 1}
    b.pods = [{'name': 'delivery-pod', 'uid': 'stable-pod-uid', 'containers': [{'name': 'delivery', 'ready': True, 'restart_count': 21,
                'waiting_reason': None, 'last_terminated_at': (stamp - timedelta(minutes=2)).isoformat(),
                'last_terminated_reason': 'Error', 'last_exit_code': 1}]}]
    b.service = {'ready_endpoints': 1, 'matching_pods': ['delivery-pod'], 'selector': {'app': 'delivery'}}
    for source, payload in [('deployment', b.deployment), ('pod_status', b.pods), ('service', b.service),
                            ('endpoints', {'ready_addresses': 1}), ('k8s_event', [])]:
        b.add(source, 'dev/Deployment/delivery', deepcopy(payload), collected_at=stamp)
    return b


def pair():
    first = healthy(2)
    second = deepcopy(first)
    for it in second.items:
        it.collected_at += timedelta(seconds=1)
    return first, second


def http_successes(at, count):
    end = at.timestamp()
    return {'2xx': count, 'success_scrape_intervals': [[end - 30, end - 15], [end - 15, end]]}


def engine(monkeypatch, bundles):
    import app.engine as module
    pending = iter(bundles)
    monkeypatch.setattr(module, 'collect', lambda *args, **kwargs: deepcopy(next(pending)))
    cfg = AppConfig(recovery={'observation_interval_s': 0.1})
    e = Engine(cfg, Settings(), SimpleNamespace(), SimpleNamespace(name='test'), None)
    monkeypatch.setattr(e, '_sleep', lambda seconds: None)
    return e


def seed(status='open', signals=None, legacy=False):
    with db.session() as s:
        inc = Incident(fingerprint='dev/delivery', namespace='dev', workload='delivery', status=status,
                       signal_reasons=signals or ['CrashLoopBackOff'], title='prior crash',
                       status_reason='original router handoff', handoff={'summary': 'original reason'})
        s.add(inc)
        s.flush()
        if legacy:
            s.add(RoutingDecision(incident_id=inc.id, level='COMPLEX', workflow='complex_group',
                features={**extract_features(healthy()), 'evidence_state': 'currently_healthy'},
                rationale='original router handoff', decided_by='evidence_state', policy={'handoff_only': True}))
            s.add(WorkflowRun(incident_id=inc.id, status='succeeded', phase='handed_off', snapshot={'original': True}))
        return inc.id


@needs_db
@pytest.mark.parametrize('late_change', [None, 'restarted', 'new_network_signal'])
def test_recent_oom_rechecks_before_router_and_keeps_diagnosis_if_conditions_change(db_url, monkeypatch, late_change):
    first, second = pair()
    signal = 'TargetWorkloadOOMKilled'
    ended = (first.items[0].collected_at - timedelta(seconds=30)).isoformat()
    for bundle in (first, second):
        bundle.pods[0]['containers'][0].update(last_terminated_reason='OOMKilled', last_exit_code=137,
                                             last_terminated_at=ended)
        bundle.events = [{'reason': 'OOMKilling', 'type': 'Warning', 'last': ended}]
    if late_change == 'restarted':
        second.pods[0]['containers'][0]['restart_count'] += 1
    runner = engine(monkeypatch, [first, second])
    iid = seed(signals=[signal])
    if late_change == 'new_network_signal':
        def merge_signal(_):
            with db.session() as s:
                s.get(Incident, iid).signal_reasons = [signal, 'NetworkDrop']
        monkeypatch.setattr(runner, '_sleep', merge_signal)
    calls, decisions = [], []

    def classify(*args):
        calls.append(args)
        return {'level': 'NORMAL', 'rationale': 'Check the continuing OOM symptom', 'confidence': 0.99}

    monkeypatch.setattr(runner, '_classify', classify)
    monkeypatch.setattr(runner, '_run_decision', lambda _iid, decision, *_: decisions.append(decision))
    runner.process(iid)
    with db.session() as s:
        incident = s.get(Incident, iid)
        [route] = list(s.scalars(select(RoutingDecision).where(RoutingDecision.incident_id == iid)))
        assert s.scalar(select(Action.id).where(Action.incident_id == iid)) is None
        if late_change is None:
            assert not calls and not decisions
            assert incident.status == 'recovery_review'
            assert route.decided_by == 'evidence_state'
            assert route.policy['analysis_skipped'] == 'observed_oom_recovery'
            assert 'selected_model' not in route.policy
            assert incident.handoff['observed_recovery']['ai_remediation'] is False
        else:
            assert len(decisions) == 1
            assert 'analysis_skipped' not in route.policy and 'observed_recovery' not in (incident.handoff or {})
            if late_change == 'restarted':
                assert len(calls) == 1
            else:
                assert not calls and route.level == 'HIGH_RISK'


@needs_db
def test_proxy_listener_webhook_feeds_bound_evidence_to_group_and_requires_route_proof(client, monkeypatch):
    from app import main
    from app.actions import typed_policy
    from app.evidence import compact_summary
    from app.incidents import PROXY_LDS_REJECTED
    from conftest import FakeKube, mk_dep, mk_pod, mk_rs

    first, second = pair()
    kube = FakeKube()
    pod = mk_pod('delivery-pod', 'delivery', 'delivery-rs')
    pod['metadata']['uid'] = first.pods[0]['uid']
    kube.pods = [pod]
    kube.deployments['delivery'] = mk_dep('delivery', 1, 'img')
    kube.replicasets = [mk_rs('delivery-rs', 'delivery', 1, 'img')]
    e = engine(monkeypatch, [first])
    e.kube = kube
    monkeypatch.setitem(main.STATE, 'engine', e)
    monkeypatch.setitem(main.STATE, 'kube', kube)
    monkeypatch.setattr(main, 'get_app_config', lambda: e.cfg)
    monkeypatch.setattr(e, '_classify', lambda *_: {'level': 'SIMPLE', 'rationale': 'old route works', 'confidence': 1})
    calls = []
    monkeypatch.setattr(e, '_run_decision', lambda _iid, decision, bundle, *_: calls.append((decision, bundle)))
    alert = {'status': 'firing', 'labels': {'alertname': PROXY_LDS_REJECTED, 'namespace': 'dev',
                                          'pod': 'delivery-pod', 'pod_uid': first.pods[0]['uid']}}
    response = client.post('/api/hooks/alertmanager', json={'alerts': [alert]},
                           headers={'Authorization': 'Bearer hook-token-abc'})
    assert response.status_code == 200 and response.json() == {'accepted': 1}
    with db.session() as session:
        iid = session.scalar(select(Incident.id))
    assert iid in e._scheduled
    e.process(iid)

    [(decision, bundle)] = calls
    assert decision.workflow == 'complex_group' and decision.features['evidence_state'] == 'fault_observed'
    assert 'Envoy LDS listener update rejected' in e._rag_query(bundle, decision)
    payload = compact_summary(bundle)['E6_metrics']['envoy_listener_rejection']
    assert payload['pod_uid'] == first.pods[0]['uid'] and payload['route_impact_observed'] is None
    assert recovery_proof(first, second, [PROXY_LDS_REJECTED]) is None
    with db.session() as session:
        evidence = session.scalar(select(Evidence).where(Evidence.incident_id == iid, Evidence.source == 'alertmanager'))
        assert (evidence.target, evidence.payload) == ('dev/Pod/delivery-pod', payload)
        assert typed_policy(session, 'COMPLEX', 'rollout_restart', iid) == 'handoff'


@pytest.mark.parametrize('gap', [None, 'loki', 'code_refs'])
def test_healthy_router_requests_recheck_not_handoff(gap):
    b = healthy()
    if gap:
        b.add(gap, 'dev/delivery', error='unavailable')
    d = decide(b, 15)
    assert d.policy.get('recheck_required') and not d.policy.get('handoff_only')
    assert d.level != 'HIGH_RISK'


@pytest.mark.parametrize('kind', ['exception', 'invalid', 'low'])
def test_classifier_bounded_fallback_and_real_risk(kind):
    from test_router_workflows import family1
    calls = []
    def classifier(prompt):
        calls.append(prompt)
        if kind == 'exception':
            raise RuntimeError('timeout')
        return {} if kind == 'invalid' else {'level': 'SIMPLE', 'confidence': 0.1, 'rationale': 'uncertain'}
    d = decide(family1(), 15, classifier)
    assert len(calls) == 2 and d.level == 'NORMAL' and d.decided_by == 'rules_fallback'
    assert not d.policy.get('handoff_only') and len(d.features['classifier_attempts']) == 2
    risk = decide(family1(), 15, classifier, signal_reasons=['NetworkDrop'])
    assert risk.level == 'HIGH_RISK'


def test_classifier_retry_can_succeed():
    from test_router_workflows import family1
    outcomes = iter([{}, {'level': 'NORMAL', 'confidence': 0.9, 'rationale': 'verified'}])
    assert decide(family1(), 15, lambda _: next(outcomes)).level == 'NORMAL'


def test_old_restarts_and_warnings_are_not_current_failure():
    first, second = pair()
    second.events = [{'type': 'Warning', 'reason': 'Unhealthy', 'last':
                      (first.items[0].collected_at - timedelta(minutes=1)).isoformat()}]
    proof = recovery_proof(first, second, ['CrashLoopBackOff'])
    assert proof and proof['cause_confirmed'] is False and proof['ai_remediation'] is False
    assert len(proof['observations']) == 2


@pytest.mark.parametrize('flaw', ['restart', 'termination', 'unready', 'endpoint', 'critical', 'unknown',
                                   'fresh_error', 'unstamped_error', 'fresh_warning', 'same_time', 'different_target',
                                   'wrong_selector_target', 'replaced_pod', 'old_baseline', 'nan_metric'])
def test_health_proof_rejects_active_unknown_or_incomplete(flaw):
    first, second = pair()
    signals = ['CrashLoopBackOff']
    c = second.pods[0]['containers'][0]
    if flaw == 'restart': c['restart_count'] += 1
    if flaw == 'termination': c['last_terminated_at'] = datetime.now(timezone.utc).isoformat()
    if flaw == 'unready': c['ready'] = False
    if flaw == 'endpoint': second.service['ready_endpoints'] = 0
    if flaw == 'critical': second.items[0].error = 'timeout'
    if flaw == 'unknown': signals.append('UnmappedBusinessFailure')
    if flaw == 'fresh_error': second.logs = [f'[delivery] {datetime.now(timezone.utc).isoformat()} ERROR current']
    if flaw == 'unstamped_error': second.logs = ['[delivery] ERROR current']
    if flaw == 'fresh_warning': second.events = [{'type': 'Warning', 'last': datetime.now(timezone.utc).isoformat()}]
    if flaw == 'same_time': second.items = deepcopy(first.items)
    if flaw == 'different_target': second.namespace = 'prd'
    if flaw == 'wrong_selector_target': second.service['matching_pods'] = ['other-workload-pod']
    if flaw == 'replaced_pod': second.pods[0]['uid'] = 'replacement-pod-uid'
    if flaw == 'old_baseline':
        for item in first.items: item.collected_at -= timedelta(hours=1)
    if flaw == 'nan_metric': second.metrics['http_5xx_rate_per_s'] = {'delivery': float('nan')}
    assert recovery_proof(first, second, signals) is None


def test_latency_needs_observed_metrics_not_readiness():
    first, second = pair()
    assert recovery_proof(first, second, ['TargetHighLatency']) is None
    for b in (first, second):
        b.metrics = {'mean_latency_ms_1m': {'delivery': 12}}
        b.add('prometheus', 'dev/delivery', b.metrics)
    assert recovery_proof(first, second, ['TargetHighLatency'])
    second.metrics['mean_latency_ms_1m']['delivery'] = 900
    assert recovery_proof(first, second, ['TargetHighLatency']) is None


@pytest.mark.parametrize('defect', [None, 'readiness_only', 'different_route', 'continuing_503',
                                    'unidentified_route', 'stale_metrics', 'same_request', 'overlapping_windows'])
def test_http_recovery_requires_success_on_original_business_route(defect):
    from app import business_http

    first, second = pair()
    affected = 'GET /api/delivery/{id}'
    unrelated = 'GET /api/status'
    for bundle in (first, second):
        at = bundle.items[0].collected_at
        counts = {affected: http_successes(at, 4), unrelated: http_successes(at, 50)}
        bundle.metrics = {'http_5xx_rate_per_s': {'delivery': 0}, 'http_rate_per_s': {'delivery': 100},
                          business_http.METRIC: counts}
        bundle.add('prometheus', 'dev/delivery', bundle.metrics, collected_at=bundle.items[0].collected_at)
    required = frozenset({affected})
    if defect == 'readiness_only':
        first.items[-1].payload[business_http.METRIC].clear()
    elif defect == 'different_route':
        second.items[-1].payload[business_http.METRIC].pop(affected)
    elif defect == 'continuing_503':
        second.items[-1].payload[business_http.METRIC][affected]['5xx'] = 0.001
    elif defect == 'unidentified_route':
        required = frozenset()
    elif defect == 'stale_metrics':
        second.items[-1].collected_at -= timedelta(seconds=50)
    elif defect == 'same_request':
        for bundle in (first, second):
            bundle.items[-1].payload[business_http.METRIC][affected]['success_scrape_intervals'] = [
                http_successes(first.items[0].collected_at, 1)['success_scrape_intervals'][0]]
    elif defect == 'overlapping_windows':
        intervals = second.items[-1].payload[business_http.METRIC][affected]['success_scrape_intervals']
        intervals[1][0] = intervals[0][1] - 1
    proof = recovery_proof(first, second, [business_http.ALERT], required_http_routes=required)
    if defect is None:
        assert proof['observations'][-1]['business_http_routes'][affected] == http_successes(second.items[-1].collected_at, 4)
    else:
        assert proof is None


@pytest.mark.parametrize('mode', ['disabled', 'success', 'missing', 'malformed', 'limit'])
def test_business_http_collection_preserves_general_metrics(monkeypatch, mode):
    from time import monotonic
    from app import business_http, evidence

    q = business_http.sample_query('dev', 'delivery')
    stamp = int(datetime.now(timezone.utc).timestamp())
    route = {'metric': {'method': 'GET', 'uri': '/api/delivery/{id}', 'status': '200'},
             'values': [[stamp - 30, '1'], [stamp - 15, '3'], [stamp, '5']]}
    called = []

    def query(_, expression, *, limit=None):
        if expression == q:
            called.append(expression)
            assert limit == business_http.MAX_SERIES + 1
            if mode == 'missing':
                raise TimeoutError('monitor disconnected')
            if mode == 'limit':
                return [route] * limit
            return [route, route] if mode == 'malformed' else [route]
        return [{'metric': {'app': 'delivery'}, 'value': [100, '1']}] if 'http_server_requests' in expression else []

    monkeypatch.setattr(evidence, '_prom', query)
    b = evidence._collect_prom(None, 'dev', 'delivery', ['delivery'], monotonic() + 10,
                              include_business_http=mode != 'disabled')
    assert called == ([] if mode == 'disabled' else [q])
    assert b.metrics['http_rate_per_s'] == {'delivery': 1.0}
    assert b.items[0].error is None
    if mode == 'disabled':
        assert business_http.METRIC not in b.metrics
    else:
        assert b.metrics[business_http.METRIC] == ({} if mode != 'success' else {
            'GET /api/delivery/{id}': {'2xx': 4.0, 'success_scrape_intervals': [(stamp - 30, stamp - 15), (stamp - 15, stamp)]}})


def test_http_counter_deltas_exclude_resets_and_late_error_series():
    from app import business_http

    labels = {'method': 'GET', 'uri': '/api/delivery/{id}'}
    route = 'GET /api/delivery/{id}'
    success = {'metric': {**labels, 'status': '200'}, 'values': [[50, 1], [65, 1], [80, 2], [95, 3]]}
    error = {'metric': {**labels, 'status': '503'}, 'values': [[80, 1], [95, 1]]}
    readings = business_http.observed_samples([success, error], 100)
    assert readings[route] == {'2xx': 2, '5xx': 0, 'success_scrape_intervals': [(80, 95)]}
    assert business_http.confirmed_successes(readings[route], 100) == 1
    error['values'] = [[80, 0], [95, 1]]
    assert business_http.observed_samples([success, error], 100)[route]['5xx'] == 1
    success['values'][-1][1] = 0
    assert business_http.observed_samples([success], 100) == {}


@pytest.mark.parametrize("defect", [None, "unidentified_route", "other_route", "same_sample",
                                    "stale_sample", "still_slow", "monitor_unavailable"])
def test_route_latency_recovery_needs_two_new_samples_of_the_original_route(defect):
    from app.route_latency import ALERT, METRIC

    first, second = pair()
    route = "GET /api/delivery/{id}"
    for b in (first, second):
        at = b.items[0].collected_at
        b.metrics = {METRIC: {route: {"at": at.timestamp(), "slow_requests": 0.0}}}
        b.add("prometheus", "dev/delivery", deepcopy(b.metrics), collected_at=at)
    required = frozenset({route})
    if defect == "unidentified_route":
        required = frozenset()
    elif defect == "other_route":
        second.metrics[METRIC]["GET /api/unrelated"] = second.metrics[METRIC].pop(route)
    elif defect == "same_sample":
        second.metrics[METRIC][route] = first.metrics[METRIC][route]
    elif defect == "stale_sample":
        second.metrics[METRIC][route]["at"] -= 46
    elif defect == "still_slow":
        second.metrics[METRIC][route]["slow_requests"] = 1.0
    elif defect == "monitor_unavailable":
        second.items[-1].collected_at -= timedelta(minutes=3)
    proof = recovery_proof(first, second, [ALERT], required_routes=required)
    if defect is None:
        assert proof and list(proof["observations"][1]["business_routes"]) == [route]
    else:
        assert proof is None


def test_route_metrics_keep_the_recording_timestamp_and_reject_wrong_or_ambiguous_input():
    from app.route_latency import observed_samples

    labels = {"namespace": "dev", "app": "delivery", "method": "GET", "uri": "/api/delivery/{id}"}
    row = {"metric": labels, "values": [[90, "5"], [100, "0"]]}
    unrelated = {"metric": {**labels, "app": "catalog"}, "values": [[100, "2"]]}
    fresh = observed_samples([row, unrelated], "dev", "delivery", 105)
    assert fresh == {"GET /api/delivery/{id}": {"at": 100.0, "slow_requests": 0.0}}
    assert observed_samples([row, row], "dev", "delivery", 105) == {}
    assert observed_samples([row], "dev", "delivery", 146) == {}


def pool_pair():
    first, second = pair()
    for b in (first, second):
        stamp = b.items[0].collected_at
        b.metrics = {'db_pool_active': {'delivery': 0}, 'db_pool_max': {'delivery': 2},
                     'db_pool_pending_threads': {'delivery': 0}, 'db_pool_acquire_timeouts_5m': {'delivery': 3.1},
                     'http_5xx_rate_per_s': {'delivery': 0}, 'mean_latency_ms_1m': {'delivery': 23.9}}
        traces = [{'pod': 'delivery-pod', 'instance': 'delivery-pod:8080', 'pool': 'delivery',
                   'samples': [[stamp.timestamp() - 25, 3], [stamp.timestamp() - 10, 3]]}]
        b.add('prometheus', 'dev/delivery', {**b.metrics, '_pool_timeout_traces': traces}, collected_at=stamp)
    return first, second


def test_pool_timeout_history_does_not_hide_observed_clearance():
    first, second = pool_pair()
    proof = recovery_proof(first, second, ['TargetConnectionPoolExhausted'])
    assert proof and proof['cause_confirmed'] is False and proof['ai_remediation'] is False
    assert all(step['pool_timeout_counter_stable'] for step in proof['observations'])
    assert first.metrics['db_pool_acquire_timeouts_5m']['delivery'] == 3.1


@pytest.mark.parametrize('history,active,expected', [(3.1, 0, True), (0, 0, False), (3.1, 2, False)])
def test_collector_fetches_raw_counters_only_for_an_idle_pool_with_timeout_history(monkeypatch, history, active, expected):
    from time import monotonic
    from app import evidence

    metric = {'app': 'delivery', 'pod': 'delivery-pod', 'instance': 'delivery-pod:8080', 'pool': 'delivery'}
    queries = {key: query % {'ns': 'dev'} for key, query in evidence.PROM_QUERIES.items()}
    sample = {'pool_active': active, 'pool_max': 2, 'pool_pending': 0, 'pool_timeouts': history}
    responses = {queries[key]: [{'metric': metric, 'value': [100, value]}] for key, value in sample.items()}
    pool_query = 'hikaricp_connections_timeout_total{namespace="dev",app="delivery"}[1m]'
    responses[pool_query] = [{'metric': metric, 'values': [[70, 3], [85, 3], [100, 3]]}]
    called = []

    def query(settings, text):
        called.append(text)
        return responses.get(text, [])

    monkeypatch.setattr(evidence, '_prom', query)
    result = evidence._collect_prom(None, 'dev', 'delivery', ['delivery'], monotonic() + 10)
    assert (pool_query in called) is expected
    traces = (result.items[0].payload or {}).get('_pool_timeout_traces')
    assert bool(traces) is expected
    if traces:
        assert traces[0]['samples'] == [[85.0, 3.0], [100.0, 3.0]]
    assert result.metrics['db_pool_acquire_timeouts_5m']['delivery'] == history


@pytest.mark.parametrize('fault', ['missing', 'stale', 'duplicate_time', 'increase', 'reset', 'other_pod',
                                   'later_counter', 'pending', 'saturated', 'other_workload_timeouts', '5xx'])
def test_pool_timeout_history_needs_stable_current_owned_evidence(fault):
    first, second = pool_pair()
    item = second.items[-1]
    trace = item.payload['_pool_timeout_traces'][0]
    if fault == 'missing':
        del item.payload['_pool_timeout_traces']
    if fault == 'stale':
        trace['samples'][1][0] -= 50
    if fault == 'duplicate_time':
        trace['samples'][1][0] = trace['samples'][0][0]
    if fault == 'increase':
        trace['samples'][1][1] += 1
    if fault == 'reset':
        trace['samples'][1][1] = 0
    if fault == 'other_pod':
        trace['pod'] = 'unrelated'
    if fault == 'later_counter':
        for sample in trace['samples']:
            sample[1] += 1
    if fault == 'pending':
        second.metrics['db_pool_pending_threads']['delivery'] = 1
    if fault == 'saturated':
        second.metrics['db_pool_active']['delivery'] = 2
    if fault == 'other_workload_timeouts':
        second.metrics['db_pool_acquire_timeouts_5m']['order'] = 1
    if fault == '5xx':
        second.metrics['http_5xx_rate_per_s']['delivery'] = 0.05
    assert recovery_proof(first, second, ['TargetConnectionPoolExhausted']) is None


@needs_db
def test_engine_resolves_observation_and_persists_both(db_url, monkeypatch):
    first, second = pair()
    e = engine(monkeypatch, [first, second])
    monkeypatch.setattr(e, '_run_group', lambda *args: pytest.fail('healthy incident must not run a group'))
    iid = seed()
    e.process(iid)
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert inc.status == 'recovery_review' and inc.handoff['observed_recovery']['ai_remediation'] is False
        assert len(list(s.scalars(select(Evidence).where(Evidence.incident_id == iid)))) == 10
        run = s.scalar(select(WorkflowRun).where(WorkflowRun.incident_id == iid))
        assert run.status == 'succeeded' and len(run.snapshot['observations']) == 1
        assert s.scalar(select(Action.id).where(Action.incident_id == iid)) is None
        from app.narrative import build_narrative
        story = build_narrative(s, iid)
        assert story['incident']['recovery_kind'] == 'observed'
        assert '소명 확인 대기' in story['headline']['stage']['label']
        assert '자동 복구 완료' not in str(story)
        assert '소명 근거' in story['headline']['next']


@needs_db
@pytest.mark.parametrize('signal_kind', ['latency', 'http'])
def test_engine_retains_alerted_routes_and_persists_the_matching_recovery(db_url, monkeypatch, signal_kind):
    from app import business_http, route_latency
    from app.incidents import Signal, ingest

    signals = route_latency if signal_kind == 'latency' else business_http

    first, second = pair()
    methods = ("GET", "POST")
    path = "/api/delivery/{id}"
    expected_routes = frozenset(f"{method} {path}" for method in methods)
    for bundle in (first, second):
        at = bundle.items[0].collected_at
        route_reading = {'at': at.timestamp(), 'slow_requests': 0.0} if signal_kind == 'latency' else http_successes(at, 3)
        bundle.metrics = {signals.METRIC: {route: route_reading for route in expected_routes}}
        bundle.add("prometheus", "dev/delivery", deepcopy(bundle.metrics), collected_at=at)
    runner = engine(monkeypatch, [first, second])
    runner.cfg = AppConfig(target_namespaces=["dev"], recovery={"observation_interval_s": 0.1})
    monkeypatch.setattr(runner, "_run_group", lambda *_: pytest.fail("matching route proof should avoid model calls"))
    with db.session() as s:
        for index, method in enumerate((*methods, methods[0])):
            sig = Signal("dev", "App", "delivery", signals.ALERT, "alertmanager", {"labels": {"method": method, "uri": path}})
            incident, outcome = ingest(s, runner.cfg, sig)
            assert outcome == ("created" if index == 0 else "merged")
            iid = incident.id
        assert route_latency.observed_routes(s, iid, signals.ALERT) == expected_routes
    runner.process(iid)
    with db.session() as s:
        incident = s.get(Incident, iid)
        assert incident.status == "recovery_review"
        assert s.scalar(select(Action.id).where(Action.incident_id == iid)) is None
        record = incident.handoff["observed_recovery"]
        assert record["ai_remediation"] is False
        assert len(record["observations"]) == 2
        snapshot_key = 'business_routes' if signal_kind == 'latency' else 'business_http_routes'
        assert all(set(snapshot[snapshot_key]) == expected_routes for snapshot in record["observations"])


@needs_db
def test_latest_collection_wins_but_failed_history_retained(db_url, monkeypatch):
    first, second = pair()
    failed = deepcopy(first)
    failed.items[0].error = 'timeout'
    e = engine(monkeypatch, [failed, first, second])
    iid = seed()
    e.process(iid)
    with db.session() as s:
        assert s.get(Incident, iid).status == 'recovery_review'
        rows = list(s.scalars(select(Evidence).where(Evidence.incident_id == iid)))
        assert len(rows) == 15 and sum(bool(row.error) for row in rows) == 1
        run = s.scalar(select(WorkflowRun).where(WorkflowRun.incident_id == iid))
        assert len(run.snapshot['prior_collections']) == 1
        assert run.snapshot['prior_collections'][0]['items'][0]['error'] == 'timeout'
        assert run.snapshot['collection']['items'][0]['error'] is None


@needs_db
def test_critical_collection_exhaustion_is_not_authority(db_url, monkeypatch):
    a, b = pair()
    a.items[0].error = b.items[0].error = 'timeout'
    e = engine(monkeypatch, [a, b])
    iid = seed()
    e.process(iid)
    with db.session() as s:
        h = s.get(Incident, iid).handoff['handoff_classification']
        assert h['class'] == 'bounded_recovery_exhausted' and len(h['attempts']) == 2


REASSESS = {'confirm': True, 'reason': '조기 이관 읽기 재확인'}


@needs_db
@pytest.mark.parametrize('active', [False, True])
def test_reassessment_never_writes_and_archives_original(client, monkeypatch, active):
    from app.main import STATE
    first, second = pair()
    if active:
        second.pods[0]['containers'][0]['restart_count'] += 1
    e = engine(monkeypatch, [first, second])
    monkeypatch.setitem(STATE, 'engine', e)
    monkeypatch.setattr(e, '_propose_and_run', lambda *args: pytest.fail('reassessment may not write'))
    monkeypatch.setattr(e, '_run_group', lambda *args: pytest.fail('reassessment may not run write-capable groups'))
    iid = seed('handed_off', legacy=True)
    r = client.post(f'/api/incidents/{iid}/reassess', json=REASSESS)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data['read_only'] is True and data['cluster_changed'] is False
    assert data['status'] == ('handed_off' if active else 'recovery_review')
    if active:
        assert data['handoff']['reassessment']['outcome'] == 'diagnosed'
    else:
        assert data['handoff']['observed_recovery']['cause_confirmed'] is False
    archived = data['handoff']['reassessments'][0]
    assert archived['status_reason'] == 'original router handoff' and archived['handoff']['summary'] == 'original reason'
    with db.session() as s:
        assert len(list(s.scalars(select(WorkflowRun).where(WorkflowRun.incident_id == iid)))) == 2
        assert s.scalar(select(Action.id).where(Action.incident_id == iid)) is None
        assert s.scalar(select(AuditLog.id).where(AuditLog.incident_id == iid,
            AuditLog.event == 'incident_reassessment_started')) is not None
    assert client.get(f'/api/incidents/{iid}').json()['incident']['status'] == data['status']
    if not active:
        assert client.get(f'/api/incidents/{iid}').json()['incident']['recovery_kind'] == 'observed'
        item = next(i for i in client.get('/api/incidents').json()['items'] if i['id'] == iid)
        assert item['recovery_kind'] == 'observed'


@needs_db
@pytest.mark.parametrize('conflict', ['action', 'run', 'control', 'command', 'claimed', 'risk', 'arbitrary',
                                     'unknown_command', 'unknown_write', 'malformed_features'])
def test_reassessment_rejects_unsafe_history(client, monkeypatch, conflict):
    from app.main import STATE
    from app.models import RunControl, OperatorCommand
    e = engine(monkeypatch, [])
    monkeypatch.setitem(STATE, 'engine', e)
    iid = seed('handed_off', legacy=True)
    with db.session() as s:
        inc = s.get(Incident, iid)
        if conflict == 'action': s.add(Action(incident_id=iid, type='rollout_undo', target='dev/Deployment/delivery', policy_mode='auto', status='denied'))
        if conflict == 'run': s.add(WorkflowRun(incident_id=iid, status='running', phase='analysis'))
        if conflict == 'control': s.add(RunControl(incident_id=iid, state='stopping'))
        if conflict == 'command': s.add(OperatorCommand(incident_id=iid, actor='x', instruction='restart', status='executing'))
        if conflict == 'unknown_command': s.add(OperatorCommand(incident_id=iid, actor='x', instruction='restart', status='failed', error='process_restarted_outcome_requires_inspection'))
        if conflict == 'unknown_write': inc.handoff = {**inc.handoff, 'handoff_classification': {'unknown_write': True}}
        if conflict == 'claimed': inc.handoff = {**inc.handoff, 'claimed_by': 'other'}
        if conflict == 'risk': inc.signal_reasons = ['NetworkDrop']
        if conflict == 'arbitrary':
            rd = s.scalar(select(RoutingDecision).where(RoutingDecision.incident_id == iid))
            rd.decided_by = 'rules'
        if conflict == 'malformed_features':
            rd = s.scalar(select(RoutingDecision).where(RoutingDecision.incident_id == iid))
            rd.features = {'evidence_state': 'currently_healthy'}
    assert client.post(f'/api/incidents/{iid}/reassess', json=REASSESS).status_code == 409
    with db.session() as s:
        assert s.get(Incident, iid).status == 'handed_off'
        assert not s.get(Incident, iid).handoff.get('reassessments')


@needs_db
def test_reassessment_auth_and_explicit_read_only(client, monkeypatch):
    from app.main import STATE
    monkeypatch.setitem(STATE, 'engine', engine(monkeypatch, []))
    iid = seed('handed_off', legacy=True)
    path = f'/api/incidents/{iid}/reassess'
    assert client.post(path, json=REASSESS, headers={'X-LiveProbe-Client': ''}).status_code == 403
    assert client.post(path, json=REASSESS, headers={'Origin': 'https://attacker.invalid'}).status_code == 403
    for body in ({**REASSESS, 'confirm': False}, {**REASSESS, 'reason': ''}, {**REASSESS, 'read_only': False},
                 {**REASSESS, 'observed_recovery_only': False}):
        assert client.post(path, json=body).status_code == 422


def test_exception_clearance_requires_fresh_code_evidence():
    from app import business_http

    first, second = pair()
    for b in (first, second):
        b.metrics = {'http_5xx_rate_per_s': {'delivery': 0}, 'http_rate_per_s': {'delivery': 1},
                     business_http.METRIC: {'GET /api/delivery/{id}': http_successes(b.items[0].collected_at, 2)}}
        b.add('prometheus', 'dev/delivery', b.metrics)
    assert recovery_proof(first, second, ['ApplicationException']) is None
    second.add('loki', 'dev/delivery', {'lines': []})
    second.code_refs = {'exceptions': [{'exception': 'CurrentFailure'}]}
    second.add('code_refs', 'dev/delivery', second.code_refs)
    assert recovery_proof(first, second, ['ApplicationException']) is None
    second.code_refs['exceptions'] = []
    assert recovery_proof(first, second, ['ApplicationException'])


@needs_db
def test_new_core_failure_stops_before_group_not_keyerror(db_url, monkeypatch):
    first, second = pair()
    second.items[0].error = 'timeout'
    e = engine(monkeypatch, [first, second])
    monkeypatch.setattr(e, '_run_decision', lambda *args: pytest.fail('failed fresh collection may not run group'))
    iid = seed()
    e.process(iid)
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert inc.status == 'handed_off'
        assert inc.handoff['handoff_classification']['class'] == 'bounded_recovery_exhausted'


@needs_db
def test_new_rules_high_risk_in_reassessment_retained_and_cannot_replay(client, monkeypatch):
    from app.main import STATE
    first, second = pair()
    second.service.update(selector={'app': 'broken'}, matching_pods=[], pods_labeled_app_eq_service=['delivery-pod'])
    e = engine(monkeypatch, [first, second])
    monkeypatch.setitem(STATE, 'engine', e)
    iid = seed('handed_off', legacy=True)
    response = client.post(f'/api/incidents/{iid}/reassess', json=REASSESS)
    assert response.status_code == 200, response.text
    assert response.json()['handoff']['handoff_classification']['class'] == 'authority_boundary'
    with db.session() as s:
        assert s.get(Incident, iid).severity_level == 'HIGH_RISK'
    assert client.post(f'/api/incidents/{iid}/reassess', json=REASSESS).status_code == 409


@needs_db
@pytest.mark.parametrize('proposal', [False, True])
def test_observed_recovery_keeps_group_result_without_crediting_an_action(db_url, monkeypatch, proposal):
    import app.engine as module
    first, second = pair()
    e = engine(monkeypatch, [first, second])
    causes = [{'cause': '이전 컨테이너 종료 흔적', 'confidence': 0.6, 'evidence_refs': ['E1']}]
    action = {'propose': True, 'type': 'rollout_restart', 'kind': 'Deployment', 'name': 'delivery'} if proposal else None
    monkeypatch.setattr(module, 'run_group', lambda *args: {'status': 'verified', 'reason': 'read before deciding',
        'budget': {'agent_calls': 6}, 'summary': '복구 원인 미확정', 'root_cause_candidates': causes, 'action': action})
    monkeypatch.setattr(e, '_propose_and_run', lambda *args: pytest.fail('a recovered service must not be changed'))
    iid = seed('analyzing')
    e._run_group(iid, decide(first, 15), first, {'signals': ['CrashLoopBackOff'], 'workload': 'delivery'})
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert inc.status == 'recovery_review'
        assert inc.summary == '복구 원인 미확정'
        assert inc.handoff['root_cause_candidates'] == causes
        assert inc.handoff['workflow_status'] == 'verified'
        assert inc.handoff['budget']['agent_calls'] == 6
        recovery = inc.handoff['observed_recovery']
        assert recovery['cause_confirmed'] is False and recovery['ai_remediation'] is False
        assert 'handoff_classification' not in inc.handoff
        assert s.scalar(select(Action.id).where(Action.incident_id == iid)) is None


@needs_db
def test_simple_workflow_retains_reviewed_diagnosis_before_action(db_url, monkeypatch):
    from app.router import Decision
    from app.workflows import AgentCall
    from test_core import _answer

    bundle = healthy()
    bundle.pods[0]['containers'][0]['waiting_reason'] = 'ImagePullBackOff'
    e = engine(monkeypatch, [])
    monkeypatch.setattr(e, '_agent_runner', lambda *args, **kwargs: lambda role, prompt: AgentCall(True, _answer(), 1))
    monkeypatch.setattr(e, '_guard_new_change', lambda *args: False)
    iid = seed('analyzing', signals=['ImagePullBackOff'])
    calls = []

    def executor(incident_id, level, action, summary):
        calls.append(action['type'])
        with db.session() as s:
            handoff = s.get(Incident, incident_id).handoff
            assert handoff['root_cause_candidates'] == _answer()['root_cause_candidates']
            assert handoff['verification']['candidate_verdicts'] == _answer()['candidate_verdicts']
            assert handoff['workflow_status'] == 'verified'
        return True

    monkeypatch.setattr(e, '_propose_and_run', executor)
    decision = Decision('SIMPLE', 'simple_group', {'has_previous_revision': True}, 'image error', 1, 'rules',
                        policy={'selected_model': 'gpt-6-luna'})
    e._run_decision(iid, decision, bundle, {'id': iid, 'workload': 'delivery',
                                         'signals': ['ImagePullBackOff']}, 'adaptive', {})
    assert calls == ['rollout_undo']


@needs_db
def test_verified_group_no_action_unproven_is_diagnostic_not_authority(db_url, monkeypatch):
    import app.engine as module
    first, second = pair()
    second.pods[0]['containers'][0]['restart_count'] += 1
    e = engine(monkeypatch, [first, second])
    monkeypatch.setattr(module, 'run_group', lambda *args: {'status': 'verified', 'reason': 'cause unclear',
        'budget': {'agent_calls': 6}, 'summary': '원인 미확정'})
    iid = seed('analyzing')
    e._run_group(iid, decide(first, 15), first, {'signals': ['CrashLoopBackOff'], 'workload': 'delivery'})
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert inc.status == 'handed_off'
        h = inc.handoff['handoff_classification']
        assert h['class'] == 'bounded_recovery_exhausted' and h['budget']['agent_calls'] == 6


def test_classifier_does_not_multiply_exhausted_engine_retries():
    from app.router import ClassifierRecoveryExhausted
    from test_router_workflows import family1
    calls = []
    def exhausted(prompt):
        calls.append(prompt)
        raise ClassifierRecoveryExhausted('shared budget used')
    assert decide(family1(), 15, exhausted).level == 'NORMAL'
    assert len(calls) == 1


@needs_db
def test_concurrent_reassessments_admit_only_one_live_run(client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app.main import STATE, reassess_incident, ReassessIn
    from fastapi import HTTPException
    first, second = pair()
    e = engine(monkeypatch, [first, second])
    entered, release = Event(), Event()
    original = e.reassess_read_only
    def held(iid, run_id):
        entered.set()
        assert release.wait(10), 'concurrent test failed to release read-only lane'
        original(iid, run_id)
    monkeypatch.setattr(e, 'reassess_read_only', held)
    monkeypatch.setitem(STATE, 'engine', e)
    iid = seed('handed_off', legacy=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(reassess_incident, iid, ReassessIn(**REASSESS), 'local-operator')
        assert entered.wait(10)
        try:
            with pytest.raises(HTTPException) as denied:
                reassess_incident(iid, ReassessIn(**REASSESS), 'local-operator')
            assert denied.value.status_code == 409
        finally:
            release.set()
        assert winner.result(timeout=10)['status'] == 'recovery_review'
    with db.session() as s:
        assert len(s.get(Incident, iid).handoff['reassessments']) == 1
        assert len(list(s.scalars(select(WorkflowRun).where(WorkflowRun.incident_id == iid)))) == 2


@needs_db
def test_active_fault_runs_group_and_existing_executor_boundary(db_url, monkeypatch):
    import app.engine as module
    first, second = pair()
    for b in (first, second):
        b.pods[0]['containers'][0].update(ready=False, waiting_reason='CrashLoopBackOff')
    e = engine(monkeypatch, [first, second, deepcopy(second)])
    monkeypatch.setattr(e, '_classify', lambda *args: {'level': 'COMPLEX', 'rationale': 'active crash', 'confidence': 0.9})
    group_calls, executor_calls = [], []
    def group(*args):
        group_calls.append(True)
        return {'status': 'verified', 'summary': '현재 장애', 'action': {'propose': True, 'type': 'rollout_restart',
                'kind': 'Deployment', 'name': 'delivery', 'params': {}}}
    def executor(iid, level, proposal, summary):
        executor_calls.append((level, proposal['type']))
        return False  # existing executor denies: retain the actual authority boundary
    monkeypatch.setattr(module, 'run_group', group)
    monkeypatch.setattr(e, '_propose_and_run', executor)
    iid = seed()
    e.process(iid)
    assert group_calls == [True] and executor_calls == [('COMPLEX', 'rollout_restart')]
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert inc.status == 'handed_off' and 'observed_recovery' not in inc.handoff
        assert inc.handoff['handoff_classification']['class'] == 'authority_boundary'


@needs_db
def test_old_group_input_gets_two_fresh_observations(db_url, monkeypatch):
    old = healthy(600)
    first, second = pair()
    e = engine(monkeypatch, [first, second])
    iid = seed('analyzing')
    recovered, _ = e._observe_recovery(iid, old, {'signals': ['CrashLoopBackOff']}, phase='group_no_action')
    assert recovered
    with db.session() as s:
        assert len(list(s.scalars(select(Evidence).where(Evidence.incident_id == iid)))) == 10
        observations = s.get(Incident, iid).handoff['observed_recovery']['observations']
        assert len(observations) == 2
        assert all(datetime.fromisoformat(o['observed_at']) > old.items[0].collected_at for o in observations)


@needs_db
def test_fault_cleared_during_group_gets_two_new_healthy_observations(db_url, monkeypatch):
    import app.engine as module
    active = healthy(10)
    active.pods[0]['containers'][0].update(ready=False, waiting_reason='CrashLoopBackOff')
    first, second = pair()
    e = engine(monkeypatch, [first, second])
    monkeypatch.setattr(module, 'run_group', lambda *args: {'status': 'verified', 'summary': '추가 현재 상태 확인 필요'})
    iid = seed('analyzing')
    e._run_group(iid, decide(active, 15), active, {'signals': ['CrashLoopBackOff'], 'workload': 'delivery'})
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert inc.status == 'recovery_review'
        assert len(inc.handoff['observed_recovery']['observations']) == 2
        assert len(list(s.scalars(select(Evidence).where(Evidence.incident_id == iid)))) == 10
