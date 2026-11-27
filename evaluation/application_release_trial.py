"""Exercise the Application model worker and native runner on a frozen service release."""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
from functools import partial
import inspect
import json
import os
import re
from pathlib import Path
from subprocess import TimeoutExpired
from tempfile import TemporaryDirectory
from time import monotonic

from application_runner.build import Builder, changed_test_ids
from application_runner.host import DockerHost
from application_runner.profiles import digest
from application_runner.source import parse_patch, snapshot, source_files

from evaluation.application_fixture_replay import remote_replay
from evaluation.comparison_summary import (delivery_memory_failed, delivery_memory_recovered,
                                           member_release_failed, member_release_recovered,
                                           order_release_failed, order_release_recovered)
from evaluation.live_repair import (DELIVERY_DATES, MEMBER_RELEASE_CASES, NODE_PROGRAM, ORDER_RELEASE_CASES, PROGRAM, CatalogBatchReleaseTrial,
                                    DeliveryMemoryReleaseTrial, DeliveryReleaseTrial, PaymentDualReleaseTrial, PaymentReleaseTrial,
                                    catalog_job_receipts, catalog_release_probe, payment_dual_probe, prepare_trial)
from evaluation.member_release_fixture import MemberReleaseTrial
from evaluation.order_native import prepare_order_toolchain, verify_order_image
from evaluation.order_release_fixture import ORDER_SOURCE, ORDER_TEST, ORDER_TRIALS


PROFILE = {"id": "payment", "service": "payment", "affected_services": ["payment"],
           "language": "python", "test_root": "tests", "image_repository": "liveprobe-eval/payment"}
CATALOG_PROFILE = {**PROFILE, "id": "catalog", "service": "catalog", "affected_services": ["catalog"],
                   "image_repository": "liveprobe-eval/catalog"}
DELIVERY_PROFILE = {**PROFILE, "id": "delivery", "service": "delivery", "affected_services": ["delivery"],
                    "image_repository": "liveprobe-eval/delivery", "language": "node", "test_root": "test"}
MEMBER_PROFILE = {**DELIVERY_PROFILE, "id": "member", "service": "member", "affected_services": ["member"],
                  "image_repository": "liveprobe-eval/member"}
ORDER_PROFILE = {**PROFILE, "id": "order", "service": "order", "affected_services": ["order"],
                 "image_repository": "liveprobe-eval/order", "language": "java", "test_root": "src/test/java"}
RELEASE_TRIALS = {**ORDER_TRIALS, "payment-12": PaymentReleaseTrial, "payment-22": PaymentDualReleaseTrial,
                  **{case: partial(MemberReleaseTrial, case) for case in MEMBER_RELEASE_CASES},
                  "catalog-24": CatalogBatchReleaseTrial, "delivery-25": DeliveryReleaseTrial,
                  "delivery-29": DeliveryMemoryReleaseTrial}
OTHER_PROFILES = {**dict.fromkeys(ORDER_RELEASE_CASES, ORDER_PROFILE), **dict.fromkeys(MEMBER_RELEASE_CASES, MEMBER_PROFILE),
                  "catalog-24": CATALOG_PROFILE, "delivery-25": DELIVERY_PROFILE,
                  "delivery-29": DELIVERY_PROFILE}
