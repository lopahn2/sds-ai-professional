#!/usr/bin/env python3
"""Sequential operator-owned P18 fault campaign. Real cluster/API only; no fabricated analysis or usage.

Requires explicit --execute, an exact frozen catalog, and a successful parent build manifest.
Uses the operator kubeconfig (context pinned); never reads Secret values. Java token stays inside its existing Pod.
"""
import argparse
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
SERVICES=('order','inventory','payment','member','catalog','delivery','notification')
TERMINAL={'resolved','handed_off','failed','closed'}
RESTART='kubectl.kubernetes.io/restartedAt'
STOP=False


def stamp():return datetime.now(timezone.utc).isoformat()


def utcnow():return datetime.now(timezone.utc)


_LOG_STAMP=re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z (.*)$')


def _log_lines_since(body,since):
    """timestamps=True 로그에서 since 이후 줄만 남기고 앞의 시각을 떼어 원래 형식으로 돌려준다.
    시각이 없는 줄(여러 줄 스택의 이어짐)은 직전 줄의 판정을 따른다."""
    keep=False;out=[]
    for line in body.splitlines():
        m=_LOG_STAMP.match(line)
        if m:
            frac=(m.group(2) or '')[:6].ljust(6,'0')
            at=datetime.fromisoformat(m.group(1)+'.'+frac).replace(tzinfo=timezone.utc)
            keep=at>=since;line=m.group(3)
        if keep:out.append(line)
    return '\n'.join(out)+('\n' if out else '')


def write_json(path,data):
    path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n');tmp.replace(path)


def _stable(dep):
    value=copy.deepcopy(dep['spec']);value.pop('replicas',None)
    template=value['template'];template['metadata'].get('annotations',{}).pop(RESTART,None)
    for c in template['spec']['containers']:
        c.pop('image',None)
        # The model can change only the memory limit, not CPU, requests or other resource keys.
        if 'resources' in c:
            c['resources'].get('limits',{}).pop('memory',None)
    return value


def restore_ops(baseline,current):
    """Only restore the agreed mutable fields, under UID + current resourceVersion CAS. Never clobber foreign drift."""
    if baseline['metadata']['uid']!=current['metadata']['uid']:raise RuntimeError('Deployment UID changed; refusing restore')
    if _stable(baseline)!=_stable(current):raise RuntimeError('unapproved executable/template drift; refusing restore')
    ops=[]
    if baseline['spec']['replicas']!=current['spec']['replicas']:
        ops.append({'op':'replace','path':'/spec/replicas','value':baseline['spec']['replicas']})
    before=baseline['spec']['template'];now=current['spec']['template']
    if [c['name'] for c in before['spec']['containers']]!=[c['name'] for c in now['spec']['containers']]:
        raise RuntimeError('container drift')
    for i,(b,c) in enumerate(zip(before['spec']['containers'],now['spec']['containers'])):
        for field in ('image','resources'):
            if b.get(field)!=c.get(field):ops.append({'op':'replace' if field in c else 'add','path':f'/spec/template/spec/containers/{i}/{field}','value':b.get(field,{})})
    old=before['metadata'].get('annotations',{}).get(RESTART);new=now['metadata'].get('annotations',{}).get(RESTART)
    if old!=new:
        ptr='/spec/template/metadata/annotations/'+RESTART.replace('/','~1')
        ops.append({'op':'remove','path':ptr} if old is None else {'op':'replace' if new is not None else 'add','path':ptr,'value':old})
    if not ops:return []
    return [{'op':'test','path':'/metadata/uid','value':baseline['metadata']['uid']},
            {'op':'test','path':'/metadata/resourceVersion','value':current['metadata']['resourceVersion']},*ops]


def owned_pod(pod,replicaset,deployment_uid):
    controllers=[x for x in pod.get('metadata',{}).get('ownerReferences',[]) if x.get('controller') and x.get('kind')=='ReplicaSet']
    if len(controllers)!=1:return False
    owner=controllers[0];metadata=replicaset.get('metadata',{})
    return (owner.get('uid')==metadata.get('uid') and owner.get('name')==metadata.get('name') and
            any(x.get('controller') and x.get('kind')=='Deployment' and x.get('uid')==deployment_uid
                for x in metadata.get('ownerReferences',[])))


def pod_delete_snapshot(listed,fresh,namespace):
    """Refresh only the first write plan: ignore status/RV/managedFields, never executable or identity drift."""
    snapshots=[]
    for pod in (listed,fresh):
        value=copy.deepcopy(pod)
        for key,expected in (('kind','Pod'),('apiVersion','v1')):
            if key in value and value[key]!=expected:raise RuntimeError('invalid Pod type; refusing reset')
            value.pop(key,None)
        metadata=value['metadata']
        if (metadata.get('namespace')!=namespace or not all(isinstance(metadata.get(k),str) and metadata[k] for k in ('name','uid','resourceVersion'))
            or not isinstance(value.get('spec'),dict)):raise RuntimeError('invalid Pod identity/spec; refusing reset')
        if (any(metadata.get(k) is not None for k in ('deletionTimestamp','deletionGracePeriodSeconds'))
            or (value.get('status') or {}).get('phase') in ('Succeeded','Failed')):
            raise RuntimeError('terminating/terminal Pod; refusing reset')
        value.pop('status',None);metadata.pop('resourceVersion');metadata.pop('managedFields',None);snapshots.append(value)
    if snapshots[0]!=snapshots[1]:raise RuntimeError('Pod identity/metadata/spec drift; refusing reset')
    return copy.deepcopy(fresh)


