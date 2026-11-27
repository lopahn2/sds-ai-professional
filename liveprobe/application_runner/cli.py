"""명시적 파일 참조와 로컬 권한 프록시로만 호스트 실행기를 시작한다."""
import argparse
import json
from pathlib import Path
import sys
import time
import urllib.parse
from .profiles import Registry
from .worker import API,Worker,safe_url


def business_reader(registry,kube):
    def read(environment,service,path):
        profile=registry.target({'environment':environment,'service':service})
        if path!=profile['verification']['path']:raise ValueError('unregistered_business_path')
        prefix=f'/api/v1/namespaces/{environment}/services/http:{service}:8080/proxy'
        if profile['language']!='java':
            identity_path='/api/config' if profile['language']=='python' else '/api/info'
            response=kube.http_get(prefix+identity_path);info=response.get('body')
            if response['status']!=200 or not isinstance(info,dict) or info.get('service')!=service or info.get('environment',info.get('env'))!=environment or not info.get('version'):
                raise ValueError('business_version_target_mismatch')
        return kube.http_get(prefix+path)
    return read


def main(argv=None):
    parser=argparse.ArgumentParser(description='Application 전용 제한 호스트 실행기')
    parser.add_argument('command',nargs='?',choices=('run','detect','watch','build-fixture'),default='run')
    parser.add_argument('--environment',choices=('dev','prd'),default='dev')
    parser.add_argument('--service',action='append')
    parser.add_argument('--since')
    parser.add_argument('--fixture')
    parser.add_argument('--confirm-deployment',action='store_true',help='fixture 배포를 읽기 조정한 뒤 표식만 해제')
    parser.add_argument('--deploy',action='store_true',help='build-fixture dev CAS 배포')
    parser.add_argument('--repo-root',type=Path,default=Path(__file__).resolve().parents[2])
    parser.add_argument('--state-dir',type=Path)
    parser.add_argument('--api-url')
    parser.add_argument('--machine-token-file',type=Path)
    parser.add_argument('--runtime-config',type=Path)
    parser.add_argument('--worker-id',default='application-host')
    parser.add_argument('--role',choices=('legacy','source','cluster','qa'),default='legacy',
                        help='통합 모드는 소스/클러스터/QA 실행기를 별도 신원으로 실행')
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--poll-seconds',type=float,default=5)
    args=parser.parse_args(argv)
    registry=Registry(args.repo_root)
    if args.command=='build-fixture' and (args.environment!='dev' or args.fixture!='catalog-title-key'):
        parser.error('등록 fixture는 dev에만 허용됩니다')
    for service in args.service or []:registry.target({'environment':args.environment,'service':service})
    # dry-run은 인증 파일·SDK·Docker를 읽거나 상태 디렉터리를 만들지 않는다.
    if args.dry_run:
        print(json.dumps({'ok':True,'command':args.command,'claimed':False,'dry_run':True,'writes':0,'profiles':[p['id'] for p in registry.profiles.values()]},ensure_ascii=False))
        return 0
    if not all((args.state_dir,args.api_url,args.machine_token_file,args.runtime_config)):
        parser.error('실행에는 --state-dir --api-url --machine-token-file --runtime-config가 필요합니다')
    if args.poll_seconds<1:parser.error('--poll-seconds는 1 이상이어야 합니다')
    from .host import DockerHost
    from .deploy import KubeTransport
    from .executor import Executor
    try:
        config=json.loads(args.runtime_config.read_text())
        config['runner_role']=args.role
        if args.role in ('source','qa') and (args.deploy or args.confirm_deployment or args.command=='build-fixture'):
            raise ValueError('runner_role_job_forbidden')
        # 부모가 인증·RBAC를 적용한 전용 loopback 프록시. 기존 kubeconfig/Secret은 읽지 않는다.
        socket_path=config.get('kube_unix_socket')
        kube_url='http://localhost' if socket_path else safe_url(config['kube_api'])
        if urllib.parse.urlsplit(kube_url).hostname not in ('127.0.0.1','localhost','::1'):
            raise ValueError('local_kubernetes_proxy_required')
        from kubernetes import client
        settings=client.Configuration();settings.host=kube_url
        if socket_path:
            settings.proxy=None
        api=client.ApiClient(settings)
        if socket_path:
            from .kube_proxy import use_private_socket
            use_private_socket(api,socket_path)
        kube=KubeTransport(api,read_only=args.role in ('source','qa'))
        kube.business_reader=business_reader(registry,kube)
        if args.command in ('detect', 'watch'):
            from datetime import datetime,timedelta,timezone
            from .detect import Detector
            if args.command == 'watch':
                detector = Detector(registry, kube, API(args.api_url,args.machine_token_file), args.state_dir/'detect')
                root = args.state_dir/'watch'
                first_since = args.since or datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
                services = args.service or list(registry.profiles)
                while True:
                    try:
                        detector.watch_once(root, services, first_since)
                    except OSError:
                        print(json.dumps({'error': 'application_catalog_or_checkpoint_unavailable'}), flush=True)
                        if args.once:
                            return 1
                    if args.once:return 0
                    time.sleep(args.poll_seconds)
            since=args.since or (datetime.now(timezone.utc)-timedelta(minutes=15)).isoformat().replace('+00:00','Z')
            outcome=Detector(registry,kube,API(args.api_url,args.machine_token_file),args.state_dir/'detect').run(
                args.environment,args.service or list(registry.profiles),since)
            print(json.dumps(outcome,ensure_ascii=False));return 0
        state=args.state_dir.resolve()
        state.mkdir(mode=0o700,parents=True,exist_ok=True)
        docker_config=state/'docker-client'
        docker_config.mkdir(mode=0o700,exist_ok=True)
        # 외부 auth 설정을 읽지 않는 새 전용 client 디렉터리만 쓴다.
        # Permit only the CLI plugin directory supplied by the local Rancher Desktop installation.
        # Registry credentials and host-side Docker configuration are never imported.
        plugins = Path('/Applications/Rancher Desktop.app/Contents/Resources/resources/darwin/docker-cli-plugins')
        cfg_path = docker_config/'config.json'
        allowed = {'cliPluginsExtraDirs': [str(plugins)]}
        if plugins.is_dir():
            if cfg_path.is_symlink():raise ValueError('docker_client_config_must_be_empty')
            if not cfg_path.exists():
                with cfg_path.open('x') as stream:json.dump(allowed, stream)
            if json.loads(cfg_path.read_text()) != allowed:raise ValueError('docker_client_config_must_be_empty')
        if any(path != cfg_path or not plugins.is_dir() for path in docker_config.iterdir()):
            raise ValueError('docker_client_config_must_be_empty')
        config['docker_config']=str(docker_config)
        host=DockerHost(config)
        if args.command=='build-fixture':
            from .fixture import FixtureBuilder
            fixture_builder=FixtureBuilder(registry,host,state/'execution')
            if args.confirm_deployment:
                outcome=fixture_builder.confirm_deployment(kube)
                print(json.dumps(outcome,ensure_ascii=False));return 0 if outcome['ok'] else 1
            outcome=fixture_builder.run(args.fixture,args.environment)
            if args.deploy:
                outcome['deployment']=fixture_builder.deploy(outcome,kube)
                outcome['ok']=outcome['deployment']['ok']
            print(json.dumps(outcome,ensure_ascii=False));return 0 if outcome['ok'] else 1
        executor=Executor(registry,kube,host,state/'execution',config)
        worker=Worker(API(args.api_url,args.machine_token_file),executor,state/'jobs',args.worker_id,role=args.role)
        while True:
            outcome=worker.once()
            print(json.dumps(outcome,ensure_ascii=False),flush=True)
            if args.once:return 0
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # SDK/HTTP/Docker 예외 원문에는 인증/업무값이 있을 수 있다.
        code=str(exc) if isinstance(exc,ValueError) and str(exc).replace('_','').isalnum() and len(str(exc))<100 else 'runner_not_provisioned_or_transport_failed'
        print(json.dumps({'ok':False,'error':code},ensure_ascii=False),file=sys.stderr)
        return 1


if __name__=='__main__':
    sys.exit(main())