PYTEST_IMAGE = "liveprobe/python-test@sha256:8f6b4b3373cb5618d58c9cafaec2d60e7c85e45e478f86019ccee47ad7cabac4"
NODETEST_IMAGE = "node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402"
PROBE = """
import json
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

port = 18080
program = 'import app; app.create_server("127.0.0.1", 18080, background=False).serve_forever()'
if sys.argv[1] == 'catalog-24':
    program = catalog_program
    os.environ.update(FIXTURE_PORT=str(port), FAULT_STATE_DIR='/tmp',
                      FIXTURE_OBSERVE_BACKGROUND_JOB='1', FIXTURE_KEEP_ACCESS_LOGS='1')
opener = build_opener(ProxyHandler({}))

def request(path, data=None):
    target = 'http://127.0.0.1:' + str(port) + path
    if isinstance(data, dict):
        data = json.dumps(data).encode()
    req = Request(target, data=data, headers={'Content-Type': 'application/json'})
    try:
        with opener.open(req, timeout=3) as response:
            return response.status, json.load(response)
    except HTTPError as response:
        with response:
            return response.code, json.load(response)

server = None
phase = 'process_start'
try:
    server = subprocess.Popen([sys.executable, '-B', '-c', program], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    phase = 'startup'
    deadline = monotonic() + 10
    while True:
        try:
            request('/healthz/ready')
            break
        except OSError:
            if server.poll() is not None or monotonic() >= deadline:
                raise RuntimeError('the fixture did not start')
            sleep(0.1)
    phase = 'business_probe'
    if sys.argv[1] == 'catalog-24':
        deadline = monotonic() + 25
        jobs = Path('/tmp/background-observations.jsonl')
        samples = []
        while len(samples) < 2 and monotonic() < deadline:
            observed = catalog_job_receipts(jobs)
            if len(observed) > len(samples):
                price = (79000, 81050)[len(samples)]
                samples.append(catalog_release_probe(request, observed[len(samples)], price))
            sleep(0.1)
        observation = {'samples': samples,
                       'invalid_price_status': request('/api/products/1/price', {'price': 0})[0],
                       'missing_product_status': request('/api/products/9999/price', {'price': 79000})[0]}
    elif sys.argv[1] == 'payment-22':
        observation = {'paid': [payment_dual_probe(request), payment_dual_probe(request)],
                       'invalid_amount_status': request('/api/payments', {'amount': 0, 'order_id': 'invalid'})[0],
                       'missing_receipt_status': request('/api/payments/9999/receipt')[0]}
    else:
        paid = []
        for _ in range(2):
            ready, _ = request('/healthz/ready')
            status, body = request('/api/payments/2/refund')
            paid.append({'ready': ready, 'business': status, 'refund': body.get('refund'),
                         'contract_ok': ready == status == 200 and body.get('refund') ==
                             {'payment_id': '2', 'refundable_amount': 32000, 'fee_refund': 960}})
        cancel_status, cancelled = request('/api/payments/1/cancel', b'{}')
        status, body = request('/api/payments/1/refund')
        missing, _ = request('/api/payments/9999/refund')
        observation = {'paid': paid, 'cancel_status': cancel_status, 'cancelled': cancelled.get('payment', {}).get('status'),
                       'cancelled_refund_status': status, 'cancelled_refund': body.get('refund'), 'missing_status': missing}
    _, runtime = request('/api/chaos')
    print('LP_RELEASE_PROBE ' + json.dumps(dict(observation, runtime_faults=runtime.get('active'))), flush=True)
except Exception as error:
    print('LP_RELEASE_FAILURE ' + json.dumps({'phase': phase, 'kind': type(error).__name__}), flush=True)
    raise SystemExit(1) from None
finally:
    if server is not None and server.poll() is None:
        server.terminate()
        try:
            server.wait(timeout=3)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=3)
"""

NODE_PROBE_SETUP = """
const {spawn} = require('node:child_process');
const {setTimeout: delay} = require('node:timers/promises');
const port = 18080;
let phase = 'startup';
const child = spawn(process.execPath, ['-e', program], {stdio: 'ignore',
    env: {...process.env, FIXTURE_PORT: String(port), FAULT_STATE_DIR: '/tmp'}});
async function request(path, data, method = 'POST') {
    const response = await fetch('http://127.0.0.1:' + port + path, data === undefined ?
        {signal: AbortSignal.timeout(3000)} : {method, signal: AbortSignal.timeout(3000),
            headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
    return [response.status, await response.json()];
}
"""

