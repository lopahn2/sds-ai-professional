"""실제 Deployer/KubeTransport + JSON Patch를 적용하는 가짜 HTTP 경계."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from liveprobe.application_runner.profiles import Registry
from liveprobe.application_runner.deploy import Deployer,KubeTransport,summarize
from liveprobe.app.runtime_fencing import OWNER_ANNOTATION,LEASE_ANNOTATION,FENCE_ANNOTATION
KEYS=(OWNER_ANNOTATION,LEASE_ANNOTATION,FENCE_ANNOTATION)

class KubeHTTP:
    def __init__(self,registry=None):
        self.registry=registry or Registry(ROOT)
        self.old=self.registry.images['catalog'];self.new='liveprobe/catalog@sha256:'+'b'*64
        self.doc={'metadata':{'uid':'u1','resourceVersion':'7','generation':1},'spec':{'replicas':1,'template':{'metadata':{'labels':{'app':'catalog'}},'spec':{'containers':[{'name':'catalog','image':self.old}]}}},'status':{'observedGeneration':1,'updatedReplicas':1,'readyReplicas':1,'availableReplicas':1}}
        self.patches=[];self.fail_image=False;self.fail_read=False;self.conflict=False
    def call_api(self,path,method,body=None,**kwargs):
        if method=='GET':
            if self.fail_read:raise OSError('read lost')
            if '/deployments/' in path:return deepcopy(self.doc)
            if path.endswith('/replicasets'):return {'items':[{'metadata':{'uid':'rs','ownerReferences':[{'uid':'u1'}]}}]}
            if path.endswith('/pods'):return {'items':[{'metadata':{'name':'pod','ownerReferences':[{'uid':'rs'}]},'status':{'containerStatuses':[{'name':'catalog','image':self.new,'imageID':'docker-pullable://'+self.new,'ready':True}]}}]}
            raise AssertionError('unexpected GET')
        self.patches.append(deepcopy(body))
        if self.conflict:
            exc=OSError();exc.status=409;raise exc
        updated=deepcopy(self.doc)
        for op in body:
            parts=[p.replace('~1','/').replace('~0','~') for p in op['path'].split('/')[1:]]
            parent=updated
            for p in parts[:-1]:parent=parent[int(p)] if isinstance(parent,list) else parent[p]
            key=int(parts[-1]) if isinstance(parent,list) else parts[-1]
            if op['op']=='test':
                if parent[key]!=op['value']:
                    exc=OSError();exc.status=422;raise exc
            elif op['op']=='remove':del parent[key]
            else:parent[key]=deepcopy(op['value'])
        updated['metadata']['resourceVersion']=str(int(updated['metadata']['resourceVersion'])+1)
        self.doc=updated
        if self.fail_image and any(op['op']=='replace' and op['path'].endswith('/image') for op in body):
            self.fail_read=True;raise TimeoutError('lost response')
        return deepcopy(self.doc)
    def business(self,env,service,path):return {'status':200,'body':{'products':[{'price':79000}]}}
    def transport(self):return KubeTransport(self,self.business)
    def payload(self):
        target=summarize(self.doc,'dev','catalog',self.registry)
        target['previous_image']=target.pop('image')
        return {'stage':'dev','targets':[target],'artifact':{'image':self.new,'affected_services':['catalog']},'authorization_kind':'auto_dev','release_id':'r','attempt_id':'a','idempotency_key':'idem'}

class FencingTests(unittest.TestCase):
    def test_acquire_then_image_cas_and_verify_releases_only_matching_markers(self):
        api=KubeHTTP();payload=api.payload()
        with tempfile.TemporaryDirectory() as tmp:
            deployer=Deployer(api.registry,api.transport(),tmp)
            result=deployer.deploy(payload,3)
            self.assertTrue(result['ok'],result)
            self.assertEqual(result['verification'],[])
            self.assertEqual(len(api.patches),2)
            acquire,image=api.patches
            self.assertEqual({k:api.doc['metadata']['annotations'][k] for k in KEYS},dict(zip(KEYS,['liveprobe-application','application:r:a','3'])))
            self.assertTrue(any(op['op']=='test' and op['path'] in ('/metadata','/metadata/annotations') for op in acquire))
            for patch in (acquire,image):
                self.assertTrue({'/metadata/uid','/metadata/resourceVersion'}.issubset({o['path'] for o in patch if o['op']=='test'}))
            for key in KEYS:
                self.assertTrue(any(o['op']=='test' and o['path']=='/metadata/annotations/'+key.replace('/','~1') for o in image))
            self.assertTrue(any(o['op']=='test' and o['value']==api.old and o['path'].endswith('/image') for o in image))
            self.assertEqual(result['targets'][0]['resource_version'],'9')
            self.assertEqual(result['targets'][0]['previous_image'],api.old)
            self.assertEqual(set(result['targets'][0]),{'environment','service','uid','resource_version','image','previous_image','write_outcome'})
            self.assertEqual(deployer.deploy(payload,3),result)
            self.assertEqual(len(api.patches),2)
            verified=deployer.verify(payload)
            self.assertEqual(verified[0]['business']['status_code'],200)
            self.assertFalse(any(k in api.doc['metadata']['annotations'] for k in KEYS))
            self.assertEqual(len(api.patches),3)
            for key in KEYS:
                self.assertTrue(any(o['op']=='test' and o['path'].endswith(key.replace('/','~1')) for o in api.patches[-1]))
    def test_any_preexisting_marker_is_409_target_owned_without_writes(self):
        from liveprobe.application_runner.deploy import Conflict
        self.assertEqual(Conflict.status,409)
        for key in KEYS:
            with self.subTest(key=key),tempfile.TemporaryDirectory() as tmp:
                api=KubeHTTP();api.doc['metadata']['annotations']={key:''};payload=api.payload()
                result=Deployer(api.registry,api.transport(),tmp).deploy(payload,3)
                self.assertEqual(result['error'],'target_owned')
                self.assertEqual(result['write_outcome'],'not_written')
                self.assertEqual(api.patches,[])
    def test_unknown_write_reconcile_is_read_only_and_uses_original_journal(self):
        api=KubeHTTP();payload=api.payload();api.fail_image=True
        with tempfile.TemporaryDirectory() as tmp:
            deployer=Deployer(api.registry,api.transport(),tmp)
            result=deployer.deploy(payload,3)
            self.assertEqual(result['write_outcome'],'unknown')
            api.fail_read=False
            result=deployer.reconcile(payload,99)
            self.assertTrue(result['ok'],result)
            self.assertEqual(len(api.patches),2)
            self.assertTrue(all(k in api.doc['metadata']['annotations'] for k in KEYS))
    def test_release_refuses_unconfirmed_journal_and_reconcile_does_not_claim_business(self):
        api=KubeHTTP();payload=api.payload()
        with tempfile.TemporaryDirectory() as tmp:
            deployer=Deployer(api.registry,api.transport(),tmp);deployer.deploy(payload,3)
            with self.assertRaisesRegex(ValueError,'ownership_release_unconfirmed'):deployer.release_confirmed(payload)
            api.business=lambda env,service,path:{'status':500,'body':{}}
            deployer.transport.business_reader=api.business
            result=deployer.reconcile(payload,7)
            self.assertTrue(result['ok']);self.assertEqual(result['verification'],[])
            self.assertEqual(len(api.patches),2)
            api.doc['spec']['template']['spec']['containers'][0]['image']=api.old
            with self.assertRaisesRegex(ValueError,'cas_conflict'):deployer.release_confirmed(payload)
            self.assertEqual(len(api.patches),2)

    def test_failed_business_or_foreign_marker_never_unlocks_target(self):
        api=KubeHTTP();payload=api.payload()
        with tempfile.TemporaryDirectory() as tmp:
            deployer=Deployer(api.registry,api.transport(),tmp);deployer.deploy(payload,3)
            api.business=lambda *a:{'status':200,'body':{'products':[]}}
            deployer.transport.business_reader=api.business
            with self.assertRaises(ValueError):deployer.verify(payload)
            self.assertEqual(len(api.patches),2)
            deployer.transport.business_reader=lambda *a:{'status':200,'body':{'products':[{'price':79000}]}}
            api.doc['metadata']['annotations'][FENCE_ANNOTATION]='4'
            with self.assertRaises(ValueError):deployer.verify(payload)
            self.assertEqual(len(api.patches),2)

if __name__=='__main__':unittest.main()