def restore_diagnostic(error,target=None):
    """Deny raw exception/header/body text; only allowlisted status tokens and message fingerprints leave memory."""
    diagnostic={'cause':'unconfirmed','code':None}
    code=getattr(error,'status',None)
    if type(code) is int and 100<=code<=599:diagnostic['code']=code
    reasons={'Conflict','NotFound','Forbidden','Unauthorized','Invalid','BadRequest','Timeout','ServerTimeout',
             'InternalError','ServiceUnavailable','TooManyRequests','MethodNotAllowed','Gone','Expired','AlreadyExists','Unknown'}
    reason=getattr(error,'reason',None)
    if isinstance(reason,str) and reason in reasons:diagnostic['reason']=reason
    body=getattr(error,'body',None)
    if isinstance(body,str) and len(body)<=131072:
        try:status=json.loads(body)
        except (ValueError,TypeError):status=None
        if isinstance(status,dict):
            code=status.get('code');reason=status.get('reason');message=status.get('message')
            if type(code) is int and 100<=code<=599:diagnostic['status_code']=code
            if isinstance(reason,str) and reason in reasons:diagnostic['reason']=reason
            if isinstance(message,str):
                diagnostic.update(message_sha256=hashlib.sha256(message.encode()).hexdigest(),message_length=len(message))
            details=status.get('details');details=details if isinstance(details,dict) else {};safe={}
            uuid=r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
            if target:
                for key,allowed in (('name',{target['name']}),('kind',{'Pod','pods'}),('group',{''})):
                    if isinstance(details.get(key),str) and details[key] in allowed:safe[key]=details[key]
                if isinstance(details.get('uid'),str) and re.fullmatch(uuid,details['uid']):safe['uid']=details['uid']
                causes=details.get('causes');allowed_causes=[]
                if isinstance(causes,list):
                    for cause in causes[:32]:
                        if (isinstance(cause,dict) and cause.get('reason') in ('FieldValueInvalid','FieldValueRequired','FieldValueForbidden','FieldValueNotFound','FieldManagerConflict')
                            and cause.get('field') in ('metadata.uid','metadata.resourceVersion','preconditions.uid','preconditions.resourceVersion')):
                            entry={k:cause[k] for k in ('reason','field')};text=cause.get('message')
                            if isinstance(text,str):entry.update(message_sha256=hashlib.sha256(text.encode()).hexdigest(),message_length=len(text))
                            allowed_causes.append(entry)
                if allowed_causes:safe['causes']=allowed_causes
                if safe:diagnostic['details']=safe
            if (target and diagnostic['code']==409 and status.get('code')==409 and reason=='Conflict' and isinstance(message,str)
                and status.get('kind','Status')=='Status' and status.get('apiVersion','v1')=='v1'):
                name=re.escape(target['name']);namespace=re.escape(target['namespace'])
                prefix=rf'Operation cannot be fulfilled on pods "{name}": '
                storage=rf'StorageError: invalid object, Code: [0-9]{{1,3}}, Key: /registry/pods/{namespace}/{name}, ResourceVersion: [0-9]{{1,30}}, AdditionalErrorMsg: '
                bound=message.startswith(f'Operation cannot be fulfilled on pods "{target["name"]}": ') or safe.get('name')==target['name']
                for field,pattern,key in (('UID',uuid,'uid'),('ResourceVersion',r'[0-9]{1,30}','resourceVersion')):
                    match=re.fullmatch(rf'(?:{prefix})?(?:{storage})?Precondition failed: {field} in precondition: ({pattern}), {field} in object meta: ({pattern})',message)
                    if bound and match and match[1]==target[key] and match[1]!=match[2]:
                        diagnostic.update(cause='precondition_mismatch',precondition={'field':field,'requested':match[1],'observed':match[2]})
                    suffix='modified' if field=='ResourceVersion' else 'deleted and then recreated'
                    match=re.fullmatch(rf'Operation cannot be fulfilled on Pod "{name}": the {field} in the precondition '
                        rf'\(({pattern})\) does not match the {field} in record \(({pattern})\)\. The object might have been {suffix}',message)
                    if match and match[1]==target[key] and match[1]!=match[2]:
                        diagnostic.update(cause='precondition_mismatch',precondition={'field':field,'requested':match[1],'observed':match[2]})
    return diagnostic


def load_module(path,name):
    spec=importlib.util.spec_from_file_location(name,path);assert spec and spec.loader
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def current_source_hashes(service):
    build=load_module(ROOT/'scripts/p18-build-targets.py','campaign_build_sources')
    return build.source_hashes(ROOT/'services'/service)


def verify_deployed_builds(rows,deployments):
    if len(rows)!=len(SERVICES) or {r.get('service') for r in rows}!=set(SERVICES):
        raise RuntimeError('build manifest service coverage mismatch')
    for row in rows:
        name=row['service']
        if row.get('status')!='passed' or row.get('test',{}).get('exit')!=0 or row.get('build',{}).get('exit')!=0:
            raise RuntimeError('build manifest contains an unsuccessful build')
        if not row.get('source_sha256') or row['source_sha256']!=current_source_hashes(name):
            raise RuntimeError('build source fingerprint mismatch')
        containers=[c for c in deployments[name]['spec']['template']['spec']['containers'] if c['name']==name]
        if len(containers)!=1 or containers[0]['image']!=row.get('image'):
            raise RuntimeError('deployed image does not match verified build')


def new_restart(before,after):
    previous={(c['uid'],c['name']):c.get('restartCount',0) for c in before}
    return any(c.get('restartCount',0)>previous.get((c['uid'],c['name']),0) for c in after)


RECORDED={'completed','fault_unobserved','analysis_incomplete','failed'}


def chaos_clear(state):
    """장애 비활성은 200과 두 키(active, release_active)가 실제로 비어 있을 때만. 본문 결측·형식 오류는 미확인."""
    if not isinstance(state,dict) or state.get('status')!=200:return False
    body=state.get('body')
    if not isinstance(body,dict) or 'active' not in body or 'release_active' not in body:return False
    return body['active'] in ([],{}) and body['release_active'] in ([],{})


def _safe(result):
    return bool((result.get('drain') or {}).get('verified') and (result.get('restoration') or {}).get('verified'))


def after_case(result):
    """원복·drain이 실제 확인된 기록 실패만 다음 건으로 진행한다. 미확정·중단은 멈춘다."""
    if not _safe(result):return 'stop'
    if result.get('quota_wait'):return 'retry'
    return 'continue' if result.get('state') in RECORDED else 'stop'


def resume_action(result):
    """재개 시 기록된 결과는 건너뛰고 quota/중단만 별도 시도로 재실행한다."""
    if not _safe(result):return 'block'
    if result.get('quota_wait') or result.get('state')=='interrupted':return 'retry'
    return 'skip' if result.get('state') in RECORDED else 'block'


def archive_attempt(output,case_id):
    """재시도 전에 이전 시도를 집계 대상 밖 attempts/에 그대로 보존한다(삭제하지 않음)."""
    target=output/'attempts'/f"{case_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
    target.parent.mkdir(parents=True,exist_ok=True);(output/case_id).rename(target);return target


def case_state(observation,details):
    if not observation.get('verified'):return 'fault_unobserved'
    return ('completed' if details and all(d['incident']['status'] in TERMINAL for d in details)
            else 'analysis_incomplete')


