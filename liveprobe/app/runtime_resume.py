"""읽기 단계 재사용과 결과 불명 쓰기 조정의 입력 지문 계약."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, default=str).encode()).hexdigest()


def read_step(state, name, input_sha256, execute):
    previous = state.get(name)
    if previous:
        if previous.get('fingerprint') != input_sha256:
            raise ValueError('실행 입력 지문이 달라 재사용 불가')
        if previous.get('outcome') == 'completed':
            return deepcopy(previous['result'])
    result = execute()
    state[name] = {'fingerprint': input_sha256, 'outcome': 'completed', 'result': deepcopy(result)}
    return result


def write_step(state, name, input_sha256, execute, reconcile):
    previous = state.get(name)
    if previous and previous.get('fingerprint') != input_sha256:
        raise ValueError('쓰기 입력 지문 충돌')
    if previous and previous.get('outcome') == 'written':
        return deepcopy(previous['result'])
    if previous and previous.get('outcome') in ('unknown', 'started'):
        result = reconcile()
    else:
        state[name] = {'fingerprint': input_sha256, 'outcome': 'started'}
        try:
            result = execute()
        except Exception:
            state[name] = {'fingerprint': input_sha256, 'outcome': 'unknown'}
            raise
    state[name] = {'fingerprint': input_sha256, 'outcome': result.get('outcome', 'unknown'), 'result': deepcopy(result)}
    return result


def source_fingerprint():
    root = Path(__file__).resolve().parent
    paths = list(root.glob('*.py')) + list((root / 'llm').glob('*.py'))
    for pattern in ('requirements*.txt', 'pyproject.toml', 'uv.lock', 'poetry.lock'):
        paths.extend(root.parent.glob(pattern))
    return fingerprint({str(path.relative_to(root.parent)): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in sorted(set(paths))})
