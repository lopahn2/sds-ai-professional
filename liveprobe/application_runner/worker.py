"""버전된 runner JSON 통신, heartbeat, 실행 전 영구 중복 방지."""
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from .profiles import digest

KINDS=['collect','source','test_build','deploy','verify','reconcile','qa','recovery']
# qa: browser + read-only dev reads only. recovery: read-only prd observation, so it stays with the cluster identity.
ROLE_KINDS={'source':['source','test_build','investigate'], 'cluster':['collect','deploy','verify','reconcile','recovery','current_version_check'], 'qa':['qa'], 'legacy':KINDS}


def redact(text):
    text=str(text)
    text=re.sub(r'Authorization\s*:\s*Bearer\s+\S+','[정제]',text,flags=re.I)
    text=re.sub(r'''(?i)["']?\b(?:password|passwd|secret|token|api[_-]?key|authorization|credential)["']?\s*[:=]\s*(?:"[^"\n]*"|'[^'\n]*'|[^\s,;}]+)''','[정제]',text)
    text=re.sub(r'(?i)(?:postgres(?:ql)?|mysql)://\S+','[정제]',text)
    text=re.sub(r'-----BEGIN[^\n]*PRIVATE KEY-----[\s\S]*?-----END[^\n]*PRIVATE KEY-----','[정제]',text)
    return text[:32768]


def read_token(path):
    path=Path(path)
    if path.is_symlink():raise ValueError('identity_file_unsafe')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        stat=os.fstat(fd)
        if stat.st_uid!=os.getuid() or stat.st_mode&0o077 or stat.st_size>16384:
            raise ValueError('identity_file_unsafe')
        value=os.read(fd,16385).decode().strip()
        if not value or any(x.isspace() for x in value):raise ValueError('identity_file_invalid')
        return value
    finally:os.close(fd)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise OSError('redirect_forbidden')


def safe_url(url):
    p=urllib.parse.urlsplit(url)
    if p.username or p.password or p.query or p.fragment or p.scheme not in ('http','https'):
        raise ValueError('invalid_endpoint')
    if p.scheme=='http' and p.hostname not in ('127.0.0.1','localhost','::1'):
        raise ValueError('tls_required')
    return url.rstrip('/')


class API:
    def __init__(self,url,token_file):
        self.url=safe_url(url)+'/api/application/v1'
        self.token_file=token_file
        self.opener=urllib.request.build_opener(NoRedirect())

    def post(self,path,body,timeout=20):
        if not (path in ('/runner/claim','/incidents') or re.fullmatch(r'/runner/jobs/[A-Za-z0-9_-]+/(heartbeat|progress|result)',path)):
            raise ValueError('machine_endpoint_forbidden')
        req=urllib.request.Request(self.url+path,data=json.dumps(body).encode(),method='POST',
                headers={'Authorization':'Bearer '+read_token(self.token_file),'Content-Type':'application/json'})
        try:
            with self.opener.open(req,timeout=min(20,timeout)) as response:
                data=response.read(2000001)
                if len(data)>2000000:raise OSError('response_too_large')
                return json.loads(data)
        except (urllib.error.URLError,ValueError):
            raise OSError('application_api_transport_failed') from None


def durable_write(path,value):
    path=Path(path)
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.new')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(value,stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
    os.replace(tmp,path)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


class LeaseGuard:
    """서버 승인 재검증과 로컬 단조 deadline을 함께 적용한다. 잃은 lease는 부활하지 않는다."""
    def __init__(self,api,path,auth,initial,wall_clock=time.time,monotonic=time.monotonic):
        self.api=api;self.path=path;self.auth=auth
        self.wall_clock=wall_clock;self.monotonic=monotonic
        self.origin_wall=wall_clock();self.origin_mono=monotonic()
        self.lost=threading.Event();self.lock=threading.Lock()
        self.deadline=self._deadline(initial,self.origin_wall,self.origin_mono)

    def _deadline(self,response,sent_wall,sent_mono):
        value=response.get('lease_expires_at') if isinstance(response,dict) else None
        if type(value) not in (int,float) or not math.isfinite(value):
            self.lost.set();raise ValueError('lease_lost')
        deadline=min(sent_mono+value-sent_wall,self.origin_mono+value-self.origin_wall)
        if value<=self.wall_clock() or deadline<=self.monotonic():
            self.lost.set();raise ValueError('lease_lost')
        return deadline

    def __call__(self):
        remaining=self.deadline-self.monotonic()
        if self.lost.is_set() or remaining<=0:
            self.lost.set();raise ValueError('lease_lost')
        return remaining

    def refresh(self, *, wait=False):
        # 쓰기 직전 확인이 지연 heartbeat 뒤에서 기다리며 시간창을 연장하지 않는다.
        # A foreground request may wait only within its already-valid local lease.
        # It always performs its own authenticated heartbeat and rechecks expiry before writing.
        remaining = self()
        acquired = self.lock.acquire(timeout=min(2, remaining)) if wait else self.lock.acquire(blocking=False)
        if not acquired:return False
        try:
            remaining=self()
            sent_wall=self.wall_clock();sent_mono=self.monotonic()
            response=self.api.post(self.path,self.auth,timeout=min(20,remaining))
            self()  # 이전 lease가 응답을 기다리는 동안 만료됐으면 갱신 응답도 거부한다.
            deadline=self._deadline(response,sent_wall,sent_mono)
            self.deadline=deadline
            self()
            return True
        except Exception:
            self.lost.set();return False
        finally:self.lock.release()

    def confirm_write(self):
        self()
        if not self.refresh(wait=True):raise ValueError('lease_lost')
        return self()