DELIVERY_PROBE = NODE_PROBE_SETUP + """
async function shipment(tracking, dueAt) {
    const [ready] = await request('/healthz/ready');
    const [submit, body] = await request('/api/deliveries', {tracking, recipient: 'synthetic-recipient', dueAt});
    const [storedStatus, stored] = await request('/api/deliveries/' + tracking);
    const [seedStatus, seed] = await request('/api/deliveries/LP1001');
    const [neighborStatus, neighbor] = await request('/api/deliveries/LP1002');
    const lastEvent = seed.events?.at(-1)?.status;
    const business = submit === 201 ? seedStatus : submit;
    const neighborIntact = neighborStatus === 200 && neighbor.tracking === 'LP1002' && neighbor.status === '배송 중';
    const storedExactly = storedStatus === 200 && body.tracking === tracking && stored.tracking === tracking
        && body.dueAt === dueAt && stored.dueAt === dueAt && stored.status === '접수' && stored.events?.length === 1;
    return {ready, business, submit_status: submit, stored_status: storedStatus, seed_status: seed.status,
        seed_event: lastEvent, neighbor_intact: neighborIntact, tracking, requested_due_at: dueAt,
        stored_exactly: storedExactly,
        contract_ok: ready === 200 && business === 200 && submit === 201 && storedExactly && neighborIntact
            && seed.tracking === 'LP1001' && seed.status === '출고' && lastEvent === '출고'};
}
async function metrics() {
    const response = await fetch('http://127.0.0.1:' + port + '/metrics', {signal: AbortSignal.timeout(3000)});
    const body = await response.text();
    const value = name => {
        const match = body.match(new RegExp('^' + name + ' ([0-9]+)$', 'm'));
        return match ? Number(match[1]) : null;
    };
    return {rss_bytes: value('process_resident_memory_bytes'),
        retained_bytes: value('delivery_retained_response_bytes'),
        array_buffer_bytes: value('delivery_runtime_array_buffer_bytes')};
}
async function transition(tracking) {
    const [ready] = await request('/healthz/ready');
    const [created] = await request('/api/deliveries', {tracking, recipient: 'synthetic-recipient', dueAt: '2032-06-10T12:00:00.000Z'});
    const path = '/api/deliveries/' + tracking;
    const [dispatched] = await request(path + '/status', {status: '출고'}, 'PATCH');
    const [business, updated] = await request(path + '/status', {status: '배송 중'}, 'PATCH');
    const [savedStatus, saved] = await request(path);
    const before = await metrics();
    const seed = [];
    for (let i = 0; i < 3; i++) seed.push(await request('/api/deliveries/LP1001'));
    const after = await metrics();
    const events = (saved.events ?? []).map(item => item.status);
    const seedIntact = seed.every(([status, row]) => status === 200 && row.tracking === 'LP1001'
        && row.status === '출고' && row.events?.length === 1);
    return {ready, business, created, dispatched, saved_status: savedStatus, tracking, saved_tracking: saved.tracking,
        saved_stage: saved.status, events, transition_note: updated.lastNote ?? null, seed_intact: seedIntact,
        memory_before: before, memory_after: after,
        contract_ok: ready === 200 && business === 200 && dispatched === 200 && savedStatus === 200 && created === 201
            && saved.tracking === tracking && saved.status === '배송 중'
            && JSON.stringify(events) === JSON.stringify(['접수', '출고', '배송 중'])
            && (updated.lastNote === undefined || updated.lastNote === '단계 변경') && seedIntact
            && before.retained_bytes === 0 && after.retained_bytes === 0 && before.rss_bytes > 0 && after.rss_bytes > 0
            && Number.isInteger(before.array_buffer_bytes) && Number.isInteger(after.array_buffer_bytes)
            && after.array_buffer_bytes - before.array_buffer_bytes < 8 * 1024 * 1024
            && after.rss_bytes - before.rss_bytes < 8 * 1024 * 1024};
}
(async () => {
    const deadline = Date.now() + 10000;
    while (true) {
        try { await request('/healthz/ready'); break; }
        catch (error) {
            if (child.exitCode !== null || Date.now() >= deadline) throw error;
            await delay(100);
        }
    }
    phase = 'business_probe';
    if (releaseCase === 'delivery-29') {
        const samples = [await transition('LP-EVAL-29-1'), await transition('LP-EVAL-29-2')];
        const [backward] = await request('/api/deliveries/LP-EVAL-29-1/status', {status: '접수'}, 'PATCH');
        const [missing] = await request('/api/deliveries/LP-EVAL-29-MISSING/status', {status: '출고'}, 'PATCH');
        const [, runtime] = await request('/api/chaos');
        console.log('LP_RELEASE_PROBE ' + JSON.stringify({samples, backward_status: backward,
            missing_shipment_status: missing, runtime_faults: runtime.active, node_runtime: process.version}));
        return;
    }
    const samples = [await shipment('LP-EVAL-25-1', dates[0]), await shipment('LP-EVAL-25-2', dates[1])];
    const [invalid] = await request('/api/deliveries', {tracking: 'LP-EVAL-BAD', recipient: 'synthetic-recipient', dueAt: 'invalid-date'});
    const [duplicate] = await request('/api/deliveries', {tracking: samples[0].tracking, recipient: 'synthetic-recipient', dueAt: dates[0]});
    const [, runtime] = await request('/api/chaos');
    console.log('LP_RELEASE_PROBE ' + JSON.stringify({samples, invalid_date_status: invalid,
        duplicate_status: duplicate, runtime_faults: runtime.active, node_runtime: process.version}));
})().catch(error => {
    console.log('LP_RELEASE_FAILURE ' + JSON.stringify({phase, kind: error?.name ?? 'Error'}));
    process.exitCode = 1;
}).finally(() => child.kill('SIGTERM'));
"""

