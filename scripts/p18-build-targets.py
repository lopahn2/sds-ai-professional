#!/usr/bin/env python3
"""Parent verification/build of seven P18 targets. No deploy, credentials, registry push or merge.

Logs/results survive each service. Every command has a deadline, and one failed service makes the run fail.
"""
import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ('payment','catalog','notification','member','delivery','order','inventory')
PYTHON = 'python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e'
NODE = 'node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402'


def pin_local_runtime(service, reference):
    """Resolve the inspected local runtime without BuildKit pulling a config digest from a registry."""
    def inspect(ref):
        return json.loads(subprocess.check_output(['docker','image','inspect',ref],timeout=15))[0]['Id']
    identity=inspect(reference)
    tag=f'liveprobe/{service}:p18-runtime-{identity.removeprefix("sha256:")}'
    subprocess.run(['docker','tag',identity,tag],check=True,timeout=15)
    if inspect(tag)!=identity:raise RuntimeError('runtime alias identity mismatch')
    return tag, {'source_reference':reference,'image_id':identity,'local_alias':tag}


def run(command, log, cwd, deadline=240):
    start=time.monotonic()
    with log.open('w') as out:
        try:
            r=subprocess.run(command,cwd=cwd,stdout=out,stderr=subprocess.STDOUT,timeout=deadline)
            code=r.returncode
        except subprocess.TimeoutExpired:
            code=124
    return {'exit':code,'seconds':round(time.monotonic()-start,2),'log':str(log.relative_to(ROOT))}


def source_hashes(directory):
    return {str(p.relative_to(directory)):hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.rglob('*')
            if p.is_file() and (p.suffix in ('.py','.js','.java','.xml','.properties','.json','.html','.css') or p.name.startswith('Dockerfile'))
            and not set(p.relative_to(directory).parts)&{'target','node_modules','__pycache__'}
            and not any(x.startswith('.') for x in p.relative_to(directory).parts)}


def verify_build(service, tag, output):
    directory=ROOT/'services'/service
    source=source_hashes(directory)
    if service in ('order','inventory'):
        command=['docker','run','--rm','--pull=never','--network=none','-e','JAVA_TOOL_OPTIONS=-XX:ActiveProcessorCount=2',
                 '-v','liveprobe-m2:/root/.m2','-v',f'{directory}:/src','-w','/src','maven:3.9-eclipse-temurin-21','mvn','-B','-o','package']
    else:
        image=NODE if service in ('member','delivery') else PYTHON
        command=['docker','run','--rm','--pull=never','--network=none','--tmpfs','/scratch:mode=1777',
                 '-e','TMPDIR=/scratch','-e','SELF_TRAFFIC=0','-e','PYTHONDONTWRITEBYTECODE=1',
                 '-v',f'{directory}:/app:ro','-w','/app',image]
        command+=['node','--test','test'] if service in ('member','delivery') else ['python','-m','unittest','discover','-s','tests','-v']
    test=run(command,output/f'{service}-parent-test.log',directory)
    result={'service':service,'source_sha256':source,'test':test,'status':'failed'}
    if test['exit']==0:
        image=f'liveprobe/{service}:{tag}'
        command=['docker','build','--pull=false','--network=none','-t',image]
        if service in ('order','inventory'):
            reference=('liveprobe/order:p18-ui-fixed' if service=='order' else
                       'liveprobe/inventory@sha256:62a2889b1794c81cea4ddd72260b5f5c20ec64ce1741429d62c114ed363e4edf')
            local_base,receipt=pin_local_runtime(service,reference)
            result['runtime_base']=receipt
            command+=['-f','Dockerfile.campaign-runtime','--build-arg',f'RUNTIME_BASE={local_base}']
        command+=['.']
        result['build']=run(command,output/f'{service}-parent-build.log',directory)
        if result['build']['exit']==0:
            info=json.loads(subprocess.check_output(['docker','image','inspect',image],timeout=15))[0]
            result['image']=next(d for d in info.get('RepoDigests',[]) if d.startswith(f'liveprobe/{service}@sha256:'))
            result['image_id']=info['Id']
            result['release_env']=[s for s in info['Config'].get('Env',[]) if s.startswith('RELEASE_FAULTS=')]
            if service in ('order','inventory'):
                result['jar_sha256']=hashlib.sha256((directory/'target/app.jar').read_bytes()).hexdigest()
                actual=subprocess.check_output(['docker','run','--rm','--network=none','--entrypoint','sha256sum',image,'/app/app.jar'],timeout=15).decode().split()[0]
                assert actual==result['jar_sha256'], 'packaged JAR/image mismatch'
            if source_hashes(directory)!=source:raise RuntimeError('source changed during verification/build')
            result['status']='passed'
    (output/f'{service}-build.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(service,result['status'],flush=True)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--tag',required=True);p.add_argument('--workers',type=int,default=2);p.add_argument('--output',type=Path,default=ROOT/'docs/evidence/P18/campaign/build');args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as e:
        rows=list(e.map(lambda n:verify_build(n,args.tag,args.output),SERVICES))
    (args.output/'build-summary.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
    if not all(x['status']=='passed' for x in rows):raise SystemExit(1)
    (args.output/'images.json').write_text(json.dumps({x['service']:x['image'] for x in rows},indent=2)+'\n')
    print('7개 대상 부모 시험·빌드·실제 배포 digest 저장 완료',flush=True)


if __name__=='__main__':main()
