#!/usr/bin/env python3
"""실환경 없이 실제 계획·CAS 조정기를 검사한다. 전송 계층만 대역이다."""
import copy
import importlib.util
from pathlib import Path
import types
import unittest

from unittest.mock import patch

_GUARDS = [patch('socket.socket.connect', side_effect=AssertionError('오프라인 시험의 네트워크 호출 금지')),
           patch('subprocess.Popen', side_effect=AssertionError('오프라인 시험의 외부 프로세스 호출 금지'))]


def setUpModule():
    for guard in _GUARDS:
        guard.start()


def tearDownModule():
    for guard in reversed(_GUARDS):
        guard.stop()


PATH = Path(__file__).with_name('samsung_cutover.py')
if PATH.exists():
    spec = importlib.util.spec_from_file_location('samsung_cutover', PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
else:
    mod = types.SimpleNamespace()


def resource(kind='Deployment', name='order', env='dev'):
    return {'apiVersion': 'apps/v1' if kind == 'Deployment' else 'v1',
            'kind': kind, 'metadata': {'namespace': env, 'name': name,
                                     'uid': '검사-uid', 'resourceVersion': '17'},
            'spec': {'replicas': 1}}


class CasTests(unittest.TestCase):
    def test_scale_uses_atomic_uid_rv_tests_before_mutation(self):
        self.assertTrue(callable(getattr(mod, 'cas_request', None)), 'CAS 요청 생성 기능이 없다')
        change = {'action': 'scale', 'before': resource(), 'replicas': 0}
        request = mod.cas_request('dev', change, dry_run=True)
        self.assertEqual(request['method'], 'PATCH')
        self.assertEqual(request['path'], '/apis/apps/v1/namespaces/dev/deployments/order?dryRun=All')
        self.assertEqual(request['content_type'], 'application/json-patch+json')
        self.assertEqual(request['body'], [
            {'op': 'test', 'path': '/metadata/uid', 'value': '검사-uid'},
            {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '17'},
            {'op': 'add', 'path': '/spec/replicas', 'value': 0}])
        self.assertEqual(change['before']['spec']['replicas'], 1)
    def test_apply_existing_tests_uid_rv_before_adding_reviewed_fields(self):
        before = resource('ConfigMap', 'order-config')
        desired = {'apiVersion': 'v1', 'kind': 'ConfigMap',
                   'metadata': {'name': 'order-config', 'namespace': 'dev'},
                   'data': {'SELF_TRAFFIC': '0'}}
        request = mod.cas_request('dev', {'action': 'apply', 'before': before, 'desired': desired}, dry_run=False)
        self.assertEqual(request['body'][:2], [
            {'op': 'test', 'path': '/metadata/uid', 'value': '검사-uid'},
            {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '17'}])
        self.assertEqual(request['body'][2:], [{'op': 'add', 'path': '/data', 'value': desired['data']}])

    def test_delete_dryrun_is_in_deleteoptions_body_with_both_preconditions(self):
        request = mod.cas_request('dev', {'action': 'delete', 'before': resource('Service', 'member')}, dry_run=True)
        self.assertEqual(request['method'], 'DELETE')
        self.assertNotIn('?', request['path'])
        self.assertEqual(request['body'], {'apiVersion': 'v1', 'kind': 'DeleteOptions',
            'preconditions': {'uid': '검사-uid', 'resourceVersion': '17'},
            'propagationPolicy': 'Foreground', 'dryRun': ['All']})

    def test_create_uses_post_without_overwrite_or_cas_fallback(self):
        desired = resource('Service', 'trade-member')
        del desired['metadata']['uid'], desired['metadata']['resourceVersion']
        request = mod.cas_request('dev', {'action': 'apply', 'before': None, 'desired': desired}, dry_run=True)
        self.assertEqual(request['method'], 'POST')
        self.assertEqual(request['path'], '/api/v1/namespaces/dev/services?dryRun=All')
        self.assertEqual(request['body'], desired)

    def test_refuses_invalid_or_forbidden_targets_and_weak_preconditions(self):
        cases = []
        for kind, name, env in [('Secret', 'member', 'dev'), ('PersistentVolumeClaim', 'member', 'dev'),
                ('StatefulSet', 'inventory-db', 'dev'), ('Namespace', 'dev', 'dev'),
                ('Deployment', 'trade-db', 'dev'), ('Deployment', 'order', 'stg'),
                ('Deployment', '../order', 'dev')]:
            cases.append({'action': 'scale', 'before': resource(kind, name, env), 'replicas': 0})
        for key in ('uid', 'resourceVersion'):
            for value in (None, '', 17):
                before = resource()
                before['metadata'][key] = value
                cases.append({'action': 'scale', 'before': before, 'replicas': 0})
        for value in (-1, True, '0', 2):
            cases.append({'action': 'scale', 'before': resource(), 'replicas': value})
        cases.append({'action': 'delete', 'before': resource('Service', 'inventory')})
        for change in cases:
            with self.subTest(change=change), self.assertRaises(mod.CutoverError):
                mod.cas_request('dev', change, dry_run=False)
        for env in ('all', 'dev/stg', '', None):
            with self.subTest(env=env), self.assertRaises(mod.CutoverError):
                mod.cas_request(env, {'action': 'scale', 'before': resource(), 'replicas': 0}, dry_run=False)


    def test_apply_service_preserves_server_assigned_spec_fields(self):
        desired = {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'order', 'namespace': 'dev'},
                   'spec': {'selector': {'app': 'order'}, 'ports': [{'port': 8080, 'targetPort': 8080}]}}
        request = mod.cas_request('dev', {'action': 'apply', 'before': resource('Service', 'order'),
                                         'desired': desired}, dry_run=True)
        self.assertNotIn('/spec', [op['path'] for op in request['body'][2:]])
        self.assertEqual(request['body'][2:], [
            {'op': 'add', 'path': '/spec/selector', 'value': {'app': 'order'}},
            {'op': 'add', 'path': '/spec/ports', 'value': [{'port': 8080, 'targetPort': 8080}]}])