MEMBER_PROBE = NODE_PROBE_SETUP + """
async function memberJourney(sequence) {
    const [ready] = await request('/healthz/ready');
    const [live] = await request('/healthz/live');
    const [seedStatus, seed] = await request('/api/members/1/profile');
    const [missing] = await request('/api/members/lp-eval-missing/profile');
    let signupStatus = null, signupIntact = null, rejectedSignupUnstored = null;
    if (dual) {
        const target = 'LP-EVAL-SIGNUP-' + sequence;
        const [status, saved] = await request('/api/members', {name: target});
        const [lookupStatus, found] = await request('/api/members?search=' + target);
        signupStatus = status;
        signupIntact = status === 201 && lookupStatus === 200 && saved.name === target
            && found.members?.length === 1 && found.members[0].id === saved.id && found.members[0].name === saved.name;
        rejectedSignupUnstored = status === 500 && lookupStatus === 200 && found.members?.length === 0;
    }
    const name = 'LP-EVAL-PROFILE-' + sequence;
    const [created, original] = await request('/api/members', {name});
    const [profile, fetched] = typeof original.id === 'string' ?
        await request('/api/members/' + original.id + '/profile') : [null, {}];
    const intact = profile === 200 && fetched.id === original.id && fetched.name === name
        && fetched.createdAt === original.createdAt;
    const seedIntact = seedStatus === 200 && seed.id === '1' && seed.name === '김하늘';
    return {ready, liveness: live, business: profile, created, profile_id: original.id ?? null,
        signup_status: signupStatus, signup_intact: signupIntact, rejected_signup_unstored: rejectedSignupUnstored,
        profile_intact: intact, seed_intact: seedIntact, missing_status: missing,
        contract_ok: ready === 200 && live === 200 && profile === 200 && created === 201 && intact
            && seedIntact && missing === 404 && (!dual || signupIntact)};
}
(async () => {
    const deadline = Date.now() + 10000;
    while (true) {
        try { await request('/healthz/ready'); break; }
        catch (error) {
            if (child.exitCode !== null || Date.now() >= deadline) throw error;
            await delay(100);
        }
    }
    phase = 'business_probe';
    const samples = [await memberJourney(1), await memberJourney(2)];
    const [invalid] = await request('/api/members', {name: ''});
    const [duplicate] = await request('/api/members', {name: 'LP-EVAL-PROFILE-1'});
    const [, runtime] = await request('/api/chaos');
    console.log('LP_RELEASE_PROBE ' + JSON.stringify({samples, invalid_name_status: invalid,
        duplicate_name_status: duplicate, runtime_faults: runtime.active, node_runtime: process.version}));
})().catch(error => {
    console.log('LP_RELEASE_FAILURE ' + JSON.stringify({phase, kind: error?.name ?? 'Error'}));
    process.exitCode = 1;
}).finally(() => child.kill('SIGTERM'));
"""


class FixtureHost(DockerHost):
    def __init__(self, workspace):
        config = workspace / "docker-config"
        plugins = config / "cli-plugins"
        plugins.mkdir(parents=True, mode=0o700)
        desktop_buildx = Path.home() / ".docker" / "cli-plugins" / "docker-buildx"
        if desktop_buildx.is_file():
            (plugins / "docker-buildx").symlink_to(desktop_buildx.resolve())
        socket = Path.home() / ".rd" / "docker.sock"
        super().__init__({"test_images": {"python": PYTEST_IMAGE, "node": NODETEST_IMAGE}, "docker_config": config,
                          "docker_host": f"unix://{socket}"})
        self.tags = []
        self.last_build_error = None
        self.last_probe_failure = None
        self.java_test_reports = []

    def invoke(self, args, timeout=300):
        if args[0] == "build":
            tag = args[args.index("--tag") + 1]
            self.tags.append(tag)
            if tag.startswith("liveprobe-eval/order:"):
                args = [*args[:-1], "--build-context", f"maven_cache={self.order_build_context}", args[-1]]
        result = super().invoke(args, timeout)
        if args[0] == "build" and result.returncode:
            self.last_build_error = result.stderr.strip().splitlines()[-1:] or ["build_command_failed"]
        return result

    def tests(self, workspace, profile, phase, added):
        report = super().tests(workspace, profile, phase, added)
        if profile["language"] == "java":
            self.java_test_reports.append({
                **{field: report[field] for field in ("phase", "exit_code", "total", "failed", "errors")},
                "failed_tests": [row["id"] for row in report["executed_tests"]
                                 if row["status"] in ("failed", "error")],
            })
        return report

    def verify(self, image, case="payment-12"):
        self.last_probe_failure = None
        if case in ORDER_RELEASE_CASES:
            return verify_order_image(self, image, dual=case == "order-21")
        if case in ("delivery-25", "delivery-29"):
            verifier = (f"const program = {json.dumps(NODE_PROGRAM)}; const dates = {json.dumps(DELIVERY_DATES)}; "
                        f"const releaseCase = {json.dumps(case)};\n" + DELIVERY_PROBE)
            command = ["--entrypoint", "node", image, "-e", verifier]
        elif case in MEMBER_RELEASE_CASES:
            verifier = f"const program = {json.dumps(NODE_PROGRAM)}; const dual = {json.dumps(case == 'member-23')};\n" + MEMBER_PROBE
            command = ["--entrypoint", "node", image, "-e", verifier]
        else:
            functions = (catalog_job_receipts, catalog_release_probe) if case == "catalog-24" else (payment_dual_probe,)
            verifier = "\n".join(inspect.getsource(function) for function in functions) + "\n"
            if case == "catalog-24":
                verifier += f"catalog_program = {PROGRAM!r}\n"
            verifier += PROBE
            command = ["--entrypoint", "python", image, "-B", "-c", verifier, case]
        run = self.invoke(["run", "--rm", "--pull=never", "--network=none", "--memory=256m", "--cpus=0.5",
                           "--pids-limit=40", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                           "--read-only", "--tmpfs", "/tmp:rw,nosuid,size=32m", "-e", "SELF_TRAFFIC=0",
                           *command], timeout=45)
        prefix = "LP_RELEASE_PROBE "
        rows = [line[len(prefix):] for line in run.stdout.splitlines() if line.startswith(prefix)]
        if run.returncode or len(rows) != 1:
            failure_prefix = "LP_RELEASE_FAILURE "
            diagnostics = [json.loads(line[len(failure_prefix):]) for line in run.stdout.splitlines()
                           if line.startswith(failure_prefix)]
            self.last_probe_failure = {"exit_code": run.returncode, "result_rows": len(rows),
                                       "reported_failure": diagnostics[0] if len(diagnostics) == 1 else None,
                                       "exception_types": re.findall(r"^([A-Za-z]+(?:Error|Exception)):", run.stderr, re.M)[-2:]}
            raise ValueError("isolated_release_probe_failed")
        return json.loads(rows[0])

    def clean_images(self):
        cleanup = {}
        for tag in self.tags:
            try:
                cleanup[tag] = self.invoke(["image", "rm", tag], timeout=20).returncode == 0
            except (OSError, TimeoutExpired):
                cleanup[tag] = False
        return cleanup