class Harness:
    def __init__(self,args):
        from kubernetes import client,config
        config.load_kube_config(context=args.context)
        self.client=client;self.core=client.CoreV1Api();self.apps=client.AppsV1Api();self.api_client=client.ApiClient()
        self.args=args;self.ns=args.namespace
        if self.ns!='dev':raise RuntimeError('this campaign is restricted to dev; stg/prd remain baseline')
        self.output=args.output;self.output.mkdir(parents=True,exist_ok=True)
        self.summary=load_module(ROOT/'scripts/p18-campaign-results.py','campaign_results')

    def serialize(self,obj):return self.api_client.sanitize_for_serialization(obj)

    def api(self,path,method='GET',data=None):
        body=json.dumps(data,ensure_ascii=False).encode() if data is not None else None
        # 읽기(GET)만 게이트웨이 일시 오류(502/503/504)에 1회 재시도한다. 상태를 바꾸는 요청은 재시도하지 않는다.
        for attempt in (0,1):
            req=urllib.request.Request(self.args.api.rstrip('/')+path,data=body,method=method,
                headers={'Content-Type':'application/json','X-LiveProbe-Client':'dashboard'})
            timeout=20;deadline=getattr(self,'_api_deadline',None)
            if deadline is not None:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise RuntimeError('incident/action drain deadline exceeded before API request')
                timeout=min(timeout,remaining)
            try:
                with urllib.request.urlopen(req,timeout=timeout) as r:return json.load(r)
            except urllib.error.HTTPError as e:
                if attempt==0 and method=='GET' and e.code in (502,503,504):
                    time.sleep(1);continue
                raise RuntimeError(f'LiveProbe API {path}: HTTP {e.code}') from None

    def _kube_timeout(self,maximum=12):
        deadline=getattr(self,'_kube_deadline',None)
        if deadline is None:return maximum
        remaining=deadline-time.monotonic()
        if remaining<=0:raise TimeoutError('restoration reset deadline exceeded; no further reset request')
        return min(maximum,remaining)

    def dep(self,name):
        if name not in SERVICES:raise RuntimeError('unapproved service')
        return self.serialize(self.apps.read_namespaced_deployment(name,self.ns,_request_timeout=self._kube_timeout()))

    def pods(self,name):
        return [self.serialize(x) for x in self.core.list_namespaced_pod(self.ns,label_selector='app='+name,_request_timeout=self._kube_timeout()).items]

    def active_pod(self,name):
        ps=[p for p in self.pods(name) if not p['metadata'].get('deletionTimestamp')]
        if not ps:raise RuntimeError('target Pod unavailable')
        return sorted(ps,key=lambda p:p['metadata']['creationTimestamp'])[-1]['metadata']['name']

    def local(self,name,path,method='GET',data=None,timeout=6):
        """HTTP inside the existing target Pod. Never retrieves its environment or a Secret."""
        payload=json.dumps(data,ensure_ascii=False,separators=(',',':')) if data is not None else ''
        pod=self.active_pod(name)
        prefix=['kubectl','--context',self.args.context,'-n',self.ns,'exec',pod,'-c',name,'--']
        if name in ('order','inventory'):
            # curl config via stdin: the token never enters operator argv, stdout, logs or the model context.
            script="printf 'header = \"Authorization: Bearer %s\"\\n' \"$CHAOS_OPERATOR_TOKEN\" | curl -K - -sS --max-time \"$1\" -X \"$2\" -H 'Content-Type: application/json' --data-binary \"$3\" -w '\\n%{http_code}\\n%{time_total}' \"http://127.0.0.1:8080$4\""
            command=prefix+['sh','-c',script,'http',str(timeout),method,payload,path]
        elif name in ('member','delivery'):
            script="const http=require('node:http');const [path,method,body,ms]=process.argv.slice(1);const start=process.hrtime.bigint();let chunks=[],done=false,timed=false;function finish(status,error){if(done)return;done=true;process.stdout.write(JSON.stringify({status,http_seconds:Number(process.hrtime.bigint()-start)/1e9,http_timeout:timed,body:Buffer.concat(chunks).toString(),...(error?{error}: {})}));}const q=http.request({host:'127.0.0.1',port:8080,path,method,headers:{'content-type':'application/json'},timeout:Number(ms)},r=>{r.on('data',c=>chunks.push(c));r.on('end',()=>finish(r.statusCode));});q.on('error',()=>{finish(null,timed?'http_timeout':'http_transport_error');process.exitCode=2;});q.on('timeout',()=>{timed=true;q.destroy();});q.end(body);"
            command=prefix+['node','-e',script,path,method,payload,str(timeout*1000)]
        else:
            script="import sys,urllib.request,urllib.error,time,json,socket\npath,method,body,seconds=sys.argv[1:]\nreq=urllib.request.Request('http://127.0.0.1:8080'+path,data=body.encode() if body else None,method=method,headers={'Content-Type':'application/json'})\nopener=urllib.request.build_opener(urllib.request.ProxyHandler({}));start=time.monotonic();status=None;payload='';timed=False;error=None\ntry:\n try:r=opener.open(req,timeout=float(seconds))\n except urllib.error.HTTPError as e:r=e\n with r:status=r.status;payload=r.read().decode()\nexcept Exception as e:\n timed=isinstance(e,(TimeoutError,socket.timeout)) or isinstance(getattr(e,'reason',None),(TimeoutError,socket.timeout));error='http_timeout' if timed else 'http_transport_error'\nprint(json.dumps(dict(status=status,body=payload,http_seconds=time.monotonic()-start,http_timeout=timed,**({'error':error} if error else {}))))\nsys.exit(2 if error else 0)\n"
            command=prefix+['python','-c',script,path,method,payload,str(timeout)]
        start=time.monotonic()
        try:r=subprocess.run(command,text=True,capture_output=True,timeout=timeout+10)
        except subprocess.TimeoutExpired:return {'status':None,'error':'transport_deadline','seconds':round(time.monotonic()-start,3)}
        output=r.stdout.strip();result={'status':None,'seconds':round(time.monotonic()-start,3),
            'transport_exit':r.returncode,'timeout_seconds':timeout,'http_seconds':None,'http_timeout':False}
        try:
            if name in ('order','inventory'):
                # 본문이 비면(시간초과·연결실패·204) 출력이 '\n코드\n시간'이라 앞 줄바꿈을 지우면 안 된다.
                head,code,elapsed=r.stdout.rstrip('\n').rsplit('\n',2)
                measured={'status':int(code) or None,'body':head,'http_seconds':float(elapsed),'http_timeout':r.returncode==28}
            else:measured=json.loads(output)
            if not isinstance(measured,dict):raise ValueError('HTTP measurement shape')
            duration=measured.get('http_seconds')
            if type(duration) not in (int,float) or not math.isfinite(duration) or duration<0:raise ValueError('HTTP measurement duration')
            status=measured.get('status')
            if status is not None and (type(status) is not int or not 100<=status<=599):raise ValueError('HTTP status shape')
            result.update(measured)
            if isinstance(result.get('body'),str):
                try:result['body']=json.loads(result['body'])
                except ValueError:result['body']=result['body'][:12000]
        except (ValueError,TypeError):result['error']='http_measurement_unavailable'
        return result

    def chaos(self,name,method='GET',data=None):
        return self.local(name,'/internal/chaos/v1/app-faults' if name in ('order','inventory') else '/api/chaos',method,data)

    def health(self,name):
        path='/actuator/health/readiness' if name in ('order','inventory') else '/healthz/ready'
        return self.local(name,path,timeout=4)

    def probe_calls(self,name):
        calls={'order':[('GET','/api/orders/check?item=a',None),('POST','/api/orders',{'item':'a'}),('GET','/api/orders/1',None)],
          'inventory':[('GET','/api/stock/a',None),('POST','/api/stock/a/reserve',{'quantity':1}),('GET','/api/stock/a/summary',None)],
          'payment':[('POST','/api/payments',{'order_id':'campaign-probe','amount':12000}),('GET','/api/payments/1/refund',None),('GET','/api/payments/1/receipt',None)],
          'catalog':[('GET','/api/products?q=keyboard',None),('GET','/api/products/1',None),('POST','/api/products/1/price',{'price':79000})],
          'notification':[('POST','/api/notifications',{'recipient':'campaign-probe','channel':'email','message':'campaign probe'}),('POST','/api/notifications/1/retry',{}),('GET','/api/notifications',None)],
          'member':[('POST','/api/login',{'name':'장애회원'}),('POST','/api/members',{'name':'campaign-probe'}),('GET','/api/members/1/profile',None)],
          'delivery':[('PATCH','/api/deliveries/LP-AUTO/status',{'status':'배송 중'}),('POST','/api/deliveries',{'tracking':'CAMPAIGN-PROBE','recipient':'campaign-probe','dueAt':'2026-12-01T00:00:00Z'}),('GET','/api/deliveries/LP1001/eta',None)]}
        ids=getattr(self,'_business_ids',{})
        if name in ('payment','notification') and name in ids:
            prefix='/api/payments/' if name=='payment' else '/api/notifications/'
            calls[name]=[(method,path.replace(prefix+'1/',prefix+ids[name]+'/'),body) for method,path,body in calls[name]]
        return calls[name]

    def incident_listing(self,attempts=5):
        """한 번의 일관된 목록 snapshot. 읽는 중 새 사건으로 쪽이 밀리면 처음부터 다시 읽고, 계속 흔들리면 실패한다."""
        for n in range(attempts):
            try:return self._incident_listing_once()
            except RuntimeError as e:
                if 'drift' not in str(e) and 'duplicate' not in str(e) or n+1==attempts:raise
                time.sleep(1)

    def _incident_listing_once(self):
        """Fail closed on unstable/truncated offset pages, before namespace filtering."""
        items=[];seen=set();total=None;offset=0
        while True:
            page=self.api(f'/api/incidents?limit=500&offset={offset}')
            count=page.get('total');batch=page.get('items')
            if type(count) is not int or count<0 or not isinstance(batch,list) or count>100000:
                raise RuntimeError('incident pagination invalid total/items or bounded window exceeded')
            if total is None:total=count
            if count!=total or len(batch)!=min(500,total-offset):
                raise RuntimeError('incident pagination total drift/truncation')
            for item in batch:
                iid=item.get('id')
                if type(iid) is not int or iid in seen:raise RuntimeError('incident pagination duplicate/invalid ID')
                seen.add(iid)
            items.extend(batch);offset+=len(batch)
            if offset==total:return items
            if not batch:raise RuntimeError('incident pagination failed to advance')

    def details_since(self,watermark):
        return [self.api('/api/incidents/'+str(x['id'])) for x in self.incident_listing()
                if x['id']>watermark and x['namespace']==self.ns]

    def business_health(self,name):
        """비파괴 업무 GET으로 건강과 현재 보관 중인 점검 ID를 함께 확인한다."""
        paths={'order':['/api/orders/check?item=a'], 'inventory':['/api/stock/a','/api/stock/a/summary'],
               'payment':['/api/payments'], 'member':['/api/members','/api/members/1/profile'],
               'catalog':['/api/products?q=keyboard','/api/products/1'],
               'delivery':['/api/deliveries','/api/deliveries/LP1001/eta'],
               'notification':['/api/notifications']}
        probes=[{'method':'GET','path':p,**self.local(name,p,timeout=5)} for p in paths[name]]
        if name in ('payment','notification'):
            key='payments' if name=='payment' else 'notifications';body=probes[0].get('body') or {}
            rows=body.get(key) if isinstance(body,dict) else None
            identifier=str(rows[0].get('id','')) if isinstance(rows,list) and rows and isinstance(rows[0],dict) else ''
            if not identifier.isdigit():return {'verified':False,'probes':probes,'error':'current_business_id_unobserved'}
            if not hasattr(self,'_business_ids'):self._business_ids={}
            self._business_ids[name]=identifier
            if name=='payment':
                for suffix in ('refund','receipt'):
                    path=f'/api/payments/{identifier}/{suffix}'
                    probes.append({'method':'GET','path':path,**self.local(name,path,timeout=5)})
        return {'verified':all(p.get('status')==200 and not p.get('transport_exit') and not p.get('error') for p in probes),'probes':probes}

    def partition_incidents(self,details,case,start,end):
        attributed=[];excluded=[];linked=set(case.get('linked_incident_ids',[]))
        for d in details:
            inc=d['incident'];reason=None
            try:
                created=datetime.fromisoformat(inc['created_at'].replace('Z','+00:00'))
                inside=datetime.fromisoformat(start)<=created<=datetime.fromisoformat(end)
            except (KeyError,TypeError,ValueError):inside=False
            if inc.get('namespace')!=self.ns:reason='unrelated_namespace'
            elif not inside:reason='outside_detection_window_or_missing_timestamp'
            elif inc.get('workload')!=case['service'] and inc['id'] not in linked:reason='unrelated_workload_no_explicit_chain'
            if reason:excluded.append({**d,'exclusion_reason':reason})
            else:attributed.append(d)
        return attributed,excluded

    def wait_ready(self,name,timeout=150):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            d=self.dep(name);s=d.get('status',{});wanted=d['spec']['replicas']
            if (s.get('observedGeneration',0)>=d['metadata'].get('generation',0) and s.get('updatedReplicas',0)==wanted
                and s.get('readyReplicas',0)==wanted and s.get('replicas',0)==wanted):
                # Pod 교체 직후 기존 Pod가 사라지고 새 Pod가 아직 조회되지 않는 짧은 구간은 미준비로 재시도한다.
                try:
                    if self.health(name)['status']==200:return True
                except RuntimeError as e:
                    if str(e)!='target Pod unavailable':raise
            time.sleep(3)
        return False

    def node_guard(self):
        nodes=self.core.list_node(_request_timeout=10).items
        for n in nodes:
            conditions={x.type:x.status for x in n.status.conditions}
            if conditions.get('Ready')!='True' or any(conditions.get(k)=='True' for k in ('MemoryPressure','DiskPressure','PIDPressure')):
                raise RuntimeError('node safety gate')

    def release_image(self,case,baseline,directory):
        container=next(c for c in baseline['spec']['template']['spec']['containers'] if c['name']==case['service'])
        data={'faults':{f['fault']:{'params':f['params']} for f in case['injections']}}
        env=json.dumps(data,separators=(',',':'));assert "'" not in env
        text=f"FROM {container['image']}\nENV RELEASE_FAULTS='{env}'\n"
        dockerfile=directory/'Dockerfile.fault';dockerfile.write_text(text)
        tag='liveprobe/'+case['service']+':p18c-'+case['id']
        with (directory/'image-build.log').open('w') as log:
            r=subprocess.run(['docker','build','--pull=false','--network=none','-f',str(dockerfile),'-t',tag,str(directory)],stdout=log,stderr=subprocess.STDOUT,timeout=150)
        if r.returncode:raise RuntimeError('fault image build failed')
        info=json.loads(subprocess.check_output(['docker','image','inspect',tag],timeout=15))[0]
        actual=next((s.split('=',1)[1] for s in info['Config'].get('Env',[]) if s.startswith('RELEASE_FAULTS=')),None)
        if json.loads(actual or '{}')!=data:raise RuntimeError('fault image ENV mismatch')
        return next(x for x in info['RepoDigests'] if x.startswith('liveprobe/'+case['service']+'@sha256:'))

    def inject(self,case,baseline,directory):
        name=case['service'];results=[]
        if case['source']=='release':
            image=self.release_image(case,baseline[name],directory);d=self.dep(name)
            ix=next(i for i,c in enumerate(d['spec']['template']['spec']['containers']) if c['name']==name)
            ops=[{'op':'test','path':'/metadata/uid','value':baseline[name]['metadata']['uid']},
                 {'op':'test','path':'/metadata/resourceVersion','value':d['metadata']['resourceVersion']},
                 {'op':'replace','path':f'/spec/template/spec/containers/{ix}/image','value':image}]
            self.apps.patch_namespaced_deployment(name,self.ns,ops,_request_timeout=20)
            results.append({'image':image,'operator_patch_submitted':True})
        else:
            for f in case['injections']:
                result=self.chaos(name,'POST',{**f,'on':True,'ttl_seconds':case['ttl_seconds']})
                results.append(result)
                if result['status']!=200:raise RuntimeError('fault control rejected')
        return results

    def logs(self,name,since=None):
        from app.redact import redact
        result=[]
        # kubernetes SDK의 read_namespaced_pod_log는 since_time을 받지 않는다(ApiTypeError). since_seconds로
        # 여유 있게 받아 온 뒤 줄 시각으로 주입 시각 이전 줄을 잘라 낸다. 또 kubernetes-client 36은 기본 응답을
        # "b'...\\n...'" 형태의 문자열로 망가뜨리므로 원시 바이트(_preload_content=False)를 UTF-8로 읽는다.
        start=datetime.fromisoformat(since) if since else None
        window=({'since_seconds':max(1,math.ceil((utcnow()-start).total_seconds())+5),'timestamps':True} if start else {})
        for p in self.pods(name):
            try:
                raw=self.core.read_namespaced_pod_log(p['metadata']['name'],self.ns,container=name,tail_lines=180,
                    **window,_preload_content=False,_request_timeout=12)
                body=raw.data.decode('utf-8',errors='replace')
                if start:body=_log_lines_since(body,start)
                result.append({'pod':p['metadata']['name'],'text':redact(body)})
            except Exception as e:result.append({'pod':p['metadata']['name'],'error':type(e).__name__})
        return result

    def observe(self,case,directory,since,before_containers):
        name=case['service'];helper=load_module(ROOT/'scripts/p18-fault-observation.py','component_observation')
        facts={'observed_at':stamp(),'since':since,'logs_since':since,'before_containers':before_containers,
               'verified':False,'components':{},'symptoms':[],'probes':[]}
        start=time.monotonic();until=start+self.args.detect_timeout
        names={f['fault'] for f in case['injections']}
        while time.monotonic()<until and not STOP:
            state=[]
            for pod in self.pods(name):
                for c in pod.get('status',{}).get('containerStatuses',[]):
                    if c['name']==name:state.append({'pod':pod['metadata']['name'],'uid':pod['metadata']['uid'],
                        'name':c['name'],'ready':c.get('ready'),'restartCount':c.get('restartCount',0),
                        'state':c.get('state',{}),'lastState':c.get('lastState',{})})
            facts['containers']=state;running=any(c.get('state',{}).get('running') is not None for c in state if 'running' in c.get('state',{}))
            try:facts['health']=self.health(name) if running else None
            except Exception as e:facts['health']={'status':None,'error':type(e).__name__}
            if 'not_ready' in names:
                try:
                    endpoints=self.serialize(self.core.read_namespaced_endpoints(name,self.ns,_request_timeout=10))
                    facts['ready_endpoints']=sum(len(s.get('addresses') or []) for s in endpoints.get('subsets') or [])
                except Exception as e:facts['endpoint_error']=type(e).__name__
            if 'liveness_fail' in names and running:
                live='/actuator/health/liveness' if name in ('order','inventory') else '/healthz/live'
                try:
                    measured=self.local(name,live,timeout=4)
                    if measured.get('status')==503 or not facts.get('liveness'):facts['liveness']=measured
                except Exception as e:facts['liveness_error']=type(e).__name__
            if running:
                labels=('code_bug_validation','code_bug','code_bug_serialization') if name=='inventory' else ('code_bug','code_bug_validation','code_bug_serialization')
                timeout=12 if 'worker_exhaustion' in names else 5
                for index,(method,path,body) in enumerate(self.probe_calls(name)):
                    try:measured=self.local(name,path,method,body,timeout=timeout)
                    except Exception as e:measured={'status':None,'error':type(e).__name__}
                    facts['probes'].append({'path':path,'method':method,'faults':[labels[index]] if index<len(labels) else [],**measured})
                facts['probes']=facts['probes'][-120:]
            facts['logs']=self.logs(name,since=since)
            if 'cpu_spin' in names:
                try:
                    custom=self.client.CustomObjectsApi()
                    metric=custom.get_namespaced_custom_object('metrics.k8s.io','v1beta1',self.ns,'pods',self.active_pod(name),_request_timeout=10)
                    cpu=next(c['usage']['cpu'] for c in metric['containers'] if c['name']==name)
                    facts['cpu_usage']=cpu;facts['cpu_metric_timestamp']=metric.get('timestamp')
                    facts['cpu_cores']=float(cpu[:-1])/1e9 if cpu.endswith('n') else float(cpu[:-1])/1000 if cpu.endswith('m') else float(cpu)
                except Exception as e:facts['cpu_metric_error']=type(e).__name__
            facts.update(helper.assess(case,facts));facts['observed_at']=stamp()
            facts['symptoms']=sorted(n for n,r in facts['components'].items() if r['verified'])
            write_json(directory/'fault-observation.json',facts)
            if facts['verified']:return facts
            time.sleep(3)
        return facts

    def wait_reset_pods(self,name,baseline,timeout):
        """수렴 조회 전후·반환 전에 상한을 검사한다. 늦은 성공은 삭제 권한이 아니다."""
        previous=getattr(self,'_kube_deadline',None);deadline=time.monotonic()+timeout
        self._kube_deadline=min(previous,deadline) if previous is not None else deadline
        try:
            while True:
                self._kube_timeout()
                current=self.dep(name)
                self._kube_timeout()
                if restore_ops(baseline,current):raise RuntimeError('baseline changed during restoration rollout')
                status=current.get('status',{});wanted=current['spec']['replicas']
                self._kube_timeout()
                pods=self.pods(name)
                self._kube_timeout()
                converged=(wanted>0 and status.get('observedGeneration',0)>=current['metadata'].get('generation',0)
                           and all(status.get(k,0)==wanted for k in ('replicas','updatedReplicas')))
                if (converged and len(pods)==wanted and
                    not any(p['metadata'].get(k) is not None for p in pods for k in ('deletionTimestamp','deletionGracePeriodSeconds'))):
                    self._kube_timeout()
                    return pods
                time.sleep(self._kube_timeout(1))
        finally:self._kube_deadline=previous

    def restore(self,baselines):
        results=[];target=getattr(self,'_case_service',None)
        changed=set(getattr(self,'_changed_services',set()))
        names=sorted(baselines,key=lambda n:n!=target)
        for name in names:
            baseline=baselines[name];row={'service':name,'verified':False,'reset':False,'delete_attempts':[]};attempt=None;operation='restore'
            previous_deadline=getattr(self,'_kube_deadline',None)
            try:
                current=self.dep(name);ops=restore_ops(baseline,current)
                try:state=self.chaos(name)
                except Exception as e:state={'status':None,'error':type(e).__name__}
                row['pre_restore_chaos']=state
                if state.get('status')!=200 and name not in changed and not ops:
                    raise RuntimeError('chaos state unreadable on unchanged workload; refusing blind reset')
                body=state.get('body') or {}
                reset=name in changed or bool(ops) or bool(body.get('active') or body.get('release_active'))
                row['reset']=reset
                reset_pods=None
                if ops:
                    self.apps.patch_namespaced_deployment(name,self.ns,ops,_request_timeout=20)
                    operation='wait_rollout_before_reset'
                    deadline=time.monotonic()+getattr(self.args,'restore_timeout',150)
                    self._kube_deadline=min(previous_deadline,deadline) if previous_deadline is not None else deadline
                    reset_pods=self.wait_reset_pods(name,baseline,getattr(self.args,'restore_timeout',150))
                    row['rollout_converged_before_reset']=True;operation='restore'
                if reset:
                    self._kube_timeout()
                    try:row['clear']=self.chaos(name,'DELETE')
                    except Exception as e:row['clear']={'error':type(e).__name__}
                    self._kube_timeout()
                    # Reset only mutated workloads, preserving identity CAS and the complete owner chain.
                    for p in reset_pods if reset_pods is not None else self.pods(name):
                        self._kube_timeout()
                        if any(p['metadata'].get(k) is not None for k in ('deletionTimestamp','deletionGracePeriodSeconds')):
                            raise RuntimeError('terminating Pod; refusing reset')
                        owners=[x for x in p['metadata'].get('ownerReferences',[]) if x.get('kind')=='ReplicaSet' and x.get('controller')]
                        if len(owners)!=1:raise RuntimeError('unowned Pod; refusing reset')
                        rs=self.serialize(self.apps.read_namespaced_replica_set(owners[0]['name'],self.ns,_request_timeout=self._kube_timeout()))
                        self._kube_timeout()
                        if not owned_pod(p,rs,baseline['metadata']['uid']):raise RuntimeError('foreign Pod owner chain; refusing reset')
                        operation='read_pod'
                        fresh=pod_delete_snapshot(p,self.serialize(self.core.read_namespaced_pod(p['metadata']['name'],self.ns,_request_timeout=self._kube_timeout())),self.ns)
                        self._kube_timeout()
                        options=self.client.V1DeleteOptions(preconditions=self.client.V1Preconditions(uid=fresh['metadata']['uid'],resource_version=fresh['metadata']['resourceVersion']))
                        attempt={'namespace':self.ns,'name':fresh['metadata']['name'],'uid':fresh['metadata']['uid'],
                                 'resourceVersion':fresh['metadata']['resourceVersion'],'operation':'delete_pod','at':stamp(),'succeeded':False,'code':None}
                        row['delete_attempts'].append(attempt);operation='delete_pod'
                        response=self.core.delete_namespaced_pod(fresh['metadata']['name'],self.ns,body=options,_request_timeout=self._kube_timeout(15))
                        code=response.get('code') if isinstance(response,dict) else getattr(response,'code',None)
                        attempt['code']=code if type(code) is int and 100<=code<=599 else None
                        if attempt['code'] is not None and not 200<=attempt['code']<300:
                            raise RuntimeError('Pod DELETE returned non-success status; refusing verification')
                        attempt.update(succeeded=True,finished_at=stamp());operation='restore'
                self._kube_deadline=previous_deadline  # 삭제 후 readiness는 기존의 별도 검증 상한을 사용한다
                ready=self.wait_ready(name,timeout=getattr(self.args,'restore_timeout',150))
                final=self.dep(name);same=not restore_ops(baseline,final)
                final_state=self.chaos(name) if ready else None
                clear_active=chaos_clear(final_state)
                business=self.business_health(name) if ready else {'verified':False,'probes':[]}
                row.update(ready=ready,baseline_fields_equal=same,final_chaos=final_state,business_health=business,
                           verified=bool(ready and same and clear_active and business['verified']))
            except Exception as e:
                try:diagnostic=restore_diagnostic(e,attempt if operation=='delete_pod' else None)
                except Exception:diagnostic={'cause':'unconfirmed','code':None,'diagnostic_unavailable':True}
                diagnostic.update(operation=operation,at=stamp())
                kind=type(e).__name__;kind=kind if kind in {'RuntimeError','ValueError','TypeError','KeyError','TimeoutError','ApiException'} else 'Exception'
                row.update(error_type=kind,error='restoration step failed; see diagnostic',diagnostic=diagnostic)
                if operation=='delete_pod' and attempt is not None:
                    attempt.update(code=diagnostic.get('code') or attempt.get('code'),finished_at=stamp(),diagnostic=diagnostic)
            finally:self._kube_deadline=previous_deadline
            results.append(row)
        return {'at':stamp(),'verified':bool(results) and all(x['verified'] for x in results),'services':results}

    def read_incident(self,iid):
        d=self.api(f'/api/incidents/{iid}')
        if d.get('incident',{}).get('id')!=iid or not isinstance(d.get('actions'),list) or not isinstance(d.get('agent_runs'),list):
            raise RuntimeError('incident readback shape/identity mismatch')
        return d

    def close_records(self,details):
        """Stop all linked runs, then read back drain/close under one bounded deadline.

        stopping is an acknowledgement only. A pending approval has no worker to consume stop.
        Record closure is explicitly not a claim of automatic or operator recovery.
        """
        deadline=time.monotonic()+getattr(self.args,'drain_timeout',getattr(self.args,'restore_timeout',150))
        previous=getattr(self,'_api_deadline',None);self._api_deadline=deadline
        try:return self._drain_records(details,deadline)
        finally:self._api_deadline=previous

    def _drain_records(self,details,deadline):
        ids=sorted({d['incident']['id'] for d in details});stopped=set();snapshots={}
        live={'proposed','pending_approval','approved','executing'}
        for iid in ids:
            d=self.read_incident(iid);snapshots[iid]=d
            for a in d['actions']:
                if a['status']=='pending_approval':
                    self.api(f"/api/actions/{a['id']}/reject",'POST',{'reason':'캠페인 격리 종료: 추가 조치를 승인하지 않음. 복구 성공 판정 아님.'})
                    d=self.read_incident(iid);snapshots[iid]=d
                    found=[x for x in d['actions'] if x['id']==a['id']]
                    if not found or found[0]['status']!='rejected':raise RuntimeError('action rejection readback failed')
            if d['incident']['status'] not in TERMINAL:
                self.api(f'/api/incidents/{iid}/control','POST',{'action':'stop'});stopped.add(iid)
        while True:
            pending=[]
            for iid in ids:
                d=self.read_incident(iid);snapshots[iid]=d
                if any(a['status']=='indeterminate' for a in d['actions']):raise RuntimeError('action outcome indeterminate; manual checkpoint')
                busy=any(a['status'] in live for a in d['actions'])
                status=d['incident']['status']
                if busy or status not in TERMINAL:
                    # An approval can appear after the first stop boundary. Reject only its legal state.
                    for a in d['actions']:
                        if a['status']=='pending_approval':
                            self.api(f"/api/actions/{a['id']}/reject",'POST',{'reason':'캠페인 격리 종료; 승인 없음'})
                    if status not in TERMINAL and iid not in stopped and status!='action_pending':
                        self.api(f'/api/incidents/{iid}/control','POST',{'action':'stop'});stopped.add(iid)
                    pending.append(iid);continue
                if status=='handed_off':
                    self.api(f'/api/incidents/{iid}/close','POST',{'reason':'캠페인 조치 drain 확인 후 기록만 종료. 원복 및 자동 복구 성공을 의미하지 않음.','confirm':True})
                    d=self.read_incident(iid);snapshots[iid]=d
                    if d['incident']['status']!='closed' or any(a['status'] in live for a in d['actions']):
                        raise RuntimeError('incident close readback failed')
            if not pending:return [snapshots[i] for i in ids]
            if time.monotonic()>=deadline:raise RuntimeError('incident/action drain deadline exceeded')
            time.sleep(min(1,max(0,deadline-time.monotonic())))

    def wait_for_quota(self,record):
        """주입 전 분석 계정 하나 이상이 실제 available일 때까지 기다린다. 빈 목록은 가용으로 보지 않는다."""
        until=time.monotonic()+getattr(self.args,'quota_wait_limit',0);poll=getattr(self.args,'quota_poll_seconds',60)
        while True:
            states=[a.get('state') for a in self.api('/api/llm/accounts').get('accounts',[])]
            if 'available' in states:return True
            if time.monotonic()>=until or STOP:return False
            record({'at':stamp(),'accounts':states});time.sleep(poll)

    def wait_no_active_incidents(self):
        """앞선 원복 등으로 생긴 진행 중 분석이 끝날 때까지 기다리며, 그동안 새 장애를 주입하지 않는다."""
        until=time.monotonic()+getattr(self.args,'preexisting_wait',0);waited=[]
        while True:
            listing=self.incident_listing()
            active=[i for i in listing if i['namespace']==self.ns and i['status'] not in TERMINAL]
            if not active:return listing,waited
            waited=sorted(set(waited)|{i['id'] for i in active})
            if time.monotonic()>=until or STOP:raise RuntimeError('existing analysis must finish before next injection')
            time.sleep(10)

    def one(self,case):
        directory=self.output/case['id'];result={**case,'state':'preparing','started_at':stamp(),'incidents':[], 'excluded_incidents':[]}
        write_json(directory/'progress.json',result);self.node_guard()
        baseline={n:self.dep(n) for n in SERVICES};write_json(directory/'baseline.json',baseline)
        verify_deployed_builds(json.loads(self.args.build_manifest.read_text()),baseline)
        if any(not self.wait_ready(n,timeout=15) for n in SERVICES):raise RuntimeError('baseline not healthy')
        result['baseline_business_health']={}
        for n in SERVICES:
            s=self.chaos(n)
            if not chaos_clear(s):
                raise RuntimeError('baseline has an active/release/unobserved fault')
            business=self.business_health(n);result['baseline_business_health'][n]=business
            if not business['verified']:raise RuntimeError('baseline business health failed')
        quota_waits=[]
        if not self.wait_for_quota(quota_waits.append):
            result['quota_waits']=quota_waits;write_json(directory/'progress.json',result)
            raise RuntimeError('model quota unavailable before injection; campaign checkpoint required')
        if quota_waits:result['quota_waits']=quota_waits
        initial,waited=self.wait_no_active_incidents();watermark=max((i['id'] for i in initial),default=0)
        if waited:result['waited_for_preexisting_incidents']=waited
        before_containers=[{'uid':pod['metadata']['uid'],'name':c['name'],'restartCount':c.get('restartCount',0)}
                           for pod in self.pods(case['service']) for c in pod.get('status',{}).get('containerStatuses',[])
                           if c['name']==case['service']]
        result['baseline_containers']=before_containers
        self._case_service=case['service'];self._changed_services=set()
        attributed={};excluded={};attempt_error=False
        try:
            result['injection_started_at']=stamp();self._changed_services.add(case['service'])
            result['injection']=self.inject(case,baseline,directory);result['state']='injected';write_json(directory/'progress.json',result)
            result['fault_observation']=self.observe(case,directory,result['injection_started_at'],before_containers)
            until=time.monotonic()+self.args.analysis_timeout
            while time.monotonic()<until and not STOP:
                details=self.details_since(watermark)
                linked,other=self.partition_incidents(details,case,result['injection_started_at'],stamp())
                attributed.update({d['incident']['id']:d for d in linked});excluded.update({d['incident']['id']:d for d in other})
                write_json(directory/'incidents-latest.json',linked)
                if linked and all(d['incident']['status'] in TERMINAL or d['incident']['status']=='action_pending' for d in linked):break
                accounts=self.api('/api/llm/accounts').get('accounts',[])
                if accounts and all(a.get('state')!='available' for a in accounts):
                    result['quota_wait']=accounts;raise RuntimeError('model quota unavailable; campaign checkpoint required')
                time.sleep(5)
        except Exception as e:
            attempt_error=True;result.update(state='failed',error_type=type(e).__name__,error=str(e)[:500])
        finally:
            # Freeze admission BEFORE stop/drain and BEFORE the first restoration write.
            result['detection_window_closed_at']=stamp()
            try:
                details=self.details_since(watermark)
                linked,other=self.partition_incidents(details,case,result['injection_started_at'],result['detection_window_closed_at'])
                attributed.update({d['incident']['id']:d for d in linked});excluded.update({d['incident']['id']:d for d in other})
                result['attributed_incident_ids']=sorted(attributed)
                result['incidents']=[attributed[i] for i in result['attributed_incident_ids']]
                # 실행기 stop/close 이전의 고정 snapshot으로 분석이 스스로 끝났는지 판정한다.
                result['analysis_timed_out']=any(d['incident']['status'] not in TERMINAL and d['incident']['status']!='action_pending' for d in result['incidents'])
                result['incidents']=self.close_records(result['incidents'])
                result['drain']={'verified':True,'at':stamp()}
                result['restoration']={'verified':False,'skipped':False,'started_at':stamp()}
                result['restoration']=self.restore(baseline)
            except Exception as e:
                result.update(state='indeterminate',lifecycle_error_type=type(e).__name__,lifecycle_error=str(e)[:500])
                result.setdefault('drain',{'verified':False,'at':stamp()})
                result.setdefault('restoration',{'verified':False,'skipped':True,'reason':'stop/drain not confirmed; no cluster restoration writes'})
            result['excluded_incidents']=[excluded[i] for i in sorted(excluded)]
            result['finished_at']=stamp();write_json(directory/'result.json',result)
        if not result['drain']['verified'] or not result['restoration']['verified']:
            result['state']='indeterminate';write_json(directory/'result.json',result)
            raise RuntimeError('drain/restoration failed; no further injections')
        quiet_until=time.monotonic()+self.args.quiet_seconds
        while time.monotonic()<quiet_until and not STOP:time.sleep(min(5,max(0,quiet_until-time.monotonic())))
        try:
            # Final costs and outcome use EXACTLY the same fixed-ID readback snapshot.
            result['incidents']=[self.read_incident(i) for i in result['attributed_incident_ids']]
            live={'proposed','pending_approval','approved','executing','indeterminate'}
            if any(d['incident']['status'] not in TERMINAL or any(a['status'] in live for a in d['actions']) for d in result['incidents']):
                raise RuntimeError('attributed incident changed after drain')
            for d in self.details_since(watermark):
                iid=d['incident']['id']
                if iid not in attributed:
                    excluded[iid]={**d,'exclusion_reason':excluded.get(iid,{}).get('exclusion_reason','outside_frozen_detection_window')}
            result['state']='failed' if attempt_error else ('interrupted' if STOP else case_state(result.get('fault_observation',{}),result['incidents']))
            # 시간 초과 뒤 실행기 stop/close로 terminal이 된 사건은 분석 완료가 아니다.
            if result['state']=='completed' and result.get('analysis_timed_out'):result['state']='analysis_incomplete'
        except Exception as e:result.update(state='indeterminate',final_snapshot_error=str(e)[:500])
        result['excluded_incidents']=[excluded[i] for i in sorted(excluded)]
        result['recorded_at']=stamp();result['usage_snapshot_at']=result['recorded_at'];write_json(directory/'result.json',result)
        print(json.dumps({'case':case['id'],'state':result['state'],'fault_observed':result.get('fault_observation',{}).get('verified'),
                          'restored':result['restoration']['verified'],'incidents':result['attributed_incident_ids']},ensure_ascii=False),flush=True)
        # 원복·drain이 확인된 실패/미관측은 기록하고 다음 건으로 진행한다. 미확정·중단은 멈춘다.
        if after_case(result)=='stop':raise RuntimeError('incomplete case; checkpoint before continuing')
        return result