NOW = '2026-11-01T09:03:00+00:00'
APPROVAL_SHA = 'e8c7dda3962de4618c4b9295ef770ea6a2e7aed4e5f977ccd6ee81a526e720b2'
LEGACY = ['order', 'inventory', 'catalog', 'payment', 'delivery', 'notification', 'member']


def digest(value):
    import hashlib
    import json
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def envelope(kind, env, at):
    return {'kind': kind, 'environment': env, 'context': 'rancher-desktop',
            'observed_at': at, 'artifact_sha256': 'a' * 64}


def evidence(env='dev'):
    backup = dict(envelope('backup', env, '2026-11-01T09:00:00+00:00'),
        available_backup_saved=True, complete_memory_snapshot=False,
        memory_reset_approval_sha256=APPROVAL_SHA,
        entries=[{'service': s, 'records': 1, 'sha256': 'b' * 64} for s in LEGACY])
    writers = dict(envelope('writers_stopped', env, '2026-11-01T09:01:00+00:00'),
        services=LEGACY.copy(), old_writes_rejected=True, self_traffic_stopped=True,
        inventory_frozen=True, external_maintenance_blocked=True, backup_receipt_sha256=digest(backup),
        terminations=[{'service': s, 'deployment_uid': '검사-uid', 'resourceVersion': '17',
            'generation': 2, 'observed_generation': 2, 'desired_replicas': 0,
            'owned_pods': [], 'previous_pod_uids': ['기존-pod-' + s],
            'absent_pod_uids': ['기존-pod-' + s], 'running_containers': 0,
            'owner_chain_complete': True, 'container_inventory_complete': True} for s in LEGACY])
    inventory = dict(envelope('inventory_migration', env, '2026-11-01T09:02:00+00:00'),
        writer_receipt_sha256=digest(writers), final_dump_sha256='c' * 64,
        dumped_at='2026-11-01T09:01:10+00:00', restored_at='2026-11-01T09:01:40+00:00',
        source={'database': 'legacy_inventory_' + env, 'server_ref': 'legacy-pg', 'records': 1, 'keys_sha256': 'd' * 64,
                'content_sha256': 'e' * 64, 'versions_sha256': 'f' * 64},
        target={'database': 'inventory_' + env, 'server_ref': 'trade-db.liveprobe-data.svc:5432', 'records': 1, 'keys_sha256': 'd' * 64,
                'content_sha256': 'e' * 64, 'versions_sha256': 'f' * 64})
    return {'backup': backup, 'writers_stopped': writers, 'inventory_migration': inventory}


def acceptance(env):
    return dict(envelope('environment_acceptance', env, NOW), state='accepted',
        checks=['data', 'single_writer', 'api', 'network', 'tls', 'resources', 'readback', 'rollback'])


