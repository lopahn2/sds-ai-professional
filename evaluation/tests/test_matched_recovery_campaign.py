from collections import deque
from copy import deepcopy
import json
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from evaluation import matched_recovery_campaign
from evaluation.isolated_recovery_agent import Unobserved, _registered_model, _turn, starting_message
from evaluation.jdbc_product_source import write_local_registry
from evaluation.matched_recovery_campaign import ARMS, CONTEXT, PreflightBlocked, comparison_matrix, matched_start, write_new
from evaluation.matched_recovery_summary import summarize
from evaluation.reservation_source_trial import java_path
from evaluation.prepare_jdbc_fixture import VARIANTS, materialize


def test_deduplicated_matrix_rotates_four_arms_per_case():
    matrix = comparison_matrix()
    assert len(matrix) == 24 and len(comparison_matrix(include_oom=True)) == 28
    assert {row["namespace"] for row in matrix} == {"dev"}
    groups = {row["case"]: [] for row in matrix}
    for row in matrix:
        groups[row["case"]].append(row["arm"])
    assert all(set(group) == set(ARMS) and len(group) == 4 for group in groups.values())
    assert {group[0] for group in groups.values()} == set(ARMS)


def test_common_identity_rejects_node_or_fault_change_and_ignores_episode_counters():
    base = {"context": CONTEXT, "namespace": "dev", "node_uid": "node", "deployment_uid": "deployment",
            "fixture_digest": "fixture", "fault_digest": "fault", "verifier_digest": "probe",
            "initial_error_digest": "line", "pod_uid": "before", "attempt": 1}
    assert matched_start(base) == matched_start({**base, "pod_uid": "next", "attempt": 2})
    assert matched_start(base) != matched_start({**base, "node_uid": "other node"})
    assert matched_start(base) != matched_start({**base, "fault_digest": "other fault"})
    with pytest.raises(PreflightBlocked):
        matched_start({**base, "namespace": "prd"})


def test_receipts_are_append_only(tmp_path):
    path = tmp_path / "report.json"
    write_new(path, {"status": "unobserved"})
    with pytest.raises(FileExistsError):
        write_new(path, {"status": "failed"})
    assert "unobserved" in path.read_text()


def test_summary_excludes_inconclusive_attempt_with_partial_recovery_evidence(tmp_path):
    receipt = tmp_path / "attempt.json"
    write_new(receipt, {"case": "KW-03", "arm": "astra", "status": "unobserved", "injected_at": "2026-11-26T00:00:00Z",
                        "recovery_ms": 100, "verified_before_cleanup": {"contract_ok": True}})

    result = summarize([receipt])

    assert result["observed"] == 0
    assert result["counts"] == {"unobserved": 1, "not_run": 23}
    assert "recovery_ms" not in next(row for row in result["rows"] if row["status"] == "unobserved")


def test_summary_keeps_a_preflight_refusal_as_unexecuted(tmp_path):
    receipt = tmp_path / "resource-gate.json"
    write_new(receipt, {"case": "KW-03", "arm": "astra", "status": "unobserved", "trial_error": "resource gate"})
    result = summarize([receipt])
    assert result["counts"] == {"not_run": 24}
    assert sum(row.get("receipt") == receipt.name for row in result["rows"]) == 1