class Worker:
    def __init__(self,api,executor,state_root,worker_id,heartbeat_seconds=10,
                 wall_clock=time.time,monotonic=time.monotonic,role='legacy'):
        if role not in ROLE_KINDS:raise ValueError('runner_role_invalid')
        self.capabilities=list(ROLE_KINDS[role])
        self.role=role
        self.api=api;self.executor=executor;self.state_root=Path(state_root);self.worker_id=worker_id
        self.heartbeat_seconds=heartbeat_seconds;self.wall_clock=wall_clock;self.monotonic=monotonic

    def once(self,dry_run=False):
        if dry_run:return {'ok':True,'claimed':False,'dry_run':True,'capabilities':self.capabilities}
        response=self.api.post('/runner/claim',{'worker_id':self.worker_id,'capabilities':self.capabilities})
        job=response.get('job') if response else None
        if job is None:return {'ok':True,'claimed':False}
        if not re.fullmatch(r'[A-Za-z0-9_-]+',job['id']) or job['kind'] not in self.capabilities or type(job['fencing_token']) is not int or job['fencing_token']<1:
            raise ValueError('invalid_job_contract')
        auth={'lease_token':job['lease_token'],'fencing_token':job['fencing_token']}
        heartbeat='/runner/jobs/'+job['id']+'/heartbeat'
        guard=LeaseGuard(self.api,heartbeat,auth,job,self.wall_clock,self.monotonic)
        if not guard.refresh():raise ValueError('lease_lost')
        stopped=threading.Event()
        def pulse():
            while not stopped.wait(self.heartbeat_seconds):
                if not guard.refresh():return
        seq=0
        def observe(phase, file=None):
            nonlocal seq
            guard()
            seq+=1
            # Telemetry is advisory; losing a progress packet cannot replay a mutation.
            try:
                self.api.post('/runner/jobs/'+job['id']+'/progress',{**auth,'seq':seq,'phase':phase,'file':file},timeout=3)
            except Exception:
                return
        self.executor.observe=observe
        self.executor.guard=guard
        self.executor.write_guard=guard.confirm_write
        thread=threading.Thread(target=pulse,daemon=True);thread.start()
        try:
            self.state_root.mkdir(mode=0o700,parents=True,exist_ok=True)
            key=digest(job['idempotency_key'])
            # A fresh lease must re-observe the currently serving version. Never reuse a prior attempt's proof.
            if job['kind']=='current_version_check':
                key += '-'+str(job['fencing_token'])
            path=self.state_root/(key+'.json')
            fd=os.open(self.state_root/(key+'.lock'),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'a+') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                fingerprint=digest({'kind':job['kind'],'payload':job['payload'],'idempotency_key':job['idempotency_key']})
                if path.exists():
                    saved=json.loads(path.read_text())
                    if saved['input_sha256']!=fingerprint:raise ValueError('job_payload_conflict')
                    result=saved['result']
                else:
                    # 실행 중 죽으면 재claim이 이 영수증을 보고 이관/조정만 한다.
                    result={'ok':False,'write_outcome':'unknown','error':'execution_interrupted_reconcile_required'}
                    durable_write(path,{'input_sha256':fingerprint,'result':result})
                    try:
                        guard();result=self.executor.execute(job)
                    except Exception as exc:
                        code=str(exc) if isinstance(exc,ValueError) and re.fullmatch('[a-z][a-z0-9_]{1,100}',str(exc)) else 'runner_execution_failed'
                        result={'ok':False,'write_outcome':'unknown' if job['kind']=='deploy' else 'not_written','error':code}
                    durable_write(path,{'input_sha256':fingerprint,'result':result})
                guard()
                receipt=self.api.post('/runner/jobs/'+job['id']+'/result',{**auth,'result':result})
                return {'ok':True,'claimed':True,'job_id':job['id'],'result_ok':result.get('ok') is True,'receipt_received':receipt is not None}
        finally:
            stopped.set();thread.join(timeout=21)