class PlanTests(unittest.TestCase):
    def test_cutover_requires_exact_linked_preservation_receipts(self):
        self.assertTrue(callable(getattr(mod, 'prepare_plan', None)), '단계별 계획 검증이 없다')
        change = {'action': 'scale', 'before': resource(), 'replicas': 0}
        plan = mod.prepare_plan('dev', 'cutover', change, receipts=evidence(), previous=[], now=NOW)
        self.assertEqual(plan['environment'], 'dev')
        self.assertEqual(plan['context'], 'rancher-desktop')
        self.assertEqual(plan['change']['before']['metadata']['resourceVersion'], '17')
        for key in ('backup', 'writers_stopped', 'inventory_migration'):
            bad = evidence()
            del bad[key]
            with self.subTest(key=key), self.assertRaises(mod.CutoverError):
                mod.prepare_plan('dev', 'cutover', change, receipts=bad, previous=[], now=NOW)

    def test_memory_exception_never_bypasses_inventory_or_freeze_comparison(self):
        self.assertTrue(callable(getattr(mod, 'prepare_plan', None)), '단계별 계획 검증이 없다')
        mutations = [lambda e: e['backup'].update(memory_reset_approval_sha256='0' * 64),
            lambda e: e['backup']['entries'].pop(),
            lambda e: e['writers_stopped'].update(old_writes_rejected=False),
            lambda e: e['writers_stopped'].update(self_traffic_stopped=False),
            lambda e: e['inventory_migration']['target'].update(records=2),
            lambda e: e['inventory_migration']['target'].update(content_sha256='0' * 64),
            lambda e: e['inventory_migration'].update(writer_receipt_sha256='0' * 64),
            lambda e: e['inventory_migration'].update(dumped_at='2026-11-01T08:00:00+00:00'),
            lambda e: e['inventory_migration'].update(environment='stg')]
        for mutate in mutations:
            bad = evidence()
            mutate(bad)
            with self.subTest(mutation=mutations.index(mutate)), self.assertRaises(mod.CutoverError):
                mod.prepare_plan('dev', 'cutover', {'action': 'scale', 'before': resource(), 'replicas': 0},
                                 receipts=bad, previous=[], now=NOW)

    def test_promotion_requires_each_prior_environment_acceptance(self):
        self.assertTrue(callable(getattr(mod, 'prepare_plan', None)), '단계별 계획 검증이 없다')
        for env, previous in [('stg', []), ('prd', [acceptance('dev')]), ('prd', [acceptance('stg')])]:
            with self.subTest(env=env, previous=previous), self.assertRaises(mod.CutoverError):
                mod.prepare_plan(env, 'cutover', {'action': 'scale', 'before': resource(env=env), 'replicas': 0},
                                 receipts=evidence(env), previous=previous, now=NOW)
        mod.prepare_plan('prd', 'cutover', {'action': 'scale', 'before': resource(env='prd'), 'replicas': 0},
                         receipts=evidence('prd'), previous=[acceptance('dev'), acceptance('stg')], now=NOW)


    def test_readonly_selftraffic_and_inventory_wiring_are_fail_closed(self):
        for name, data in [('business-api-config', {'DOMAIN_MODE': 'business', 'READ_ONLY_MODE': '0', 'SELF_TRAFFIC': '0'}),
                ('order-config', {'SELF_TRAFFIC': '1'}),
                ('inventory-config', {'SELF_TRAFFIC': '0', 'INVENTORY_DB_URL': 'jdbc:postgresql://old-db/db'})]:
            before = resource('ConfigMap', name)
            desired = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'namespace': 'dev', 'name': name}, 'data': data}
            with self.subTest(name=name), self.assertRaises(mod.CutoverError):
                mod.prepare_plan('dev', 'cutover', {'action': 'apply', 'before': before, 'desired': desired},
                                 receipts=evidence(), previous=[], now=NOW)
        desired = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'namespace': 'dev', 'name': 'business-api-config'},
                   'data': {'DOMAIN_MODE': 'business', 'READ_ONLY_MODE': '0', 'SELF_TRAFFIC': '0'}}
        mod.prepare_plan('dev', 'activate', {'action': 'apply', 'before': resource('ConfigMap', 'business-api-config'),
                         'desired': desired}, receipts=evidence(), previous=[], now=NOW)

    def test_legacy_member_cannot_be_restarted_or_reconnected_as_rollback(self):
        with self.assertRaises(mod.CutoverError):
            mod.prepare_plan('dev', 'cutover', {'action': 'scale', 'before': resource(name='member'), 'replicas': 1},
                             receipts=evidence(), previous=[], now=NOW)
        desired = {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'namespace': 'dev', 'name': 'member'}, 'spec': {'selector': {'app': 'member'}}}
        with self.assertRaises(mod.CutoverError):
            mod.prepare_plan('dev', 'cutover', {'action': 'apply', 'before': resource('Service', 'member'), 'desired': desired},
                             receipts=evidence(), previous=[], now=NOW)


    def test_writer_ready_zero_without_owned_pod_and_container_termination_is_refused(self):
        bad = evidence()
        bad['writers_stopped']['ready_replicas'] = 0
        bad['writers_stopped'].pop('terminations', None)
        bad['inventory_migration']['writer_receipt_sha256'] = digest(bad['writers_stopped'])
        with self.assertRaises(mod.CutoverError):
            mod.prepare_plan('dev', 'cutover', {'action': 'scale', 'before': resource(), 'replicas': 0},
                             receipts=bad, previous=[], now=NOW)

    def test_inventory_same_db_name_in_different_servers_is_valid_but_same_endpoint_is_not(self):
        good = evidence()
        good['inventory_migration']['source']['database'] = 'inventory_dev'
        mod.prepare_plan('dev', 'cutover', {'action': 'scale', 'before': resource(), 'replicas': 0},
                         receipts=good, previous=[], now=NOW)
        good['inventory_migration']['source']['server_ref'] = good['inventory_migration']['target']['server_ref']
        with self.assertRaises(mod.CutoverError):
            mod.prepare_plan('dev', 'cutover', {'action': 'scale', 'before': resource(), 'replicas': 0},
                             receipts=good, previous=[], now=NOW)


