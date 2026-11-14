"""P18 final pre-delete CAS regressions; synthetic clients only, no cluster/DB/model."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('restore_cas_runner',ROOT/'scripts/p18-campaign-run.py')
assert spec and spec.loader
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
UID='11111111-1111-4111-8111-111111111111'


def dep(name='payment'):
    return {'metadata':{'name':name,'uid':name+'-uid','resourceVersion':'1'},'spec':{'replicas':1,'template':{'metadata':{},'spec':{'containers':[{'name':name,'image':'baseline'}]}}}}


def pod():
    return {'apiVersion':'v1','kind':'Pod','metadata':{'namespace':'dev','name':'payment-pod','uid':UID,'resourceVersion':'10',
            'labels':{'app':'payment'},'annotations':{'safe':'original'},'finalizers':[],
            'ownerReferences':[{'apiVersion':'apps/v1','kind':'ReplicaSet','controller':True,'name':'payment-rs','uid':'payment-rsuid'}]},
            'spec':{'containers':[{'name':'payment','image':'baseline','resources':{'limits':{'memory':'256Mi'}},'env':[{'name':'MODE','value':'baseline'}]}]},
            'status':{'phase':'Running','conditions':[{'type':'Ready','status':'False'}]}}


class APIError(Exception):
    def __init__(self,message='',status:object=409,reason='Conflict',details=None):
        self.status=status;self.reason=reason
        self.body=json.dumps({'kind':'Status','apiVersion':'v1','reason':'Conflict','code':409,'message':message,'details':details or {}})
        self.headers={'Authorization':'Bearer '+'fixture-'+'credential-'+str(937)}
    def __str__(self):return 'HTTP response headers: '+str(self.headers)+'; padding='+'x'*700+'; HTTP response body: '+self.body


class RestoreCAS(unittest.TestCase):
    def setUp(self):
        self.listed=pod();self.fresh=copy.deepcopy(self.listed);self.events=[];self.deletes=[];self.checked=[]
        h=r.Harness.__new__(r.Harness);self.h=h;h.ns='dev';h.args=NS(restore_timeout=0)
        h._case_service='payment';h._changed_services={'payment'};h.serialize=lambda x:copy.deepcopy(x)
        h.dep=lambda n:dep(n);h.pods=lambda n:self.events.append('list') or [copy.deepcopy(self.listed)]
        def rs(*a,**k):
            self.events.append('owner')
            return {'metadata':{'name':'payment-rs','uid':'payment-rsuid','ownerReferences':[{'kind':'Deployment','controller':True,'uid':'payment-uid'}]}}
        def read(name,namespace,**k):self.events.append('fresh');return copy.deepcopy(self.fresh)
        def delete(name,namespace,body,**k):self.events.append('delete');self.deletes.append((name,namespace,body))
        h.apps=NS(read_namespaced_replica_set=rs);h.core=NS(read_namespaced_pod=read,delete_namespaced_pod=delete)
        h.client=NS(V1DeleteOptions=lambda **k:k,V1Preconditions=lambda **k:k)
        h.chaos=lambda *a,**k:{'status':200,'body':{'active':{},'release_active':{}}}
        h.wait_ready=lambda n,**k:self.checked.append(n) or True;h.business_health=lambda n:{'verified':True}

    def restore(self):return self.h.restore({'payment':dep()})

    def test_status_only_rv_change_uses_fresh_cas_once_after_owner_check(self):
        self.fresh['metadata']['resourceVersion']='11';self.fresh['metadata']['managedFields']=[{'manager':'kubelet','time':'later'}]
        self.fresh['status']['conditions'][0]['status']='True'
        out=self.restore()
        self.assertTrue(out['verified']);self.assertEqual(self.events,['list','owner','fresh','delete'])
        self.assertEqual(self.deletes,[('payment-pod','dev',{'preconditions':{'uid':UID,'resource_version':'11'}})])
        self.assertEqual(self.listed['metadata']['resourceVersion'],'10')


    def test_non_status_drift_never_deletes(self):
        paths=[('metadata','uid'),('metadata','namespace'),('metadata','name'),('metadata','ownerReferences',0,'uid'),
               ('metadata','labels','app'),('metadata','annotations','safe'),('metadata','finalizers'),
               ('metadata','deletionTimestamp'),('metadata','deletionGracePeriodSeconds'),
               ('spec','containers',0,'image'),('spec','containers',0,'resources','limits','memory'),
               ('spec','containers',0,'env',0,'value'),('kind',),('apiVersion',)]
        for path in paths:
            with self.subTest(path=path):
                self.fresh=copy.deepcopy(self.listed);obj=self.fresh
                for key in path[:-1]:obj=obj[key]
                obj[path[-1]]=['foreign'] if path[-1]=='finalizers' else (0 if path[-1]=='deletionGracePeriodSeconds' else 'foreign')
                self.deletes.clear();out=self.restore()
                self.assertFalse(out['verified']);self.assertEqual(self.deletes,[])


    def test_listed_terminating_pod_is_not_silently_skipped_as_restored(self):
        self.listed['metadata']['deletionTimestamp']='2026-10-26T00:00:00Z';self.fresh=copy.deepcopy(self.listed)
        out=self.restore();self.assertFalse(out['verified']);self.assertEqual(self.deletes,[])


    def test_success_records_only_bound_delete_request_metadata(self):
        self.fresh['metadata']['resourceVersion']='11';out=self.restore();row=out['services'][0]
        self.assertTrue(out['verified']);self.assertIn('delete_attempts',row);attempt=row['delete_attempts'][0]
        self.assertEqual({k:attempt[k] for k in ('namespace','name','uid','resourceVersion','operation')},
                         {'namespace':'dev','name':'payment-pod','uid':UID,'resourceVersion':'11','operation':'delete_pod'})
        self.assertTrue(attempt['succeeded']);self.assertIn('at',attempt);self.assertIn('code',attempt)
        self.assertNotIn('spec',json.dumps(row));self.assertNotIn('env',json.dumps(row))


    def test_409_is_sticky_one_delete_with_safe_structured_failure(self):
        error=APIError('unclassified message')
        def fail(*a,**k):self.events.append('delete');self.deletes.append(k['body']);raise error
        self.h.core.delete_namespaced_pod=fail
        out=self.h.restore({n:dep(n) for n in r.SERVICES});row=out['services'][0]
        self.assertFalse(out['verified']);self.assertEqual(len(self.deletes),1);self.assertEqual(self.events.count('fresh'),1)
        self.assertEqual(set(self.checked),set(r.SERVICES)-{'payment'})
        self.assertTrue(error.headers['Authorization'] not in json.dumps(out),'unsafe exception text stored')
        self.assertIn('diagnostic',row);self.assertEqual(row['diagnostic']['code'],409)
        self.assertEqual(row['diagnostic']['reason'],'Conflict');self.assertEqual(row['diagnostic']['cause'],'unconfirmed')
        self.assertEqual(row['delete_attempts'][0]['code'],409);self.assertFalse(row['delete_attempts'][0]['succeeded'])
        result={'drain':{'verified':True},'restoration':out,'state':'completed'}
        self.assertEqual(r.after_case(result),'stop');self.assertEqual(r.resume_action(result),'block')


    def test_standard_cas_message_survives_long_headers_as_validated_fields(self):
        for field,requested,observed in (('UID',UID,'22222222-2222-4222-8222-222222222222'),('ResourceVersion','10','12')):
            with self.subTest(field=field):
                message=(f'Operation cannot be fulfilled on pods "payment-pod": StorageError: invalid object, Code: 4, '
                         f'Key: /registry/pods/dev/payment-pod, ResourceVersion: 0, AdditionalErrorMsg: Precondition failed: '
                         f'{field} in precondition: {requested}, {field} in object meta: {observed}')
                error=APIError(message,details={'name':'payment-pod','kind':'pods','group':'','uid':UID})
                def fail(*a,**k):raise error
                self.h.core.delete_namespaced_pod=fail;row=self.restore()['services'][0]
                self.assertIn('diagnostic',row);diagnostic=row['diagnostic']
                self.assertEqual(diagnostic['cause'],'precondition_mismatch')
                self.assertEqual(diagnostic['precondition'],{'field':field,'requested':requested,'observed':observed})
                self.assertEqual(diagnostic['details'],{'name':'payment-pod','kind':'pods','group':'','uid':UID})
                self.assertEqual(diagnostic['message_length'],len(message));self.assertEqual(diagnostic['message_sha256'],hashlib.sha256(message.encode()).hexdigest())
                self.assertTrue(error.headers['Authorization'] not in json.dumps(row),'unsafe headers stored')
                self.assertNotIn('message',diagnostic)


    def test_observed_server_pod_cas_messages_keep_validated_precondition_fields(self):
        target={'namespace':'dev','name':'payment-pod','uid':UID,'resourceVersion':'10'}
        for field,requested,observed,suffix in (
            ('ResourceVersion','10','12','modified'),
            ('UID',UID,'22222222-2222-4222-8222-222222222222','deleted and then recreated')):
            with self.subTest(field=field):
                message=(f'Operation cannot be fulfilled on Pod "payment-pod": the {field} in the precondition '
                         f'({requested}) does not match the {field} in record ({observed}). '
                         f'The object might have been {suffix}')
                error=APIError(message,details={'name':'payment-pod','kind':'Pod'})
                diagnostic=r.restore_diagnostic(error,target)
                self.assertEqual(diagnostic['cause'],'precondition_mismatch')
                self.assertEqual(diagnostic['precondition'],{'field':field,'requested':requested,'observed':observed})
                self.assertNotIn('message',diagnostic)
                error.body=json.dumps({**json.loads(error.body),'message':message+' opaque-private-value'})
                self.assertEqual(r.restore_diagnostic(error,target)['cause'],'unconfirmed')
                error.body=json.dumps({**json.loads(error.body),'message':message.replace(requested,'9',1)})
                self.assertEqual(r.restore_diagnostic(error,target)['cause'],'unconfirmed')

    def test_final_read_failure_never_deletes_and_identifies_preflight(self):
        for error in (APIError('not found',status=404,reason='NotFound'),RuntimeError('read unavailable')):
            with self.subTest(error_type=type(error).__name__):
                def fail(*a,**k):self.events.append('fresh');raise error
                self.h.core.read_namespaced_pod=fail;out=self.restore();row=out['services'][0]
                self.assertFalse(out['verified']);self.assertEqual(self.deletes,[]);self.assertEqual(row['delete_attempts'],[])
                self.assertEqual(row['diagnostic']['operation'],'read_pod')


    def test_real_list_get_type_defaults_only_are_normalized(self):
        self.listed.pop('kind');self.listed.pop('apiVersion');self.fresh['metadata']['resourceVersion']='11'
        self.assertTrue(self.restore()['verified']);self.assertEqual(len(self.deletes),1)
        for field in ('kind','apiVersion'):
            for value in (None,'wrong'):
                with self.subTest(field=field,value=value):
                    self.listed[field]=value;self.deletes.clear()
                    self.assertFalse(self.restore()['verified']);self.assertEqual(self.deletes,[])
                    self.listed.pop(field)

    def test_extra_metadata_spec_and_top_level_changes_fail_closed(self):
        for section,key,value in (('metadata','creationTimestamp','later'),('metadata','generation',2),
                                  ('spec','nodeName','other-node'),(None,'unexpected','new')):
            with self.subTest(section=section,key=key):
                self.fresh=copy.deepcopy(self.listed);(self.fresh[section] if section else self.fresh)[key]=value
                self.deletes.clear();self.assertFalse(self.restore()['verified']);self.assertEqual(self.deletes,[])

    def test_invalid_fresh_identity_or_spec_never_deletes(self):
        for section,key in (('metadata','name'),('metadata','namespace'),('metadata','uid'),('metadata','resourceVersion'),(None,'spec')):
            with self.subTest(section=section,key=key):
                self.fresh=copy.deepcopy(self.listed);(self.fresh[section] if section else self.fresh).pop(key)
                self.deletes.clear();self.assertFalse(self.restore()['verified']);self.assertEqual(self.deletes,[])

    def test_foreign_replicaset_chain_stops_before_final_read(self):
        self.h.apps.read_namespaced_replica_set=lambda *a,**k:{'metadata':{'name':'payment-rs','uid':'payment-rsuid',
            'ownerReferences':[{'kind':'Deployment','controller':True,'uid':'foreign-uid'}]}}
        self.assertFalse(self.restore()['verified']);self.assertEqual(self.deletes,[]);self.assertNotIn('fresh',self.events)

    def test_arbitrary_status_text_and_details_are_fingerprinted_not_stored(self):
        sentinel='opaque-'+'sensitive-'+str(18273645)
        text=f'Secret token={sentinel} https://user:{sentinel}@example.invalid/ Authorization: Bearer {sentinel}'
        error=APIError(text,status=text,reason=text,details={'name':text,'kind':'Secret','group':text,'uid':text,
                       'causes':[{'reason':text,'field':text,'message':text}]})
        body=json.loads(error.body);body.update(reason=text,code=text);error.body=json.dumps(body)
        def fail(*a,**k):raise error
        self.h.core.delete_namespaced_pod=fail;row=self.restore()['services'][0];diagnostic=row['diagnostic']
        self.assertTrue(sentinel not in json.dumps(row),'unsafe status text stored')
        self.assertEqual(diagnostic['cause'],'unconfirmed');self.assertIsNone(diagnostic['code'])
        self.assertNotIn('reason',diagnostic);self.assertNotIn('details',diagnostic)
        self.assertEqual(diagnostic['message_length'],len(text));self.assertEqual(diagnostic['message_sha256'],hashlib.sha256(text.encode()).hexdigest())

    def test_standard_looking_but_unbound_or_malformed_messages_are_unconfirmed(self):
        base=f'Operation cannot be fulfilled on pods "payment-pod": Precondition failed: ResourceVersion in precondition: 10, ResourceVersion in object meta: 12'
        messages=[base+' token='+'fixture-'+'sensitive',base.replace('payment-pod','foreign-pod'),base.replace('precondition: 10','precondition: 9'),
                  base.replace('object meta: 12','object meta: 10'),base.replace('object meta: 12','object meta: secret')]
        for message in messages:
            with self.subTest(index=messages.index(message)):
                error=APIError(message)
                def fail(*a,**k):raise error
                self.h.core.delete_namespaced_pod=fail;diagnostic=self.restore()['services'][0]['diagnostic']
                self.assertEqual(diagnostic['cause'],'unconfirmed');self.assertNotIn('precondition',diagnostic)

    def test_diagnostic_failure_cannot_turn_delete_failure_into_success(self):
        def fail(*a,**k):self.deletes.append(k['body']);raise APIError()
        self.h.core.delete_namespaced_pod=fail
        with patch.object(r,'restore_diagnostic',side_effect=RuntimeError('diagnostic unavailable')):
            out=self.h.restore({n:dep(n) for n in r.SERVICES})
        self.assertFalse(out['verified']);self.assertEqual(len(self.deletes),1)
        self.assertEqual(set(self.checked),set(r.SERVICES)-{'payment'})
        self.assertTrue(out['services'][0]['diagnostic']['diagnostic_unavailable'])

    def test_diagnostic_does_not_call_untrusted_exception_stringifier(self):
        class Unprintable(APIError):
            def __str__(self):raise AssertionError('must not stringify untrusted exception')
        def fail(*a,**k):raise Unprintable('unknown')
        self.h.core.delete_namespaced_pod=fail
        out=self.restore();self.assertFalse(out['verified']);self.assertEqual(out['services'][0]['diagnostic']['code'],409)

    def test_synthetic_sdk_serializes_fresh_preconditions_to_wire_names(self):
        class Preconditions:
            def __init__(self,uid,resource_version):self.uid=uid;self.resource_version=resource_version
        class DeleteOptions:
            def __init__(self,preconditions):self.preconditions=preconditions
        wire=[]
        def delete(name,namespace,body,**kwargs):
            wire.append(json.loads(json.dumps({'name':name,'namespace':namespace,'body':{'preconditions':{
                'uid':body.preconditions.uid,'resourceVersion':body.preconditions.resource_version}},'timeout':kwargs['_request_timeout']})))
        self.h.client=NS(V1DeleteOptions=DeleteOptions,V1Preconditions=Preconditions);self.h.core.delete_namespaced_pod=delete
        self.fresh['metadata']['resourceVersion']='99';self.assertTrue(self.restore()['verified'])
        self.assertEqual(wire,[{'name':'payment-pod','namespace':'dev','body':{'preconditions':{'uid':UID,'resourceVersion':'99'}},'timeout':15}])


    def test_terminal_phase_is_not_status_only_refreshable(self):
        for phase in ('Succeeded','Failed'):
            with self.subTest(phase=phase):
                self.fresh['status']['phase']=phase
                self.assertFalse(self.restore()['verified']);self.assertEqual(self.deletes,[])


    def test_status_causes_keep_only_allowlisted_fields_and_message_fingerprints(self):
        message='resourceVersion conflict '+'sensitive-'+str(273645)
        error=APIError('unknown',details={'name':'payment-pod','kind':'pods','causes':[
            {'reason':'FieldValueInvalid','field':'metadata.resourceVersion','message':message},
            {'reason':message,'field':'spec.containers[0].env','message':message}]})
        def fail(*a,**k):raise error
        self.h.core.delete_namespaced_pod=fail;row=self.restore()['services'][0]
        self.assertIn('causes',row['diagnostic'].get('details',{}))
        self.assertEqual(row['diagnostic']['details']['causes'],[{'reason':'FieldValueInvalid','field':'metadata.resourceVersion',
            'message_sha256':hashlib.sha256(message.encode()).hexdigest(),'message_length':len(message)}])
        self.assertTrue(message not in json.dumps(row),'unsafe cause message stored')
        self.assertEqual(row['diagnostic']['cause'],'unconfirmed')


    def test_owner_read_status_race_is_reproduced_without_relaxing_server_cas(self):
        owner_read=self.h.apps.read_namespaced_replica_set
        def owner(*a,**k):
            result=owner_read(*a,**k);self.fresh['metadata']['resourceVersion']='11'
            self.fresh['status']['conditions'][0]['status']='True';return result
        self.h.apps.read_namespaced_replica_set=owner
        def delete(name,namespace,body,**k):
            self.deletes.append(body)
            if body['preconditions']['resource_version']!=self.fresh['metadata']['resourceVersion']:raise APIError('stale snapshot')
        self.h.core.delete_namespaced_pod=delete
        self.assertTrue(self.restore()['verified']);self.assertEqual(len(self.deletes),1)
        self.assertEqual(self.deletes[0]['preconditions'],{'uid':UID,'resource_version':'11'})

    def test_prior_success_then_409_does_not_rebase_or_erase_failure(self):
        second=copy.deepcopy(self.listed);second['metadata'].update(name='payment-second',uid='33333333-3333-4333-8333-333333333333')
        third=copy.deepcopy(self.listed);third['metadata']['name']='payment-third';pods=[self.listed,second,third]
        self.h.pods=lambda n:[copy.deepcopy(p) for p in pods]
        reads=[]
        def read(name,*a,**k):reads.append(name);return copy.deepcopy(next(p for p in pods if p['metadata']['name']==name))
        def delete(name,*a,**k):
            self.deletes.append(name)
            if name=='payment-second':raise APIError('conflict after first successful Pod')
        self.h.core.read_namespaced_pod=read;self.h.core.delete_namespaced_pod=delete
        out=self.h.restore({n:dep(n) for n in r.SERVICES});row=out['services'][0]
        self.assertFalse(out['verified']);self.assertEqual(self.deletes,['payment-pod','payment-second'])
        self.assertEqual(reads,['payment-pod','payment-second']);self.assertEqual([a['succeeded'] for a in row['delete_attempts']],[True,False])
        self.assertEqual(row['delete_attempts'][1]['code'],409);self.assertEqual(set(self.checked),set(r.SERVICES)-{'payment'})

    def test_malformed_error_body_remains_unknown_without_raw_fallback(self):
        for body in ('not-json','[]','null','{}','x'*131073):
            with self.subTest(body_length=len(body)):
                error=APIError();error.body=body
                def fail(*a,**k):raise error
                self.h.core.delete_namespaced_pod=fail;row=self.restore()['services'][0]
                self.assertEqual(row['diagnostic']['cause'],'unconfirmed')
                self.assertTrue(error.headers['Authorization'] not in json.dumps(row),'unsafe raw fallback')
                self.assertFalse(row['verified'])


    def test_success_code_is_observed_or_unknown_never_invented(self):
        for response,code in (({'code':200},200),(NS(code=202),202),({'code':'unsafe'},None),({},None)):
            with self.subTest(code=code,response_type=type(response).__name__):
                self.h.core.delete_namespaced_pod=lambda *a,**k:response
                row=self.restore()['services'][0]
                self.assertTrue(row['verified']);self.assertEqual(row['delete_attempts'][0]['code'],code)


    def test_explicit_non_success_delete_status_cannot_become_verified(self):
        self.h.core.delete_namespaced_pod=lambda *a,**k:{'code':409,'reason':'Conflict','message':'unclassified response'}
        out=self.h.restore({n:dep(n) for n in r.SERVICES});row=out['services'][0]
        self.assertFalse(out['verified']);self.assertEqual(len(row['delete_attempts']),1)
        self.assertEqual(row['delete_attempts'][0]['code'],409);self.assertFalse(row['delete_attempts'][0]['succeeded'])
        self.assertEqual(set(self.checked),set(r.SERVICES)-{'payment'})


if __name__=='__main__':unittest.main(verbosity=2)
