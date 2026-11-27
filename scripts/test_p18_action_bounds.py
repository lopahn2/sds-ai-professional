"""Actual CEL regression; no Kubernetes writes. Reuses the independent reviewer tool.
Run with liveprobe/.venv/bin/python scripts/test_p18_action_bounds.py.
Scratch tools are intentionally explicit; no secret files or environment dumps.
"""
import copy
import json
from pathlib import Path
import runpy
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
REVIEW = Path('/Users/hwany/.hermes/cache/scratch/p18-independent-boundary')
OUT = Path('/Users/hwany/.hermes/cache/scratch/p18-action-bounds')


def fixtures():
    original = json.loads((REVIEW / 'cel-input.json').read_text())
    cases = original['cases']
    positive = {'control-noop', 'allowed-memory'}
    for case in cases:
        case['expected'] = case['name'].split(':', 1)[1] in positive
    baseline = cases[0]['old']

    def add(name, change, expected=False, prior=None, resource='deployments'):
        for ns in ('liveprobe-target', 'dev', 'stg', 'prd'):
            old = copy.deepcopy(prior or baseline)
            old['metadata']['namespace'] = ns
            if ns == 'liveprobe-target':
                old['metadata']['labels']['app.kubernetes.io/part-of'] = 'liveprobe-target'
            new = copy.deepcopy(old)
            change(new)
            # 2026-11-13 dev/prd 전환: stg는 퇴역 환경이라 실행자 허용 namespace에서 빠졌다. 같은 사례를 stg에서도 유지해
            # 다른 namespace에서 허용되는 변경도 stg에서는 실제 CEL이 거부하는지(퇴역 환경 실행 거부) 검증한다.
            cases.append({'name': ns + ':' + name, 'old': old, 'new': new, 'expected': expected and ns != 'stg',
                          'request': {'namespace': ns, 'resource': {'resource': resource},
                                      'userInfo': {'username': 'system:serviceaccount:liveprobe-system:safe-action-executor'}}})

    def c(n): return n['spec']['template']['spec']['containers'][0]
    def p(n): return n['spec']['template']['spec']
    add('positive-noop', lambda n: None, True)
    add('positive-scale', lambda n: n['spec'].update(replicas=2), True)
    add('positive-memory-lower', lambda n: c(n)['resources']['limits'].update(memory='256Mi'), True)
    add('positive-restart', lambda n: n['spec']['template']['metadata']['annotations'].update({'kubectl.kubernetes.io/restartedAt': '2026-10-25T12:00:00+09:00'}), True)
    restarted = copy.deepcopy(baseline)
    restarted['spec']['template']['metadata']['annotations']['kubectl.kubernetes.io/restartedAt'] = '2026-10-25T12:00:00+09:00'
    add('positive-restart-rollback-remove', lambda n: n['spec']['template']['metadata']['annotations'].pop('kubectl.kubernetes.io/restartedAt'), True, restarted)
    add('positive-restart-rollback-replace', lambda n: n['spec']['template']['metadata']['annotations'].update({'kubectl.kubernetes.io/restartedAt': '2026-10-24T01:02:03Z'}), True, restarted)
    no_annotations = copy.deepcopy(baseline)
    no_annotations['spec']['template']['metadata'].pop('annotations')
    add('positive-noop-annotations-absent', lambda n: None, True, no_annotations)
    add('positive-restart-annotations-absent', lambda n: n['spec']['template']['metadata'].update(annotations={'kubectl.kubernetes.io/restartedAt': '2026-10-25T03:00:00Z'}), True, no_annotations)
    add('memory-plus-command', lambda n: (c(n)['resources']['limits'].update(memory='768Mi'), c(n).update(command=['bad'])))
    annotated = copy.deepcopy(baseline)
    annotated['metadata']['annotations'] = {'review.invalid/unchanged': 'fixed'}
    annotated['spec']['template']['metadata']['annotations']['review.invalid/unchanged'] = 'fixed'
    add('remove-top-annotation', lambda n: n['metadata'].pop('annotations'), prior=annotated)
    add('remove-template-annotation', lambda n: n['spec']['template']['metadata']['annotations'].pop('review.invalid/unchanged'), prior=annotated)
    add('positive-restart-preserve-other-annotations', lambda n: n['spec']['template']['metadata']['annotations'].update({'kubectl.kubernetes.io/restartedAt': '2026-10-25T03:00:00.123Z'}), True, annotated)
    scaled = copy.deepcopy(baseline); scaled['spec']['replicas'] = 2
    add('positive-scale-rollback', lambda n: n['spec'].update(replicas=1), True, scaled)
    raised = copy.deepcopy(baseline); c(raised)['resources']['limits']['memory'] = '768Mi'
    add('positive-memory-rollback', lambda n: c(n)['resources']['limits'].update(memory='512Mi'), True, raised)
    changes = {
        'future-spec-field': lambda n: n['spec'].update(futureField={'arbitrary': True}),
        'future-template-field': lambda n: n['spec']['template'].update(futureField=True),
        'future-pod-field': lambda n: p(n).update(futureField={'arbitrary': True}),
        'future-container-field': lambda n: c(n).update(futureField=True),
        'future-resource-field': lambda n: c(n)['resources'].update(futureField=True),
        'metadata-future-field': lambda n: n['metadata'].update(futureField=True),
        'init-add': lambda n: p(n).update(initContainers=[{'name': 'init', 'image': 'example.invalid/init:v1'}]),
        'command': lambda n: c(n).update(command=['sh']),
        'args': lambda n: c(n).update(args=['bad']),
        'workingDir': lambda n: c(n).update(workingDir='/bad'),
        'cpu-request': lambda n: c(n)['resources']['requests'].update(cpu='1'),
        'cpu-remove': lambda n: c(n)['resources']['limits'].pop('cpu'),
        'extra-resource-limit': lambda n: c(n)['resources']['limits'].update({'ephemeral-storage': '1Gi'}),
        'memory-too-low': lambda n: c(n)['resources']['limits'].update(memory='255Mi'),
        'memory-too-high': lambda n: c(n)['resources']['limits'].update(memory='769Mi'),
        'memory-remove': lambda n: c(n)['resources']['limits'].pop('memory'),
        'replicas-zero': lambda n: n['spec'].update(replicas=0),
        'strategy': lambda n: n['spec'].update(strategy={'type': 'Recreate'}),
        'paused': lambda n: n['spec'].update(paused=True),
        'revisionHistoryLimit': lambda n: n['spec'].update(revisionHistoryLimit=0),
        'minReadySeconds': lambda n: n['spec'].update(minReadySeconds=10),
        'progressDeadlineSeconds': lambda n: n['spec'].update(progressDeadlineSeconds=100),
        'metadata-finalizers': lambda n: n['metadata'].update(finalizers=['review.invalid/lock']),
        'metadata-ownerReferences': lambda n: n['metadata'].update(ownerReferences=[]),
        'metadata-annotations-remove': lambda n: n['metadata'].update(annotations={}),
        'template-name': lambda n: n['spec']['template']['metadata'].update(name='bad'),
        'template-finalizers': lambda n: n['spec']['template']['metadata'].update(finalizers=['review.invalid/lock']),
        'template-other-annotation': lambda n: n['spec']['template']['metadata']['annotations'].update({'sidecar.istio.io/inject': 'true'}),
        'restart-near-match': lambda n: n['spec']['template']['metadata']['annotations'].update({'kubectl.kubernetes.io/restartedAt-extra': '2026-10-25T12:00:00Z'}),
        'restart-invalid': lambda n: n['spec']['template']['metadata']['annotations'].update({'kubectl.kubernetes.io/restartedAt': 'arbitrary'}),
        'hostPID': lambda n: p(n).update(hostPID=True),
        'dnsPolicy': lambda n: p(n).update(dnsPolicy='Default'),
        'nodeSelector': lambda n: p(n).update(nodeSelector={'bad': 'true'}),
        'pod-resources': lambda n: p(n).update(resources={'limits': {'cpu': '4'}}),
        'probe': lambda n: c(n).update(readinessProbe={'exec': {'command': ['false']}}),
        'ports': lambda n: c(n).update(ports=[{'containerPort': 9999}]),
        'lifecycle': lambda n: c(n).update(lifecycle={'postStart': {'exec': {'command': ['false']}}}),
    }
    for name, change in changes.items(): add(name, change)
    initialized = copy.deepcopy(baseline)
    p(initialized)['initContainers'] = [{'name': 'init', 'image': 'example.invalid/init:v1', 'env': [{'name': 'REVIEW', 'value': 'old'}]}]
    add('init-modify-image', lambda n: p(n)['initContainers'][0].update(image='example.invalid/other:v2'), prior=initialized)
    add('init-modify-env', lambda n: p(n)['initContainers'][0]['env'][0].update(value='changed'), prior=initialized)
    add('init-remove', lambda n: p(n).pop('initContainers'), prior=initialized)
    add('init-unchanged-noop', lambda n: None, True, initialized)
    sidecar = copy.deepcopy(baseline); p(sidecar)['containers'].append({'name': 'sidecar', 'image': 'example.invalid/sidecar:v1'})
    add('multi-container-noop', lambda n: None, prior=sidecar)
    cm = {'metadata': {'name': 'order-config', 'labels': {'liveprobe.io/managed': 'true'}}, 'data': {'REVIEW': 'old'}}
    add('configmap-data-only', lambda n: n['data'].update(REVIEW='unapproved'), prior=cm, resource='configmaps')
    add('configmap-unrelated-name', lambda n: n['metadata'].update(name='unrelated'), prior=cm, resource='configmaps')
    add('configmap-noop-denied', lambda n: None, prior=cm, resource='configmaps')
    return cases


