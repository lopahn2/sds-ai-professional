"""원복 패치가 만든 롤아웃과 Pod 삭제의 경쟁 회귀. 실제 클러스터·모델·DB 없음."""
import copy
from types import SimpleNamespace as NS
from unittest.mock import patch
import unittest

import test_p18_campaign_restore_cas as cas

r = cas.r


class RestoreRollout(unittest.TestCase):
    def setUp(self):
        fixture = cas.RestoreCAS()
        fixture.setUp()
        self.fixture = fixture
        self.h = fixture.h
        self.clock = 0.0
        self.patched = False
        self.events = fixture.events
        self.baseline = cas.dep()
        self.baseline['spec']['template']['metadata']['annotations'] = {}
        self.current = copy.deepcopy(self.baseline)
        self.current['spec']['template']['metadata']['annotations'] = {r.RESTART: 'AI가 시작한 재시작'}
        self.restored = copy.deepcopy(self.baseline)
        self.restored['metadata']['generation'] = 2
        self.h.args.restore_timeout = 4
        self.h.dep = self.read_deployment
        self.h.apps.patch_namespaced_deployment = self.apply_baseline
        self.h.pods = self.list_pods
        self.old = copy.deepcopy(fixture.listed)
        self.old['metadata'].update(name='payment-previous', deletionTimestamp='2026-10-30T15:32:41Z')

    def read_deployment(self, name):
        if not self.patched:
            return copy.deepcopy(self.current)
        result = copy.deepcopy(self.restored)
        result['status'] = {'observedGeneration': 2 if self.clock >= 1 else 1,
                            'updatedReplicas': 1, 'readyReplicas': 1, 'replicas': 1}
        return result

    def apply_baseline(self, *args, **kwargs):
        self.events.append('patch')
        self.patched = True

    def list_pods(self, name):
        self.events.append('list@' + str(self.clock))
        return [copy.deepcopy(self.old), copy.deepcopy(self.fixture.listed)] if self.clock < 2 else [copy.deepcopy(self.fixture.listed)]

    def advance(self, seconds):
        self.clock += seconds

    def run_restore(self):
        with patch.object(r.time, 'monotonic', side_effect=lambda: self.clock), patch.object(r.time, 'sleep', side_effect=self.advance):
            return self.h.restore({'payment': self.baseline})

    def test_baseline_patch_waits_for_observed_rollout_and_old_pod_disappearance_before_first_delete(self):
        result = self.run_restore()
        self.assertTrue(result['verified'], result)
        self.assertGreaterEqual(self.clock, 2)
        self.assertEqual(len(self.fixture.deletes), 1)
        self.assertEqual(self.fixture.deletes[0][0], 'payment-pod')
        self.assertLess(self.events.index('patch'), self.events.index('delete'))
        self.assertTrue(result['services'][0]['rollout_converged_before_reset'])

    def test_rollout_timeout_blocks_every_delete(self):
        self.old['metadata']['deletionGracePeriodSeconds'] = 30
        self.list_pods = lambda name: [copy.deepcopy(self.old)]
        self.h.pods = self.list_pods
        result = self.run_restore()
        self.assertFalse(result['verified'])
        self.assertEqual(self.fixture.deletes, [])
        self.assertEqual(self.clock, 4)
        self.assertEqual(result['services'][0]['diagnostic']['operation'], 'wait_rollout_before_reset')

    def test_unobserved_generation_blocks_delete_even_if_pods_exist(self):
        read = self.h.dep
        def unobserved(name):
            result = read(name)
            if self.patched:
                result['status']['observedGeneration'] = 1
            return result
        self.h.dep = unobserved
        self.assertFalse(self.run_restore()['verified'])
        self.assertEqual(self.fixture.deletes, [])
        self.assertEqual(self.clock, 4)

    def test_empty_pod_window_is_read_only_until_replacement_exists(self):
        self.h.pods = lambda name: [] if self.clock < 2 else [copy.deepcopy(self.fixture.listed)]
        self.assertTrue(self.run_restore()['verified'])
        self.assertGreaterEqual(self.clock, 2)
        self.assertEqual(len(self.fixture.deletes), 1)

    def test_deployment_drift_during_wait_refuses_delete(self):
        for drift in ('uid', 'image', 'cpu'):
            with self.subTest(drift=drift):
                self.setUp()
                read = self.h.dep
                def changed(name):
                    result = read(name)
                    if self.patched:
                        if drift == 'uid': result['metadata']['uid'] = 'foreign'
                        elif drift == 'image': result['spec']['template']['spec']['containers'][0]['image'] = 'foreign'
                        else: result['spec']['template']['spec']['containers'][0]['resources'] = {'limits': {'cpu': '4'}}
                    return result
                self.h.dep = changed
                self.assertFalse(self.run_restore()['verified'])
                self.assertEqual(self.fixture.deletes, [])

    def test_fresh_pod_spec_drift_still_blocks_first_delete(self):
        self.fixture.fresh['spec']['containers'][0]['image'] = 'foreign'
        self.assertFalse(self.run_restore()['verified'])
        self.assertEqual(self.fixture.deletes, [])

    def test_owner_chain_is_not_relaxed_after_rollout(self):
        self.h.apps.read_namespaced_replica_set = lambda *a, **k: {'metadata': {'uid': 'foreign'}}
        self.assertFalse(self.run_restore()['verified'])
        self.assertEqual(self.fixture.deletes, [])

    def test_409_after_convergence_is_one_delete_without_rebase(self):
        def conflict(*args, **kwargs):
            self.events.append('conflict')
            self.fixture.deletes.append(kwargs['body'])
            raise cas.APIError('unclassified message')
        self.h.core.delete_namespaced_pod = conflict
        result = self.run_restore()
        self.assertFalse(result['verified'])
        self.assertEqual(self.events.count('conflict'), 1)
        self.assertEqual(len(self.fixture.deletes), 1)
        self.assertEqual(result['services'][0]['diagnostic']['code'], 409)
        self.assertEqual(r.after_case({'drain': {'verified': True}, 'restoration': result, 'state': 'indeterminate'}), 'stop')

    def test_late_deployment_response_cannot_authorize_delete(self):
        read = self.h.dep
        def late(name):
            result = read(name)
            if self.patched:
                self.advance(5)
                result['status']['observedGeneration'] = 2
            return result
        self.h.dep = late
        self.h.pods = lambda name: [copy.deepcopy(self.fixture.listed)]
        result = self.run_restore()
        self.assertFalse(result['verified'])
        self.assertEqual(self.fixture.deletes, [])
        self.assertEqual(self.clock, 5)

    def test_late_pod_list_response_cannot_authorize_delete(self):
        read = self.h.dep
        def converged(name):
            result = read(name)
            if self.patched: result['status']['observedGeneration'] = 2
            return result
        self.h.dep = converged
        def late(name):
            self.advance(5)
            return [copy.deepcopy(self.fixture.listed)]
        self.h.pods = late
        self.assertFalse(self.run_restore()['verified'])
        self.assertEqual(self.fixture.deletes, [])

    def test_deadline_remains_binding_through_owner_read_before_delete(self):
        owner = self.h.apps.read_namespaced_replica_set
        def late(*args, **kwargs):
            self.advance(5)
            return owner(*args, **kwargs)
        self.h.apps.read_namespaced_replica_set = late
        self.assertFalse(self.run_restore()['verified'])
        self.assertEqual(self.fixture.deletes, [])

    def test_sdk_read_timeouts_use_remaining_shared_budget(self):
        requests = []
        self.h._kube_deadline = 4
        self.h.apps.read_namespaced_deployment = lambda *a, **k: requests.append(k['_request_timeout']) or cas.dep()
        self.h.core.list_namespaced_pod = lambda *a, **k: requests.append(k['_request_timeout']) or NS(items=[])
        with patch.object(r.time, 'monotonic', return_value=2):
            r.Harness.dep(self.h, 'payment')
            r.Harness.pods(self.h, 'payment')
        self.assertEqual(requests, [2, 2])

    def test_expired_budget_blocks_sdk_reads_before_transport(self):
        requests = []
        self.h._kube_deadline = 4
        self.h.apps.read_namespaced_deployment = lambda *a, **k: requests.append('dep') or cas.dep()
        self.h.core.list_namespaced_pod = lambda *a, **k: requests.append('pods') or NS(items=[])
        with patch.object(r.time, 'monotonic', return_value=4):
            with self.assertRaises(TimeoutError): r.Harness.dep(self.h, 'payment')
            with self.assertRaises(TimeoutError): r.Harness.pods(self.h, 'payment')
        self.assertEqual(requests, [])

    def test_late_clear_response_blocks_pod_delete(self):
        clear = self.h.chaos
        def late(name, method='GET', data=None):
            result = clear(name, method, data)
            if method == 'DELETE': self.advance(3)
            return result
        self.h.chaos = late
        self.assertFalse(self.run_restore()['verified'])
        self.assertEqual(self.fixture.deletes, [])
        self.assertIsNone(self.h._kube_deadline)

    def test_late_fresh_pod_read_blocks_pod_delete(self):
        read = self.h.core.read_namespaced_pod
        def late(*args, **kwargs):
            self.advance(3)
            return read(*args, **kwargs)
        self.h.core.read_namespaced_pod = late
        self.assertFalse(self.run_restore()['verified'])
        self.assertEqual(self.fixture.deletes, [])
        self.assertIsNone(self.h._kube_deadline)

    def test_readiness_uses_existing_separate_budget_after_reset(self):
        def ready(name, **kwargs):
            self.assertIsNone(self.h._kube_deadline)
            self.advance(5)
            return True
        self.h.wait_ready = ready
        self.assertTrue(self.run_restore()['verified'])
        self.assertEqual(len(self.fixture.deletes), 1)
        self.assertIsNone(self.h._kube_deadline)


if __name__ == '__main__':
    unittest.main(verbosity=2)
