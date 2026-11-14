"""장애 성분별 필수 관측 판정 회귀. 모든 데이터는 단위 fixture다."""
import importlib.util
from pathlib import Path
import unittest


def module():
    path=Path(__file__).with_name('p18-fault-observation.py')
    spec=importlib.util.spec_from_file_location('fault_observation',path)
    assert spec and spec.loader
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    return m


def case(*faults,service='payment'):
    return {'service':service,'injections':[{'fault':name,'params':{}} for name in faults]}


class RequiredObservations(unittest.TestCase):
    def test_unrelated_503_is_not_memory_evidence(self):
        result=module().assess(case('memory_leak'),{'probes':[{'status':503}]})
        self.assertFalse(result['verified'])
        self.assertFalse(result['components']['memory_leak']['verified'])


    def test_new_oom_is_memory_evidence_but_old_oom_is_not(self):
        m=module();c={'uid':'pod','name':'payment','restartCount':2,'lastState':{'terminated':{'reason':'OOMKilled','exitCode':137}}}
        facts={'before_containers':[{'uid':'pod','name':'payment','restartCount':1}],'containers':[c]}
        self.assertTrue(m.assess(case('memory_leak'),facts)['verified'])
        facts['before_containers'][0]['restartCount']=2
        self.assertFalse(m.assess(case('memory_leak'),facts)['verified'])

    def test_java_heap_oom_is_not_kernel_oom_but_proves_memory_failure(self):
        facts={'since':'2026-10-25T00:00:00Z','logs_since':'2026-10-25T00:00:00Z',
               'logs':[{'text':'Terminating due to java.lang.OutOfMemoryError: Java heap space'}]}
        result=module().assess(case('memory_leak',service='order'),facts)
        self.assertTrue(result['verified'])
        self.assertEqual(result['components']['memory_leak']['evidence']['kind'],'jvm_heap_oom')
        facts['logs_since']='2026-10-24T00:00:00Z'
        self.assertFalse(module().assess(case('memory_leak',service='order'),facts)['verified'])

    def test_combined_error_and_latency_needs_measured_http_latency(self):
        m=module();c=case('error_rate','latency_spike');c['injections'][1]['params']={'seconds':3}
        facts={'probes':[{'status':503,'seconds':20,'http_seconds':0.1}]}
        result=m.assess(c,facts);self.assertFalse(result['verified'])
        self.assertTrue(result['components']['error_rate']['verified'])
        self.assertFalse(result['components']['latency_spike']['verified'])
        facts['probes'].append({'status':200,'http_seconds':3.1})
        self.assertTrue(m.assess(c,facts)['verified'])


    def test_code_components_require_their_distinct_real_stack_frames(self):
        c=case('code_bug','code_bug_validation')
        facts={'since':'2026-10-25T00:00:00Z','logs_since':'2026-10-25T00:00:00Z',
               'probes':[{'faults':['code_bug'],'status':500},{'faults':['code_bug_validation'],'status':500}],
               'logs':[{'text':'Traceback (most recent call last):\n  File "app.py", line 1, in calculate_fee\nZeroDivisionError: division by zero'}]}
        m=module();result=m.assess(c,facts)
        self.assertTrue(result['components']['code_bug']['verified'])
        self.assertFalse(result['components']['code_bug_validation']['verified'])
        self.assertFalse(result['verified'])
        facts['logs'].append({'text':'Traceback (most recent call last):\n  File "app.py", line 2, in refund_quote\nKeyError: refundable_amount'})
        self.assertTrue(m.assess(c,facts)['verified'])

    def test_cpu_requires_an_actual_metric(self):
        m=module();c=case('cpu_spin');facts={'probes':[{'status':503}],'symptoms':['observed_cpu_pressure']}
        self.assertFalse(m.assess(c,facts)['verified'])
        facts['cpu_cores']=0.4
        self.assertTrue(m.assess(c,facts)['verified'])
        for invalid in (None,True,float('nan'),float('inf'),-1):
            facts['cpu_cores']=invalid
            self.assertFalse(m.assess(c,facts)['verified'])

    def test_background_and_invariant_require_separate_exception_stacks(self):
        c=case('background_job_failure','business_invariant')
        facts={'since':'2026-10-25T00:00:00Z','logs_since':'2026-10-25T00:00:00Z',
               'logs':[{'text':'Traceback (most recent call last):\n  File "app.py", line 1, in background_job\nSettlementError: fixture'}]}
        m=module();result=m.assess(c,facts)
        self.assertTrue(result['components']['background_job_failure']['verified'])
        self.assertFalse(result['verified'])
        facts['logs'].append({'text':'Traceback (most recent call last):\n  File "app.py", line 1, in check_invariants\nDuplicateChargeError: fixture'})
        self.assertTrue(m.assess(c,facts)['verified'])


    def test_readiness_requires_zero_ready_endpoints(self):
        m=module();facts={'health':{'status':503},'ready_endpoints':1}
        self.assertFalse(m.assess(case('not_ready'),facts)['verified'])
        facts['ready_endpoints']=0
        self.assertTrue(m.assess(case('not_ready'),facts)['verified'])

    def test_liveness_requires_failed_live_probe_and_new_restart(self):
        m=module();facts={'liveness':{'status':503},'before_containers':[{'uid':'pod','name':'payment','restartCount':1}],
                        'containers':[{'uid':'pod','name':'payment','restartCount':1}]}
        self.assertFalse(m.assess(case('liveness_fail'),facts)['verified'])
        facts['containers'][0]['restartCount']=2
        self.assertTrue(m.assess(case('liveness_fail'),facts)['verified'])

    def test_crash_and_config_need_repeat_exit1_and_config_stack(self):
        m=module();c=case('crash','startup_config_error')
        facts={'since':'2026-10-25T00:00:00Z','logs_since':'2026-10-25T00:00:00Z',
               'containers':[{'uid':'new','name':'payment','restartCount':2,'lastState':{'terminated':{'exitCode':1}}}],
               'logs':[]}
        result=m.assess(c,facts);self.assertTrue(result['components']['crash']['verified']);self.assertFalse(result['verified'])
        facts['logs']=[{'text':'Traceback (most recent call last):\n  File "app.py", line 1, in load_settings\nKeyError: fixture'}]
        self.assertTrue(m.assess(c,facts)['verified'])
        facts['containers'][0]['restartCount']=1
        self.assertFalse(m.assess(c,facts)['verified'])

    def test_hang_requires_http_timeout_not_exec_failure(self):
        m=module();facts={'probes':[{'status':None,'transport_exit':2,'seconds':20}]}
        self.assertFalse(m.assess(case('hang'),facts)['verified'])
        facts['probes'][0].update(http_timeout=True,http_seconds=5,timeout_seconds=5)
        self.assertTrue(m.assess(case('hang'),facts)['verified'])


    def test_pool_requires_its_503_stack_and_wait(self):
        m=module();facts={'since':'2026-10-25T00:00:00Z','logs_since':'2026-10-25T00:00:00Z',
                        'probes':[{'status':503,'http_seconds':10.1}],
                        'logs':[{'text':'WorkerPoolExhausted: fixture\n    at acquire (chaos.js:1:1)'}]}
        self.assertTrue(m.assess(case('worker_exhaustion'),facts)['verified'])
        facts['probes'][0]['http_seconds']=0.1
        self.assertFalse(m.assess(case('worker_exhaustion'),facts)['verified'])

    def test_dependency_needs_502_and_real_connection_refusal_stack(self):
        m=module();facts={'since':'2026-10-25T00:00:00Z','logs_since':'2026-10-25T00:00:00Z',
                        'probes':[{'status':502}],
                        'logs':[{'text':'DependencyError: connect ECONNREFUSED\n    at callDependency (dependency.js:1:1)'}]}
        self.assertTrue(m.assess(case('dependency_failure'),facts)['verified'])
        facts['probes'][0]['status']=503
        self.assertFalse(m.assess(case('dependency_failure'),facts)['verified'])

    def test_intermittent_needs_its_actual_exception_not_generic_500(self):
        m=module();facts={'probes':[{'status':500}], 'since':'2026-10-25T00:00:00Z','logs_since':'2026-10-25T00:00:00Z', 'logs':[]}
        self.assertFalse(m.assess(case('intermittent_exception'),facts)['verified'])
        facts['logs']=[{'text':'Traceback (most recent call last):\n  File "runtime.py", line 1, in read_session_state\nTransientStateError: fixture'}]
        self.assertTrue(m.assess(case('intermittent_exception'),facts)['verified'])


    def test_code_frame_contracts_cover_all_seven_sources_and_three_paths(self):
        m=module()
        for service,frames in m.CODE_FRAMES.items():
            for name,frame in zip(m.CODE_NAMES,frames):
                with self.subTest(service=service,fault=name):
                    if service in ('order','inventory'):
                        stack=f'java.lang.RuntimeException: fixture\n    at io.liveprobe.target.{service}.AppFaultBusiness.{frame}(AppFaultBusiness.java:1)'
                    elif service in ('member','delivery'):
                        stack=f'TypeError: fixture\n    at {frame} (business.js:1:1)'
                    else:stack=f'Traceback (most recent call last):\n  File "app.py", line 1, in {frame}\nValueError: fixture'
                    facts={'since':'2026-10-25T00:00:00Z','logs_since':'2026-10-25T00:00:00Z',
                           'logs':[{'text':stack}],'probes':[{'faults':[name],'status':500}]}
                    self.assertTrue(m.assess(case(name,service=service),facts)['verified'])
                    facts['probes'][0]['status']=503
                    self.assertFalse(m.assess(case(name,service=service),facts)['verified'])

    def test_generic_503_cannot_satisfy_other_catalog_components(self):
        import runpy
        names={item['fault'] for recipe in runpy.run_path(str(Path(__file__).with_name('p18-campaign-catalog.py')))['recipes']() for item in recipe[2]}
        self.assertEqual(len(names),18)
        m=module()
        for name in names:
            with self.subTest(fault=name):
                result=m.assess(case(name),{'probes':[{'status':503}],'symptoms':['application_exception_log','container_restart']})
                self.assertEqual(result['verified'],name=='error_rate')
        self.assertFalse(m.assess(case('unsupported_fault'),{'probes':[{'status':503}]})['verified'])

    def test_quantitative_unknowns_are_not_normal_or_slow(self):
        m=module()
        for value in (None,True,float('nan'),float('inf')):
            facts={'probes':[{'status':200,'http_seconds':value,'seconds':20}]}
            self.assertFalse(m.assess(case('latency'),facts)['verified'])
        self.assertFalse(m.assess(case('not_ready'),{'health':{'status':503},'ready_endpoints':False})['verified'])
        self.assertFalse(m.assess(case('latency_spike'),{'probes':[{'status':200,'http_seconds':4}]})['verified'])


if __name__=='__main__':unittest.main()
