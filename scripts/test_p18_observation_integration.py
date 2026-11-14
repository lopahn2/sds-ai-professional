"""실제 주입 없는 HTTP 계측·관측기 통합 회귀."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import tempfile

ROOT=Path(__file__).resolve().parents[1]
import sys
if str(ROOT/'liveprobe') not in sys.path:sys.path.insert(0,str(ROOT/'liveprobe'))  # 실행기 main()과 같은 app.redact 경로
spec=importlib.util.spec_from_file_location('integrated_runner',ROOT/'scripts/p18-campaign-run.py')
assert spec and spec.loader
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)


class ObservationIntegration(unittest.TestCase):
    def test_inner_http_timer_is_not_exec_wall_clock(self):
        h=r.Harness.__new__(r.Harness);h.args=NS(context='fixture');h.ns='dev';h.active_pod=lambda _:'fixture'
        out=NS(stdout='{"status":200,"http_seconds":0.025,"http_timeout":false,"body":"{}"}',returncode=0)
        with patch.object(r.subprocess,'run',return_value=out),patch.object(r.time,'monotonic',side_effect=[0,20]):
            result=h.local('payment','/api/payments')
        self.assertEqual(result['status'],200)
        self.assertEqual(result['http_seconds'],0.025)
        self.assertEqual(result['seconds'],20)
        self.assertFalse(result['http_timeout'])

    def test_curl_timeout_without_body_is_measured_as_http_timeout(self):
        # curl -sS --max-time 5 ... -w '\n%{http_code}\n%{time_total}' 가 응답 없이 시간초과(exit28)한 실제 출력 형태
        h=r.Harness.__new__(r.Harness);h.args=NS(context='fixture');h.ns='dev';h.active_pod=lambda _:'fixture'
        out=NS(stdout='\n000\n5.004213',returncode=28)
        with patch.object(r.subprocess,'run',return_value=out),patch.object(r.time,'monotonic',side_effect=[0,5.3]):
            result=h.local('order','/api/orders/check?item=a',timeout=5)
        self.assertNotIn('error',result)
        self.assertIsNone(result['status'])
        self.assertTrue(result['http_timeout'])
        self.assertAlmostEqual(result['http_seconds'],5.004213)
        self.assertEqual(result['transport_exit'],28)

    def test_curl_ok_and_empty_body_keep_status(self):
        h=r.Harness.__new__(r.Harness);h.args=NS(context='fixture');h.ns='dev';h.active_pod=lambda _:'fixture'
        for stdout,status,body in (('{"ok":true}\n200\n0.012',200,{'ok':True}),('\n204\n0.010',204,'')):
            with self.subTest(status=status):
                with patch.object(r.subprocess,'run',return_value=NS(stdout=stdout,returncode=0)),patch.object(r.time,'monotonic',side_effect=[0,1]):
                    result=h.local('inventory','/api/stock/a',timeout=5)
                self.assertNotIn('error',result)
                self.assertEqual(result['status'],status)
                self.assertEqual(result['body'],body)
                self.assertFalse(result['http_timeout'])

    def test_curl_unparseable_output_stays_unavailable(self):
        h=r.Harness.__new__(r.Harness);h.args=NS(context='fixture');h.ns='dev';h.active_pod=lambda _:'fixture'
        for stdout in ('', '000', 'abc\nxyz', '\n000\n-1'):
            with self.subTest(stdout=stdout):
                with patch.object(r.subprocess,'run',return_value=NS(stdout=stdout,returncode=28)),patch.object(r.time,'monotonic',side_effect=[0,1]):
                    result=h.local('order','/api/orders/1',timeout=5)
                self.assertEqual(result.get('error'),'http_measurement_unavailable')
                self.assertFalse(result['http_timeout'])
    def test_chaos_state_requires_explicit_keys(self):
        ok={'status':200,'body':{'active':[],'release_active':[]}}
        self.assertTrue(r.chaos_clear(ok))
        for bad in ({'status':200,'body':''},{'status':200,'body':{}},{'status':200,'body':{'active':[]}},
                    {'status':200,'body':None},{'status':200,'body':{'active':[],'release_active':[{'name':'x'}]}},
                    {'status':200,'body':{'active':{'hang':{}},'release_active':[]}},{'status':503,'body':{'active':[],'release_active':[]}},
                    None,{'status':200,'body':[]}):
            with self.subTest(bad=bad):
                self.assertFalse(r.chaos_clear(bad))

    def test_observe_does_not_accept_generic_503_as_memory(self):
        h=r.Harness.__new__(r.Harness);h.args=NS(detect_timeout=1);h.ns='dev'
        h.pods=lambda _:[];h.probe_calls=lambda _:[('GET','/business',None)]
        h.local=lambda *a,**k:{'status':503,'seconds':0.1,'http_seconds':0.01,'transport_exit':0}
        h.logs=lambda *a,**k:[]
        case={'service':'payment','injections':[{'fault':'memory_leak','params':{}}]}
        with tempfile.TemporaryDirectory(dir=str(Path.home()/'.hermes/cache/scratch')) as d:
            with patch.object(r.time,'monotonic',side_effect=[0,0,0,2]),patch.object(r.time,'sleep'):
                result=h.observe(case,Path(d),'2026-10-25T00:00:00Z',[])
        self.assertFalse(result['verified'])
        self.assertFalse(result['components']['memory_leak']['verified'])


    def test_current_business_ids_replace_evicted_seed_one(self):
        h=r.Harness.__new__(r.Harness);calls=[]
        def local(name,path,*args,**kwargs):
            calls.append(path)
            if path=='/api/payments':return {'status':200,'body':{'payments':[{'id':'901'}]}}
            if path=='/api/notifications':return {'status':200,'body':{'notifications':[{'id':'802'}]}}
            return {'status':404 if '/1/' in path else 200,'body':{}}
        h.local=local
        self.assertTrue(h.business_health('payment')['verified'])
        h.business_health('notification')
        self.assertTrue(any('/901/refund' in path for path in calls))
        self.assertTrue(any('/802/retry' in path for _,path,_ in h.probe_calls('notification')))
        self.assertFalse(any('/1/' in path for _,path,_ in h.probe_calls('payment')))
    def test_pod_logs_use_parameters_the_real_sdk_accepts(self):
        # 실제 CoreV1Api.read_namespaced_pod_log는 since_time을 받지 않는다(ApiTypeError). 전송만 가짜로 바꾼다.
        # 또 kubernetes-client 36은 str 응답을 "b'...'" 문자열로 돌려주므로 원시 바이트를 읽어야 실제 줄바꿈이 보존된다.
        from kubernetes import client as k8s
        api=k8s.CoreV1Api(k8s.ApiClient());seen=[]
        raw=('2026-10-31T11:00:30.000000000Z Traceback (most recent call last):\n'
             '2026-10-31T11:00:30.000001000Z   File "x", line 1\n'
             '2026-10-31T11:00:30.000002000Z TransientStateError: boom\n').encode()
        def fake_call(*args,**kwargs):
            seen.append((args[3],kwargs.get('_preload_content',True)))
            if kwargs.get('_preload_content',True):return str(raw)  # 실제 SDK의 손상된 문자열 형태
            return NS(data=raw)
        h=r.Harness.__new__(r.Harness);h.ns='dev';h.core=api
        h.pods=lambda _:[{'metadata':{'name':'payment-abc'}}]
        clock=r.datetime(2026,10,31,11,0,40,tzinfo=r.timezone.utc)
        with patch.object(api.api_client,'call_api',side_effect=fake_call),patch.object(r,'utcnow',return_value=clock,create=True):
            out=h.logs('payment',since='2026-10-31T11:00:00.250000+00:00')
        self.assertEqual(len(out),1)
        self.assertNotIn('error',out[0])
        self.assertIn('TransientStateError: boom',out[0]['text'])
        self.assertIn('Traceback (most recent call last):\n  File "x", line 1\n',out[0]['text'])  # 실제 줄바꿈
        self.assertNotIn("b'",out[0]['text'])
        query,preload=seen[0];query=dict(query)
        self.assertIs(preload,False)
        self.assertEqual(query.get('container'),'payment')
        self.assertEqual(query.get('timestamps'),True)
        self.assertGreaterEqual(query.get('sinceSeconds'),41)  # 주입 시각을 놓치지 않도록 올림 + 여유
        self.assertLessEqual(query.get('sinceSeconds'),60)

    def test_pod_logs_drop_lines_before_injection(self):
        h=r.Harness.__new__(r.Harness);h.ns='dev'
        body=('2026-10-31T10:59:59.900000000Z old IntermittentException: before\n'
              '2026-10-31T11:00:01.000000000Z io.x.IntermittentException: after\n'
              '2026-10-31T11:00:01.000100000Z \tat io.x.Filter.run(Filter.java:28)\n'
              'no-timestamp continuation\n').encode()
        h.core=NS(read_namespaced_pod_log=lambda *a,**k:NS(data=body))
        h.pods=lambda _:[{'metadata':{'name':'order-abc'}}]
        clock=r.datetime(2026,10,31,11,0,20,tzinfo=r.timezone.utc)
        with patch.object(r,'utcnow',return_value=clock,create=True):
            text=h.logs('order',since='2026-10-31T11:00:00+00:00')[0]['text']
        self.assertNotIn('before',text)
        self.assertIn('io.x.IntermittentException: after',text)
        self.assertIn('\tat io.x.Filter.run(Filter.java:28)',text)
        self.assertNotIn('2026-10-31T11:00:01',text)  # 판정기가 보는 원문 형식은 이전과 동일

    def test_pod_log_errors_stay_errors_not_empty_text(self):
        h=r.Harness.__new__(r.Harness);h.ns='dev'
        def boom(*a,**k):raise TimeoutError()
        h.core=NS(read_namespaced_pod_log=boom);h.pods=lambda _:[{'metadata':{'name':'order-abc'}}]
        out=h.logs('order',since='2026-10-31T11:00:00+00:00')
        self.assertEqual(out,[{'pod':'order-abc','error':'TimeoutError'}])

    def test_wait_ready_survives_pod_replacement_gap(self):
        h=r.Harness.__new__(r.Harness);h.args=NS(context='fixture');h.ns='dev'
        d={'metadata':{'generation':2},'spec':{'replicas':1},'status':{'observedGeneration':2,'updatedReplicas':1,'readyReplicas':1,'replicas':1}}
        h.dep=lambda _:d;answers=[RuntimeError('target Pod unavailable'),{'status':200}]
        def health(_):
            value=answers.pop(0)
            if isinstance(value,Exception):raise value
            return value
        h.health=health
        with patch.object(r.time,'monotonic',side_effect=[0,0,1,1]),patch.object(r.time,'sleep'):
            self.assertTrue(h.wait_ready('payment',timeout=10))
        self.assertEqual(answers,[])


if __name__=='__main__':unittest.main()
