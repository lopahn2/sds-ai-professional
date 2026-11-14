"""로컬 Docker 전용: 고정 Node22·network none·128MiB 실제 재시작/OOM 검증."""
import json
import pathlib
import subprocess
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[3]
BASE = 'node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402'

def run(*args, timeout=15):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{args[:3]} exit={result.returncode}: {result.stderr}')
    return result.stdout.strip()


def request(name, route, method='GET', body=None):
    expression = f"const r=await fetch('http://127.0.0.1:8080'+{json.dumps(route)},{{method:{json.dumps(method)},headers:{{'content-type':'application/json'}},body:{json.dumps(json.dumps(body)) if body is not None else 'undefined'}}});console.log(JSON.stringify({{status:r.status,body:await r.json()}}))"
    return json.loads(run('docker', 'exec', name, 'node', '--input-type=module', '-e', expression))


def ready(name):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        try:
            return request(name, '/api/chaos')
        except (RuntimeError, json.JSONDecodeError):
            time.sleep(.1)
    raise RuntimeError('서버 준비 제한 초과')


def stopped(name):
    run('docker', 'wait', name, timeout=12)
    state = json.loads(run('docker', 'inspect', '--format', '{{json .State}}', name))
    deadline = time.monotonic() + 2
    while state['ExitCode'] == 137 and not state['OOMKilled'] and time.monotonic() < deadline:
        time.sleep(.1)
        state = json.loads(run('docker', 'inspect', '--format', '{{json .State}}', name))
    return {'exit': state['ExitCode'], 'oom': state['OOMKilled']}


def probe(service):
    name = f'p18-node-campaign-{service}-{uuid.uuid4().hex[:8]}'
    mount = str(ROOT / 'services' / service) + ':/app:ro'
    results = {'service': service, 'base': BASE}
    try:
        run('docker', 'run', '-d', '--name', name, '--pull=never', '--network=none', '--memory=128m', '--memory-swap=128m', '--user=node', '-v', mount, '-w', '/app', '-e', 'SELF_TRAFFIC=0', '-e', 'FAULT_STATE_DIR=/home/node/fault-state', BASE, 'node', 'server.js')
        ready(name)
        request(name, '/api/chaos', 'POST', {'fault': 'crash'})
        results['runtime_crash_first'] = stopped(name)
        run('docker', 'start', name)
        results['runtime_crash_restart'] = stopped(name)
        assert results['runtime_crash_first']['exit'] == results['runtime_crash_restart']['exit'] == 1
        run('docker', 'rm', name)
        run('docker', 'run', '-d', '--name', name, '--pull=never', '--network=none', '--memory=128m', '--memory-swap=128m', '--user=node', '-v', mount, '-w', '/app', '-e', 'SELF_TRAFFIC=0', '-e', 'FAULT_STATE_DIR=/home/node/fault-state', BASE, 'node', 'server.js')
        fresh = ready(name)
        assert fresh['body']['active'] == []
        results['replacement_active'] = fresh['body']['active']
        request(name, '/api/chaos', 'POST', {'fault': 'memory_leak', 'params': {'mib_per_second': 64}})
        results['runtime_oom_first'] = stopped(name)
        run('docker', 'start', name)
        results['runtime_oom_restart'] = stopped(name)
        print(json.dumps(results, ensure_ascii=False), flush=True)
        # 런타임별 OOMKilled 플래그 편차를 숨기지 않고 별도 증거로 반환한다.
        assert all(results[key]['exit'] == 137 for key in ['runtime_oom_first', 'runtime_oom_restart'])
        return results
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True)


if __name__ == '__main__':
    print(json.dumps([probe(service) for service in ['member', 'delivery']], ensure_ascii=False, indent=2))
