"""수집·dispatch는 실제 구현, 외부 Kube HTTP만 치환한다."""
from copy import deepcopy
from pathlib import Path
import json
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from liveprobe.application_runner.executor import Executor
from liveprobe.application_runner.deploy import KubeTransport
from test_runner_fencing import KubeHTTP

class ObservationHTTP(KubeHTTP):
    def __init__(self):super().__init__();self.calls=[];self.empty=False
    def call_api(self,path,method,body=None,**kwargs):
        self.calls.append((path,method,kwargs))
        if '/log' in path:return '' if self.empty else '2026-11-02T01:00:00Z Traceback (most recent call last):\n2026-11-02T01:00:00Z ValueError: password=do-not-export\n'+'x'*2300
        if path.endswith('/events'):
            return {'items':[] if self.empty else [{'involvedObject':{'uid':'u1','name':'catalog'},'lastTimestamp':'2026-11-02T01:00:00Z','reason':'Failed','message':'token=do-not-export'}]}
        if '/deployments/inventory' in path:
            doc=deepcopy(self.doc);doc['metadata']['uid']='inv';doc['spec']['template']['spec']['containers']=[{'name':'inventory','image':self.registry.images['inventory']}];return doc
        if path.endswith('/pods'):
            return {'items':[] if self.empty else [{'metadata':{'name':'pod','uid':'pod-uid','ownerReferences':[{'uid':'rs'}]},'spec':{'containers':[{'name':'catalog'}]}}]}
        return super().call_api(path,method,body,**kwargs)


class RestartLogHTTP(ObservationHTTP):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.changed_after_log = set()
        self.rejected_logs = {}
        self.previous_log = (b'2026-11-02T01:00:00Z Traceback (most recent call last):\n'
                             b'2026-11-02T01:00:01Z KeyError: password=do-not-export\n')

    def add(self, name, *, owner='current', waiting=False, finished='2026-11-02T01:00:01Z'):
        state = {'waiting': {'reason': 'CrashLoopBackOff'}} if waiting else {'running': {}}
        last = {'containerID': 'containerd://previous-' + name, 'finishedAt': finished}
        self.rows.append({'metadata': {'name': name, 'uid': 'uid-' + name, 'ownerReferences': [
            {'kind': 'ReplicaSet', 'controller': True, 'uid': owner}]},
            'spec': {'containers': [{'name': 'catalog', 'image': self.old}]},
            'status': {'containerStatuses': [{'name': 'catalog', 'state': state,
                                              'lastState': {'terminated': last}}]}})

    def call_api(self, path, method, body=None, **kwargs):
        query = dict(kwargs.get('query_params') or [])
        status = self.rejected_logs.get((path, query.get('previous', False)))
        if path.endswith('/log') and (status or query.get('previous')):
            self.calls.append((path, method, kwargs))
            if status:
                failure = OSError('untrusted error contains a sensitive diagnostic')
                failure.status = status
                raise failure
            return self.previous_log
        if path.endswith('/replicasets'):
            self.calls.append((path, method, kwargs))
            template = deepcopy(self.doc['spec']['template'])
            template['metadata']['labels']['pod-template-hash'] = 'current'
            old = deepcopy(template)
            old['spec']['containers'][0]['image'] = self.new
            return {'items': [{'metadata': {'uid': uid, 'ownerReferences': [
                {'kind': 'Deployment', 'controller': True, 'uid': owner}]}, 'spec': {'template': tmpl}}
                for uid, owner, tmpl in [('current', 'u1', template), ('old', 'u1', old), ('foreign', 'another', template)]]}
        if path.endswith('/pods') and query.get('labelSelector') == 'app=catalog':
            self.calls.append((path, method, kwargs))
            return {'items': deepcopy(self.rows)}
        for row in self.rows:
            name = row['metadata']['name']
            if path.endswith('/pods/' + name):
                self.calls.append((path, method, kwargs))
                current = deepcopy(row)
                if name in self.changed_after_log:
                    current['status']['containerStatuses'][0]['lastState']['terminated']['containerID'] += '-new'
                return current
        return super().call_api(path, method, body, **kwargs)