def release_probe_passed(observed, case="payment-12"):
    if case in ORDER_RELEASE_CASES:
        return observed["runtime_faults"] == [] and order_release_recovered(observed["samples"], dual=case == "order-21")
    if case in MEMBER_RELEASE_CASES:
        return (observed["runtime_faults"] == []
                and member_release_recovered(observed["samples"], dual=case == "member-23")
                and observed["invalid_name_status"] == 400 and observed["duplicate_name_status"] == 409)
    if case == "delivery-29":
        return (delivery_memory_recovered(observed["samples"]) and observed["backward_status"] == 409
                and observed["missing_shipment_status"] == 404 and observed["runtime_faults"] == [])
    if case == "delivery-25":
        rows = observed["samples"]
        return (len(rows) == 2 and all(row["contract_ok"] for row in rows)
                and rows[0]["tracking"] != rows[1]["tracking"] and observed["invalid_date_status"] == 400
                and observed["duplicate_status"] == 409 and observed["runtime_faults"] == [])
    if case == "catalog-24":
        rows = observed["samples"]
        return (len(rows) == 2 and all(row["contract_ok"] for row in rows)
                and rows[1]["job"]["sequence"] > rows[0]["job"]["sequence"]
                and rows[1]["job"]["observed_at_monotonic"] - rows[0]["job"]["observed_at_monotonic"] >= 14
                and observed["invalid_price_status"] == 400 and observed["missing_product_status"] == 404
                and observed["runtime_faults"] == [])
    if case == "payment-22":
        return (len(observed["paid"]) == 2 and all(row["contract_ok"] for row in observed["paid"])
                and observed["invalid_amount_status"] == 400 and observed["missing_receipt_status"] == 404
                and observed["runtime_faults"] == [])
    return (all(row["contract_ok"] for row in observed["paid"]) and len(observed["paid"]) == 2
            and observed["cancel_status"] == 200 and observed["cancelled"] == "cancelled"
            and observed["cancelled_refund_status"] == 200 and observed["cancelled_refund"] ==
                {"payment_id": "1", "refundable_amount": 0, "fee_refund": 0}
            and observed["missing_status"] == 404 and observed["runtime_faults"] == [])


