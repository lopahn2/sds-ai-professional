"""프로필 실제 경로와 업무 HTTP 상태/모드 불변식 회귀."""
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from liveprobe.application_runner.profiles import Registry
from liveprobe.application_runner.deploy import KubeTransport,validate_business

class RuntimeProfileTests(unittest.TestCase):
    def test_registered_java_paths_match_current_controllers(self):
        registry=Registry(ROOT)
        self.assertEqual(registry.get('order')['verification']['path'],'/api/orders/check?item=a')
        self.assertEqual(registry.get('inventory')['verification']['path'],'/api/stock/a')
        self.assertEqual(registry.get('notification')['language'],'python')
    def test_proxy_business_reads_actual_status_and_python_config_identity(self):
        class API:
            def __init__(self):self.calls=[];self.status=200
            def call_api(self,path,method,**kwargs):
                self.calls.append((path,method,kwargs))
                body={'service':'catalog','environment':'dev','version':'v1'} if path.endswith('/api/config') else {'products':[{'price':1}]}
                return (body,self.status,{}) if not kwargs.get('_return_http_data_only',True) else body
        from liveprobe.application_runner.cli import business_reader
        api=API();kube=KubeTransport(api);read=business_reader(Registry(ROOT),kube)
        out=read('dev','catalog','/api/products')
        self.assertEqual(out['status'],200)
        self.assertTrue(any(p.endswith('/api/config') for p,m,k in api.calls))
        self.assertTrue(all(m=='GET' for p,m,k in api.calls))
        api.status=503
        with self.assertRaises(ValueError):validate_business(Registry(ROOT).get('catalog'),read('dev','catalog','/api/products'))
        with self.assertRaises(ValueError):read('dev','catalog','/api/chaos')
    def test_domain_business_source_and_replica_fresh_are_different_invariants(self):
        registry=Registry(ROOT)
        source={'read_only':True,'db_ready':True,'sync':{'state':'source','pending_outbox':0}}
        self.assertTrue(validate_business(registry.get('business-api'),{'status':200,'body':source})['ok'])
        with self.assertRaises(ValueError):validate_business(registry.get('trade-member'),{'status':200,'body':source})
        fresh={'read_only':True,'db_ready':True,'sync':{'state':'fresh','unresolved_quarantine':0,'dependency_error':False,'storage_rebuild_required':False,'observed_offset_lag':0}}
        self.assertTrue(validate_business(registry.get('trade-member'),{'status':200,'body':fresh})['ok'])
        with self.assertRaises(ValueError):validate_business(registry.get('business-api'),{'status':200,'body':fresh})
        source['sync']['pending_outbox']=1
        with self.assertRaises(ValueError):validate_business(registry.get('business-api'),{'status':200,'body':source})

if __name__=='__main__':unittest.main()
