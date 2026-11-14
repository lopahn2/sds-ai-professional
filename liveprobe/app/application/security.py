"""사람과 runner는 별도 파일/종류/권한으로 검증한다. 키는 저장·출력하지 않는다."""
import hmac
import json
from pathlib import Path
from .safety import fail


class Identities:
    def __init__(self, settings):
        self.settings = settings

    def _load(self, machine):
        path = self.settings.machine_identity_file if machine else self.settings.identity_file
        if not path:
            return None
        try:
            data = json.loads(Path(path).read_text())
            principals = data['principals']
            if not isinstance(principals, list) or not principals:
                return None
            if not all(isinstance(p, dict) and isinstance(p.get('subject'), str) and p['subject']
                       and isinstance(p.get('token'), str) and len(p['token']) >= 16
                       and isinstance(p.get('scopes'), list) and all(isinstance(s, str) for s in p['scopes']) for p in principals):
                return None
            return principals
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def configured(self, machine=False):
        return self._load(machine) is not None

    def authorized_subject(self, subject, scope):
        principals = self._load(False)
        return bool(principals and any(p['subject'] == subject and scope in p['scopes'] for p in principals))

    def authenticate_creator(self, authorization):
        # Detection can create incidents, never attest/approve as a human.
        if authorization and authorization.startswith('Bearer '):
            token = authorization[7:]
            machine = self._load(True)
            if machine and any(hmac.compare_digest(token, principal['token']) for principal in machine):
                return self.authenticate(authorization, 'incident:create', machine=True)
        return self.authenticate(authorization, 'incident:create')

    def authenticate(self, authorization, scope, *, machine=False):
        own = self._load(machine)
        other = self._load(not machine)
        if authorization and authorization.startswith('Bearer '):
            token = authorization[7:]
            # 하나의 bearer가 두 종류에 등록되어도 사람 권한으로 승격하지 않는다.
            if other and any(hmac.compare_digest(token, p['token']) for p in other):
                fail(403, 'identity_kind_forbidden', '사람과 실행기의 인증 경로는 분리되어 있습니다.')
        if own is None:
            fail(503, 'identity_unconfigured', '필요한 인증 파일이 구성되지 않았습니다.')
        if not authorization or not authorization.startswith('Bearer '):
            fail(401, 'authentication_required', '인증이 필요합니다.')
        token = authorization[7:]
        for principal in own:
            if hmac.compare_digest(token, principal['token']):
                if scope not in principal['scopes']:
                    fail(403, 'scope_forbidden', '이 작업의 권한이 없습니다.')
                return {'subject': principal['subject'], 'kind': 'machine' if machine else 'human'}
        fail(401, 'authentication_invalid', '인증을 확인할 수 없습니다.')
