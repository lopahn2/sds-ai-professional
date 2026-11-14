"""기록/모델 입력 정제와 비밀 원문을 포함하지 않는 고정 오류."""
import hashlib
import json
import re
from fastapi import HTTPException

SENSITIVE = re.compile(r'(?i)(password|passwd|token|api[_-]?key|secret|authorization|credential)')
ASSIGNMENT = re.compile(r'''(?i)((?:password|passwd|token|api[_-]?key|secret|authorization|credential)\s*[=:]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;]+)''')
BEARER = re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+')
DSN = re.compile(r'\b(?:postgres(?:ql)?|mysql|redis)(?:\+[a-z]+)?://[^\s]+', re.I)


SAFE_EXTENSIONS = {'.py', '.java', '.js', '.json', '.xml', '.properties', '.sql', '.html', '.css', '.lock', '.toml', '.md', '.txt'}
DENIED_PATH = re.compile(r'(secret|private|credential|oauth|password|token|auth|dump|conftest)', re.I)


def safe_source_path(path):
    from pathlib import PurePosixPath
    if not isinstance(path, str) or not path or '\\' in path or '\x00' in path:
        return False
    parts = path.split('/')
    if any(not part or part in ('.', '..') or part.startswith('.') or DENIED_PATH.search(part) for part in parts):
        return False
    parsed = PurePosixPath(path)
    return not parsed.is_absolute() and (parsed.suffix.lower() in SAFE_EXTENSIONS or parsed.name == 'Dockerfile')


def test_file(test_id):
    """Canonical regression ID -> source-relative test file, never a command."""
    if not isinstance(test_id, str) or len(test_id) > 1000:
        return None
    if '::' in test_id:
        path, name = test_id.split('::', 1)
        if (not safe_source_path(path) or not name or any(ord(c) < 32 for c in name) or
            not ((path.startswith('tests/') and path.endswith('.py') and
                  re.fullmatch(r'[A-Za-z_]\w*(?:::[A-Za-z_]\w*)?', name)) or
                 (path.startswith('test/') and path.endswith('.js')))):
            return None
        return path
    if re.fullmatch(r'(?:[A-Za-z_]\w*\.)+[A-Za-z_]\w*#[A-Za-z_]\w*', test_id):
        path = 'src/test/java/' + test_id.split('#')[0].replace('.', '/') + '.java'
        return path if safe_source_path(path) else None
    return None


def scrub(value):
    if isinstance(value, str):
        return DSN.sub('[정제된 연결정보]', BEARER.sub('Bearer [정제됨]', ASSIGNMENT.sub(r'\1[정제됨]', value)))[:100000]
    if isinstance(value, dict):
        counters = {'input_tokens', 'output_tokens', 'cached_input_tokens', 'reasoning_output_tokens'}
        return {str(k): (v if v is None or type(v) is int and v >= 0 else None) if k in counters
                else '[정제됨]' if SENSITIVE.search(str(k)) else scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value[:2000]]
    return value


def sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def text_sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def fail(status, code, message='요청을 안전하게 처리할 수 없습니다.'):
    raise HTTPException(status, detail={'code': code, 'message_ko': message})