class QueueTransport:
    """HTTP 상태·본문만 대역이며 계획과 검증 로직은 교체하지 않는다."""
    def __init__(self, responses, context='rancher-desktop'):
        self.responses = list(responses)
        self.calls = []
        self.pinned_context = context

    def context(self):
        return self.pinned_context

    def request(self, request):
        self.calls.append(copy.deepcopy(request))
        if not self.responses:
            raise AssertionError('예정하지 않은 전송이다')
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


class ExecuteTests(unittest.TestCase):
    def setUp(self):
        self.plan = mod.prepare_plan('dev', 'cutover', {'action': 'scale', 'before': resource(), 'replicas': 0},
                                     receipts=evidence(), previous=[], now=NOW)

    def execute(self, transport, **options):
        return mod.execute_one(self.plan, reviewed_sha256=digest(self.plan), transport=transport, now=NOW, **options)

    def dry_receipt(self):
        after = resource()
        after['spec']['replicas'] = 0
        transport = QueueTransport([(200, resource()), (200, after), (200, resource())])
        return self.execute(transport, dry_run=True)

    def test_server_dryrun_is_not_local_simulation_and_real_apply_reads_exact_target(self):
        self.assertTrue(callable(getattr(mod, 'execute_one', None)), '전송·재조회 실행기가 없다')
        dry = self.dry_receipt()
        after = resource()
        after['spec']['replicas'] = 0
        after['metadata']['resourceVersion'] = '18'
        transport = QueueTransport([(200, resource()), (200, after), (200, after)])
        receipt = self.execute(transport, dry_run=False, dry_run_receipt=dry)
        self.assertEqual([r['method'] for r in transport.calls], ['GET', 'PATCH', 'GET'])
        self.assertEqual(transport.calls[0]['path'], transport.calls[-1]['path'])
        self.assertNotIn('dryRun', transport.calls[1]['path'])
        self.assertEqual(receipt['state'], 'readback_verified')
        self.assertEqual(receipt['readback']['resourceVersion'], '18')

    def test_409_stops_without_refresh_retry_or_readback(self):
        self.assertTrue(callable(getattr(mod, 'execute_one', None)), '전송·재조회 실행기가 없다')
        for dry_run in (True, False):
            transport = QueueTransport([(200, resource()), (409, {'message': '출력하면 안 되는 내부 진단'})])
            with self.subTest(dry_run=dry_run), self.assertRaises(mod.CutoverError) as caught:
                self.execute(transport, dry_run=dry_run, dry_run_receipt=None if dry_run else self.dry_receipt())
            self.assertEqual([r['method'] for r in transport.calls], ['GET', 'PATCH'])
            self.assertNotIn('내부 진단', str(caught.exception))

    def test_wrong_context_changed_identity_or_unreviewed_plan_never_writes(self):
        self.assertTrue(callable(getattr(mod, 'execute_one', None)), '전송·재조회 실행기가 없다')
        for key, value in [('uid', '다른-uid'), ('resourceVersion', '18')]:
            observed = resource()
            observed['metadata'][key] = value
            transport = QueueTransport([(200, observed)])
            with self.subTest(key=key), self.assertRaises(mod.CutoverError):
                self.execute(transport, dry_run=True)
            self.assertEqual(len(transport.calls), 1)
        transport = QueueTransport([], context='other')
        with self.assertRaises(mod.CutoverError):
            self.execute(transport, dry_run=True)
        self.assertEqual(transport.calls, [])
        with self.assertRaises(mod.CutoverError):
            mod.execute_one(self.plan, reviewed_sha256='0' * 64, transport=QueueTransport([]), now=NOW, dry_run=True)

    def test_real_write_requires_bound_dryrun_and_mismatched_readback_is_failure(self):
        self.assertTrue(callable(getattr(mod, 'execute_one', None)), '전송·재조회 실행기가 없다')
        transport = QueueTransport([])
        with self.assertRaises(mod.CutoverError):
            self.execute(transport, dry_run=False)
        self.assertEqual(transport.calls, [])
        dry = self.dry_receipt()
        changed = copy.deepcopy(dry)
        changed['plan_sha256'] = '0' * 64
        with self.assertRaises(mod.CutoverError):
            self.execute(transport, dry_run=False, dry_run_receipt=changed)
        after = resource()
        after['spec']['replicas'] = 0
        after['metadata']['resourceVersion'] = '18'
        transport = QueueTransport([(200, resource()), (200, after), (200, resource())])
        with self.assertRaises(mod.CutoverError):
            self.execute(transport, dry_run=False, dry_run_receipt=dry)
        self.assertEqual(len(transport.calls), 3)