@pytest.mark.parametrize("foreign_owner, ready_endpoint, expected_failure", [
    (False, False, True),
    (True, False, False),
    (False, True, False),
])
def test_jdbc_product_adjudicates_only_the_crashing_candidate(monkeypatch, foreign_owner, ready_endpoint, expected_failure):
    from evaluation import matched_jdbc_product as product
    from evaluation.jdbc_dev_manifest import NAME, manifest

    broken = "liveprobe/eval-reservation@sha256:" + "a" * 64
    candidate = "liveprobe/eval-reservation@sha256:" + "b" * 64
    baseline = next(item for item in manifest(broken)["items"] if item["kind"] == "Deployment")
    baseline["metadata"]["uid"] = "deployment-uid"
    current = deepcopy(baseline)
    current["metadata"].update(generation=5, resourceVersion="10",
                               annotations={"deployment.kubernetes.io/revision": "5"})
    current["status"] = {"observedGeneration": 5, "replicas": 1, "updatedReplicas": 1}
    current["spec"]["template"]["spec"]["containers"][0]["image"] = candidate
    objects = {
        "replicasets": [{"metadata": {
            "uid": "replica-uid", "annotations": {"deployment.kubernetes.io/revision": "5"},
            "ownerReferences": [{"kind": "Deployment", "controller": True,
                                 "uid": "foreign" if foreign_owner else "deployment-uid"}],
        }}],
        "pods": [{
            "metadata": {"uid": "pod-uid", "ownerReferences": [
                {"kind": "ReplicaSet", "uid": "replica-uid", "controller": True},
            ]},
            "spec": {"containers": [{"name": NAME, "image": candidate}]},
            "status": {"containerStatuses": [{
                "name": NAME, "imageID": "sha256:" + "c" * 64, "ready": False, "restartCount": 5,
                "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                "lastState": {"terminated": {"exitCode": 1}},
            }]},
        }],
        "endpointslices": [{"endpoints": [{"conditions": {"ready": ready_endpoint}}]}],
    }
    regression = {"before_failed": True, "after_passed": True, "regression_executed": True}
    row = {"id": "application-incident", "native_signal": True, "fault_image": broken, "candidate": candidate,
           "model_calls": [], "error_code": "dev_business_verification_failed", "pending_write": False,
           "regression": regression, "dev_verified": False, "state": "failed"}
    monkeypatch.setattr(product, "product_rows", lambda _: [row])
    monkeypatch.setattr(product.runtime, "deployment", lambda: current)
    monkeypatch.setattr(product.runtime.cluster, "get", lambda kind, *, selector: {"items": objects[kind]})
    now = [1]
    monkeypatch.setattr(product, "monotonic", lambda: now[0])
    monkeypatch.setattr(product, "sleep", lambda _: now.__setitem__(0, 901))

    result = {"status": "unobserved"}
    if expected_failure:
        product.observe(result, baseline, {"image": broken}, 0, 0)
        assert result["status"] == "unrecovered"
        assert result["regression"]["receipt"] == regression
        assert result["failed_before_cleanup"]["pod_uid"] == "pod-uid"
        assert result["failed_before_cleanup"]["ready_endpoints"] == 0
    else:
        with pytest.raises(Unobserved):
            product.observe(result, baseline, {"image": broken}, 0, 0)
        assert result["status"] == "unobserved"


def test_jdbc_tool_accepts_listed_path_without_expanding_source_scope():
    name = "ReservationQuery.java"
    listed = java_path(name, test=False)
    assert java_path(listed.as_posix(), test=False) == listed
    with pytest.raises(ValueError, match='outside'):
        java_path(listed.parent.as_posix() + '/../../../../pom.xml', test=False)
    with pytest.raises(ValueError, match='outside'):
        java_path(listed.as_posix(), test=True)


def test_dev_jdbc_intake_accepts_only_the_cluster_log_observer(monkeypatch):
    from liveprobe.app.application import browser_access as browser
    from liveprobe.app.application.catalog import DEV_RESERVATION

    def denied(*args, **kwargs):
        raise HTTPException(403, {'code': 'scope_forbidden'})

    monkeypatch.setattr(browser, 'actor', denied)
    roles = ['cluster']
    screening = {'dev_only': {DEV_RESERVATION}}
    identities = SimpleNamespace(runner=lambda _: {'runner_role': roles[0]})
    control = SimpleNamespace(catalog=SimpleNamespace(**screening))
    app = SimpleNamespace(state=SimpleNamespace(control=control, identities=identities))
    request = SimpleNamespace(headers={'authorization': 'Bearer synthetic'})
    body = {'environment': 'dev', 'service': DEV_RESERVATION, 'reproduction': None,
            'signal': SimpleNamespace(source='pod_log')}
    assert browser.incident_creator(app, request, SimpleNamespace(**body)) == {'runner_role': 'cluster'}
    for change in ({'environment': 'prd'}, {'service': 'inventory'}, {'signal': None}, {'reproduction': 'command'}):
        with pytest.raises(HTTPException):
            browser.incident_creator(app, request, SimpleNamespace(**{**body, **change}))
    roles[0] = 'source'
    with pytest.raises(HTTPException):
        browser.incident_creator(app, request, SimpleNamespace(**body))


