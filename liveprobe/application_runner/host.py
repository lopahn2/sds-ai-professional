"""셸 없는 고정 Docker argv. 자격·호스트 socket은 시험 컨테이너에 전달하지 않는다."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import time
import uuid
import xml.etree.ElementTree as ET
from .build import test_command,final_manifest
from .profiles import digest, manifest_image


@dataclass
class ProcessResult:
    returncode:int
    stdout:str
    stderr:str


def process(argv,cwd=None,timeout=300):
    # 호출자 환경의 토큰/클라우드/DB 자격을 상속하지 않는다.
    env={'PATH':'/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1'}
    if '--config' in argv and argv[0]=='docker':
        client_dir = Path(argv[argv.index('--config')+1])
        env['BUILDX_CONFIG'] = str(client_dir.parent/'buildx')
        env['DOCKER_BUILDKIT'] = '1'
    result=subprocess.run(argv,cwd=cwd,env=env,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=timeout,shell=False)
    return ProcessResult(result.returncode,result.stdout,result.stderr)


class DockerHost:
    def __init__(self,config,run=process):
        self.config=config;self.run=run
        self.guard=lambda:None

    def observe_manifest(self,image):
        manifest_image(image)
        result=self.invoke(['image','inspect',image])
        if result.returncode:raise ValueError('source_manifest_not_local')
        observed=final_manifest(json.loads(result.stdout)[0],image.split('@')[0])
        if observed!=image:raise ValueError('source_manifest_mismatch')
        return observed

    def invoke(self,args,timeout=300):
        guarded=args[0] not in ('rm',) and args[:2] not in (['network','rm'],['container','inspect'],['network','inspect'])
        if guarded:
            remaining=getattr(self,'write_guard',self.guard)()
            if isinstance(remaining,(int,float)):timeout=min(timeout,remaining)
        # DOCKER_CONFIG은 부모가 준비한 인증 없는 전용 client 설정만 허용한다.
        prefix=['docker']
        if self.config.get('docker_config'):
            prefix+=['--config',str(self.config['docker_config'])]
        if self.config.get('docker_host'):
            host=self.config['docker_host']
            if not host.startswith('unix:///'):
                raise ValueError('local_docker_socket_required')
            prefix+=['--host',host]
        result=self.run(prefix+args,timeout=timeout)
        if guarded:self.guard()
        return result

    def tests(self,workspace,profile,phase,added):
        language=profile['language']
        image=self.config.get('test_images',{}).get(language)
        manifest_image(image)
        owner='application-'+uuid.uuid4().hex
        reports=workspace.parent/(owner+'-reports')
        reports.mkdir(mode=0o700)
        network=None;database=None
        try:
            if language=='domain':
                dbimage=manifest_image(self.config.get('database_image'))
                network=owner
                created=self.invoke(['network','create','--internal','--label','liveprobe.application.owner='+owner,owner])
                if created.returncode:raise ValueError('test_network_unavailable')
                database=owner+'-db'
                started=self.invoke(['run','-d','--name',database,'--label','liveprobe.application.owner='+owner,
                     '--network',network,'--network-alias','database','--memory=256m','--cpus=0.5',
                     '--cap-drop=ALL','--security-opt=no-new-privileges','--tmpfs','/var/lib/postgresql/data:rw,size=134217728',
                     '-e','POSTGRES_HOST_AUTH_METHOD=trust',dbimage,'-c','shared_buffers=16MB','-c','max_connections=30'])
                if started.returncode:raise ValueError('test_database_unavailable')
                deadline=time.monotonic()+45
                while True:
                    check=self.invoke(['exec',database,'pg_isready','-U','postgres'],timeout=5)
                    if check.returncode==0:break
                    if time.monotonic()>=deadline:raise ValueError('test_database_not_ready')
                    time.sleep(0.5)
            runner_dir=Path(__file__).resolve().parent
            base=['run','--rm','--name',owner,'--label','liveprobe.application.owner='+owner,
                  '--network='+str(network or 'none'),'--memory=1g','--cpus=1','--pids-limit=128',
                  '--cap-drop=ALL','--security-opt=no-new-privileges','--read-only','--user',str(os.getuid())+':'+str(os.getgid()),
                  '--tmpfs','/tmp:rw,nosuid,size=268435456',
                  '--mount','type=bind,src='+str(workspace.resolve())+',dst=/workspace,readonly',
                  '--mount','type=bind,src='+str(runner_dir)+',dst=/runner,readonly',
                  '--mount','type=bind,src='+str(reports.resolve())+',dst=/reports',
                  '--workdir','/workspace','-e','PYTHONDONTWRITEBYTECODE=1','-e','PYTHONPATH=/workspace:/workspace/src:/workspace/tests',
                  '-e','SELF_TRAFFIC=0','-e','HOME=/tmp']
            if language=='java':
                # Maven 산출물만 별도 쓰기 mount다. 시험·pom·Dockerfile은 읽기 전용이다.
                (workspace/'target').mkdir(exist_ok=True)
                (reports/'java-target').mkdir()
                base+=['--mount','type=bind,src='+str((reports/'java-target').resolve())+',dst=/workspace/target']
            if language=='domain':
                base+=['-e','TEST_DOMAIN_DSN=host=database user=postgres dbname=postgres connect_timeout=3',
                       '-e','APPLICATION_TEST_MODE='+profile['mode']]
            base+=['-e','APPLICATION_TEST_PHASE='+phase]
            if language=='node':
                paths=sorted(p.relative_to(workspace).as_posix() for p in (workspace/'test').rglob('*.js'))
                # 파일별 프로세스를 실행하여 그 파일의 실제 native TAP 결과를 연결한다.
                rows=[];total=failed=errors=0;exit_code=0
                for path in paths:
                    result=self.invoke(base+[image,*test_command(profile,[path])],timeout=240)
                    tests=re.findall(r'^# tests (\d+)$',result.stdout,re.M)
                    failures=re.findall(r'^# fail (\d+)$',result.stdout,re.M)
                    if len(tests)!=1 or len(failures)!=1:raise ValueError('native_test_report_missing')
                    count=int(tests[0]);bad=int(failures[0]);total+=count;failed+=bad
                    # TAP의 실제 leaf 시험 수가 없는 파일은 실행 증거로 인정하지 않는다.
                    if count>0:
                        from .reports import tap_rows
                        rows.extend(tap_rows(result.stdout,path,phase))
                    if result.returncode not in (0,1):errors+=1
                    exit_code=max(exit_code,result.returncode)
                return {'exit_code':exit_code,'total':total,'failed':failed,'errors':errors,'executed_tests':rows,'phase':phase}
            result=self.invoke(base+[image,*test_command(profile,[])],timeout=300)
            from .reports import junit_rows,totals
            if language=='java':
                rows=[]
                for report_path in (reports/'java-target/surefire-reports').glob('TEST-*.xml'):
                    if report_path.is_symlink():raise ValueError('unsafe_test_report')
                    rows.extend(junit_rows(report_path.read_text(),'java',phase))
                report=totals(rows)
            elif language=='python':
                path=reports/'pytest.xml'
                if not path.is_file() or path.is_symlink():raise ValueError('native_test_report_missing')
                report=totals(junit_rows(path.read_text(),'python',phase))
            else:
                path=reports/'unittest.json'
                if not path.is_file() or path.is_symlink():raise ValueError('native_test_report_missing')
                report=json.loads(path.read_text())
            report.update(exit_code=result.returncode,phase=phase,mode=profile.get('mode'))
            return report
        finally:
            from .worker import durable_write
            cleanup={'owner':owner,'containers':[],'network':{'status':'not_created'},
                     'reports':{'path':str(reports.resolve())}}
            # 실패한 생성/응답 유실도 이름으로 재조회한다. 타 소유 label은 지우지 않는다.
            def remove_owned(kind,name):
                row={'name':name,'status':'unresolved'}
                try:
                    inspected=self.invoke([kind,'inspect',name])
                    documents=json.loads(inspected.stdout)
                    if not documents:
                        row['status']='absent';return row
                    document=documents[0]
                    labels=(document['Config']['Labels'] if kind=='container' else document['Labels']) or {}
                    if labels.get('liveprobe.application.owner')!=owner:
                        row['status']='retained_foreign';return row
                    command=['rm','-f',name] if kind=='container' else ['network','rm',name]
                    removed=self.invoke(command)
                    if removed.returncode==0:
                        remaining=self.invoke([kind,'inspect',name])
                        if not json.loads(remaining.stdout):row['status']='removed'
                except (OSError,ValueError,KeyError,IndexError,TypeError):pass
                return row
            for name in [owner,database]:
                if name:cleanup['containers'].append(remove_owned('container',name))
            if network:cleanup['network']=remove_owned('network',network)
            try:
                shutil.rmtree(reports)
                cleanup['reports']['status']='removed'
            except OSError:cleanup['reports']['status']='unresolved'
            durable_write(workspace.parent/'cleanup'/(owner+'.json'),cleanup)

    def build(self,workspace,profile,tree_sha):
        recipe=workspace/'Dockerfile'
        if recipe.exists():
            text=re.sub(r'\\\r?\n',' ',recipe.read_text())
            if (re.search(r'--(?:network|mount|security|device|privileged)\b',text,re.I) or
                re.search(r'^\s*(?:ADD|COPY)\s+[^\n]*(?:https?://|git@|ssh://)',text,re.I|re.M) or
                re.search(r'^\s*#\s*syntax\s*=',text,re.I|re.M)):
                raise ValueError('unsafe_build_recipe')
        platform=self.config.get('platform','linux/arm64')
        if platform not in ('linux/arm64','linux/amd64'):raise ValueError('platform_not_registered')
        tag=profile['image_repository']+':'+workspace.name
        result=self.invoke(['build','--network=none','--pull=false','--platform',platform,'--tag',tag,str(workspace)],timeout=600)
        if result.returncode:raise ValueError('final_build_failed')
        inspected=self.invoke(['image','inspect',tag])
        if inspected.returncode:raise ValueError('manifest_digest_not_observed')
        # Containerd-backed local store must report the repository manifest digest.
        # Image config Id, build log text, and an unimported OCI export are not proof.
        image=final_manifest(json.loads(inspected.stdout)[0],profile['image_repository'])
        if self.observe_manifest(image)!=image:raise ValueError('source_manifest_mismatch')
        return {'image':image,'source_tree_sha256':tree_sha,'manifest_observed':True,'platform':platform}
