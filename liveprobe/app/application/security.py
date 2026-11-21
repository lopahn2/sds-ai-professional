"""사람과 runner는 별도 파일/종류/권한으로 검증한다. 키는 저장·출력하지 않는다."""
import hashlib
import hmac
import json
from pathlib import Path
from .safety import fail
from .. import local_operator
from ..human_sessions import SCOPES


class Identities:
    def __init__(self, settings, *, settings_provider=None):
        self._settings_provider = settings_provider if settings_provider is not None else lambda: settings

    @property
    def settings(self):
        return self._settings_provider()

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
            if any(p['subject'] == local_operator.SUBJECT for p in principals):
                return None
            return principals
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def configured(self, machine=False):
        return (not machine and self.operator_key() is not None) or self._load(machine) is not None

    def operator_key(self):
        return local_operator.operator_key(self.settings.local_operator_key_file, self.settings.local_read_key_file)

    def authorized_subject(self, subject, scope):
        if subject == local_operator.SUBJECT:
            return scope in SCOPES and self.operator_key() is not None
        principals = self._load(False)
        return bool(principals and any(p['subject'] == subject and scope in p['scopes'] for p in principals))

    def credential_scopes(self, subject, fingerprint):
        """Recheck the exact human credential used at login, including a subsequent revocation or rotation."""
        def matches(record):
            return hmac.compare_digest(hashlib.sha256(record['token'].encode()).hexdigest(), fingerprint)
        if any(matches(p) for p in self._load(True) or ()):
            return ()
        principal = next((p for p in self._load(False) or () if matches(p)), None)
        return principal['scopes'] if principal and principal['subject'] == subject else ()

    def human_scopes(self, subject):
        """사람 파일에 등록된 주체의 현재 scopes."""
        principals = self._load(False) or []
        return sorted({scope for p in principals if p['subject'] == subject for scope in p['scopes']})

    def authenticate_human(self, authorization):
        """역할 세션 승인·폐기(CLI): 사람 파일 주체와 현재 scopes. 실행기 자격은 identity_kind_forbidden이다."""
        principals = self._load(False)
        machine = self._load(True)
        if authorization and authorization.startswith('Bearer '):
            token = authorization[7:]
            if machine and any(hmac.compare_digest(token, p['token']) for p in machine):
                fail(403, 'identity_kind_forbidden', '사람과 실행기의 인증 경로는 분리되어 있습니다.')
        if principals is None:
            fail(503, 'identity_unconfigured', '필요한 인증 파일이 구성되지 않았습니다.')
        if not authorization or not authorization.startswith('Bearer '):
            fail(401, 'authentication_required', '인증이 필요합니다.')
        token = authorization[7:]
        for principal in principals or ():
            if hmac.compare_digest(token, principal['token']):
                return {'subject': principal['subject'], 'kind': 'human', 'scopes': list(principal['scopes'])}
        fail(401, 'authentication_invalid', '인증을 확인할 수 없습니다.')

    def authenticate_creator(self, authorization):
        # Detection can create incidents, never attest/approve as a human.
        if authorization and authorization.startswith('Bearer '):
            token = authorization[7:]
            machine = self._load(True)
            if machine and any(hmac.compare_digest(token, principal['token']) for principal in machine):
                return self.authenticate(authorization, 'incident:create', machine=True)
        return self.authenticate(authorization, 'incident:create')

    def runner(self, authorization):
        """Authenticate identity and load server-owned role scopes, never client capabilities."""
        actor = self.authenticate(authorization, 'runner', machine=True)
        rows = self._load(True) or []
        principals = [row for row in rows if row['subject'] == actor['subject']]
        # A duplicate subject must not bind two independently privileged tokens to one lease.
        if len(principals) != 1:
            fail(403, 'runner_subject_ambiguous')
        scopes = principals[0]['scopes']
        roles = [role for role in ('source', 'cluster', 'qa') if 'runner:' + role in scopes]
        if len(roles) > 1 or (self.settings.unified_mode and len(roles) != 1):
            fail(403, 'runner_role_separation_required', '소스 실행기와 배포 실행기는 별도 계정이어야 합니다.')
        return {**actor, 'runner_role': roles[0] if roles else None}

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