@pytest.mark.parametrize(("stderr", "diagnosis"), [
    ("dial tcp 127.0.0.1:6443: connect: operation not permitted", "socket access is blocked"),
    ("dial tcp 127.0.0.1:6443: connect: connection refused", "connection refused"),
    ("Error from server (Forbidden): readiness denied", "denied this identity"),
    ("dial tcp 127.0.0.1:6443: i/o timeout", "timed out"),
])
def test_api_preflight_distinguishes_local_access_from_server_failure(monkeypatch, stderr, diagnosis):
    monkeypatch.setattr(matched_recovery_campaign.subprocess, "run",
                        lambda cmd, **kwargs: CompletedProcess(cmd, 1, "", stderr))
    with pytest.raises(PreflightBlocked, match=diagnosis):
        matched_recovery_campaign.require_ready_api()


def test_dev_preflight_reports_reachable_api_when_cpu_gate_blocks(monkeypatch):
    from evaluation.harness import real

    chaos = real._load_chaos()

    def overloaded():
        raise chaos.NodeGateStop({"cpu_m": 3085, "cpu_cap_m": 4000, "mem_mi": 7204, "alloc_mi": 11945}, ["cpu"])

    monkeypatch.setattr(real, "_load_chaos", lambda: chaos)
    monkeypatch.setattr(chaos, "node_gate", overloaded)
    monkeypatch.setattr(matched_recovery_campaign.subprocess, "run",
                        lambda cmd, **kwargs: CompletedProcess(cmd, 0, "ok\n", ""))
    with pytest.raises(PreflightBlocked, match=r"API is reachable.*\(cpu\).*3085/4000m"):
        matched_recovery_campaign.dev_preflight()


def test_source_export_contains_one_variant_and_no_coordinator(tmp_path):
    digests = []
    for variant in VARIANTS:
        root = tmp_path / variant
        digests.append(materialize(variant, root))
        assert len(list(root.rglob("ReservationQuery.java"))) == 1
        assert not list(root.rglob("*sql"))
        assert all(other not in next(root.rglob("ReservationQuery.java")).read_text() for other in VARIANTS)
    assert len(set(digests)) == len(VARIANTS)


def test_jdbc_product_registration_is_dev_only_in_api_and_runner(tmp_path, monkeypatch):
    from liveprobe.app.application.catalog import DEV_RESERVATION
    from liveprobe.app.application.control import ControlPlane
    from liveprobe.application_runner.profiles import Registry

    root = Path(__file__).resolve().parents[2]
    output = write_local_registry(root, tmp_path, 'liveprobe/eval-reservation@sha256:' + '0' * 64)
    settings = SimpleNamespace(catalog_file=output / 'system-catalog.json',
                               profiles_file=output / 'application-profiles.json', testing=False)
    control, runner = ControlPlane(None, settings), Registry(tmp_path)
    monkeypatch.setattr(control, 'environment_paused', lambda _: False)
    assert control.catalog.configured and control.catalog.sha256 == runner.catalog_sha256
    control.validate_intake(SimpleNamespace(environment='dev', service=DEV_RESERVATION, reproduction=None))
    assert runner.target({'environment': 'dev', 'service': DEV_RESERVATION})['affected_services'] == [DEV_RESERVATION]
    with pytest.raises(HTTPException) as failure:
        control.validate_intake(SimpleNamespace(environment='prd', service=DEV_RESERVATION, reproduction=None))
    assert failure.value.detail['code'] == 'service_dev_only'
    with pytest.raises(ValueError, match='target_group_mismatch'):
        runner.target({'environment': 'prd', 'service': DEV_RESERVATION})
    assert DEV_RESERVATION not in Registry(root).profiles

    incident = SimpleNamespace(document={'service': DEV_RESERVATION, 'data': {'release': {
        'stage_results': {'dev': {'verified': True}}, 'affected_services': [DEV_RESERVATION],
    }}})
    transitions = []
    monkeypatch.setattr(control, 'transition', lambda _, incident, state, details=None: transitions.append(state))
    monkeypatch.setattr(control, 'enqueue', lambda *_: pytest.fail('dev-only incident queued prd work'))
    control.collect_release_targets(None, incident, 'prd')
    assert transitions == ['completed']


