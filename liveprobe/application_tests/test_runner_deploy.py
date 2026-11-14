"""실제 배포 조정기를 돌리고 K8s/HTTP transport만 치환한다."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from liveprobe.application_runner.profiles import Registry,digest


class DeployTests(unittest.TestCase):
    def test_cas_readback_unknown_and_cross_group_guards(self):
        try:
            from liveprobe.application_runner.deploy import Deployer, summarize, Conflict
        except ImportError:
            self.fail('배포 CAS 구현이 없습니다')
        image='liveprobe/payment@sha256:'+'a'*64
        new='liveprobe/payment@sha256:'+'b'*64
        doc={'metadata':{'uid':'u1','resourceVersion':'7','annotations':{}},'spec':{'template':{'metadata':{'labels':{'app':'payment'}},'spec':{'containers':[{'name':'payment','image':image}]}}},'status':{'observedGeneration':1,'updatedReplicas':1,'readyReplicas':1,'availableReplicas':1},'generation':1}
        class Transport:
            def __init__(self): self.doc=deepcopy(doc);self.patches=[];self.lose=False;self.conflict=False;self.unreadable=False
            def read(self,env,service):
                if self.unreadable: raise OSError('정적 오류')
                return deepcopy(self.doc)
            def patch(self,env,service,ops):
                self.patches.append(ops)
                if self.conflict: raise Conflict('cas_conflict')
                for op in ops:
                    if op['op']=='add' and op['path'].startswith('/metadata/annotations/'):
                        self.doc['metadata']['annotations'][op['path'].split('/')[-1].replace('~1','/')]=op['value']
                    if op['op']=='replace' and op['path'].endswith('/image'):
                        self.doc['spec']['template']['spec']['containers'][0]['image']=new
                self.doc['metadata']['resourceVersion']=str(int(self.doc['metadata']['resourceVersion'])+1)
                if self.lose and any(o['op']=='replace' for o in ops): self.unreadable=True;raise TimeoutError()
            def pods(self,env,service): return [{'image':new,'image_id':new,'ready':True,'owned':True}]
            def business(self,env,service,path): return {'status':200,'body':{'payments':[{'amount':100,'charged_amount':100}], 'processed':1}}
        registry=Registry(ROOT)
        target={**summarize(doc,'dev','payment',registry),'previous_image':image}
        payload={'stage':'dev','targets':[target],'artifact':{'image':new,'affected_services':['payment']},'authorization_kind':'auto_dev','release_id':'release','attempt_id':'attempt','idempotency_key':'idem'}
        with tempfile.TemporaryDirectory() as tmp:
            t=Transport();runner=Deployer(registry,t,Path(tmp))
            result=runner.deploy(payload,3)
            self.assertTrue(result['ok'])
            self.assertEqual(result['write_outcome'],'written')
            paths={op['path'] for op in t.patches[1] if op['op']=='test'}
            self.assertTrue({'/metadata/uid','/metadata/resourceVersion','/spec/template','/metadata/annotations/liveprobe.io~1application-owner','/metadata/annotations/liveprobe.io~1application-lease','/metadata/annotations/liveprobe.io~1application-fence'}.issubset(paths))
            runner.deploy(payload,3)
            self.assertEqual(len(t.patches),2)
        with tempfile.TemporaryDirectory() as tmp:
            t=Transport();t.lose=True;runner=Deployer(registry,t,Path(tmp))
            result=runner.deploy(payload,3)
            self.assertFalse(result['ok']);self.assertEqual(result['write_outcome'],'unknown')
            runner.deploy(payload,3)
            self.assertEqual(len(t.patches),2)
            t.unreadable=False
            self.assertTrue(runner.reconcile(payload,3)['ok'])
            self.assertEqual(len(t.patches),2)
        with tempfile.TemporaryDirectory() as tmp:
            t=Transport();t.conflict=True;runner=Deployer(registry,t,Path(tmp))
            out=runner.deploy(payload,3)
            self.assertEqual(out['error'],'cas_conflict');self.assertEqual(out['write_outcome'],'not_written')
            runner.deploy(payload,3);self.assertEqual(len(t.patches),1)
            bad=deepcopy(payload);bad['targets'][0]['system']='samsung-delivery'
            with self.assertRaises(ValueError): runner.deploy(bad,3)
            bad=deepcopy(payload);bad['stage']='prd';bad['authorization_kind']='auto_dev'
            with self.assertRaises(ValueError): runner.deploy(bad,3)

    def test_raw_sdk36_response_types_map_and_business_body_rejection(self):
        try:
            from liveprobe.application_runner.deploy import KubeTransport, validate_business
        except ImportError:
            self.fail('SDK 어댑터 구현이 없습니다')
        class API:
            def call_api(self,*args,**kwargs):
                self.args=args;self.kwargs=kwargs;return {}
        api=API();transport=KubeTransport(api)
        transport.patch('dev','payment',[{'op':'test','path':'/metadata/uid','value':'u'}])
        self.assertEqual(api.kwargs['response_types_map'],{200:'object',201:'object'})
        self.assertEqual(api.kwargs['header_params']['Content-Type'],'application/json-patch+json')
        p=Registry(ROOT).get('payment')
        with self.assertRaises(ValueError):validate_business(p,{'status':200,'body':{'healthy':True}})
        with self.assertRaises(ValueError):validate_business(p,{'status':200,'body':{'payments':[{'amount':100,'charged_amount':200}]}})

if __name__=='__main__':unittest.main()
