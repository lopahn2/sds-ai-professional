"""P18 lifecycle safety regressions. Fake clients only; no cluster/network/model writes."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('lifecycle_runner',ROOT/'scripts/p18-campaign-run.py')
assert spec and spec.loader
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)


def dep(name='payment'):
    return {'metadata':{'name':name,'uid':name+'-uid','resourceVersion':'1','generation':1},'spec':{'replicas':1,'template':{'metadata':{},'spec':{'containers':[{'name':name,'image':'baseline'}]}}},'status':{'observedGeneration':1,'updatedReplicas':1,'readyReplicas':1,'replicas':1}}


def detail(i=11,status='handed_off',workload='payment',created=None):
    return {'incident':{'id':i,'status':status,'namespace':'dev','workload':workload,'created_at':created or r.stamp()},'agent_runs':[{'id':i,'input_tokens':7,'output_tokens':3}],'actions':[]}


def case():
    return {'id':'payment-09','service':'payment','system':'payment','source':'runtime','injections':[{'fault':'memory_leak','params':{}}],'ttl_seconds':900}


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='p18-lifecycle-',dir=str(Path.home()/'.hermes/cache/scratch'))
        self.addCleanup(self.tmp.cleanup)
        self.h=r.Harness.__new__(r.Harness);self.h.ns='dev';self.h.output=Path(self.tmp.name)
        self.h.args=NS(analysis_timeout=0,detect_timeout=0,quiet_seconds=0,restore_timeout=0,drain_timeout=0,build_manifest=self.h.output/'build.json')
        self.h.args.build_manifest.write_text('[]');r.STOP=False

    def prepare(self,sequence=None):
        h=self.h;self.events=[];self.records={};pending=sequence or [];self.sequence=iter(pending)
        h.node_guard=lambda:None;h.dep=lambda n:dep(n);h.wait_ready=lambda *a,**k:True
        h.chaos=lambda *a,**k:{'status':200,'body':{'active':{},'release_active':{}}}
        h.pods=lambda n:[];h.local=lambda *a,**k:{'status':200,'transport_exit':0,'body':{'payments':[{'id':'1'}],'notifications':[{'id':'1'}]}}
        def api(path,method='GET',data=None):
            self.events.append((method,path))
            if '?' in path:return {'items':[],'total':0}
            if path=='/api/llm/accounts':return {'accounts':[{'name':'primary','state':'available'}]}
            iid=int(path.split('/')[3]);d=self.records[iid]
            if method=='GET':return copy.deepcopy(d)
            if path.endswith('/control'):
                d['incident']['status']='handed_off'
                for a in d['actions']:
                    if a['status'] in ('approved','executing'):a['status']='succeeded'
                return {'state':'stopping'}
            if path.endswith('/close'):d['incident']['status']='closed';return {'status':'closed'}
            raise AssertionError(path)
        h.api=api
        def inject(*a):
            self.events.append(('inject','payment'))
            for batch in pending:
                for d in batch:d['incident']['created_at']=r.stamp()
            return []
        h.inject=inject;h.observe=lambda *a:{'verified':True}
        h.restore=lambda *a:self.events.append(('restore','writes')) or {'verified':True}
        def details(watermark):
            ds=next(self.sequence,[])
            for d in ds:self.records[d['incident']['id']]=copy.deepcopy(d)
            return copy.deepcopy(ds)
        h.details_since=details
        return self.events

    def run_case(self):
        with patch.object(r,'verify_deployed_builds'),patch('builtins.print'):
            return self.h.one(case())

    def saved(self):return json.loads((self.h.output/case()['id']/'result.json').read_text())

    def test_stop_and_drain_before_restore(self):
        self.prepare([[detail(status='analyzing')]])
        self.run_case()
        self.assertLess(self.events.index(('POST','/api/incidents/11/control')),self.events.index(('restore','writes')))
        self.assertLess(self.events.index(('GET','/api/incidents/11')),self.events.index(('restore','writes')))

    def test_drain_timeout_is_indeterminate_no_restore_or_next_injection(self):
        self.prepare([[detail(status='acting')]])
        old=self.h.api
        self.h.api=lambda p,m='GET',data=None: {'state':'stopping'} if p.endswith('/control') else old(p,m,data)
        with self.assertRaises(RuntimeError):self.run_case()
        self.assertNotIn(('restore','writes'),self.events)
        self.assertEqual(self.saved()['state'],'indeterminate')

    def test_pending_approval_reject_readback_close_before_restore(self):
        d=detail(status='action_pending');d['actions']=[{'id':33,'status':'pending_approval'}]
        self.prepare([[d]]);old=self.h.api
        def api(p,m='GET',data=None):
            if p=='/api/actions/33/reject':
                self.events.append((m,p));self.records[11]['actions'][0]['status']='rejected';self.records[11]['incident']['status']='handed_off';return {'status':'rejected'}
            return old(p,m,data)
        self.h.api=api;out=self.run_case()
        self.assertIn(('POST','/api/actions/33/reject'),self.events)
        self.assertNotIn(('POST','/api/incidents/11/control'),self.events)
        self.assertEqual(out['incidents'][0]['incident']['status'],'closed')

    def test_approved_executing_and_proposed_never_sent_to_reject(self):
        for status in ('approved','executing','proposed'):
            with self.subTest(status=status):
                d=detail(status='acting');d['actions']=[{'id':33,'status':status}]
                self.prepare([[d]])
                if status=='proposed':
                    with self.assertRaises(RuntimeError):self.run_case()
                    self.assertEqual(self.saved()['state'],'indeterminate')
                else:self.run_case()
                self.assertFalse(any('/reject' in p for _,p in self.events))

    def test_unrelated_workload_cannot_complete_case(self):
        self.prepare([[detail(workload='catalog')]])
        # 원복·drain 확인된 미감지 건은 실패로 기록하고 계속 진행한다(완료로 바꾸지 않음).
        self.run_case()
        out=self.saved();self.assertEqual(out['state'],'analysis_incomplete');self.assertEqual(out['incidents'],[])
        self.assertEqual(r.after_case(out),'continue')
        self.assertEqual(out['excluded_incidents'][0]['incident']['workload'],'catalog')

    def test_analysis_timeout_stopped_by_runner_is_not_completed(self):
        # 분석이 시간 안에 끝나지 않아 실행기가 stop/close한 사건은 terminal이어도 완료가 아니다.
        self.prepare([[detail(status='analyzing')]])
        self.run_case()
        out=self.saved()
        self.assertTrue(out['analysis_timed_out'])
        self.assertEqual(out['state'],'analysis_incomplete')
        self.assertEqual(out['attributed_incident_ids'],[11])

    def test_final_snapshot_fixed_ids_usage_and_late_exclusions_consistent(self):
        self.prepare([[detail()],[detail(12,'analyzing','catalog')]])
        out=self.run_case()
        self.assertEqual(out['state'],r.case_state(out['fault_observation'],out['incidents']))
        self.assertEqual(out['state'],'completed')
        self.assertEqual([d['incident']['id'] for d in out['incidents']],[11])
        self.assertEqual(out['incidents'][0]['agent_runs'][0]['input_tokens'],7)
        self.assertEqual([d['incident']['id'] for d in out['excluded_incidents']],[12])

    def test_late_target_is_excluded_after_window_freeze(self):
        self.prepare([[detail()]])
        old=self.h.restore
        def restore(*a):
            self.sequence=iter([[detail(12,'analyzing','payment')]])
            return old(*a)
        self.h.restore=restore;out=self.run_case()
        self.assertEqual(out['attributed_incident_ids'],[11])
        self.assertEqual(out['excluded_incidents'][0]['incident']['id'],12)
        self.assertIn('window',out['excluded_incidents'][0]['exclusion_reason'])

    def test_release_active_blocks_baseline(self):
        self.prepare();self.h.chaos=lambda *a,**k:{'status':200,'body':{'release_active':{'code_bug':{}}}}
        with self.assertRaises(RuntimeError):self.run_case()
        self.assertNotIn(('inject','payment'),self.events)

    def test_baseline_business_500_blocks_injection(self):
        self.prepare();self.h.local=lambda *a,**k:{'status':500}
        with self.assertRaises(RuntimeError):self.run_case()
        self.assertNotIn(('inject','payment'),self.events)

    def test_health_checks_only_non_destructive_gets(self):
        calls=[];self.h.local=lambda n,p,m='GET',data=None,**k:calls.append((n,p,m,data)) or {'status':200,'body':{'payments':[{'id':'1'}],'notifications':[{'id':'1'}]}}
        for n in r.SERVICES:self.assertTrue(self.h.business_health(n)['verified'])
        self.assertTrue(calls);self.assertTrue(all(m=='GET' and data is None for _,_,m,data in calls))

    def test_pagination_finds_old_active_page_two(self):
        self.prepare()
        def api(p,*a,**k):
            if p=='/api/llm/accounts':return {'accounts':[{'name':'primary','state':'available'}]}
            offset=int(parse_qs(urlsplit(p).query).get('offset',['0'])[0])
            return {'total':501,'items':([{'id':i,'namespace':'stg','status':'closed'} for i in range(2,502)] if not offset else [{'id':1,'namespace':'dev','status':'analyzing'}])}
        self.h.api=api
        with self.assertRaisesRegex(RuntimeError,'existing analysis'):self.run_case()
        self.assertNotIn(('inject','payment'),self.events)

    def test_pagination_rejects_repeated_page_missing_total_and_total_drift(self):
        for mode in ('repeat','missing','drift','short'):
            with self.subTest(mode=mode):
                calls=[]
                def api(p,*a,**k):
                    calls.append(p)
                    if mode=='missing':return {'items':[]}
                    count=1 if mode=='short' else 500
                    return {'total':1000+(1 if mode=='drift' and len(calls)>1 else 0),'items':[{'id':i,'namespace':'stg'} for i in range(count)]}
                self.h.api=api
                with self.assertRaisesRegex(RuntimeError,'pagination'):self.h.details_since(0)

    def configure_restore(self):
        h=self.h;self.deleted=[];self.visited=[];h._case_service='payment';h._changed_services={'payment'}
        def read(n):self.visited.append(n);return dep(n)
        h.dep=read;h.serialize=lambda x:x
        h.pods=lambda n:[{'metadata':{'namespace':'dev','name':n+'-pod','uid':n+'-poduid','resourceVersion':'1','ownerReferences':[{'kind':'ReplicaSet','controller':True,'name':n+'-rs','uid':n+'-rsuid'}]},'spec':{}}]
        h.apps=NS(read_namespaced_replica_set=lambda rs,*a,**k:{'metadata':{'name':rs,'uid':rs.replace('-rs','-rsuid'),'ownerReferences':[{'kind':'Deployment','controller':True,'uid':rs.replace('-rs','-uid')}]}})
        h.core=NS(read_namespaced_pod=lambda p,*a,**k:copy.deepcopy(h.pods(p.removesuffix('-pod'))[0]),
                  delete_namespaced_pod=lambda p,*a,**k:self.deleted.append(p))
        h.client=NS(V1DeleteOptions=lambda **k:k,V1Preconditions=lambda **k:k)
        h.chaos=lambda *a,**k:{'status':200,'body':{'active':{},'release_active':{}}};h.wait_ready=lambda *a,**k:True
        h.local=lambda *a,**k:{'status':200,'body':{'payments':[{'id':'1'}],'notifications':[{'id':'1'}]}}

    def test_restore_target_first_continue_after_unrelated_service_failure(self):
        self.configure_restore();read=self.h.dep
        def fail(n):
            if n=='order':self.visited.append(n);raise RuntimeError('unrelated read failure')
            return read(n)
        self.h.dep=fail;out=self.h.restore({n:dep(n) for n in r.SERVICES})
        self.assertEqual(self.visited[0],'payment');self.assertIn('notification',self.visited)
        self.assertFalse(out['verified']);self.assertIn('payment-pod',self.deleted)
        self.assertEqual(len(out['services']),7)

    def test_restore_does_not_reset_six_normal_services(self):
        self.configure_restore();out=self.h.restore({n:dep(n) for n in r.SERVICES})
        self.assertTrue(out['verified']);self.assertEqual(self.deleted,['payment-pod'])

    def test_restore_empty_or_keyless_chaos_body_is_not_clear(self):
        for body in ('',{},{'active':{}}):
            with self.subTest(body=body):
                self.configure_restore();self.h.chaos=lambda *a,**k:{'status':200,'body':copy.deepcopy(body)}
                out=self.h.restore({n:dep(n) for n in r.SERVICES})
                self.assertFalse(out['verified'])

    def test_empty_chaos_body_blocks_baseline(self):
        for body in ('',{},{'active':{}}):
            with self.subTest(body=body):
                self.prepare();self.h.chaos=lambda *a,**k:{'status':200,'body':copy.deepcopy(body)}
                with self.assertRaises(RuntimeError):self.run_case()
                self.assertNotIn(('inject','payment'),self.events)

    def test_final_target_reopens_cannot_remain_completed(self):
        self.prepare([[detail()]])
        old=self.h.restore
        def restore(*a):
            out=old(*a);self.records[11]['incident']['status']='analyzing';return out
        self.h.restore=restore
        with self.assertRaises(RuntimeError):self.run_case()
        out=self.saved();self.assertEqual(out['state'],'indeterminate')
        self.assertEqual(out['incidents'][0]['incident']['status'],'analyzing')

    def test_explicit_chain_ids_only_inside_frozen_window(self):
        ds=[detail(11,workload='payment',created='2026-01-01T00:00:01Z'),detail(12,workload='catalog',created='2026-01-01T00:00:01Z'),detail(13,workload='order',created='2026-01-01T00:00:01Z'),detail(14,workload='member',created='2026-01-01T00:00:03Z')]
        c=case();c['linked_incident_ids']=[12,14]
        included,excluded=self.h.partition_incidents(ds,c,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:02+00:00')
        self.assertEqual([d['incident']['id'] for d in included],[11,12]);self.assertEqual([d['incident']['id'] for d in excluded],[13,14])

    def test_close_ack_without_closed_readback_blocks_restore(self):
        self.prepare([[detail()]])
        old=self.h.api
        self.h.api=lambda p,m='GET',data=None: {'status':'closed'} if p.endswith('/close') else old(p,m,data)
        with self.assertRaises(RuntimeError):self.run_case()
        self.assertNotIn(('restore','writes'),self.events);self.assertEqual(self.saved()['state'],'indeterminate')

    def test_stopping_ack_does_not_drain_executing_action_even_if_terminal(self):
        d=detail();d['actions']=[{'id':33,'status':'executing'}];self.prepare([[d]])
        with self.assertRaises(RuntimeError):self.run_case()
        self.assertNotIn(('restore','writes'),self.events);self.assertEqual(self.saved()['state'],'indeterminate')

    def test_restoration_failure_persists_actual_attempt_not_skipped(self):
        self.prepare([[detail()]])
        def restore(*a):self.events.append(('restore','writes'));raise RuntimeError('request outcome unknown')
        self.h.restore=restore
        with self.assertRaises(RuntimeError):self.run_case()
        out=self.saved();self.assertTrue(out['drain']['verified'])
        self.assertFalse(out['restoration'].get('skipped',False));self.assertEqual(out['state'],'indeterminate')

    def test_known_changed_crashing_target_still_gets_restore_attempt(self):
        self.configure_restore();calls=[]
        def chaos(n,method='GET',data=None):
            calls.append(method)
            return {'status':None} if len(calls)==1 else {'status':200,'body':{'active':{},'release_active':{}}}
        self.h.chaos=chaos;out=self.h.restore({'payment':dep()})
        self.assertEqual(self.deleted,['payment-pod']);self.assertTrue(out['verified'])

    def test_missing_old_harness_flags_do_not_weaken_readback(self):
        self.prepare([[detail(status='acting')]])
        del self.h.args.drain_timeout
        old=self.h.api
        self.h.api=lambda p,m='GET',data=None: {'state':'stopping'} if p.endswith('/control') else old(p,m,data)
        with self.assertRaises(RuntimeError):self.run_case()
        self.assertEqual(self.saved()['state'],'indeterminate');self.assertNotIn(('restore','writes'),self.events)

    def test_drain_http_requests_use_remaining_deadline_budget(self):
        import io
        self.h.args.api='http://unit-fixture.invalid';self.h._api_deadline=3
        with patch.object(r.time,'monotonic',return_value=1),patch.object(r.urllib.request,'urlopen',return_value=io.StringIO('{}')) as call:
            self.h.api('/api/incidents/11')
        self.assertEqual(call.call_args.kwargs['timeout'],2)
        with patch.object(r.time,'monotonic',return_value=4),patch.object(r.urllib.request,'urlopen') as call:
            with self.assertRaisesRegex(RuntimeError,'deadline'):self.h.api('/api/incidents/11')
            call.assert_not_called()

    def _http_error(self,code):
        import io
        return r.urllib.error.HTTPError('http://unit-fixture.invalid/x',code,'fixture',{},io.BytesIO(b'{}'))

    def test_get_retries_transient_gateway_error_once(self):
        # 실제 사례(catalog-06): 사이드카가 연결 재사용 중 끊긴 응답을 503 UC로 돌려 사례 전체가 failed가 됨
        import io
        self.h.args.api='http://unit-fixture.invalid';self.h._api_deadline=None
        answers=[self._http_error(503),io.StringIO('{"ok":1}')]
        def opener(*a,**k):
            value=answers.pop(0)
            if isinstance(value,Exception):raise value
            return value
        with patch.object(r.urllib.request,'urlopen',side_effect=opener) as call,patch.object(r.time,'sleep') as nap:
            self.assertEqual(self.h.api('/api/incidents?limit=500&offset=0'),{'ok':1})
        self.assertEqual(call.call_count,2);nap.assert_called_once()

    def test_get_gateway_error_twice_still_fails(self):
        self.h.args.api='http://unit-fixture.invalid';self.h._api_deadline=None
        with patch.object(r.urllib.request,'urlopen',side_effect=[self._http_error(503),self._http_error(502)]) as call,patch.object(r.time,'sleep'):
            with self.assertRaisesRegex(RuntimeError,'HTTP 502'):self.h.api('/api/incidents/11')
        self.assertEqual(call.call_count,2)

    def test_post_and_non_gateway_errors_never_retry(self):
        self.h.args.api='http://unit-fixture.invalid';self.h._api_deadline=None
        for method,code in (('POST',503),('GET',500),('GET',404),('DELETE',503)):
            with self.subTest(method=method,code=code):
                with patch.object(r.urllib.request,'urlopen',side_effect=[self._http_error(code)]) as call,patch.object(r.time,'sleep'):
                    with self.assertRaisesRegex(RuntimeError,f'HTTP {code}'):self.h.api('/api/incidents/11/stop',method=method,data={} if method=='POST' else None)
                self.assertEqual(call.call_count,1)

    def test_get_retry_respects_drain_deadline(self):
        self.h.args.api='http://unit-fixture.invalid';self.h._api_deadline=3
        clock=iter([1,3.5])
        with patch.object(r.time,'monotonic',side_effect=lambda:next(clock)),patch.object(r.urllib.request,'urlopen',side_effect=[self._http_error(503)]) as call,patch.object(r.time,'sleep'):
            with self.assertRaisesRegex(RuntimeError,'HTTP 503|deadline'):self.h.api('/api/incidents/11')
        self.assertEqual(call.call_count,1)

    def test_restore_business_500_not_healthy(self):
        self.configure_restore();self.h.local=lambda *a,**k:{'status':500}
        self.assertFalse(self.h.restore({'payment':dep()})['verified'])


    def test_real_restore_409_persists_indeterminate_without_final_usage_marker(self):
        self.prepare([[detail()]]);self.configure_restore();self.h.restore=lambda b:r.Harness.restore(self.h,b)
        self.h.core.read_namespaced_pod=lambda p,*a,**k:copy.deepcopy(self.h.pods(p.removesuffix('-pod'))[0])
        class Conflict(Exception):
            status=409;reason='Conflict';body='{"kind":"Status","reason":"Conflict","code":409,"message":"unclassified conflict"}'
        def fail(p,*a,**k):self.deleted.append(p);raise Conflict()
        self.h.core.delete_namespaced_pod=fail
        with self.assertRaisesRegex(RuntimeError,'drain/restoration failed'):self.run_case()
        out=self.saved();self.assertEqual(out['state'],'indeterminate');self.assertTrue(out['drain']['verified'])
        self.assertFalse(out['restoration']['verified']);self.assertEqual(self.deleted,['payment-pod'])
        self.assertEqual(len(out['restoration']['services']),7)
        self.assertTrue(all(row['verified'] for row in out['restoration']['services'] if row['service']!='payment'))
        self.assertNotIn('recorded_at',out);self.assertNotIn('usage_snapshot_at',out)
        self.assertEqual(r.after_case(out),'stop');self.assertEqual(r.resume_action(out),'block')


if __name__=='__main__':unittest.main(verbosity=2)