@pytest.mark.parametrize("line", ["", " \t", "an error\nwith a hint", "bad\x00line"])
def test_first_message_rejects_non_log_material(line):
    with pytest.raises(Unobserved):
        starting_message(line)


def test_short_first_message_has_no_extra_task_context():
    assert starting_message("ERROR timeout") == "ERROR timeout\n장애를 복구하라."


class Session:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.sent = []

    def receive(self):
        return self.responses.popleft()

    def send(self, packet):
        self.sent.append(packet)

    def request(self, method, params):
        return self.receive()


def test_unavailable_effort_is_unobserved():
    state = Session([{"data": [{"model": "requested", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}]}])
    with pytest.raises(Unobserved):
        _registered_model(state, "requested")


def test_a_fresh_turn_can_only_use_its_scoped_broker():
    packet = {"id": "request-1", "method": "item/tool/call", "params": {
        "threadId": "thread", "turnId": "turn", "callId": "1", "tool": "observe", "arguments": {}}}
    finish = {"method": "turn/completed", "params": {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}}}
    session = Session([packet, finish])
    assert _turn(session, "thread", "turn", {"observe": {}}, lambda tool, args: {"status": 200}) == {
        "status": "completed", "tool_calls": 1, "usage": None}
    assert session.sent[0]["id"] == "request-1"
    with pytest.raises(Unobserved):
        _turn(Session([packet]), "other", "turn", {"observe": {}}, lambda *_: pytest.fail("out-of-scope access"))
    with pytest.raises(Unobserved):
        _turn(Session([packet, packet]), "thread", "turn", {"observe": {}}, lambda *_: {})
    blocked = {"method": "item/started", "params": {"item": {"type": "commandExecution", "command": "private input"}}}
    with pytest.raises(Unobserved, match=r"\(commandExecution\)$") as error:
        _turn(Session([blocked]), "thread", "turn", {"observe": {}}, lambda *_: pytest.fail("built-in tool access"))
    assert "private input" not in str(error.value)


@pytest.mark.parametrize('resources', [[], [{'uri': 'resource://outside'}]])
def test_attested_mcp_inventory_accepts_only_an_empty_internal_result(resources):
    scope = {'threadId': 'thread', 'turnId': 'turn'}
    item = {'type': 'mcpToolCall', 'id': 'internal-1', 'server': 'codex',
            'tool': 'list_mcp_resources', 'arguments': {}}
    started = {'method': 'item/started', 'params': {**scope, 'item': {**item, 'status': 'inProgress'}}}
    completed = {'method': 'item/completed', 'params': {**scope, 'item': {**item, 'status': 'completed',
        'result': {'content': [{'type': 'text', 'text': json.dumps({'resources': resources})}]}}}}
    finish = {'method': 'turn/completed', 'params': {'threadId': 'thread', 'turn': {'id': 'turn', 'status': 'completed'}}}
    session = Session([started, completed, finish])
    if resources:
        with pytest.raises(Unobserved, match='did not return an empty result'):
            _turn(session, 'thread', 'turn', {}, lambda *_: pytest.fail('unexpected tool'), empty_mcp_inventory=True)
    else:
        result = _turn(session, 'thread', 'turn', {}, lambda *_: pytest.fail('unexpected tool'), empty_mcp_inventory=True)
        assert (result['empty_mcp_list_calls'], result['tool_calls']) == (1, 0)