def evaluate(base=False):
    OUT.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(ROOT / 'liveprobe/.venv/bin/python'), str(REVIEW / 'prepare.py')], check=True, capture_output=True)
    render = runpy.run_path(str(ROOT / 'scripts/render-p18-actions.py'))['render']
    if base:
        import yaml
        items = list(yaml.safe_load_all((ROOT / 'deploy/optional/safe-action-executor/safe-action-executor.yaml').read_text()))
    else:
        items = render()
    policy = next(x for x in items if x and x['kind'] == 'ValidatingAdmissionPolicy' and x['metadata']['name'] == 'safe-action-executor-bounds')
    cases = fixtures()
    if base:
        cases = [case for case in cases if case['request']['namespace'] == 'liveprobe-target']
    prefix = 'base-' if base else ''
    payload = {'expressions': [v['expression'] for v in policy['spec']['validations']], 'cases': cases}
    (OUT / (prefix + 'input.json')).write_text(json.dumps(payload))
    result = subprocess.run(['go', 'run', '-mod=mod', '.', str(OUT / (prefix + 'input.json'))], cwd=REVIEW, check=True, capture_output=True, text=True)
    (OUT / (prefix + 'results.jsonl')).write_text(result.stdout)
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(rows) == len(cases)
    return cases, rows