class BoundaryTests(unittest.TestCase):
    def plan(self, change):
        return mod.prepare_plan('dev', 'cutover', change, receipts=evidence(), previous=[], now=NOW)

    def run_one(self, plan, transport, **options):
        return mod.execute_one(plan, reviewed_sha256=digest(plan), transport=transport, now=NOW, **options)

    def test_server_dryrun_is_followed_by_nonpersistence_readback(self):
        plan = self.plan({'action': 'scale', 'before': resource(), 'replicas': 0})
        after = resource()
        after['spec']['replicas'] = 0
        changed = resource()
        changed['metadata']['resourceVersion'] = '18'
        transport = QueueTransport([(200, resource()), (200, after), (200, changed)])
        with self.assertRaises(mod.CutoverError):
            self.run_one(plan, transport, dry_run=True)
        self.assertEqual(len(transport.calls), 3)

    def test_delete_failure_status_cannot_be_success_even_with_http_200(self):
        before = resource('Service', 'member')
        plan = self.plan({'action': 'delete', 'before': before})
        transport = QueueTransport([(200, before), (200, {'kind': 'Status', 'status': 'Failure'}), (200, before)])
        with self.assertRaises(mod.CutoverError):
            self.run_one(plan, transport, dry_run=True)
        self.assertEqual(len(transport.calls), 2)

    def test_delete_requires_absence_after_real_delete_not_merely_accepted(self):
        before = resource('Service', 'member')
        plan = self.plan({'action': 'delete', 'before': before})
        success = {'kind': 'Status', 'status': 'Success'}
        dry = self.run_one(plan, QueueTransport([(200, before), (200, success), (200, before)]), dry_run=True)
        for status, body in [(200, before), (200, dict(before, metadata=dict(before['metadata'], uid='새-uid'))), (403, {})]:
            transport = QueueTransport([(200, before), (202, success), (status, body)])
            with self.subTest(status=status, body=body), self.assertRaises(mod.CutoverError):
                self.run_one(plan, transport, dry_run=False, dry_run_receipt=dry)
            self.assertEqual(len(transport.calls), 3)
        result = self.run_one(plan, QueueTransport([(200, before), (200, success), (404, {})]),
                              dry_run=False, dry_run_receipt=dry)
        self.assertEqual(result['readback'], {'absent': True})

    def test_mutating_caller_plan_during_get_cannot_change_reviewed_request(self):
        plan = self.plan({'action': 'scale', 'before': resource(), 'replicas': 0})
        after = resource()
        after['spec']['replicas'] = 0
        class MutatingTransport(QueueTransport):
            def request(self, request):
                result = super().request(request)
                plan['change']['replicas'] = 1
                return result
        transport = MutatingTransport([(200, resource()), (200, after), (200, resource())])
        self.run_one(plan, transport, dry_run=True)
        self.assertEqual(transport.calls[1]['body'][-1]['value'], 0)