def preserves_existing_tests(before, after):
    original = ast.parse(before)
    candidate = ast.parse(after)

    def without_added(body, previous):
        known = {node.name for node in previous if isinstance(node, ast.FunctionDef)}
        return [node for node in body if not (isinstance(node, ast.FunctionDef)
                and node.name.startswith("test_") and node.name not in known)]

    classes = {node.name: node for node in original.body if isinstance(node, ast.ClassDef)}
    original_names = {node.name for node in original.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))}

    def added_test_class(node):
        return (isinstance(node, ast.ClassDef) and node.name not in original_names
                and (node.name.startswith("Test") or node.name.endswith("Tests"))
                and len(node.bases) == 1 and isinstance(node.bases[0], ast.Name) and node.bases[0].id in classes
                and not node.keywords and not node.decorator_list
                and all(isinstance(method, ast.FunctionDef) and method.name.startswith("test_") for method in node.body))

    candidate.body = [node for node in without_added(candidate.body, original.body) if not added_test_class(node)]
    for node in candidate.body:
        if isinstance(node, ast.ClassDef) and node.name in classes:
            node.body = without_added(node.body, classes[node.name].body)
    return ast.dump(candidate) == ast.dump(original)


def test_candidate(workspace, host, baseline, frozen, patch, *, resolve_class_names=False, profile=PROFILE):
    changes = parse_patch(patch["diff"], baseline, profile["test_root"])
    node = profile["language"] == "node"
    java = profile["language"] == "java"
    code_file = ORDER_SOURCE if java else "business.js" if node else "app.py"
    if code_file not in changes:
        raise ValueError("fixture_code_change_required")
    for path, body in changes.items():
        if path == code_file:
            continue
        if not path.startswith(profile["test_root"] + "/"):
            raise ValueError("fixture_patch_scope_invalid")
        if path in frozen["files"]:
            if java:
                raise ValueError("fixture_existing_tests_changed")
            original = frozen["files"][path]
            unchanged = body.startswith(original) if node else preserves_existing_tests(original, body)
            if not unchanged:
                raise ValueError("fixture_existing_tests_changed")
    if resolve_class_names and not (node or java):
        canonical = []
        for identifier in patch["tests_added"]:
            path, name = identifier.split("::", 1)
            changed = changed_test_ids(frozen["files"].get(path, ""), changes[path], "python")
            matched = [test for test in changed if test == name or test.split("::")[-1] == name]
            if len(matched) != 1:
                raise ValueError("fixture_test_id_ambiguous")
            canonical.append(path + "::" + matched[0])
        patch = {**patch, "tests_added": canonical}
    tested = Builder(workspace, host).run(frozen, patch, profile)
    return patch, tested


def measure_candidate(workspace, host, baseline, frozen, patch, *, resolve_class_names=False, case="payment-12"):
    record = {"patch": patch, "verified": None, "status": "unrecovered"}
    reports = len(host.java_test_reports)
    try:
        build_start = monotonic()
        if case not in ("payment-12", "order-11", "member-13") and len(patch["tests_added"]) < 2:
            raise ValueError("added_regression_required")
        profile = OTHER_PROFILES.get(case, PROFILE)
        patch, tested = test_candidate(workspace, host, baseline, frozen, patch,
                                       resolve_class_names=resolve_class_names, profile=profile)
        record.update(patch=patch, test_build=tested, test_build_wall_ms=round((monotonic() - build_start) * 1000))
        artifact = tested["artifact"]["image"]
        after = host.verify(artifact, case)
        record["candidate_probe"] = after
        if artifact != frozen["base_image"] and release_probe_passed(after, case):
            record.update(status="verified_recovered", verified=after["samples" if case in OTHER_PROFILES else "paid"])
            return record, tested
        error = "original_request_or_independent_regression_failed"
    except TimeoutExpired:
        error = "isolated_native_stage_timeout"
    except SyntaxError:
        error = "candidate_python_syntax_invalid"
    except (OSError, ValueError) as failure:
        error = str(failure)
    finally:
        if host.java_test_reports[reports:]:
            record["native_test_reports"] = host.java_test_reports[reports:]
    record["failure"] = error
    if error == "isolated_release_probe_failed" and host.last_probe_failure:
        record["probe_failure"] = host.last_probe_failure
    return record, {"ok": False, "write_outcome": "not_written", "error": error}


