"""실제 SDK 시그니처와 배포 CAS를 외부 연결 없이 검사한다."""
from copy import deepcopy
import inspect
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kubernetes.client import ApiClient
from app.change_api import ReleaseKube
from app.changes import ChangeError, ChangeService

IMAGE = 'repo/svc@sha256:' + '1' * 64
NEXT = 'repo/svc@sha256:' + '2' * 64


def deployment():
    return {'metadata': {'uid': 'uid-1', 'resourceVersion': '41', 'labels': {
        'liveprobe.io/managed': 'true', 'app.kubernetes.io/part-of': 'sys'}},
        'spec': {'template': {'metadata': {'labels': {'app': 'svc'}}, 'spec': {
            'containers': [{'name': 'svc', 'image': IMAGE, 'env': [{'name': 'MODE', 'value': 'safe'}]}]}}}}


class SDKTransport:
    def __init__(self, dep=None, lose_response=False, readback_image=None):
        self.dep = deepcopy(dep or deployment())
        self.calls = []
        self.lose_response = lose_response
        self.readback_image = readback_image
    def call_api(self, *args, **kwargs):
        inspect.signature(ApiClient.call_api).bind(self, *args, **kwargs)
        self.calls.append((args, kwargs))
        if args[1] == 'GET':
            return deepcopy(self.dep)
        self.dep['spec']['template']['spec']['containers'][0]['image'] = self.readback_image or NEXT
        if self.lose_response:
            raise TimeoutError('합성 응답 유실')
        return deepcopy(self.dep)


class ReleaseContract(unittest.TestCase):
    def release(self, transport):
        release = object.__new__(ReleaseKube)
        release.api_client = transport
        return release
    def call(self, transport, **extra):
        from app.runtime_fencing import template_fingerprint
        return self.release(transport).patch_image('stg', 'svc', IMAGE, NEXT, '41',
            expected_uid='uid-1', template_sha256=template_fingerprint(deployment()), **extra)
    def test_sdk36_uid_rv_image_configuration_tests(self):
        transport = SDKTransport()
        self.call(transport)
        kwargs = next(call[1] for call in transport.calls if call[0][1] == 'PATCH')
        self.assertIn('response_types_map', kwargs)
        tests = {op['path']: op['value'] for op in kwargs['body'] if op['op'] == 'test'}
        self.assertEqual(tests['/metadata/uid'], 'uid-1')
        self.assertEqual(tests['/metadata/resourceVersion'], '41')
        self.assertEqual(tests['/spec/template'], deployment()['spec']['template'])
        self.assertEqual(tests['/spec/template/spec/containers/0/image'], IMAGE)
    def test_mutation_conflicts_never_patch(self):
        from app.runtime_fencing import OWNER_ANNOTATION
        for kind in ('uid', 'rv', 'config', 'owner'):
            dep = deployment()
            if kind == 'uid': dep['metadata']['uid'] = 'uid-2'
            if kind == 'rv': dep['metadata']['resourceVersion'] = '42'
            if kind == 'config': dep['spec']['template']['spec']['containers'][0]['env'][0]['value'] = 'changed'
            if kind == 'owner': dep['metadata']['annotations'] = {OWNER_ANNOTATION: 'application-release-1'}
            transport = SDKTransport(dep)
            with self.assertRaises(ChangeError): self.call(transport)
            self.assertEqual([c[0][1] for c in transport.calls], ['GET'])
    def test_response_loss_readback_once_without_second_patch(self):
        transport = SDKTransport(lose_response=True)
        result = self.call(transport)
        self.assertEqual(result['write_outcome'], 'written')
        self.assertEqual([c[0][1] for c in transport.calls], ['GET', 'PATCH', 'GET'])
    def test_unknown_write_not_retried(self):
        transport = SDKTransport(lose_response=True, readback_image='unknown')
        with self.assertRaises(ChangeError) as caught: self.call(transport)
        self.assertEqual(caught.exception.code, 'release_write_unknown')
        self.assertEqual([c[0][1] for c in transport.calls], ['GET', 'PATCH', 'GET'])
    def test_service_does_not_substitute_fresh_resource_version(self):
        from app.runtime_fencing import template_fingerprint
        dep = deployment(); dep['metadata']['resourceVersion'] = '42'
        calls = []
        service = ChangeService(NS(), NS(), None, NS(get_deployment=lambda *a: dep),
            release=NS(patch_image=lambda *a, **k: calls.append((a, k))))
        with self.assertRaises(ChangeError):
            service._write('stg', 'svc', 'sys', 'uid-1', IMAGE, NEXT,
                resource_version='41', template_sha256=template_fingerprint(deployment()))
        self.assertEqual(calls, [])
    def test_prd_refuses_rebuilt_image_without_stg_revalidation(self):
        from app.models import utcnow
        row = NS(build={'image': IMAGE, 'review_checked_at': utcnow().isoformat(), 'pr_state': 'MERGED',
            'review_completed': True, 'merge_sha': 'a' * 40, 'main_sha': 'a' * 40,
            'merge_build': {'commit': 'a' * 40, 'image': NEXT}},
            stages={'stg': {'status': 'verified', 'new_image': IMAGE}})
        with self.assertRaises(ChangeError) as caught: ChangeService._release_image(row, 'prd')
        self.assertEqual(caught.exception.code, 'artifact_revalidation_required')


if __name__ == '__main__': unittest.main()