class AdapterTests(unittest.TestCase):
    def test_kubernetes36_response_types_map_matches_real_sdk_signature(self):
        import sys
        calls = []
        class Configuration:
            verify_ssl = True
            host = 'https://example.invalid'
        class ApiClient:
            def __init__(self, configuration):
                self.configuration = configuration
            def call_api(self, path, method, *, query_params, body, header_params,
                         response_types_map, auth_settings, _return_http_data_only,
                         _preload_content, _request_timeout):
                calls.append(response_types_map)
                return {'kind': 'Deployment'}, 200, {}
        package = types.SimpleNamespace(
            client=types.SimpleNamespace(Configuration=Configuration, ApiClient=ApiClient),
            config=types.SimpleNamespace(
                list_kube_config_contexts=lambda: ([{'name': 'rancher-desktop'}], None),
                load_kube_config=lambda **kwargs: None))
        with patch.dict(sys.modules, {'kubernetes': package}):
            transport = mod.KubernetesTransport()
            status, body = transport.request({'method': 'GET',
                'path': '/apis/apps/v1/namespaces/dev/deployments/order',
                'body': None, 'content_type': 'application/json'})
        self.assertEqual(status, 200)
        self.assertEqual(body['kind'], 'Deployment')
        self.assertEqual(calls, [{200: 'object', 201: 'object', 202: 'object'}])

    def test_real_api_adapter_pins_context_disables_retries_and_preserves_delete_body(self):
        self.assertTrue(callable(getattr(mod, 'KubernetesTransport', None)), '실제 API 전송 어댑터가 없다')
        from unittest.mock import patch
        import sys
        events = []
        class Configuration:
            verify_ssl = True
            host = 'https://example.invalid'
        class ApiClient:
            def __init__(self, configuration):
                events.append(('configuration', configuration.retries))
            def call_api(self, path, method, **kwargs):
                events.append((path, method, kwargs))
                return {'kind': 'Status', 'status': 'Success'}, 200, {}
        def load(**kwargs):
            events.append(('load', kwargs['context'], kwargs['persist_config']))
        package = types.SimpleNamespace(client=types.SimpleNamespace(Configuration=Configuration, ApiClient=ApiClient),
            config=types.SimpleNamespace(list_kube_config_contexts=lambda: ([{'name': 'rancher-desktop'}], {'name': 'other'}),
                                         load_kube_config=load))
        with patch.dict(sys.modules, {'kubernetes': package}):
            transport = mod.KubernetesTransport()
            self.assertEqual(transport.context(), 'rancher-desktop')
            request = mod.cas_request('dev', {'action': 'delete', 'before': resource('Service', 'member')}, dry_run=True)
            status, body = transport.request(request)
            self.assertEqual((status, body['status']), (200, 'Success'))
            self.assertEqual(events[0], ('load', 'rancher-desktop', False))
            self.assertEqual(events[1], ('configuration', 0))
            self.assertEqual(events[2][2]['body']['dryRun'], ['All'])
            self.assertEqual(events[2][2]['_request_timeout'], 20)
            with self.assertRaises(mod.CutoverError):
                mod.KubernetesTransport(context='other')
            self.assertEqual(len(events), 3)


class HardeningTests(unittest.TestCase):
    def test_plan_drops_unneeded_observed_specs_and_annotations(self):
        before = resource()
        before['metadata']['annotations'] = {'기록금지': '관측 원문'}
        before['spec']['미필요한_관측값'] = '기록금지'
        plan = mod.prepare_plan('dev', 'cutover', {'action': 'scale', 'before': before, 'replicas': 0},
                                 receipts=evidence(), previous=[], now=NOW)
        self.assertEqual(set(plan['change']['before']), {'apiVersion', 'kind', 'metadata'})
        self.assertEqual(set(plan['change']['before']['metadata']), {'name', 'namespace', 'uid', 'resourceVersion'})

    def test_domain_deployment_cannot_override_readonly_or_reference_another_config(self):
        desired = {'apiVersion': 'apps/v1', 'kind': 'Deployment',
            'metadata': {'name': 'business-api', 'namespace': 'dev'},
            'spec': {'template': {'spec': {'containers': [{'name': 'business-api',
                'envFrom': [{'configMapRef': {'name': 'business-api-config'}}],
                'env': [{'name': 'READ_ONLY_MODE', 'value': '0'}]}]}}}}
        with self.assertRaises(mod.CutoverError):
            mod.prepare_plan('dev', 'cutover', {'action': 'apply', 'before': None, 'desired': desired},
                             receipts=evidence(), previous=[], now=NOW)
        del desired['spec']['template']['spec']['containers'][0]['env']
        desired['spec']['template']['spec']['containers'][0]['envFrom'] = [{'configMapRef': {'name': 'other'}}]
        with self.assertRaises(mod.CutoverError):
            mod.prepare_plan('dev', 'cutover', {'action': 'apply', 'before': None, 'desired': desired},
                             receipts=evidence(), previous=[], now=NOW)

    def test_unknown_change_fields_and_partial_payloads_are_rejected(self):
        for change in [{'action': 'scale', 'before': resource(), 'replicas': 0, 'force': True},
                       {'action': 'apply', 'desired': resource()},
                       {'apiVersion': 'v1', 'kind': 'List', 'items': []}]:
            with self.subTest(change=change), self.assertRaises(mod.CutoverError):
                mod.cas_request('dev', change, dry_run=True)

    def test_adapter_refuses_insecure_tls_and_secret_paths(self):
        from unittest.mock import patch
        import sys
        calls = []
        class Configuration:
            verify_ssl = False
            host = 'https://example.invalid'
        package = types.SimpleNamespace(client=types.SimpleNamespace(Configuration=Configuration,
            ApiClient=lambda **kw: types.SimpleNamespace(call_api=lambda *a, **k: calls.append(a))),
            config=types.SimpleNamespace(list_kube_config_contexts=lambda: ([{'name': 'rancher-desktop'}], None),
                                         load_kube_config=lambda **kw: None))
        with patch.dict(sys.modules, {'kubernetes': package}):
            with self.assertRaises(mod.CutoverError):
                mod.KubernetesTransport()
            Configuration.verify_ssl = True
            transport = mod.KubernetesTransport()
            with self.assertRaises(mod.CutoverError):
                transport.request({'method': 'GET', 'path': '/api/v1/namespaces/dev/secrets/member',
                                   'body': None, 'content_type': 'application/json'})
        self.assertEqual(calls, [])