class ActionBounds(unittest.TestCase):
    def test_base_policy_actual_cel(self):
        cases, rows = evaluate(base=True)
        self.assertTrue(cases)
        self.assertEqual([(c['name'], c['expected']) for c in cases], [(r['case'], r['allowed']) for r in rows])
        self.assertFalse([r for r in rows if r['errors']])
        summary = {'base_cases': len(cases), 'allowed': sum(r['allowed'] for r in rows),
                   'denied': sum(not r['allowed'] for r in rows), 'error_cases': 0, 'mismatches': 0}
        (OUT / 'base-summary.json').write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary))

    def test_actual_cel(self):
        cases, rows = evaluate()
        failures = []
        for case, row in zip(cases, rows):
            if case['name'] != row['case'] or case['expected'] != row['allowed']:
                failures.append({'case': case['name'], 'expected': case['expected'], 'actual': row})
        summary = {'cases': len(cases), 'allowed': sum(r['allowed'] for r in rows),
                   'denied': sum(not r['allowed'] for r in rows), 'error_cases': sum(bool(r['errors']) for r in rows),
                   'mismatches': len(failures)}
        (OUT / 'summary.json').write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary))
        self.assertEqual(failures, [], json.dumps(failures, indent=2))
        self.assertEqual(summary['error_cases'], 0, 'fixtures must return boolean deny, not evaluation errors')

    def test_new_environment_configmap_patch_removed(self):
        items = runpy.run_path(str(ROOT / 'scripts/render-p18-actions.py'))['render']()
        for item in items:
            if item['kind'] == 'Role' and item['metadata']['namespace'] in ('dev', 'stg', 'prd'):
                for rule in item['rules']:
                    if 'configmaps' in rule['resources']:
                        self.assertNotIn('patch', rule['verbs'])
                        self.assertEqual(rule['verbs'], ['get'])

    def test_retired_stg_has_no_executor_rbac_and_admission_excludes_it(self):
        # 2026-11-13 dev/prd 전환: 실행자 쓰기 권한은 활성 환경 dev/prd에만 둔다(권한 축소). go/CEL 도구 없이도 도는 선언 검사.
        items = runpy.run_path(str(ROOT / 'scripts/render-p18-actions.py'))['render']()
        rbac = [i for i in items if i['kind'] in ('Role', 'RoleBinding')]
        self.assertEqual({i['metadata']['namespace'] for i in rbac}, {'dev', 'prd'})
        self.assertFalse([i for i in items if i['metadata'].get('namespace') == 'stg'])
        policy = next(i for i in items if i['kind'] == 'ValidatingAdmissionPolicy' and i['metadata']['name'] == 'safe-action-executor-bounds')
        validations = [v['expression'] for v in policy['spec']['validations']]
        self.assertEqual(validations[0], "request.namespace in ['liveprobe-target','dev','prd']")
        self.assertIn("request.namespace in ['dev','prd']", validations[1])
        self.assertFalse(any("'stg'" in v for v in validations), '퇴역 환경은 실행자 허용 조건에 남지 않는다')
        # LiveProbe API 신원 쓰기 '거부' 정책은 반대 방향이다. stg를 빼면 보존 중인 퇴역 namespace의 거부가 사라지므로 유지한다.
        guard = next(i for i in items if i['kind'] == 'ValidatingAdmissionPolicy' and i['metadata']['name'] == 'liveprobe-deny-target-writes')
        self.assertEqual(guard['spec']['matchConditions'][0]['expression'], "request.namespace in ['liveprobe-target','dev','stg','prd']")

    def test_committed_actions_declaration_matches_renderer(self):
        # 커밋된 선언이 렌더러와 어긋나 stg 권한이 남아 있지 않도록 재현성을 확인한다.
        items = runpy.run_path(str(ROOT / 'scripts/render-p18-actions.py'))['render']()
        committed = json.loads((ROOT / 'deploy/environments/actions.json').read_text())
        self.assertEqual(committed, {'apiVersion': 'v1', 'kind': 'List', 'items': items})

    def test_registered_deployment_owners_are_matched_by_workload_name(self):
        items = runpy.run_path(str(ROOT / 'scripts/render-p18-actions.py'))['render']()
        policy = next(i for i in items if i['metadata']['name'] == 'safe-action-executor-bounds'
                      and i['kind'] == 'ValidatingAdmissionPolicy')
        owner_check = policy['spec']['validations'][-1]['expression']
        self.assertIn("(oldObject.metadata.name == 'catalog' && "
                      "oldObject.metadata.labels[?'app.kubernetes.io/part-of'].orValue('') in [\"catalog\",\"samsung-trade\"])", owner_check)
        self.assertIn("(oldObject.metadata.name == 'delivery' && "
                      "oldObject.metadata.labels[?'app.kubernetes.io/part-of'].orValue('') in [\"delivery\",\"samsung-delivery\"])", owner_check)
        self.assertNotIn("oldObject.metadata.name == 'business-api'", owner_check)

    def test_observability_rules_watch_only_active_environments(self):
        # 활성 환경 dev/prd만 새로 기록·경보한다. 커밋된 선언은 렌더러 출력과 같아야 한다.
        import yaml
        cm = runpy.run_path(str(ROOT / 'scripts/render-p18-observability.py'))['render']()
        self.assertEqual(json.loads((ROOT / 'deploy/environments/observability.json').read_text()), cm)
        self.assertNotIn('stg', cm['data']['recording.yml'])
        self.assertIn('namespace=~"liveprobe-target|dev|prd"', cm['data']['recording.yml'])
        group = next(g for g in yaml.safe_load(cm['data']['alerts.yml'])['groups'] if g['name'] == 'p18-environments')
        self.assertEqual(len(group['rules']), 9)
        for rule in group['rules']:
            self.assertIn('namespace=~"dev|prd"', rule['expr'])
            self.assertNotIn('stg', rule['expr'])
        no_endpoints = next(rule for rule in group['rules'] if rule['alert'] == 'TargetServiceNoEndpoints')
        self.assertEqual(no_endpoints['for'], '10m')
        oom = next(rule for rule in group['rules'] if rule['alert'] == 'TargetWorkloadOOMKilled')
        self.assertEqual(oom['for'], '0s')
        self.assertIn('and on (namespace,pod,uid,container)', oom['expr'])
        self.assertIn('time() - kube_pod_container_status_last_terminated_timestamp{namespace=~"dev|prd"} < 120', oom['expr'])
        listeners = next(rule for rule in group['rules'] if rule['alert'] == 'TargetProxyListenerUpdateRejected')
        self.assertIn('increase(envoy_listener_manager_lds_update_rejected', listeners['expr'])
        self.assertIn('on (namespace,pod,pod_uid,instance)', listeners['expr'])
        proxy_job = next(job for job in yaml.safe_load(cm['data']['prometheus.yml'])['scrape_configs']
                         if job['job_name'] == 'envoy-stats')
        self.assertIn({'source_labels': ['__meta_kubernetes_pod_uid'], 'target_label': 'pod_uid'},
                      proxy_job['relabel_configs'])
        self.assertIn('envoy_listener_manager_lds_update_rejected', proxy_job['metric_relabel_configs'][0]['regex'])
        route_alert = next(rule for rule in group['rules'] if rule['alert'] == 'TargetRouteSlowRequests')
        error_alert = next(rule for rule in group['rules'] if rule['alert'] == 'TargetHigh5xxRate')
        self.assertIn('sum by (namespace, app, method, uri)', error_alert['expr'])
        self.assertIn('or on (namespace, app)', error_alert['expr'])
        self.assertIn('> 0.04', error_alert['expr'])
        self.assertEqual(error_alert['for'], '15s')
        route_group = next(g for g in yaml.safe_load(cm['data']['recording.yml'])['groups']
                           if g['name'] == 'p18-business-route-latency')
        rule, = route_group['rules']
        self.assertIn(rule['record'], route_alert['expr'])
        self.assertIn('sum by (namespace, app, method, uri)', rule['expr'])
        self.assertIn('le="0.5"', rule['expr'])
        self.assertIn('le="+Inf"', rule['expr'])


if __name__ == '__main__':
    unittest.main()