def main():
    sys.path.insert(0,str(ROOT/'liveprobe'))
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--context',default='rancher-desktop');p.add_argument('--namespace',default='dev')
    p.add_argument('--api',default='http://liveprobe.localhost');p.add_argument('--catalog',type=Path,default=ROOT/'docs/evidence/P18/campaign/catalog.json')
    p.add_argument('--build-manifest',type=Path,default=ROOT/'docs/evidence/P18/campaign/build/build-summary.json')
    p.add_argument('--output',type=Path,default=ROOT/'docs/evidence/P18/campaign/runs');p.add_argument('--cases',nargs='+')
    p.add_argument('--execute',action='store_true');p.add_argument('--detect-timeout',type=int,default=150);p.add_argument('--analysis-timeout',type=int,default=600)
    p.add_argument('--restore-timeout',type=int,default=150);p.add_argument('--quiet-seconds',type=int,default=125)
    p.add_argument('--quota-wait-limit',type=int,default=21600);p.add_argument('--quota-poll-seconds',type=int,default=60)
    p.add_argument('--preexisting-wait',type=int,default=900);p.add_argument('--max-attempts',type=int,default=3)
    a=p.parse_args()
    if not a.execute:p.error('explicit --execute required')
    expected=a.catalog.with_suffix('.sha256').read_text().split()[0];actual=hashlib.sha256(a.catalog.read_bytes()).hexdigest()
    if expected!=actual:raise RuntimeError('catalog SHA mismatch')
    allcases=json.loads(a.catalog.read_text())['cases']
    if len(allcases)!=180 or len({c['id'] for c in allcases})!=180:raise RuntimeError('catalog coverage mismatch')
    chosen=[c for c in allcases if not a.cases or c['id'] in a.cases]
    if a.cases and len(chosen)!=len(set(a.cases)):raise RuntimeError('unknown case ID')
    chosen.sort(key=lambda c:(int(c['id'].rsplit('-',1)[1]),SERVICES.index(c['system'])))
    a.output.mkdir(parents=True,exist_ok=True)
    with (a.output/'.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        h=Harness(a);write_json(a.output/'run-contract.json',{'catalog_sha256':actual,'case_order':[c['id'] for c in chosen],
            'build_manifest_sha256':hashlib.sha256(a.build_manifest.read_bytes()).hexdigest(),
            'analysis_timeout':a.analysis_timeout,'detect_timeout':a.detect_timeout,'quiet_seconds':a.quiet_seconds,'operator_context':a.context,'namespace':a.namespace})
        try:
            for c in chosen:
                for _ in range(a.max_attempts):
                    folder=a.output/c['id'];existing=folder/'result.json'
                    if existing.exists():
                        action=resume_action(json.loads(existing.read_text()))
                        if action=='skip':break
                        if action=='block':raise RuntimeError('existing indeterminate attempt preserved; operator review required')
                        archive_attempt(a.output,c['id'])
                    elif (folder/'progress.json').exists():
                        progress=json.loads((folder/'progress.json').read_text())
                        # 주입 시작 기록이 있으나 결과가 없으면 원복 여부를 알 수 없으므로 재시도하지 않는다.
                        if progress.get('injection_started_at') or progress.get('state')!='preparing':
                            raise RuntimeError('injected attempt without result preserved; operator review required')
                        archive_attempt(a.output,c['id'])
                    if STOP:break
                    if after_case(h.one(c))!='retry':break
                else:raise RuntimeError('attempt budget exhausted; checkpoint before continuing')
                if STOP:break
        finally:
            records=[json.loads(x.read_text()) for x in a.output.glob('*/result.json')]
            h.summary.write_report(records,a.output/'aggregate')
    print('관측 실행 원장·토큰 집계 저장',flush=True)


if __name__=='__main__':
    def stop(signum,frame):
        global STOP
        STOP=True
    signal.signal(signal.SIGINT,stop);signal.signal(signal.SIGTERM,stop)
    main()