class FinalBoundaryTests(unittest.TestCase):
    def plan(self):
        return mod.prepare_plan('dev', 'cutover', {'action': 'scale', 'before': resource(), 'replicas': 0},
                                 receipts=evidence(), previous=[], now=NOW)

    def test_real_write_refuses_receipt_without_nonpersistence_readback(self):
        plan = self.plan()
        after = resource()
        after['spec']['replicas'] = 0
        dry = mod.execute_one(plan, reviewed_sha256=digest(plan), transport=QueueTransport(
            [(200, resource()), (200, after), (200, resource())]), now=NOW, dry_run=True)
        dry.pop('nonpersistent_readback')
        transport = QueueTransport([(200, resource()), (200, after), (200, after)])
        with self.assertRaises(mod.CutoverError):
            mod.execute_one(plan, reviewed_sha256=digest(plan), transport=transport, now=NOW,
                            dry_run=False, dry_run_receipt=dry)
        self.assertEqual(transport.calls, [])

    def test_live_pod_or_unfinished_container_is_not_termination(self):
        for key, value in [('owned_pods', ['종료중-pod']), ('running_containers', 1),
                           ('container_inventory_complete', False), ('absent_pod_uids', []),
                           ('observed_generation', 1)]:
            bad = evidence()
            bad['writers_stopped']['terminations'][0][key] = value
            bad['inventory_migration']['writer_receipt_sha256'] = digest(bad['writers_stopped'])
            with self.subTest(key=key), self.assertRaises(mod.CutoverError):
                mod.prepare_plan('dev', 'cutover', self.plan()['change'], receipts=bad, previous=[], now=NOW)

    def test_freeze_receipt_must_assert_external_maintenance_block(self):
        bad = evidence()
        bad['writers_stopped'].pop('external_maintenance_blocked', None)
        bad['inventory_migration']['writer_receipt_sha256'] = digest(bad['writers_stopped'])
        with self.assertRaises(mod.CutoverError):
            mod.prepare_plan('dev', 'cutover', self.plan()['change'], receipts=bad, previous=[], now=NOW)


