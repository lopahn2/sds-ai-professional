"""UID/RV/이미지/template CAS와 결과불명 재조회. 자동 원복/재쓰기는 없다."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
from .profiles import digest, manifest_image

from liveprobe.app.runtime_fencing import OWNER_ANNOTATION, LEASE_ANNOTATION, FENCE_ANNOTATION
MARKERS=(OWNER_ANNOTATION, LEASE_ANNOTATION, FENCE_ANNOTATION)


class Conflict(ValueError):
    status=409


def pointer(key):
    return '/metadata/annotations/'+key.replace('~','~0').replace('/','~1')


def marker_values(payload,fence):
    return dict(zip(MARKERS,('liveprobe-application',
                f"application:{payload['release_id']}:{payload['attempt_id']}",str(fence))))


def identity_tests(doc):
    return [{'op':'test','path':'/metadata/uid','value':doc['metadata']['uid']},
            {'op':'test','path':'/metadata/resourceVersion','value':doc['metadata']['resourceVersion']}]


def owned_tests(values):
    return [{'op':'test','path':pointer(key),'value':value} for key,value in values.items()]


def acquire_patch(doc,values):
    metadata=doc['metadata'];annotations=metadata.get('annotations') or {}
    if any(key in annotations for key in MARKERS):raise Conflict('target_owned')
    ops=identity_tests(doc)
    # RFC6902 has no absent-value test. Testing the complete map (or parent
    # metadata when annotations is absent) atomically proves all three absent.
    if isinstance(metadata.get('annotations'),dict):
        ops.append({'op':'test','path':'/metadata/annotations','value':annotations})
        ops.extend({'op':'add','path':pointer(k),'value':v} for k,v in values.items())
    else:
        ops.extend([{'op':'test','path':'/metadata','value':metadata},
                    {'op':'add','path':'/metadata/annotations','value':values}])
    return ops


def summarize(doc,environment,service,registry):
    p=registry.target({'environment':environment,'service':service})
    containers=doc['spec']['template']['spec']['containers']
    selected=[c for c in containers if c['name']==service]
    if len(selected)!=1:
        raise ValueError('container_not_registered')
    image=manifest_image(selected[0]['image'],p['image_repository'])
    return {'environment':environment,'service':service,'system':p['system'],'uid':doc['metadata']['uid'],
            'resource_version':doc['metadata']['resourceVersion'],'container':service,'image':image,
            'template_sha256':digest(doc['spec']['template'])}


def validate_business(profile,response):
    if response.get('status')!=200 or not isinstance(response.get('body'),dict):
        raise ValueError('business_http_failed')
    b=response['body']; kind=profile['verification']['kind']
    valid=False
    if kind=='payment':
        rows=b.get('payments')
        valid=isinstance(rows,list) and bool(rows) and all(type(r.get('amount')) is int and r['amount']>0 and r.get('charged_amount')==r['amount'] for r in rows)
    elif kind=='catalog':
        rows=b.get('products')
        valid=isinstance(rows,list) and bool(rows) and all(type(r.get('price')) is int and r['price']>0 for r in rows)
    elif kind=='notification':
        rows=b.get('notifications')
        valid=isinstance(rows,list) and bool(rows) and type(b.get('queue_depth')) is int and b['queue_depth']>=0 and all(r.get('status') in ('queued','sent','failed','pending') for r in rows)
    elif kind=='delivery':
        rows=b.get('deliveries')
        steps=b.get('steps',[])
        valid=isinstance(rows,list) and bool(rows) and len(steps)==4 and all(r.get('status') in steps and r.get('events') and r['events'][-1].get('status')==r['status'] for r in rows)
    elif kind=='stock':
        valid=b.get('item')=='a' and type(b.get('quantity')) is int and b['quantity']>=0 and b.get('available')==(b['quantity']>0)
    elif kind=='order':
        inv=b.get('inventory',{})
        valid=b.get('item')=='a' and type(b.get('orderable')) is bool and inv.get('item')=='a' and b['orderable']==inv.get('available') and type(inv.get('quantity')) is int and inv['quantity']>=0
    elif kind=='domain':
        sync=b.get('sync',{})
        expected_state='source' if profile.get('mode')=='business' else 'fresh'
        valid=b.get('db_ready') is True and type(b.get('read_only')) is bool and sync.get('state')==expected_state
        if expected_state=='source':valid=valid and type(sync.get('pending_outbox')) is int and sync['pending_outbox']==0
        else:valid=valid and type(sync.get('unresolved_quarantine')) is int and sync['unresolved_quarantine']==0 and sync.get('dependency_error') is False and sync.get('storage_rebuild_required') is False and sync.get('observed_offset_lag')==0
    if not valid:
        raise ValueError('business_invariant_failed')
    return {'ok':True,'body_sha256':digest(b),'profile_id':profile['id'],'status_code':response['status']}


class KubeTransport:
    def __init__(self,api,business_reader=None):
        self.api=api;self.business_reader=business_reader
        self.guard=lambda:None

    def _call(self,path,method,body=None,query=None):
        remaining=getattr(self,'write_guard',self.guard)() if method=='PATCH' else self.guard()
        timeout=min(20,remaining) if isinstance(remaining,(int,float)) else 20
        try:
            result=self.api.call_api(path,method,body=body,query_params=query or [],
                header_params={'Accept':'application/json','Content-Type':'application/json-patch+json' if method=='PATCH' else 'application/json'},
                auth_settings=['BearerToken'],response_types_map={200:'object',201:'object'},
                _return_http_data_only=True,_request_timeout=timeout)
            self.guard()
            return result
        except Exception as exc:
            if getattr(exc,'status',None)==409 or (method=='PATCH' and getattr(exc,'status',None)==422):
                raise Conflict('cas_conflict') from None
            raise OSError('kubernetes_transport_failed') from None

    def http_get(self,path):
        try:
            body,status,_=self.api.call_api(path,'GET',header_params={'Accept':'application/json'},
                auth_settings=['BearerToken'],response_types_map={200:'object'},_return_http_data_only=False,_request_timeout=20)
            return {'status':status,'body':body}
        except Exception as exc:
            status=getattr(exc,'status',None)
            if type(status) is int and 400<=status<=599:return {'status':status,'body':{}}
            raise OSError('business_transport_failed') from None

    def read(self,env,service):
        return self._call(f'/apis/apps/v1/namespaces/{env}/deployments/{service}','GET')

    def patch(self,env,service,ops):
        return self._call(f'/apis/apps/v1/namespaces/{env}/deployments/{service}','PATCH',ops)

    def pods(self,env,service):
        deployment=self.read(env,service)
        uid=deployment['metadata']['uid']
        sets=self._call(f'/apis/apps/v1/namespaces/{env}/replicasets','GET',query=[('labelSelector','app='+service)])
        owned={r['metadata']['uid'] for r in sets.get('items',[]) if any(o.get('uid')==uid for o in r['metadata'].get('ownerReferences',[]))}
        data=self._call(f'/api/v1/namespaces/{env}/pods','GET',query=[('labelSelector','app='+service)])
        result=[]
        for pod in data.get('items',[]):
            if pod['metadata'].get('deletionTimestamp'):
                continue
            is_owned=any(o.get('uid') in owned for o in pod['metadata'].get('ownerReferences',[]))
            for c in pod.get('status',{}).get('containerStatuses',[]):
                if c['name']==service:
                    result.append({'image':c.get('image'),'image_id':c.get('imageID','').removeprefix('docker-pullable://'),
                                   'ready':c.get('ready') is True,'owned':is_owned})
        return result

    def logs(self,env,service,since):
        data=self._call(f'/api/v1/namespaces/{env}/pods','GET',query=[('labelSelector','app='+service)])
        result=[]
        # 로그 원문은 아래 호출 직후 정제하고 원문/예외를 저장하지 않는다.
        from .worker import redact
        for pod in data.get('items',[])[:4]:
            name=pod['metadata']['name']
            response=self.api.call_api(f'/api/v1/namespaces/{env}/pods/{name}/log','GET',
                query_params=[('container',service),('sinceTime',since),('timestamps',True),('tailLines',200),('limitBytes',32768)],
                response_types_map={200:'str'},auth_settings=['BearerToken'],_return_http_data_only=True,
                _preload_content=False,_request_timeout=20)
            # The generated client's 'str' conversion uses str(bytes), escaping every line break.
            if hasattr(response, 'read'):
                try:
                    raw = response.read(32768)
                    text = raw.decode('utf-8', errors='replace')
                finally:
                    response.close()
                    response.release_conn()
            else:
                text = response.decode('utf-8', errors='replace') if isinstance(response, bytes) else response
            if text and text.strip():
                from .observations import utc_now
                result.append({'service':service,'text':redact(text),'observed_at':utc_now()})
        return result

    def events(self,env,service,uid):
        from .observations import utc_now
        from .worker import redact
        rows=self._call(f'/api/v1/namespaces/{env}/events','GET',query=[('fieldSelector','involvedObject.uid='+uid)])
        result=[]
        for event in rows.get('items',[]):
            if event.get('involvedObject',{}).get('uid')!=uid:continue
            safe={k:event[k] for k in ('type','reason','message','count','firstTimestamp','lastTimestamp','eventTime') if k in event}
            if safe:result.append({'text':redact(json.dumps(safe,ensure_ascii=False)),'observed_at':utc_now()})
        return result

    def business(self,env,service,path):
        if not self.business_reader:
            raise ValueError('business_reader_not_provisioned')
        return self.business_reader(env,service,path)


class Deployer:
    def __init__(self,registry,transport,state_root):
        self.registry=registry;self.transport=transport;self.state_root=Path(state_root)
        self.state_root.mkdir(mode=0o700,parents=True,exist_ok=True)

    def validate(self,payload,fence):
        stage=payload['stage']
        if stage not in ('dev','prd') or type(fence) is not int or fence<1:
            raise ValueError('invalid_deployment')
        expected='user_prd' if stage=='prd' else 'auto_dev'
        if payload['authorization_kind']!=expected:
            raise ValueError('approval_required')
        for key in ('release_id','attempt_id','idempotency_key'):
            if not isinstance(payload.get(key),str) or not payload[key] or len(payload[key])>200:
                raise ValueError('deployment_identifier_required')
        targets=payload['targets']
        if not targets or len({t['service'] for t in targets})!=len(targets):
            raise ValueError('duplicate_or_empty_targets')
        affected=set()
        for t in targets:
            p=self.registry.target(t)
            if t['environment']!=stage or t['container']!=t['service']:
                raise ValueError('deployment_target_mismatch')
            manifest_image(t['previous_image'],p['image_repository'])
            manifest_image(payload['artifact']['image'],p['image_repository'])
            affected.update(p['affected_services'])
        if {t['service'] for t in targets}!=affected or set(payload['artifact'].get('affected_services',affected))!=affected:
            raise ValueError('affected_services_mismatch')

    @contextmanager
    def lock(self,key):
        path=self.state_root/(digest(key)+'.lock')
        fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'a+') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX)
            yield

    def state_path(self,payload):
        return self.state_root/(digest(payload['idempotency_key'])+'.json')

    def store(self,path,value):
        tmp=path.with_suffix('.new')
        fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as f:
            json.dump(value,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
        fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)

    def deploy(self,payload,fence):
        self.validate(payload,fence)
        with self.lock(payload['idempotency_key']):
            path=self.state_path(payload)
            if path.exists():
                saved=json.loads(path.read_text())
                if saved['payload_sha256']!=digest(payload):raise Conflict('idempotency_payload_conflict')
                return saved['result']  # never retry an uncertain external write
            values=marker_values(payload,fence)
            state={'payload_sha256':digest(payload),'markers':values,
                   'result':{'ok':False,'write_outcome':'not_written','targets':[],'verification':[],'error':None}}
            self.store(path,state)
            for target in payload['targets']:
                env,service=target['environment'],target['service']
                acquired_here=False
                try:
                    doc=self.transport.read(env,service)
                    current=summarize(doc,env,service,self.registry)
                    for key in ('uid','resource_version','container','template_sha256'):
                        if current[key]!=target[key]:raise Conflict('cas_conflict')
                    if current['image']!=target['previous_image']:raise Conflict('cas_conflict')
                    ops=acquire_patch(doc,values)
                    state['result']['write_outcome']='unknown';self.store(path,state)
                    self.transport.patch(env,service,ops)
                    acquired_here=True
                    # A rejected CAS made no workload write. Re-read ownership, template
                    # and old image before retrying a resourceVersion race with status updates.
                    for cas_attempt in range(3):
                        acquired=self.transport.read(env,service)
                        if acquired['metadata']['uid']!=target['uid']:raise Conflict('cas_conflict')
                        if any(acquired['metadata'].get('annotations',{}).get(k)!=v for k,v in values.items()):
                            raise Conflict('target_owned')
                        if summarize(acquired,env,service,self.registry)['template_sha256']!=target['template_sha256']:
                            raise Conflict('cas_conflict')
                        containers=acquired['spec']['template']['spec']['containers']
                        index=next(i for i,c in enumerate(containers) if c['name']==target.get('container',target['service']))
                        image_path=f'/spec/template/spec/containers/{index}/image'
                        ops=identity_tests(acquired)+owned_tests(values)+[
                            {'op':'test','path':f'/spec/template/spec/containers/{index}/name','value':target['container']},
                            {'op':'test','path':'/spec/template','value':acquired['spec']['template']},
                            {'op':'test','path':image_path,'value':target['previous_image']},
                            {'op':'replace','path':image_path,'value':payload['artifact']['image']}]
                        try:
                            self.transport.patch(env,service,ops)
                            break
                        except Conflict:
                            if cas_attempt == 2:raise
                    observed=self.transport.read(env,service)
                    row=summarize(observed,env,service,self.registry)
                    if row['uid']!=target['uid'] or row['image']!=payload['artifact']['image']:
                        raise OSError('write_readback_unknown')
                    row={k:row[k] for k in ('environment','service','uid','resource_version','image')}
                    row.update(previous_image=target['previous_image'],write_outcome='written')
                    state['result']['targets'].append(row)
                    state['result']['write_outcome']='written';self.store(path,state)
                except Conflict as exc:
                    state['result']['write_outcome']='unknown' if acquired_here else ('written' if state['result']['targets'] else 'not_written')
                    state['result']['error']=str(exc);self.store(path,state)
                    return state['result']
                except Exception:
                    state['result']['error']='write_result_unknown' if state['result']['write_outcome']=='unknown' else 'target_read_failed'
                    self.store(path,state);return state['result']
            state['result'].update(ok=True,error=None)
            self.store(path,state)
            return state['result']

    def observe_verification(self,payload,business=True):
        result=[]
        for target in payload['targets']:
            env=target['environment'];service=target['service'];p=self.registry.target(target)
            doc=self.transport.read(env,service)
            observed=summarize(doc,env,service,self.registry)
            if observed['uid']!=target['uid'] or observed['image']!=payload['artifact']['image']:
                raise ValueError('image_readback_mismatch')
            status=doc.get('status',{});replicas=doc['spec'].get('replicas',1)
            generation=doc['metadata'].get('generation',doc.get('generation',0))
            if replicas<1 or status.get('observedGeneration',0)<generation or any(status.get(k,0)!=replicas for k in ('updatedReplicas','readyReplicas','availableReplicas')):
                raise ValueError('rollout_not_observed')
            pods=self.transport.pods(env,service)
            if len(pods)!=replicas or not all(x['owned'] and x['ready'] and x['image_id']==payload['artifact']['image'] for x in pods):
                raise ValueError('pod_manifest_not_observed')
            if business:
                checked=validate_business(p,self.transport.business(env,service,p['verification']['path']))
                result.append({'environment':env,'service':service,'image':observed['image'],'business':checked})
        return result

    def verify(self,payload):
        # Business and immutable-image observations must all succeed before any
        # ownership release. Release is metadata-only, never a second deployment.
        result=self.observe_verification(payload)
        with self.lock(payload['idempotency_key']):
            path=self.state_path(payload)
            if not path.is_file():raise ValueError('write_journal_missing')
            state=json.loads(path.read_text())
            state['result']['verification']=result
            self.store(path,state)
        self.release_confirmed(payload)
        return result

    def release_confirmed(self,payload):
        # A distinct metadata-only finalization after verification/reconciliation.
        # reconcile itself remains strictly read-only at the Kubernetes boundary.
        with self.lock(payload['idempotency_key']):
            path=self.state_path(payload)
            if not path.is_file():raise ValueError('write_journal_missing')
            state=json.loads(path.read_text());values=state['markers']
            if state['payload_sha256']!=digest(payload):raise Conflict('idempotency_payload_conflict')
            metadata_only=state.get('metadata_only_confirmed') is True
            if not metadata_only and not state.get('reconciled') and not state['result'].get('verification'):
                raise ValueError('ownership_release_unconfirmed')
            expected=marker_values(payload,int(values[FENCE_ANNOTATION]))
            if values!=expected:raise Conflict('target_owned')
            for target in payload['targets']:
                for attempt in range(3):
                    doc=self.transport.read(target['environment'],target['service'])
                    if doc['metadata']['uid']!=target['uid']:raise Conflict('cas_conflict')
                    observed=summarize(doc,target['environment'],target['service'],self.registry)
                    expected_image=target['previous_image'] if metadata_only else payload['artifact']['image']
                    if observed['image']!=expected_image:raise Conflict('cas_conflict')
                    if metadata_only and observed['template_sha256']!=target['template_sha256']:raise Conflict('cas_conflict')
                    annotations=doc['metadata'].get('annotations') or {}
                    if not any(k in annotations for k in MARKERS):break
                    if any(annotations.get(k)!=v for k,v in values.items()):raise Conflict('target_owned')
                    index=next(i for i,c in enumerate(doc['spec']['template']['spec']['containers']) if c['name']==target.get('container',target['service']))
                    try:
                        self.transport.patch(target['environment'],target['service'],identity_tests(doc)+owned_tests(values)+[
                            {'op':'test','path':f'/spec/template/spec/containers/{index}/image','value':expected_image},
                            {'op':'test','path':'/spec/template','value':doc['spec']['template']},
                            *[{'op':'remove','path':pointer(k)} for k in MARKERS]])
                        break
                    except Conflict:
                        if attempt == 2:raise
            state['released']=True
            self.store(path,state)
        return {'ok':True}

    def reconcile(self,payload,fence):
        # The new reconciliation fence must never replace the original ownership
        # values. No PATCH (including release) and no call back to deploy here.
        self.validate(payload,fence)
        with self.lock(payload['idempotency_key']):
            path=self.state_path(payload)
            if not path.exists():
                return {'ok':False,'write_outcome':'unknown','targets':[],'verification':[],'error':'write_journal_missing'}
            state=json.loads(path.read_text())
            if state['payload_sha256']!=digest(payload):raise Conflict('idempotency_payload_conflict')
            rows=[];metadata_rows=[]
            state['metadata_only_confirmed']=False
            try:
                for target in payload['targets']:
                    doc=self.transport.read(target['environment'],target['service'])
                    row=summarize(doc,target['environment'],target['service'],self.registry)
                    if row['uid']!=target['uid']:raise Conflict('cas_conflict')
                    annotations=doc['metadata'].get('annotations') or {}
                    has_markers=any(k in annotations for k in MARKERS)
                    own_markers=all(annotations.get(k)==v for k,v in state['markers'].items())
                    if has_markers and not own_markers:raise Conflict('target_owned')
                    metadata_only=(row['image']==target['previous_image'] and own_markers and
                                   row['template_sha256']==target['template_sha256'])
                    metadata_rows.append(metadata_only)
                    row={k:row[k] for k in ('environment','service','uid','resource_version','image')}
                    outcome='written' if row['image']==payload['artifact']['image'] else ('unknown' if has_markers else 'not_written')
                    row.update(previous_image=target['previous_image'],write_outcome=outcome)
                    rows.append(row)
                outcomes={r['write_outcome'] for r in rows}
                if outcomes=={'written'}:
                    self.observe_verification(payload,business=False)
                    result={'ok':True,'write_outcome':'written','targets':rows,'verification':[],'error':None}
                    state['reconciled']=True
                elif all(metadata_rows):
                    result={'ok':False,'write_outcome':'unknown','targets':rows,'verification':[],'error':'metadata_only_observed'}
                    state['metadata_only_confirmed']=True
                elif all(r['image']==t['previous_image'] for r,t in zip(rows,payload['targets'])) and not any(metadata_rows):
                    outcome='not_written' if state['result']['write_outcome']=='not_written' else 'unknown'
                    result={'ok':False,'write_outcome':outcome,'targets':rows,'verification':[],'error':'deployment_not_observed'}
                else:raise ValueError('partial_or_foreign_write')
            except Exception:
                result={'ok':False,'write_outcome':'unknown','targets':rows,'verification':[],'error':'write_outcome_unresolved'}
            state['result']=result;self.store(path,state)
            return result

