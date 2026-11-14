"""등록 범위의 예외 감지와 최초 표본 보존. dry-run은 읽기도 하지 않는다."""
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
from .profiles import digest
from .worker import durable_write
from .observations import bounded


def exception_sample(text):
    lines=text.splitlines()
    for i,line in enumerate(lines):
        tail='\n'.join(lines[i:i+30])
        trace='Traceback (most recent call last)' in line
        exception=bool(re.search(r'\b[\w.$]*(?:Exception|Error)\b',line))
        stack=bool(re.search(r'(?:File ".+", line \d+|\bat\s+[\w.$]+\(.+?:\d+\))',tail))
        if trace or ((exception or 'ERROR' in line) and stack):
            # Stop before unrelated access/INFO records so timing noise cannot
            # turn one open exception signature into repeated incident writes.
            block=[]
            for item in lines[i:i+30]:
                plain=re.sub(r'^\d{4}-\d\d-\d\dT[^\s]+\s*','',item)
                if block and re.match(r'(?:INFO\b|WARN\b|\{.*(?:"level"|"event"))',plain):break
                block.append(item)
            return bounded('\n'.join(block))[0]
    return None


def exception_samples(text):
    """Keep distinct exception heads from one Pod, including two faults in one collection."""
    heads = []
    seen = set()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not re.search(r'Traceback \(most recent call last\)|(?:[\w$.]+(?:Exception|Error))(?=[:\s]|$)', line):
            continue
        sample = exception_sample('\n'.join(lines[i:i+18]))
        if not sample:
            continue
        # Match the line that owns the trace, excluding summaries/JSON markers.
        core = re.sub(r'^\d{4}-\d\d-\d\dT[^\s]+\s*', '', line).strip()
        if core.startswith(('{', 'at ')) or not core:
            continue
        if core not in seen:
            heads.append(sample)
            seen.add(core)
        if len(heads) == 4:
            break
    return heads


def signature(sample):
    clean=re.sub(r'(?m)^\d{4}-\d\d-\d\dT[^\s]+\s*','',sample)
    clean=re.sub(r'0x[0-9a-fA-F]+','<address>',clean)
    clean=re.sub(r'line \d+','line <n>',clean)
    return digest(clean.encode())


class Detector:
    def __init__(self,registry,kube,api,state_root):
        self.registry=registry;self.kube=kube;self.api=api;self.state_root=Path(state_root)

    def run(self,environment,services,since,dry_run=False):
        services=sorted(set(services))
        for service in services:self.registry.target({'environment':environment,'service':service})
        if not services:raise ValueError('detection_targets_required')
        if not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z',since):raise ValueError('invalid_log_since')
        datetime.fromisoformat(since.replace('Z','+00:00'))
        if dry_run:return {'ok':True,'dry_run':True,'writes':0,'reported':0,'duplicates':0,'services':services,'environment':environment}
        self.state_root.mkdir(mode=0o700,parents=True,exist_ok=True)
        fd=os.open(self.state_root/'detect.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            path=self.state_root/'signatures.json'
            if path.is_symlink():raise ValueError('detection_state_unsafe')
            state=json.loads(path.read_text()) if path.exists() else {}
            reported=duplicates=0
            for service in services:
                for row in self.kube.logs(environment,service,since):
                    sample=exception_sample(row['text'])
                    if sample is None:continue
                    when=row['observed_at']
                    signal={'source':'pod_log','observed_at':when,'sample':sample,'sha256':digest(sample.encode())}
                    key=digest({'environment':environment,'service':service,'signature':signature(sample)})
                    if key in state:
                        duplicates+=1;continue
                    # Before the API write, durably suppress replay after a lost
                    # response. An unknown incident write requires operator lookup.
                    state[key]={'signal':signal,'incident_id':None,'write_outcome':'unknown'}
                    durable_write(path,state)
                    incident=self.api.post('/incidents',{'environment':environment,'service':service,
                        'reason':f'등록 서비스의 Pod 로그에서 애플리케이션 예외를 관측했습니다. 사건 서명 {signature(sample)[:16]}','signal':signal})
                    state[key].update(incident_id=incident.get('id'),write_outcome='written')
                    durable_write(path,state);reported+=1
            return {'ok':True,'dry_run':False,'reported':reported,'duplicates':duplicates}