def run(workspace, prior=None, *, use_worktree_worker=False, critic_sol=False, report_native_result=False, case="payment-12"):
    host = FixtureHost(workspace)
    trial = RELEASE_TRIALS[case]()
    profile = OTHER_PROFILES.get(case, PROFILE)
    try:
        opening = prepare_trial(trial)
        trial.close_process()
        if case in ORDER_RELEASE_CASES:
            prepare_order_toolchain(host, workspace, trial)
        baseline = workspace / trial.fixture.parent.name
        trial.fixture.rename(baseline)
        files = {path: digest(body.encode()) for path, body in source_files(baseline).items()}
        built = host.build(baseline, profile, digest(files))
        image = built["image"]
        precondition = host.verify(image, case)
        samples = precondition["samples" if case in OTHER_PROFILES else "paid"]
        if (len(samples) != 2 or precondition["runtime_faults"] != []
                or any(row["ready"] != 200 or row["business"] != 500 for row in samples)):
            raise ValueError("the frozen image did not reproduce the original HTTP failure")
        if case in ORDER_RELEASE_CASES and not all(order_release_failed(row, dual=case == "order-21") for row in samples):
            raise ValueError("the frozen Java image did not reproduce the order calculation failure")
        if case == "catalog-24" and any(
                (row["exception"], row["job"].get("status"), row["job"].get("exception"), row["inventory_intact"])
                != ("ValueError", "failed", "IndexRebuildError", True) for row in samples):
            raise ValueError("the frozen image did not reproduce both catalog failures")
        if case == "payment-22" and any(
                (row["fee_status"], row["fee_exception"], row["receipt_exception"],
                 row["failed_payment_left_ledger_intact"]) != (500, "ZeroDivisionError", "ValueError", True)
                for row in precondition["paid"]):
            raise ValueError("the frozen image did not reproduce both payment failures")
        if case == "delivery-25" and any((row["submit_status"], row["stored_status"], row["seed_status"],
                                       row["seed_event"], row["neighbor_intact"]) != (500, 404, "접수", "출고", True)
                                      for row in samples):
            raise ValueError("the frozen image did not reproduce both delivery failures")
        if case == "delivery-29" and not all(delivery_memory_failed(row) for row in samples):
            raise ValueError("the frozen image did not reproduce both delivery defects")
        if case in MEMBER_RELEASE_CASES and not all(member_release_failed(row, dual=case == "member-23") for row in samples):
            raise ValueError("the frozen member image did not reproduce the selected HTTP failure paths")
        frozen = snapshot(baseline, profile, image, {"image": image, "profile_id": profile["id"], "file_sha256": files})
        node = profile["language"] == "node"
        java = profile["language"] == "java"
        source_file = ORDER_SOURCE if java else "business.js" if node else "app.py"
        visible = {source_file, ORDER_TEST if java else "test/index.js" if node else "tests/test_contract.py"}
        outbound = {**frozen, "files": {path: frozen["files"][path] for path in sorted(visible)},
                    "file_sha256": {path: frozen["file_sha256"][path] for path in sorted(visible)}}
        traces = trial.traces if case != "payment-12" else [trial.trace_observation]
        trace = traces[0]
        if not trace or "exception" not in trace:
            raise ValueError("version-pinned exception frame is missing")
        if java:
            trace_observation = {"traces": traces, "test_contract": "Add a new Java test class without editing the "
                                 "existing tests. Follow the local JUnit style and report the exact package.Class#method."}
            trace_rows = [f'java.lang.{item["exception"]}: {item["message"]}\n'
                          f'    at {item["function"]}({item["path"]}:{item["line"]})' for item in traces]
        elif node:
            trace_observation = {"traces": traces, "test_contract": "Add an independent new Node test file under test/ or append tests without changing the existing file."}
            trace_rows = [f'{item["exception"]}: {item["message"]}\n'
                          f'    at {item["function"]}({item["path"]}:{item["line"]})' for item in traces]
            if case == "delivery-29":
                memory_before, memory_after = samples[0]["memory_before"], samples[0]["memory_after"]
                trace_rows.append("Three reads of the original shipment increased delivery_retained_response_bytes "
                                  f'from {memory_before["retained_bytes"]} to {memory_after["retained_bytes"]} and '
                                  f'delivery_runtime_array_buffer_bytes from {memory_before["array_buffer_bytes"]} '
                                  f'to {memory_after["array_buffer_bytes"]}.')
        elif case != "payment-12":
            trace_observation = {"traces": traces}
            trace_rows = [f'File "{item["path"]}", line {item["line"]}, in {item["function"]}\n'
                          f'{item["exception"]}: {item["message"]}' for item in traces]
        else:
            trace_observation = {"trace": trace}
            trace_rows = [f'{trace["exception"]} at {trace["path"]}:{trace["line"]} {trace["function"]}; '
                          f'key: {trace["key"]}']
        start = monotonic()
        attempted_at = datetime.now(timezone.utc).isoformat()
        packet = {
            "case": opening["case"], "source_path": source_file, "source": trace["function"],
            "source_sha256": files[source_file], "release": outbound,
            **({"isolated_fixture_profile": "legacy-member"} if case in MEMBER_RELEASE_CASES else {}),
            "observations": {"first_log": opening["initial_error_log"], "observed_health": opening["fault_before"],
                             **trace_observation, "runtime_faults": []},
            "evidence_rows": [
                opening["initial_error_log"],
                *trace_rows,
                "Readiness returned HTTP 200. The runtime confirmed no active fault switches.",
            ],
            "followup": {"observed_at": trial.trace_observed_at, "trace": {**trace, "original_request": samples[0]}},
        }
        if critic_sol:
            packet["critic_sol"] = True
        attempts = []

        def native_builder(candidate):
            measured, report = measure_candidate(workspace, host, baseline, frozen, candidate, case=case)
            attempts.append(measured)
            return report

        if prior:
            previous_image = prior["baseline_image"]
            previous = snapshot(baseline, profile, previous_image,
                                {"image": previous_image, "profile_id": profile["id"], "file_sha256": files})
            patch = prior["patch"]
            if (prior["input_sha256"] != opening["input_sha256"] or patch["source_snapshot_id"] != previous["id"]
                    or prior["fixture_sha256"] != opening["fixture_sha256"]):
                raise ValueError("previous candidate does not belong to this frozen release")
            patch = {**patch, "source_snapshot_id": frozen["id"]}
            replay = {"arm": "liveprobe", "application_state": "resumed_candidate_test", "calls": [], "usage": {},
                      "estimated_usd": None, "source_decision_arm": prior["arm"], "prior_tests_added": patch["tests_added"]}
        else:
            replay = remote_replay(packet, use_worktree_worker=use_worktree_worker,
                                   native_builder=native_builder if report_native_result else None)
            patch = replay.pop("queued_patch")
        record = {**opening, **replay, "attempted_at": attempted_at, "baseline_image": image, "baseline_probe": precondition,
                  "scope": "isolated Application worker, native test and build runner, and networkless OCI probe",
                  "status": "unrecovered", "verified": None, "successful_config_writes": 0}
        if attempts:
            record.update(attempts[-1])
            record["native_attempts"] = attempts
            if record["verified"] and record["application_state"] != "dev_targets_collecting":
                record.update(status="unrecovered", verified=None, failure="native_result_not_accepted_by_control_plane")
        elif patch and not report_native_result:
            measured, _ = measure_candidate(workspace, host, baseline, frozen, patch,
                                            resolve_class_names=bool(prior), case=case)
            record.update(measured)
            if prior and record["verified"]:
                record["status"] = "verified_candidate_component"
        record["wall_duration_ms"] = round((monotonic() - start) * 1000)
        record["image_cleanup"] = host.clean_images()
        return record
    except ValueError as error:
        if host.last_build_error:
            raise ValueError(f"{error}: {host.last_build_error[0]}") from None
        if str(error) == "isolated_release_probe_failed" and host.last_probe_failure:
            raise ValueError(f"{error}: {json.dumps(host.last_probe_failure, ensure_ascii=False)}") from None
        raise
    finally:
        if "record" not in locals() or "image_cleanup" not in record:
            host.clean_images()
        trial.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", choices=RELEASE_TRIALS, default="payment-12")
    parser.add_argument("--resume-candidate", type=Path, help="validate a saved candidate without calling the model again")
    parser.add_argument("--use-worktree-worker", action="store_true", help="test the worktree worker in temporary storage")
    parser.add_argument("--critic-sol", action="store_true", help="compare Sol as critic with the Astra control")
    parser.add_argument("--report-native-result", action="store_true", help="report local native build results to the isolated worker")
    args = parser.parse_args()
    target = args.output.resolve()
    if target.exists():
        raise SystemExit("the release receipt already exists")
    target.parent.mkdir(exist_ok=True, parents=True)
    if args.resume_candidate and args.use_worktree_worker:
        parser.error("choose either a saved candidate or an isolated worktree worker")
    if args.critic_sol and args.case not in ("payment-12", "member-13"):
        parser.error("--critic-sol is available for the payment-12 and member-13 routing experiments")
    if args.critic_sol and args.case == "payment-12" and not args.use_worktree_worker:
        parser.error("the payment-12 critic experiment requires --use-worktree-worker")
    if args.report_native_result and args.resume_candidate:
        parser.error("native worker feedback requires a new repair attempt")
    prior = json.loads(args.resume_candidate.read_text(encoding="utf-8")) if args.resume_candidate else None
    with TemporaryDirectory(prefix="release-", dir=target.parent.parent) as tmp:
        record = run(Path(tmp), prior, use_worktree_worker=args.use_worktree_worker, critic_sol=args.critic_sol,
                     report_native_result=args.report_native_result, case=args.case)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(record, output, indent=2, ensure_ascii=False)
        output.write("\n")
    print(json.dumps({key: record.get(key) for key in ("arm", "status", "reason_code", "failure", "wall_duration_ms",
                                                       "test_build_wall_ms", "usage", "estimated_usd")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
