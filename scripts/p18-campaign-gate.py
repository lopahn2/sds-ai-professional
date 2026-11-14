#!/usr/bin/env python3
"""향후 P18 재개용 읽기 전용 기준선 발급기. 과거 r2 발급기·영수증은 수정하지 않는다.

주입·장애 해제·Kubernetes 선언 변경은 없다. 기본 경로에는 모델 호출도 없으며,
--probe-model을 명시한 경우에만 API Pod의 구독형 Codex CLI로 별도 가용성을 점검한다.
최적화 실행을 거부하고 명시적 21대상 관측과 실제 최근 모델 성공 근거만 발급한다.
발급기/실행기 지문은 로컬 파일 계약이며 악의적인 로컬 파일 변조의 암호학적 증명은 아니다.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import argparse
import fcntl
import hashlib
import importlib.util
import json
import re
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / 'docs/evidence/P18/campaign/runs-r2'
SCHEMA = 'liveprobe_campaign_gate/v2'
PAYMENT04 = {
    'attempts/payment-04-20261026T010357668389Z/result.json': 'd0cc9a1a9f17fb7e4256ed75c8127a23a2845a33d23b183f8e927f6240b432b2',
    'attempts/payment-04-20261027T121850213802Z/result.json': '8b58c197476ac12ecb2b0c9103ad38777fc69d6a3483a303f3ae88d3c3363b4f',
    'payment-04/result.json': '58bf3a2dcf8de1df484df33a3f0203a8232fc753de6fab3d827915be54c1a177',
}


class GateError(ValueError):
    """명시적 기준선 검증 실패."""


def require(value, message):
    if not value:
        raise GateError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_observation(dep, health, chaos, business, baseline_equal):
    wanted = dep['spec']['replicas']
    status = dep.get('status', {})
    require(type(wanted) is int and wanted > 0, '목표 복제수 오류')
    require(status.get('observedGeneration', 0) >= dep['metadata']['generation'] and
            all(status.get(k, 0) == wanted for k in ('replicas', 'readyReplicas', 'updatedReplicas')), 'Deployment 준비 미확인')
    require(health.get('status') == 200 and chaos.get('status') == 200, '건강/장애 상태 HTTP 오류')
    body = chaos.get('body')
    require(isinstance(body, dict) and 'active' in body and 'release_active' in body and
            body['active'] in ({}, []) and body['release_active'] in ({}, []), '장애 활성 또는 상태 결측')
    require(business.get('verified') is True and bool(business.get('probes')) and
            all(p.get('status') == 200 for p in business['probes']), '업무 건강 미확인')
    require(baseline_equal is True, '원래 기준선과 불일치')
    return True


def parse_time(value):
    require(isinstance(value, str), '성공 시각 결측')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(result.tzinfo is not None, '성공 시각 시간대 결측')
    return result


def model_evidence(calls, accounts, clock):
    successes = [c for c in calls if c.get('status') == 'succeeded' and
                 type(c.get('input_tokens')) is int and c['input_tokens'] >= 0 and
                 type(c.get('output_tokens')) is int and c['output_tokens'] >= 0]
    latest = max((parse_time(c.get('created_at')) for c in successes), default=None)
    age = (clock - latest).total_seconds() if latest is not None else None
    fresh = age is not None and 0 <= age <= 1800
    return {'latest_success_at': latest.isoformat() if latest else None,
            'actual_model_success': bool(successes) and fresh and any(a.get('state') == 'available' for a in accounts),
            'succeeded_calls_observed': len(successes), 'accounts': accounts,
            'scope': '검증에 제공된 실제 성공 근거. 기본 경로는 최근 AgentRun, 명시 점검은 별도 실제 구독 CLI 반환값. 시나리오 비용과 분리.'}


def api(path):
    with urlopen('http://liveprobe.localhost' + path, timeout=3) as response:
        return json.load(response)


RUNTIME_PROBE_CODE = r'''
from dataclasses import asdict
from datetime import datetime, timezone
import json, os, sys
from app.config import Settings
from app.llm.codex_cli import CodexCLIProvider
from app.llm.base import LLMError
account = sys.argv[1]
record = {'schema':'liveprobe_runtime_model_probe/v1','success':False,'provider':'codex_cli',
          'account':account,'model':'gpt-6.1-sol','effort':'xhigh','runtime_pod':os.environ.get('HOSTNAME'),
          'response_ok':False,'usage':None,'duration_ms':None}
try:
    settings = Settings()
    home = settings.codex_home if account == 'primary' else settings.codex_home_spare
    if not home: raise LLMError('config','점검할 구독 계정이 설정되지 않음')
    provider = CodexCLIProvider(settings.codex_bin, home)
    result = provider.complete(role='availability_probe', prompt='가용성 점검이다. 도구를 사용하지 말고 ready가 true인 JSON만 응답하라.',
        schema={'type':'object','properties':{'ready':{'type':'boolean'}}}, model='gpt-6.1-sol', effort='xhigh', timeout=60)
    record.update(provider=result.provider, model=result.model, effort=result.effort,
                  response_ok=result.data == {'ready':True}, usage=asdict(result.usage), duration_ms=result.duration_ms)
    record['success'] = record['response_ok']
except LLMError as error:
    record.update(error_kind=error.kind, usage=asdict(error.usage), duration_ms=error.duration_ms)
except Exception:
    record['error_kind'] = 'runtime'
record['at'] = datetime.now(timezone.utc).isoformat()
print(json.dumps(record, ensure_ascii=False))
raise SystemExit(0 if record['success'] else 1)
'''


def runtime_model_probe(account):
    """운영자가 명시한 경우만 실제 API Pod에서 구독 CLI 1회를 점검한다. 원문/인증값 출력 금지."""
    require(account in ('primary', 'spare'), '허용하지 않은 점검 계정')
    command = ['kubectl', '--context', 'rancher-desktop', '-n', 'liveprobe-system', 'exec', '-i',
               'deployment/liveprobe-api', '-c', 'api', '--', 'env', 'PYTHONPATH=/app',
               '/app/.venv/bin/python', '-B', '-', account]
    record = {'schema': 'liveprobe_runtime_model_probe/v1', 'success': False, 'provider': 'codex_cli',
              'account': account, 'model': 'gpt-6.1-sol', 'effort': 'xhigh', 'usage': None,
              'duration_ms': None, 'at': None, 'runtime_pod': None, 'kubectl_exit': None,
              'scope': '실제 구독 CLI 가용성 점검. AgentRun을 생성하거나 API 응답을 합성하지 않음. 시나리오 비용과 별도.'}
    try:
        process = subprocess.run(command, input=RUNTIME_PROBE_CODE, text=True, capture_output=True, timeout=90)
    except subprocess.TimeoutExpired:
        return {**record, 'error_kind': 'timeout'}
    except OSError:
        return {**record, 'error_kind': 'transport'}
    record['kubectl_exit'] = process.returncode
    try:
        payload = json.loads(process.stdout)
        require(isinstance(payload, dict), '점검 결과 형식 오류')
        usage = payload.get('usage') or {}
        record['usage'] = {k: usage.get(k) if type(usage.get(k)) is int and usage[k] >= 0 else None
                           for k in ('input_tokens', 'output_tokens', 'cached_input_tokens', 'reasoning_output_tokens')}
        record['duration_ms'] = payload.get('duration_ms') if type(payload.get('duration_ms')) is int else None
        pod = payload.get('runtime_pod')
        record['runtime_pod'] = pod if isinstance(pod, str) and re.fullmatch(r'[a-z0-9.-]{1,253}', pod) else None
        stamp = parse_time(payload.get('at'))
        record['at'] = stamp.isoformat()
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        record['success'] = (process.returncode == 0 and payload.get('schema') == record['schema']
                             and payload.get('success') is True and payload.get('response_ok') is True
                             and payload.get('provider') == 'codex_cli' and payload.get('model') == 'gpt-6.1-sol'
                             and payload.get('effort') == 'xhigh' and payload.get('account') == account
                             and record['runtime_pod'] is not None and 0 <= age <= 1800
                             and all(record['usage'][k] is not None for k in ('input_tokens', 'output_tokens')))
        if not record['success']:
            kind = payload.get('error_kind')
            record['error_kind'] = kind if kind in ('auth', 'quota', 'timeout', 'config', 'provider', 'invalid_output', 'runtime') else 'invalid_probe'
    except (ValueError, TypeError, KeyError, AttributeError):
        record['error_kind'] = 'invalid_probe'
    return record


def issue(args):
    source = ROOT / 'scripts/p18-campaign-run.py'
    require(sha(source) == args.expected_runner, '실행기 지문 불일치')
    spec = importlib.util.spec_from_file_location('campaign_gate_runner', source)
    if spec is None or spec.loader is None:
        raise GateError('실행기 로더 없음')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    manifest = ROOT / 'docs/evidence/P18/campaign/build-r2/build-summary.json'
    require(sha(manifest) == json.loads((RUNS / 'run-contract.json').read_text())['build_manifest_sha256'], '빌드 지문 불일치')
    require({rel: sha(RUNS / rel) for rel in PAYMENT04} == PAYMENT04, 'payment04 원본 지문 변경')
    baseline = json.loads(args.dev_baseline.read_text())
    old = json.loads((ROOT / 'docs/evidence/P18/campaign/continuation/resume-20261026/preflight.json').read_text())
    reference = {(x['namespace'], x['name']): x for x in old['stg_prd']}
    with (RUNS / '.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        h = runner.Harness(SimpleNamespace(context='rancher-desktop', namespace='dev', output=args.output / 'readonly', api='http://liveprobe.localhost'))
        local = h.local

        def readonly(name, path, method='GET', data=None, timeout=6):
            require(method == 'GET' and data is None, '읽기 전용 경계 위반')
            return local(name, path, method, data, timeout)

        h.local = readonly
        h.node_guard()
        ready = api('/api/readyz')
        require(ready.get('background') is True and ready.get('incident_worker') ==
                {'alive': True, 'cleanup_pending': False, 'ready': True}, 'worker 미준비')
        require(not [i for i in h.incident_listing() if i['namespace'] == 'dev' and i['status'] not in runner.TERMINAL], 'dev 활성 사건 존재')
        rows = []
        for namespace in ('dev', 'stg', 'prd'):
            h.ns = namespace
            deployments = {n: h.dep(n) for n in runner.SERVICES}
            runner.verify_deployed_builds(json.loads(manifest.read_text()), deployments)

            def check(name):
                dep = deployments[name]
                images = [c['image'] for c in dep['spec']['template']['spec']['containers']]
                if namespace == 'dev':
                    equal = not runner.restore_ops(baseline[name], dep)
                else:
                    ref = reference[namespace, name]
                    equal = dep['metadata']['uid'] == ref['uid'] and dep['metadata']['generation'] == ref['generation'] and images == ref['images']
                health, chaos, business = h.health(name), h.chaos(name), h.business_health(name)
                check_observation(dep, health, chaos, business, equal)
                pods = h.pods(name)
                require(bool(pods) and not any(p['metadata'].get('deletionTimestamp') for p in pods), 'Pod 없음 또는 종료 중')
                return {'namespace': namespace, 'name': name, 'uid': dep['metadata']['uid'], 'generation': dep['metadata']['generation'],
                        'images': images, 'ready': True, 'baseline_fields_equal': True, 'chaos_active': [], 'release_active': [],
                        'health_http_status': health['status'], 'business_verified': True,
                        'business_http_statuses': [p['status'] for p in business['probes']]}

            with ThreadPoolExecutor(max_workers=7) as pool:
                rows.extend(pool.map(check, runner.SERVICES))
        require(len(rows) == 21 and len({(x['namespace'], x['name']) for x in rows}) == 21, '21대상 관측 불완전')
        items = api('/api/incidents?limit=40')['items']
        with ThreadPoolExecutor(max_workers=6) as pool:
            details = list(pool.map(lambda i: api('/api/incidents/' + str(i['id'])), items))
        calls = [c for d in details for c in d.get('agent_runs', [])]
        accounts = [{'name': a['name'], 'state': a['state']} for a in api('/api/llm/accounts')['accounts']]
        explicit_probe = bool(getattr(args, 'probe_model', False))
        if explicit_probe:
            probe = runtime_model_probe(args.probe_account)
            runner.write_json(args.output / 'model-runtime-probe.json', probe)
            require(probe['success'] is True, '실제 구독 CLI 가용성 점검 실패')
            # 실제 별도 점검의 반환값이다. AgentRun이나 API 응답으로 가장하지 않는다.
            calls = [{'status': 'succeeded', 'created_at': probe['at'], **probe['usage']}]
            accounts = [{'name': a['name'], 'state': a['state']} for a in api('/api/llm/accounts')['accounts']]
        clock = datetime.now(timezone.utc)
        model = model_evidence(calls, accounts, clock)
        model['evidence_source'] = 'explicit_codex_runtime_probe' if explicit_probe else 'observed_agent_runs'
        model['new_model_call'] = explicit_probe
        if explicit_probe:
            model['runtime_probe_sha256'] = sha(args.output / 'model-runtime-probe.json')
        require(model['actual_model_success'] is True, '실제 최근 모델 성공 미확인')
        issuer = {'path': 'scripts/p18-campaign-gate.py', 'sha256': sha(Path(__file__)), 'optimize': sys.flags.optimize}
        common = {'schema': SCHEMA, 'at': clock.isoformat(), 'issuer': issuer, 'runner_sha256': sha(source),
                  'read_only': True, 'new_injection_performed': False}
        runner.write_json(args.output / 'campaign-model-gate.json', {**common, **model})
        runner.write_json(args.output / 'campaign-readonly-gate.json', {**common, 'gate': 'pass', 'deployments': rows,
                          'build_manifest_sha256': sha(manifest), 'dev_baseline_sha256': sha(args.dev_baseline),
                          'active_dev_incidents': [], 'payment04_results_sha256': PAYMENT04,
                          'payment04_actual_injections': 3, 'payment04_remaining_attempts': 0})


def main(argv=None):
    if sys.flags.optimize:
        sys.stderr.write('최적화 실행 거부: 기준선 영수증 발급 없음\n')
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expected-runner', required=True)
    parser.add_argument('--dev-baseline', type=Path, required=True)
    parser.add_argument('--probe-model', action='store_true', help='실제 API Pod에서 구독 CLI 가용성 1회 점검(별도 토큰 사용)')
    parser.add_argument('--probe-account', choices=('primary', 'spare'), default='primary')
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=False)
    try:
        issue(args)
    except Exception as error:
        sys.stderr.write('기준선 발급 거부: ' + type(error).__name__ + '\n')
        return 3
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