class ObservationTests(unittest.TestCase):
    def test_collect_real_logs_events_deployment_and_rollout_scoped_and_bounded(self):
        api=ObservationHTTP()
        with tempfile.TemporaryDirectory() as tmp:
            executor=Executor(api.registry,KubeTransport(api),object(),tmp,{})
            result=executor.collect({'environment':'dev','service':'catalog','catalog_sha256':api.registry.catalog_sha256,'since':'2026-11-02T00:00:00Z'})
            self.assertTrue(result['logs'])
            sources={r['source'] for r in result['evidence']}
            self.assertEqual(sources,{'pod_log','k8s_event','deployment','rollout'})
            self.assertEqual(len({r['id'] for r in result['evidence']}),len(result['evidence']))
            for row in result['evidence']:
                self.assertEqual(set(row),{'id','service','environment','observed_at','source','text','quality'})
                self.assertRegex(row['id'],r'^E\d+-(catalog|inventory)$')
                self.assertEqual(row['environment'],'dev')
                self.assertRegex(row['observed_at'],r'Z$')
                self.assertLessEqual(len(row['text']),2000)
                self.assertIn(row['quality'],('observed','truncated'))
            self.assertTrue(any(r['quality']=='truncated' for r in result['evidence']))
            for row in result['logs']:
                self.assertEqual(set(row),{'service','environment','observed_at','text'})
                self.assertRegex(row['observed_at'],r'Z$');self.assertLessEqual(len(row['text']),2000)
            self.assertNotIn('do-not-export',json.dumps(result))
            self.assertTrue(all('/namespaces/dev/' in p for p,m,k in api.calls))
            self.assertTrue(all(m=='GET' for p,m,k in api.calls))
            self.assertTrue(any('/deployments/inventory' in p for p,m,k in api.calls))
    def test_empty_logs_events_have_no_synthetic_rows(self):
        api=ObservationHTTP();api.empty=True;api.doc.pop('status')
        with tempfile.TemporaryDirectory() as tmp:
            result=Executor(api.registry,KubeTransport(api),object(),tmp,{}).collect({'environment':'dev','service':'catalog','catalog_sha256':api.registry.catalog_sha256,'since':'2026-11-02T00:00:00Z'})
            self.assertEqual(result['logs'],[])
            self.assertEqual({r['source'] for r in result['evidence']},{'deployment'})

    def test_previous_logs_are_scoped_to_the_current_deployment_and_labeled_as_previous(self):
        api = RestartLogHTTP()
        api.add('crashed', waiting=True)
        api.add('retired', owner='old', waiting=True)
        api.add('unrelated', owner='foreign', waiting=True)
        api.add('expired', waiting=True, finished='2026-11-01T23:59:59Z')
        since = '2026-11-02T00:00:00Z'
        with tempfile.TemporaryDirectory() as tmp:
            executor = Executor(api.registry, KubeTransport(api), object(), tmp, {})
            result = executor.collect({'environment': 'dev', 'service': 'catalog',
                                       'catalog_sha256': api.registry.catalog_sha256, 'since': since})
        catalog_logs = [row['text'] for row in result['logs'] if row['service'] == 'catalog']
        self.assertEqual(len(catalog_logs), 1)
        self.assertIn('pod=crashed container=catalog finished_at=2026-11-02T01:00:01Z', catalog_logs[0])
        self.assertIn('KeyError:', catalog_logs[0])
        self.assertNotIn('do-not-export', json.dumps(result))
        rows = [row for row in result['evidence'] if row['service'] == 'catalog' and row['source'] == 'pod_log']
        self.assertEqual([row['text'] for row in rows], catalog_logs)
        self.assertTrue(rows[0]['text'].startswith('Previous terminated container:'))
        log_calls = [(path, dict(params['query_params'])) for path, method, params in api.calls
                     if dict(params.get('query_params') or []).get('container') == 'catalog']
        self.assertEqual(log_calls, [('/api/v1/namespaces/dev/pods/crashed/log', {
            'container': 'catalog', 'sinceTime': since, 'timestamps': True, 'tailLines': 200,
            'limitBytes': 32768, 'previous': True})])
        self.assertEqual(sum('/deployments/catalog' in path for path, _, _ in api.calls), 1)

    def test_collect_keeps_late_trace_when_the_display_log_was_truncated(self):
        api = RestartLogHTTP()
        api.add('crashed', waiting=True)
        api.previous_log = (b'2026-11-02T01:00:00Z INFO startup\n' * 90) + api.previous_log
        with tempfile.TemporaryDirectory() as tmp:
            result = Executor(api.registry, KubeTransport(api), object(), tmp, {}).collect({
                'environment': 'dev', 'service': 'catalog', 'catalog_sha256': api.registry.catalog_sha256,
                'since': '2026-11-02T00:00:00Z',
            })
        catalog = [row for row in result['evidence'] if row['service'] == 'catalog' and row['source'] == 'pod_log']
        self.assertEqual(len(catalog), 2)
        self.assertEqual(catalog[0]['quality'], 'truncated')
        self.assertNotIn('KeyError:', catalog[0]['text'])
        self.assertEqual(catalog[1]['quality'], 'observed')
        self.assertIn('KeyError:', catalog[1]['text'])
        self.assertTrue(catalog[1]['text'].startswith('Previous terminated container:'))

    def test_previous_log_races_do_not_drop_current_logs_or_mask_permission_errors(self):
        api = RestartLogHTTP()
        api.add('restarted')
        api.add('expired-while-reading', waiting=True)
        api.add('different-image', waiting=True)
        api.rows[-1]['spec']['containers'][0]['image'] = api.new
        api.changed_after_log.add('restarted')
        path = '/api/v1/namespaces/dev/pods/expired-while-reading/log'
        api.rejected_logs[path, True] = 400
        kube = KubeTransport(api)
        self.assertEqual(len(kube.logs('dev', 'catalog', '2026-11-02T00:00:00Z')), 1)
        self.assertFalse(any('/pods/different-image/log' in path for path, _, _ in api.calls))
        api.rejected_logs[path, True] = 403
        with self.assertRaisesRegex(OSError, '^kubernetes_log_transport_failed$'):
            kube.logs('dev', 'catalog', '2026-11-02T00:00:00Z')

    def test_separate_runners_share_only_the_exact_tested_artifact(self):
        from liveprobe.application_runner.worker import durable_write

        api = KubeHTTP()
        payload = api.payload()
        artifact = payload['artifact']
        artifact.update({k: 'a' * 64 for k in
                         ('source_snapshot_id', 'diff_sha256', 'source_tree_sha256', 'build_receipt_sha256')})
        job = {'kind': 'deploy', 'payload': payload, 'fencing_token': 3}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {'artifact_receipts_dir': root / 'exchange'}
            host = type('Host', (), {})()
            source = Executor(api.registry, api.transport(), host, root / 'source', dict(config, runner_role='source'))
            cluster = Executor(api.registry, api.transport(), host, root / 'cluster', dict(config, runner_role='cluster'))
            receipt = source.artifact_receipt(source.artifact_key(artifact))
            durable_write(receipt, {'ok': True, 'artifact': dict(artifact, diff_sha256='b' * 64)})
            with self.assertRaisesRegex(ValueError, 'tested_artifact_receipt_mismatch'):
                cluster.execute(job)
            durable_write(receipt, {'ok': True, 'artifact': artifact})
            receipt.chmod(0o666)
            with self.assertRaisesRegex(ValueError, 'tested_artifact_receipt_untrusted'):
                cluster.execute(job)
            self.assertFalse(api.patches)
            receipt.chmod(0o600)
            self.assertTrue(cluster.execute(job)['ok'])

    def test_executor_verify_gate_and_reconcile_original_payload_no_redeploy(self):
        from liveprobe.application_runner.worker import durable_write
        from liveprobe.application_runner.deploy import Deployer
        api=KubeHTTP();payload=api.payload()
        payload['artifact'].update({k:'a'*64 for k in ('source_snapshot_id','diff_sha256','source_tree_sha256','build_receipt_sha256')})
        with tempfile.TemporaryDirectory() as tmp:
            executor=Executor(api.registry,api.transport(),type('Host',(),{})(),tmp,{})
            key=executor.artifact_key(payload['artifact'])
            durable_write(Path(tmp)/'artifacts'/(key+'.json'),{'ok':True,'artifact':payload['artifact']})
            result=executor.execute({'kind':'deploy','payload':payload,'fencing_token':3})
            self.assertTrue(result['ok'])
            self.assertFalse((Path(tmp)/'promotions'/(key+'-dev.json')).exists())
            wrapped={'original_job_id':'old','original_kind':'deploy','original_payload':payload,'idempotency_key':'idem'}
            result=executor.execute({'kind':'reconcile','payload':wrapped,'fencing_token':99})
            self.assertTrue(result['ok']);self.assertEqual(len(api.patches),2)
            result=executor.execute({'kind':'verify','payload':payload,'fencing_token':2})
            self.assertTrue(result['ok']);self.assertEqual(result['write_outcome'],'not_written')
            self.assertEqual(result['verification'][0]['business']['status_code'],200)
            self.assertTrue((Path(tmp)/'promotions'/(key+'-dev.json')).is_file())

if __name__=='__main__':unittest.main()