class RegressionTests(unittest.TestCase):
    def test_create_dryrun_and_readback_use_only_the_exact_named_object(self):
        desired = {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'namespace': 'dev', 'name': 'trade-member'},
            'spec': {'selector': {'app': 'trade-member'}, 'ports': [{'port': 8080, 'targetPort': 8080}]}}
        plan = mod.prepare_plan('dev', 'cutover', {'action': 'apply', 'before': None, 'desired': desired},
                                 receipts=evidence(), previous=[], now=NOW)
        after = copy.deepcopy(desired)
        after['metadata'].update(uid='신규-uid', resourceVersion='20')
        dry_transport = QueueTransport([(404, {}), (201, after), (404, {})])
        dry = mod.execute_one(plan, reviewed_sha256=digest(plan), transport=dry_transport, now=NOW, dry_run=True)
        real_transport = QueueTransport([(404, {}), (201, after), (200, after)])
        result = mod.execute_one(plan, reviewed_sha256=digest(plan), transport=real_transport, now=NOW,
                                 dry_run=False, dry_run_receipt=dry)
        self.assertEqual(result['state'], 'readback_verified')
        self.assertEqual([r['method'] for r in real_transport.calls], ['GET', 'POST', 'GET'])
        conflict = QueueTransport([(404, {}), (409, {})])
        with self.assertRaises(mod.CutoverError):
            mod.execute_one(plan, reviewed_sha256=digest(plan), transport=conflict, now=NOW, dry_run=True)
        self.assertEqual(len(conflict.calls), 2)

    def test_expired_future_or_edited_plan_has_no_transport_calls(self):
        plan = mod.prepare_plan('dev', 'cutover', {'action': 'scale', 'before': resource(), 'replicas': 0},
                                 receipts=evidence(), previous=[], now=NOW)
        for now in ('2026-11-01T09:19:00+00:00', '2026-11-01T09:02:59+00:00'):
            transport = QueueTransport([])
            with self.subTest(now=now), self.assertRaises(mod.CutoverError):
                mod.execute_one(plan, reviewed_sha256=digest(plan), transport=transport, now=now, dry_run=True)
            self.assertEqual(transport.calls, [])
        old_hash = digest(plan)
        plan['change']['replicas'] = 1
        transport = QueueTransport([])
        with self.assertRaises(mod.CutoverError):
            mod.execute_one(plan, reviewed_sha256=old_hash, transport=transport, now=NOW, dry_run=True)
        self.assertEqual(transport.calls, [])

    def test_transport_failure_5xx_422_and_missing_readback_identity_never_retry(self):
        plan = mod.prepare_plan('dev', 'cutover', {'action': 'scale', 'before': resource(), 'replicas': 0},
                                 receipts=evidence(), previous=[], now=NOW)
        for response in ((500, {'message': '민감 진단'}), (422, {}), TimeoutError('민감 진단')):
            transport = QueueTransport([(200, resource()), response])
            with self.subTest(response=type(response).__name__), self.assertRaises(mod.CutoverError) as caught:
                mod.execute_one(plan, reviewed_sha256=digest(plan), transport=transport, now=NOW, dry_run=True)
            self.assertNotIn('민감 진단', str(caught.exception))
            self.assertEqual(len(transport.calls), 2)
        observed = resource()
        del observed['metadata']['uid']
        transport = QueueTransport([(200, observed)])
        with self.assertRaises(mod.CutoverError):
            mod.execute_one(plan, reviewed_sha256=digest(plan), transport=transport, now=NOW, dry_run=True)
        self.assertEqual(len(transport.calls), 1)

    def test_pure_import_does_not_read_files_or_open_network(self):
        spec = importlib.util.spec_from_file_location('독립_가져오기', PATH)
        assert spec is not None and spec.loader is not None
        isolated = importlib.util.module_from_spec(spec)
        with patch.object(Path, 'read_text', side_effect=AssertionError('파일 읽기 금지')), \
             patch.object(Path, 'read_bytes', side_effect=AssertionError('파일 읽기 금지')):
            spec.loader.exec_module(isolated)
        self.assertEqual(isolated.CONTEXT, 'rancher-desktop')

    def test_all_public_renderer_app_objects_fit_single_environment_plans(self):
        renderer_path = PATH.with_name('render-samsung-environments.py')
        spec = importlib.util.spec_from_file_location('삼성_선언기_읽기전용', renderer_path)
        assert spec is not None and spec.loader is not None
        renderer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(renderer)
        self.assertEqual(set(renderer.SERVICES), set(mod.SERVICES))
        docs = renderer.render({s: 'example.invalid/' + s + '@sha256:' + '1' * 64 for s in renderer.SERVICES},
                               inventory_migrated_envs=renderer.ENVIRONMENTS)
        count = 0
        for obj in docs:
            env = obj['metadata'].get('namespace')
            if env not in mod.ENVIRONMENTS:
                continue
            previous = [acceptance(e) for e in mod.ENVIRONMENTS[:mod.ENVIRONMENTS.index(env)]]
            plan = mod.prepare_plan(env, 'cutover', {'action': 'apply', 'before': None, 'desired': obj},
                                     receipts=evidence(env), previous=previous, now=NOW)
            self.assertEqual(plan['environment'], env)
            self.assertEqual(mod.fingerprint(plan), digest(plan))
            count += 1
        self.assertGreater(count, 100)


class LastCasBoundaryTests(unittest.TestCase):
    def test_transport_itself_refuses_existing_mutation_without_both_cas_conditions(self):
        import sys
        calls = []
        class Configuration:
            verify_ssl = True
            host = 'https://example.invalid'
        package = types.SimpleNamespace(client=types.SimpleNamespace(Configuration=Configuration,
            ApiClient=lambda **kw: types.SimpleNamespace(call_api=lambda *a, **k: calls.append(a))),
            config=types.SimpleNamespace(list_kube_config_contexts=lambda: ([{'name': 'rancher-desktop'}], None),
                                         load_kube_config=lambda **kw: None))
        with patch.dict(sys.modules, {'kubernetes': package}):
            transport = mod.KubernetesTransport()
            for method, body in [('PATCH', [{'op': 'add', 'path': '/spec/replicas', 'value': 0}]),
                                  ('DELETE', {'kind': 'DeleteOptions', 'preconditions': {'uid': '검사-uid'}})]:
                with self.subTest(method=method), self.assertRaises(mod.CutoverError):
                    transport.request({'method': method, 'path': '/apis/apps/v1/namespaces/dev/deployments/order',
                        'body': body, 'content_type': 'application/json-patch+json' if method == 'PATCH' else 'application/json'})
        self.assertEqual(calls, [])

    def test_writer_termination_receipt_is_bound_to_existing_deployment_uid(self):
        before = resource()
        before['metadata']['uid'] = '재생성된-uid'
        with self.assertRaises(mod.CutoverError):
            mod.prepare_plan('dev', 'cutover', {'action': 'scale', 'before': before, 'replicas': 0},
                             receipts=evidence(), previous=[], now=NOW)


if __name__ == '__main__':
    unittest.main()
