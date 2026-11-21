"""dev-qa-prd/v3: 실제 dev 화면 QA 영수증, prd 위험 그림, 혼합 원인 인프라 관문, 배포 후 복구 확인, 보완 회차.

판정은 실행기가 보고한 영수증을 서버가 가진 release 지문·소스 스냅숏·대상·관측 창과 대조해서만 한다.
영수증은 잘라 내지 않는다. 크기·지문·민감 정보 검사를 통과하면 원형 그대로 보존하고, 통과하지 못하면 거부 사유와
최소 식별 정보만 남긴다. 과거 v2 기록(사람 dev 시험 대기 포함)은 그대로 두며 새 QA 영수증을 만들지 않는다.
"""
import copy
import json
import math
import re
import uuid
from datetime import datetime, timezone
from sqlalchemy import select
from ..environments import QA_RELEASE_PROTOCOL
from .safety import fail, scrub, sha, text_sha
from .storage import Job

HEX = re.compile(r'^[a-f0-9]{64}$')
CODE = re.compile(r'^[a-z][a-z0-9_]{1,100}$')
STEP_STATUSES = ('passed', 'failed', 'not_run', 'unverifiable')
QA_OUTCOMES = ('passed', 'failed', 'unverifiable', 'tests_missing')
QA_PROTOCOL = 'qa-browser/v1'
RECOVERY_PROTOCOL = 'prd-recovery/v1'
RECOVERY_OUTCOMES = ('passed', 'failed', 'unverifiable')
RECOVERY_ATTEMPTS = 3
RECOVERY_RETRY_SECONDS = 60
FOLLOWUP_ONLY_STATES = ('completed',)
RECEIPT_MAX_BYTES = 200_000
# 실행기 helper(recovery_probe.GET_OPERATIONS)가 허용하는 등록 업무 읽기. 이 밖의 서비스는 복구 판정 수단이 없다.
RECOVERY_KINDS = {'order', 'stock', 'catalog', 'payment', 'notification', 'delivery'}
PRD_WRITE_CHAIN = ('prd_deploy_queued', 'prd_verifying', 'prd_verified', 'awaiting_recovery')
DATA_PATH = re.compile(r'(?i)(^|/)(db/migration|migrations?|alembic|flyway|liquibase|schema)(/|$)|\.sql$')
DATA_ACCESS = re.compile(r'(?i)\b(insert\s+into|update\s+\w+\s+set|delete\s+from|create\s+table|alter\s+table|jdbc|'
                         r'executeUpdate|executeQuery|repository|entitymanager|session\.(add|delete|commit|merge)|'
                         r'cursor\.execute|\.execute\(|\.commit\(|sqlite3|psycopg|sqlalchemy|redis|kafka|producer\.send)')
DESTRUCTIVE = re.compile(r'(?i)\b(drop\s+(table|column|schema|index|database|constraint)|truncate\b|delete\s+from'
                         r'|alter\s+table\s+\S+\s+(drop|rename)|rename\s+(column|table|to)\b|update\s+\S+\s+set\b)')
BUILD_FILES = re.compile(r'(?i)(^|/)(Dockerfile|pom\.xml|package(-lock)?\.json|pyproject\.toml|requirements[^/]*\.(txt|lock))$')
# 회차를 닫을 때 다음 회차가 새로 만들어야 하는 키. patch·analysis는 다음 회차의 문맥으로 남긴다.
ROUND_KEYS = ('release', 'test_build', 'merge_review', 'qa_history', 'recovery_history', 'infra_history',
              'patch_revisions', 'isolated_build_retry')


def text(value, limit=240):
    return str(scrub(value))[:limit] if isinstance(value, str) and value.strip() else None


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def hexval(value):
    return value if isinstance(value, str) and HEX.fullmatch(value) else None


# ---------------------------------------------------------------- pure projections shared by both APIs

WORK_STATES = {'approval': ('awaiting_prd_approval', 'awaiting_user_test'),
               'qa_fix': ('qa_failed',), 'recovery': ('awaiting_recovery',)}


def work_of(state):
    return next((work for work, states in WORK_STATES.items() if state in states), None)


def mega_stage(state, document=None):
    data = (document or {}).get('data') or {}
    release = data.get('release') or {}
    if state in ('completed','recovery_review'):
        return 'done'
    if state in ('failed', 'handoff'):
        return 'stopped'
    if state == 'awaiting_prd_approval':
        return 'approval'
    if state in PRD_WRITE_CHAIN:
        return 'recovery'
    if state == 'reconciliation_required':
        return 'recovery' if (release.get('approval') or {}).get('consumed_by_job_id') else 'fix'
    if state in ('qa_queued', 'qa_passed', 'qa_failed', 'awaiting_user_test') or (
            state == 'prd_targets_collecting' and release.get('protocol') == QA_RELEASE_PROTOCOL):
        return 'qa'
    if state in ('patch_proposed', 'test_build_queued', 'test_build_passed', 'dev_targets_collecting',
                 'dev_deploy_queued', 'dev_verifying', 'dev_verified', 'dev_infra_pending', 'dev_infra_verified',
                 'prd_targets_collecting'):
        return 'fix'
    return 'analysis'


def outcome_view(record):
    if not isinstance(record, dict):
        return None
    return {'outcome': record.get('outcome'), 'accepted': record.get('accepted'), 'observed_at': record.get('observed_at')}


def projection(document, state):
    data = document.get('data') or {}
    release = data.get('release') or {}
    return {'work': work_of(state), 'mega_stage': mega_stage(state, document), 'round': data.get('round', 1),
            'protocol': release.get('protocol'), 'qa': outcome_view(release.get('qa')),
            'recovery': outcome_view(release.get('recovery'))}


# ---------------------------------------------------------------- version-pinned source of the release

def patched_files(source_files, diff):
    """In-memory mirror of the runner's exact patch rules. None means the tested tree cannot be reconstructed."""
    if not isinstance(diff, str) or not isinstance(source_files, dict):
        return None
    lines = diff.splitlines(keepends=True)
    changes, i = {}, 0
    try:
        while i < len(lines):
            if not lines[i].startswith('--- ') or i + 1 >= len(lines) or not lines[i + 1].startswith('+++ '):
                return None
            old, new = lines[i][4:].strip(), lines[i + 1][4:].strip()
            i += 2
            if not new.startswith('b/') or (old != '/dev/null' and old != 'a/' + new[2:]) or new[2:] in changes:
                return None
            name = new[2:]
            original = [] if old == '/dev/null' else source_files.get(name)
            if original is None:
                return None
            original = original.splitlines(keepends=True) if isinstance(original, str) else original
            output, cursor, hunks = [], 0, 0
            while i < len(lines) and lines[i].startswith('@@ '):
                m = re.fullmatch(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n?', lines[i])
                if not m:
                    return None
                count, newcount = int(m[2] or 1), int(m[4] or 1)
                offset = max(0, int(m[1]) - 1)
                if offset < cursor or offset > len(original):
                    return None
                output.extend(original[cursor:offset])
                cursor, consumed, produced = offset, 0, 0
                i += 1
                while i < len(lines) and not lines[i].startswith(('@@ ', '--- ')):
                    line = lines[i]
                    if not line or line[0] not in ' +-':
                        return None
                    if line[0] in ' -':
                        if cursor >= len(original) or original[cursor] != line[1:]:
                            return None
                        cursor += 1
                        consumed += 1
                    if line[0] in ' +':
                        output.append(line[1:])
                        produced += 1
                    i += 1
                if consumed != count or produced != newcount:
                    return None
                hunks += 1
            if not hunks:
                return None
            output.extend(original[cursor:])
            changes[name] = ''.join(output)
    except (TypeError, ValueError, IndexError):
        return None
    return changes or None


def released_file_hashes(document):
    """Exact file hashes of the built tree, only when they reproduce the build receipt's source_tree_sha256.

    Unchanged files use the snapshot's runner-observed hashes. Changed files are rebuilt from the stored patch;
    if stored text was altered (scrubbed/limited), the tree digest no longer matches and the proof fails closed.
    """
    data = document.get('data') or {}
    source, patch = data.get('source_snapshot') or {}, data.get('patch') or {}
    artifact = (data.get('release') or {}).get('artifact') or {}
    hashes = source.get('file_sha256')
    changes = patched_files(source.get('files'), patch.get('diff'))
    if changes is None or not isinstance(hashes, dict):
        return None
    hashes = {**hashes, **{path: text_sha(body) for path, body in changes.items()}}
    return hashes if sha(hashes) == artifact.get('source_tree_sha256') else None


# ---------------------------------------------------------------- receipts

def receipt_digest_valid(raw):
    try:
        body = {k: v for k, v in raw.items() if k != 'receipt_id'}
        return hexval(raw.get('receipt_id')) is not None and sha(body) == raw['receipt_id']
    except (TypeError, ValueError):
        return False


def preserved(raw):
    """Whole receipt or None. Oversized or secret-bearing receipts are refused, never truncated or rewritten."""
    try:
        size = len(json.dumps(raw, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError):
        return None, 'qa_receipt_invalid'
    if size > RECEIPT_MAX_BYTES:
        return None, 'receipt_too_large'
    if scrub(raw) != raw:
        return None, 'receipt_contains_sensitive_value'
    return copy.deepcopy(raw), None


def proof_passed(raw):
    """Final DOM + matching live network response + allowed independent read for every executed flow."""
    flows = raw.get('flows')
    if isinstance(flows, list) and flows:
        for flow in flows:
            if not isinstance(flow, dict) or flow.get('outcome') != 'passed':
                return False
            net, dom, read = flow.get('network') or {}, flow.get('dom') or {}, flow.get('independent_read') or {}
            if (type(net.get('status')) is not int or net.get('method') != 'GET' or not hexval(net.get('body_sha256'))
                    or not hexval(dom.get('text_sha256')) or not hexval(flow.get('screenshot_sha256'))
                    or read.get('matched') is not True or not hexval(read.get('body_sha256'))
                    or any(not isinstance(s, dict) or s.get('status') != 'passed' for s in flow.get('steps') or [None])):
                return False
        return True
    steps = raw.get('steps') or []
    return bool(steps) and all(isinstance(s, dict) and s.get('status') == 'passed' and hexval(s.get('dom_sha256'))
                               and hexval(s.get('network_sha256')) and hexval(s.get('independent_sha256'))
                               and hexval(s.get('screenshot_sha256')) and type(s.get('http_status')) is int for s in steps)


def qa_verdict(raw, release, document, profile, allowed_origins, now=None):
    """(record to store, rejection code). The record keeps the runner receipt intact when it is safe to keep."""
    if not isinstance(raw, dict):
        return {'outcome': None}, 'qa_receipt_invalid'
    kept, code = preserved(raw)
    minimal = {k: raw.get(k) for k in ('protocol', 'outcome', 'release_id', 'receipt_id', 'observed_at')
               if type(raw.get(k)) in (str, int, float) and len(str(raw.get(k))) <= 200}
    if kept is None:
        return {**minimal, 'outcome': minimal.get('outcome') if minimal.get('outcome') in QA_OUTCOMES else None}, code
    record = kept
    artifact = release.get('artifact') or {}
    if raw.get('protocol') != QA_PROTOCOL or raw.get('outcome') not in QA_OUTCOMES:
        return record, 'qa_receipt_invalid'
    if not receipt_digest_valid(raw):
        return record, 'qa_receipt_digest_mismatch'
    if any(raw.get(k) != release.get(k) for k in ('release_id', 'artifact_sha256', 'plan_sha256')):
        return record, 'qa_receipt_release_mismatch'
    dev = (release.get('stage_results') or {}).get('dev') or {}
    dev_images = {row.get('image') for row in dev.get('verification') or [] if isinstance(row, dict)}
    if (raw.get('image') != artifact.get('image') or raw.get('observed_image') != artifact.get('image')
            or dev.get('verified') is not True or dev_images != {artifact.get('image')}):
        return record, 'qa_observed_image_mismatch'
    if raw.get('ui_url') not in allowed_origins:
        return record, 'qa_dev_origin_unregistered'
    observed = number(raw.get('observed_at'))
    if observed is None or (now is not None and observed > now + 60):
        return record, 'qa_observed_time_invalid'
    steps = raw.get('steps')
    if not isinstance(steps, list) or not steps or any(not isinstance(s, dict) or not isinstance(s.get('id'), str)
                                                       or s.get('status') not in STEP_STATUSES for s in steps):
        return record, 'qa_receipt_invalid'
    tests = raw.get('developer_tests') if isinstance(raw.get('developer_tests'), list) else []
    if tests:
        released = released_file_hashes(document)
        if released is None:
            return record, 'released_source_unverified'
        for row in tests:
            if (not isinstance(row, dict) or not isinstance(row.get('test_id'), str) or row.get('file') not in released
                    or released[row['file']] != row.get('file_sha256')
                    or not row['file'].startswith((profile.get('test_root') or '\0') + '/')):
                return record, 'developer_test_not_in_released_source'
    if raw['outcome'] == 'passed' and (not tests or not proof_passed(raw)):
        return record, 'qa_passed_without_proof'
    return record, None


def epoch(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def recovery_verdict(raw, release, payload, now):
    if not isinstance(raw, dict):
        return {'outcome': None}, 'recovery_receipt_invalid'
    kept, code = preserved(raw)
    if kept is None:
        return {'outcome': raw.get('outcome') if raw.get('outcome') in RECOVERY_OUTCOMES else None,
                'receipt_id': hexval(raw.get('receipt_id'))}, code
    record = kept
    contract = payload.get('recovery_contract') or {}
    target = next((t for t in payload.get('targets', []) if t.get('service') == contract.get('service')), {})
    if raw.get('protocol') != RECOVERY_PROTOCOL or raw.get('outcome') not in RECOVERY_OUTCOMES:
        return record, 'recovery_receipt_invalid'
    if not receipt_digest_valid(raw):
        return record, 'recovery_receipt_digest_mismatch'
    if any(raw.get(k) != release.get(k) for k in ('release_id', 'artifact_sha256', 'plan_sha256')):
        return record, 'recovery_release_mismatch'
    observed = number(raw.get('observed_at'))
    if observed is None or observed > now + 60:
        return record, 'recovery_observed_time_invalid'
    if raw['outcome'] == 'unverifiable':
        # Kept as evidence; never closes the incident.
        return record, None
    if raw.get('image') != (release.get('artifact') or {}).get('image'):
        return record, 'recovery_release_mismatch'
    if (raw.get('environment') != 'prd' or raw.get('service') != contract.get('service')
            or raw.get('deployment_uid') != target.get('uid') or raw.get('path') != (contract.get('operation') or {}).get('path')):
        return record, 'recovery_target_mismatch'
    deployed = epoch(contract.get('deployed_at'))
    start, end = number(raw.get('window_start')), number(raw.get('window_end'))
    if deployed is None or start is None or end is None or start < deployed or end <= start or end > observed + 1:
        return record, 'recovery_window_invalid'
    if raw['outcome'] == 'passed':
        # Either closure path requires the incident's own source-attributed failing operation.
        if not hexval(contract.get('original_error_sha256')) or type(contract.get('http_status')) is not int:
            return record, 'recovery_original_operation_unattributed'
        if raw.get('method') == 'original_error_absent':
            if (raw.get('original_error_unmatched') is not False or type(raw.get('success_count')) is not int
                    or raw['success_count'] < 1 or raw.get('error_count') != 0):
                return record, 'recovery_signal_unproven'
        elif raw.get('method') == 'harmless_check':
            if contract.get('allow_harmless_check') is not True or raw.get('status_code') != 200 or not hexval(raw.get('body_sha256')):
                return record, 'recovery_business_unproven'
        else:
            return record, 'recovery_receipt_invalid'
    elif raw.get('reason') not in ('recovery_error_still_observed', 'recovery_business_invariant_failed'):
        return record, 'recovery_receipt_invalid'
    return record, None


# ---------------------------------------------------------------- prd risk picture

def file_kind(path, test_root):
    if test_root and path.startswith(test_root + '/'):
        return 'test'
    if DATA_PATH.search(path):
        return 'migration'
    if BUILD_FILES.search(path):
        return 'build'
    if path.endswith(('.properties', '.json', '.toml', '.xml', '.yaml', '.yml')):
        return 'config'
    return 'code'


def risk_picture(release, document, profile):
    """Static reading of the exact diff. Absence of a pattern is reported as '미관측', never as proof of safety."""
    data = document.get('data') or {}
    patch = data.get('patch') or {}
    diff = patch.get('diff') if isinstance(patch.get('diff'), str) else None
    test_root = (profile or {}).get('test_root')
    missing = []
    parsed = patched_files((data.get('source_snapshot') or {}).get('files'), diff) if diff else None
    if parsed is None:
        missing.append('diff_unparsed')
    files, current = [], None
    for line in (diff or '').splitlines():
        if line.startswith('+++ b/'):
            name = line[6:].strip()
            current = {'file': name, 'kind': file_kind(name, test_root), 'destructive': False,
                       'data_access': False, 'statements': []}
            files.append(current)
        elif current is not None and line[:1] in '+-' and not line.startswith(('+++', '---')):
            body = line[1:].strip()
            if current['kind'] == 'test':
                continue
            if DESTRUCTIVE.search(body):
                current['destructive'] = True
            if DATA_ACCESS.search(body) or DESTRUCTIVE.search(body):
                current['data_access'] = True
                if len(current['statements']) < 3:
                    current['statements'].append(text(body, 160))
    destructive = any(f['destructive'] for f in files)
    if destructive:
        impact = 'destructive'
    elif any(f['kind'] == 'migration' for f in files):
        impact = 'schema_or_data_change'
    elif any(f['data_access'] for f in files):
        impact = 'data_access_code_changed'
    elif parsed is None:
        impact = 'unknown'
    else:
        impact = 'no_data_path_observed'
    new_image = (release.get('artifact') or {}).get('image')
    targets = [{'environment': t.get('environment'), 'service': t.get('service'), 'uid': t.get('uid'),
                'previous_image': t.get('previous_image'), 'new_image': new_image}
               for t in (release.get('targets') or {}).get('prd', [])]
    if not targets or any(not t['uid'] or not t['previous_image'] or not t['new_image'] for t in targets):
        missing.append('prd_targets_unobserved')
    if not release.get('rollback_ko'):
        missing.append('rollback_unspecified')
    verification = (profile or {}).get('verification') or {}
    if verification.get('kind') not in RECOVERY_KINDS or not verification.get('path'):
        missing.append('recovery_check_unregistered')
    preservation = {
        'destructive': '기존 데이터를 지우거나 이름을 바꾸거나 일괄 수정하는 구문이 변경에 있습니다. 보존·복원 근거가 확인되기 전에는 승인할 수 없습니다.',
        'schema_or_data_change': '데이터 구조 변경 파일이 있습니다. 삭제·이름 변경 구문은 관측되지 않았지만 이전 이미지로 되돌려도 바뀐 구조는 남습니다.',
        'data_access_code_changed': '데이터 읽기·쓰기 코드가 바뀌었습니다. 저장 데이터에 주는 영향은 dev QA와 복구 관측으로 확인합니다.',
        'no_data_path_observed': '변경 diff에서 데이터 저장소 경로·구문이 관측되지 않았습니다. 정적 검사이므로 데이터 영향이 없다는 증명은 아닙니다.',
        'unknown': '변경 내용을 해석하지 못해 데이터 영향을 판단할 수 없습니다.'}[impact]
    complete = not missing
    blocked = destructive or not complete
    return {'schema': 'prd-risk/v2', 'assessment': 'complete' if complete else 'incomplete', 'missing': missing,
            'targets': targets, 'changed_files': files, 'data_impact': impact,
            'data_change': True if impact in ('destructive', 'schema_or_data_change', 'data_access_code_changed') else None,
            'destructive': True if destructive else (False if parsed is not None else None),
            'basis_ko': '정확한 diff의 정적 검사와 등록 프로필·관측된 prd 대상으로 계산했습니다.',
            'approval_blocked': blocked,
            'block_code': 'destructive_change_evidence_missing' if destructive else 'risk_assessment_incomplete' if blocked else None,
            'preservation_ko': preservation, 'rollback_ko': release.get('rollback_ko'),
            'recovery_check': {'method': 'business_path_probe', 'path': verification.get('path'), 'kind': verification.get('kind'),
                               'profile_id': (profile or {}).get('id'),
                               'summary_ko': '배포 후 원래 오류 신호와 같은 업무 요청의 성공을 관측하고, 없으면 등록된 무해 읽기로 업무 결과를 확인합니다.'},
            'patch_diff_sha256': patch.get('diff_sha256'), 'patch_summary_ko': text(patch.get('summary_ko')),
            'patch_risk_ko': text(patch.get('risk_ko'))}


def original_collection(data):
    history = data.get('collection_history') or []
    if history:
        return 'collection_history[0]', history[0]
    return 'collection', data.get('collection') or {}


def original_operation(document, profile):
    """The incident's own failing request on the registered business operation, or None.

    Attribution needs an actual collected http_access error (4xx/5xx) of this service and environment on exactly
    the registered GET path. Another operation of the same service, a healthy check or missing logs never qualify.
    """
    verification = (profile or {}).get('verification') or {}
    path, service = verification.get('path'), document.get('service')
    if verification.get('kind') not in RECOVERY_KINDS or not isinstance(path, str):
        return None
    data = document.get('data') or {}
    basis, collection = original_collection(data)
    rows = [(r.get('text'), basis + '.' + kind, r.get('environment'))
            for kind in ('logs', 'evidence') for r in collection.get(kind, [])
            if isinstance(r, dict) and r.get('service') == service]
    rows.append(((data.get('signal') or {}).get('sample'), 'signal', document.get('environment')))
    for value, source, environment in rows:
        if not isinstance(value, str) or environment != document.get('environment'):
            continue
        for line in value.splitlines():
            start = line.find('{')
            if start < 0:
                continue
            try:
                event = json.loads(line[start:])
            except ValueError:
                continue
            if (isinstance(event, dict) and event.get('event') == 'http_access' and event.get('service') == service
                    and event.get('environment', environment) == environment and event.get('method') == 'GET'
                    and event.get('uri') == path and type(event.get('status')) is int and 400 <= event['status'] <= 599):
                status = event['status']
                return {'method': 'GET', 'path': path, 'http_status': status, 'source': source,
                        'evidence_sha256': text_sha(line[start:]),
                        # Same canonical fingerprint as recovery_probe.http_error_signature.
                        'original_error_sha256': sha({'type': 'http_access', 'service': service, 'method': 'GET',
                                                      'path': path, 'status': status})}
    return None


def stage_time(document, state):
    return next((s.get('occurred_at') for s in reversed(document.get('stages', []))
                 if s.get('state') == state and number(s.get('occurred_at'))), None)


def infra_blocker(release):
    """Mixed-cause order: code release verified in dev → verified dev infra decision → QA → approval."""
    gate = release.get('infra_gate')
    if gate is None:
        return None
    infra = release.get('infra') or {}
    if infra.get('release_id') != release.get('release_id') or infra.get('state') not in ('verified', 'not_required'):
        return 'mixed_infra_unverified'
    if gate == 'verified' and infra.get('state') != 'verified':
        return 'mixed_infra_unverified'
    return None


class QAReleaseControl:
    """Mixed into ReleaseControl. Uses ControlPlane's store/transition/enqueue helpers."""

    def qa_protocol(self, release):
        return (release or {}).get('protocol') == QA_RELEASE_PROTOCOL

    def drop(self, incident, key):
        document = copy.deepcopy(incident.document)
        document.setdefault('data', {}).pop(key, None)
        incident.document = document

    def qa_origins(self, service):
        profile = self.catalog.profiles.get(service, {})
        system = profile.get('system')
        origins = {f'http://{service}.{system}.dev.localhost/'} if system else set()
        configured = (getattr(self.settings, 'qa_ui_origins', None) or {}).get(service)
        if configured:
            origins.add(configured.rstrip('/') + '/')
        return origins

    def infra_gate_for(self, data):
        """Server-derived at release creation. Linked incidents always wait for the core coordinator's decision."""
        if data.get('mixed_required') is True:
            return 'verified'
        return 'decision' if data.get('unified_origin') else None

    # -------------------------------------------------------------- dev verified → (mixed infra) → prd plan → QA
    def after_dev_verified(self, session, incident):
        release = incident.document['data']['release']
        if self.qa_protocol(release) and release.get('infra_gate'):
            self.transition(session, incident, 'dev_infra_pending', {'release_id': release['release_id']})
            return
        self.collect_release_targets(session, incident, 'prd')

    def mixed_pending(self):
        """Coordinator poll: applications whose current release waits for the infra decision (no new signal needed)."""
        self.require_store()
        from .storage import Incident
        with self.store.read() as session:
            return [{'application_id': row.id,
                     'release_id': ((row.document.get('data') or {}).get('release') or {}).get('release_id')}
                    for row in session.scalars(select(Incident).where(Incident.state == 'dev_infra_pending'))]

    def record_mixed_infra(self, incident_id, result):
        """Consume one UnifiedService.advance_mixed() result for the current release, exactly once. Server-only."""
        self.require_store()
        result = result if isinstance(result, dict) else {}
        with self.store.transaction() as session:
            incident = self.get(session, incident_id)
            data = incident.document.get('data', {})
            release = copy.deepcopy(data.get('release') or {})
            gate = release.get('infra_gate')
            if not gate or not self.qa_protocol(release):
                fail(409, 'mixed_not_required')
            if incident.state != 'dev_infra_pending' or not release.get('valid') or result.get('release_id') != release.get('release_id'):
                fail(409, 'mixed_gate_stale')
            state = result.get('state')
            reason = result.get('reason') if isinstance(result.get('reason'), str) and CODE.fullmatch(result['reason']) else None
            if state == 'waiting':
                return {'application_id': incident.id, 'state': incident.state, 'reason': reason}
            if state not in ('verified', 'not_required', 'blocked'):
                fail(422, 'mixed_result_invalid')
            receipt = hexval(result.get('receipt_sha256'))
            history = data.get('infra_history', [])
            if receipt and any(h.get('receipt_sha256') == receipt for h in history):
                fail(409, 'mixed_receipt_already_consumed')
            action = result.get('action_id')
            record = {'state': state, 'release_id': release['release_id'],
                      'action_id': action if isinstance(action, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', action) else None,
                      'receipt_sha256': receipt,
                      'dev_image': result.get('dev_image') if isinstance(result.get('dev_image'), str) else None,
                      'verified_at': number(result.get('verified_at')), 'reason': reason, 'consumed_at': self.clock()}
            self.data(incident, 'infra_history', [*history, record])
            pending_since = stage_time(incident.document, 'dev_infra_pending')
            if state == 'not_required' and gate == 'decision':
                release['infra'] = record
                self.data(incident, 'release', release)
                self.transition(session, incident, 'dev_infra_verified', {'decision': 'not_required'})
                self.collect_release_targets(session, incident, 'prd')
            elif (state == 'verified' and record['action_id'] and receipt and record['dev_image'] == release['artifact']['image']
                    and record['verified_at'] and pending_since and record['verified_at'] >= pending_since):
                release['infra'] = record
                self.data(incident, 'release', release)
                self.transition(session, incident, 'dev_infra_verified', {'action_id': record['action_id']})
                self.collect_release_targets(session, incident, 'prd')
            else:
                code = {'blocked': 'mixed_infra_blocked', 'not_required': 'mixed_required_by_server'}.get(
                    state, 'mixed_infra_receipt_invalid')
                self.stop(session, incident, code)
            return {'application_id': incident.id, 'state': incident.state}

    def queue_qa(self, session, incident):
        doc = incident.document
        release = doc['data']['release']
        blocked = infra_blocker(release)
        if blocked:
            return self.stop(session, incident, blocked)
        profile = self.catalog.profiles.get(doc['service'], {})
        payload = {'stage': 'dev', 'environment': 'dev', 'service': doc['service'], 'profile_id': profile.get('id'),
                   'release_id': release['release_id'], 'artifact_sha256': release['artifact_sha256'],
                   'plan_sha256': release['plan_sha256'], 'artifact': copy.deepcopy(release['artifact']),
                   'image': release['artifact']['image'], 'round': doc['data'].get('round', 1),
                   # The runner pins QA to this dev Deployment UID and its ready Pods before and after the browser run.
                   'targets': copy.deepcopy((release.get('targets') or {}).get('dev', []))}
        self.transition(session, incident, 'qa_queued', {'release_id': release['release_id'], 'round': payload['round']})
        self.enqueue(session, incident, 'qa', payload)

    def qa_result(self, session, incident, job, result):
        data = incident.document['data']
        release = copy.deepcopy(data.get('release') or {})
        profile = self.catalog.profiles.get(incident.document['service'], {})
        record, code = qa_verdict(result.get('qa'), release, incident.document, profile,
                                  self.qa_origins(incident.document['service']), self.clock())
        if code is None and result.get('write_outcome') != 'not_written':
            code = 'qa_write_outcome_invalid'
        if code is None and (result.get('ok') is True) != (record.get('outcome') == 'passed'):
            code = 'qa_result_inconsistent'
        if code is None and record['outcome'] == 'tests_missing':
            required = self.catalog.missing_controller_test(incident.document['service'])
            released = released_file_hashes(incident.document)
            if (not required or record.get('reason') != 'required_controller_test_missing'
                    or released is None or required['test_file'] in released):
                code = 'required_controller_test_missing_unverified'
        stale = (incident.state != 'qa_queued' or not self.qa_protocol(release) or not release.get('valid')
                 or job.payload.get('release_id') != release.get('release_id') or job.payload.get('plan_sha256') != release.get('plan_sha256'))
        if stale and code is None:
            code = 'qa_report_stale'
        record.update(accepted=code is None, rejection_code=code, job_id=job.id, round=data.get('round', 1), reported_at=self.clock())
        self.data(incident, 'qa_history', [*data.get('qa_history', []), record])
        self.event(session, incident, 'qa_reported', {'outcome': record.get('outcome'), 'accepted': record['accepted'],
                                                      'code': code, 'receipt_id': record.get('receipt_id')})
        if stale:
            return
        release['qa'] = record
        self.data(incident, 'release', release)
        if code is None and record['outcome'] == 'passed':
            self.transition(session, incident, 'qa_passed', {'receipt_id': record['receipt_id']})
            self.transition(session, incident, 'awaiting_prd_approval', {'qa_receipt_id': record['receipt_id']})
            return
        if code is None and record['outcome'] == 'tests_missing':
            self.event(session, incident, 'qa_tests_missing', {'reason': record.get('reason'), 'service': incident.document['service']})
            if not any(r.get('reason') == 'qa_tests_missing' for r in data.get('rounds', [])):
                return self.start_round(session, incident, 'qa_tests_missing', None, {'subject': 'liveprobe'})
            return self.stop(session, incident, 'qa_required_test_missing')
        self.transition(session, incident, 'qa_failed', {'outcome': record.get('outcome'), 'code': code or record.get('reason')})

    # -------------------------------------------------------------- prd risk / approval
    def finish_prd_plan(self, session, incident, release):
        profile = self.catalog.profiles.get(incident.document['service'], {})
        release['risk'] = risk_picture(release, incident.document, profile)
        release['risk_sha256'] = sha(release['risk'])
        release['plan_sha256'] = self.release_fingerprint(release)
        self.data(incident, 'release', release)
        self.queue_qa(session, incident)

    def v3_approval_ready(self, release, body=None):
        blocked = infra_blocker(release)
        if blocked:
            return blocked
        qa = release.get('qa') or {}
        if not (qa.get('accepted') is True and qa.get('outcome') == 'passed' and qa.get('plan_sha256') == release.get('plan_sha256')
                and qa.get('release_id') == release.get('release_id') and qa.get('artifact_sha256') == release.get('artifact_sha256')):
            return 'qa_receipt_required'
        risk = release.get('risk') or {}
        if not risk or sha(risk) != release.get('risk_sha256'):
            return 'risk_picture_invalid'
        if risk.get('destructive') is True:
            return 'destructive_change_evidence_missing'
        if risk.get('assessment') != 'complete' or risk.get('approval_blocked') is not False:
            return 'risk_assessment_incomplete'
        if body is not None and (getattr(body, 'qa_receipt_id', None) != qa.get('receipt_id') or
                                 getattr(body, 'risk_sha256', None) != release.get('risk_sha256')):
            return 'approval_evidence_conflict'
        return None

    # -------------------------------------------------------------- recovery
    def queue_recovery(self, session, incident):
        doc = incident.document
        release = doc['data']['release']
        profile = self.catalog.profiles.get(doc['service'], {})
        history = doc['data'].get('recovery_history', [])
        if incident.state != 'awaiting_recovery':
            self.transition(session, incident, 'awaiting_recovery', {'release_id': release['release_id']})
        # Server-recorded acceptance time of the prd write result. Never synthesized from the current clock.
        deployed = stage_time(incident.document, 'prd_verifying')
        if deployed is None:
            return self.stop(session, incident, 'recovery_deployment_time_unobserved')
        operation = original_operation(doc, profile)
        contract = {'service': doc['service'], 'environment': 'prd', 'release_id': release['release_id'],
                    'image': release['artifact']['image'],
                    'operation': {'method': 'GET', 'path': (profile.get('verification') or {}).get('path')},
                    'deployed_at': datetime.fromtimestamp(deployed, timezone.utc).isoformat().replace('+00:00', 'Z'),
                    'original_error_sha256': (operation or {}).get('original_error_sha256'),
                    'http_status': (operation or {}).get('http_status'),
                    # A harmless read may only stand in for the incident's own attributed failing operation.
                    'allow_harmless_check': operation is not None}
        payload = {'stage': 'prd', 'environment': 'prd', 'service': doc['service'], 'profile_id': profile.get('id'),
                   'release_id': release['release_id'], 'artifact_sha256': release['artifact_sha256'],
                   'plan_sha256': release['plan_sha256'], 'artifact': copy.deepcopy(release['artifact']),
                   'targets': copy.deepcopy((release.get('targets') or {}).get('prd', [])), 'recovery_contract': contract,
                   'attribution': {k: operation[k] for k in ('source', 'evidence_sha256')} if operation else None,
                   'attempt': len(history) + 1,
                   # Bounded retries are spaced so a later window can observe real post-deployment traffic.
                   'not_before': self.clock() + (RECOVERY_RETRY_SECONDS if history else 0)}
        self.enqueue(session, incident, 'recovery', payload)

    def recovery_result(self, session, incident, job, result):
        data = incident.document['data']
        release = copy.deepcopy(data.get('release') or {})
        record, code = recovery_verdict(result.get('recovery'), release, job.payload, self.clock())
        if code is None and result.get('write_outcome') != 'not_written':
            code = 'recovery_write_outcome_invalid'
        if code is None and (result.get('ok') is True) != (record.get('outcome') == 'passed'):
            code = 'recovery_result_inconsistent'
        stale = incident.state != 'awaiting_recovery' or job.payload.get('release_id') != release.get('release_id')
        if stale and code is None:
            code = 'recovery_report_stale'
        record.update(accepted=code is None, rejection_code=code, job_id=job.id, attempt=job.payload.get('attempt'),
                      attribution=job.payload.get('attribution'), reported_at=self.clock())
        history = [*data.get('recovery_history', []), record]
        self.data(incident, 'recovery_history', history)
        self.event(session, incident, 'recovery_reported', {'outcome': record.get('outcome'), 'accepted': record['accepted'], 'code': code})
        if stale:
            return
        release['recovery'] = record
        self.data(incident, 'release', release)
        if code is None and record['outcome'] == 'passed':
            from .accountability import conclude
            conclude(self,session,incident,{'recovery_receipt_id':record['receipt_id']})
            return
        if code is None and record['outcome'] == 'failed':
            return self.stop(session, incident, 'recovery_error_still_observed'
                             if record.get('reason') == 'recovery_error_still_observed' else 'recovery_business_failed')
        if len(history) < RECOVERY_ATTEMPTS:
            return self.queue_recovery(session, incident)
        self.stop(session, incident, 'recovery_not_observed' if job.payload.get('attribution')
                  else 'recovery_original_operation_unattributed')

    # -------------------------------------------------------------- rounds and supplements
    def start_round(self, session, incident, reason, note, actor, request_id=None):
        data = incident.document['data']
        current = data.get('round', 1)
        for job in session.scalars(select(Job).where(Job.incident_id == incident.id, Job.status == 'queued')):
            job.status = 'cancelled'
            self.event(session, incident, 'job_cancelled', {'job_id': job.id, 'reason': 'round_closed'})
        release = copy.deepcopy(data.get('release'))
        if release:
            release['valid'] = False
            release['invalidation_reason'] = 'superseded_by_round'
            for key in ('user_test', 'approval'):
                if release.get(key):
                    release[key]['invalidated'] = True
        patch = data.get('patch') or {}
        archived = {'round': current, 'closed_at': self.clock(), 'closed_state': incident.state, 'reason': reason,
                    'note_ko': text(note, 2000), 'requested_by': actor.get('subject'), 'request_id': request_id,
                    'patch_diff_sha256': patch.get('diff_sha256'), 'patch_summary_ko': text(patch.get('summary_ko')),
                    'release': release, 'test_build': data.get('test_build'), 'merge_review': data.get('merge_review'),
                    'qa_history': data.get('qa_history', []), 'recovery_history': data.get('recovery_history', []),
                    'infra_history': data.get('infra_history', []), 'patch_revisions': data.get('patch_revisions', [])}
        self.data(incident, 'rounds', [*data.get('rounds', []), archived])
        for key in ROUND_KEYS:
            self.drop(incident, key)
        self.data(incident, 'round', current + 1)
        request = {'round': current + 1, 'reason': reason, 'note_ko': text(note, 2000),
                   'request_id': request_id, 'qa': (release or {}).get('qa')}
        if reason == 'qa_tests_missing':
            request['qa_requirement'] = self.catalog.missing_controller_test(incident.document['service'])
        self.data(incident, 'round_request', request)
        document = copy.deepcopy(incident.document)
        document['error'] = None
        document.pop('handoff_classification', None)
        incident.document = document
        # Each round re-collects fresh evidence and re-pins the source actually running in the environment.
        self.transition(session, incident, 'collecting', {'round': current + 1, 'reason': reason})
        self.enqueue(session, incident, 'collect', {'environment': incident.document['environment'],
            'service': incident.document['service'],
            'since': datetime.fromtimestamp(self.clock() - 900, timezone.utc).isoformat().replace('+00:00', 'Z'),
            'catalog_sha256': data.get('catalog_sha256', self.catalog.sha256)})

    def supplement_blocker(self, session, incident):
        state = incident.state
        jobs = list(session.scalars(select(Job).where(Job.incident_id == incident.id,
                                                      Job.status.in_(['queued', 'leased', 'reconciliation_required']))))
        if state == 'reconciliation_required' or any(j.status == 'reconciliation_required' for j in jobs):
            return 'write_outcome_unresolved'
        if any(j.status == 'leased' for j in jobs):
            return 'job_in_progress'
        if state in PRD_WRITE_CHAIN and not (state == 'prd_deploy_queued' and jobs and all(j.status == 'queued' for j in jobs)):
            return 'prd_change_in_progress'
        if state == 'dev_infra_pending':
            # The core coordinator may be executing the verified dev infra action for this release.
            return 'infra_decision_in_progress'
        lease = incident.document.get('data', {}).get('analysis_lease') or {}
        if state == 'analyzing' and lease.get('until', 0) > self.clock():
            return 'analysis_in_progress'
        return None

    def _supplements(self, incident, changes):
        items = [{**item, **changes[item['id']]} if item['id'] in changes else item
                 for item in incident.document.get('data', {}).get('supplements', [])]
        self.data(incident, 'supplements', items)

    def apply_pending_supplements(self, session, incident):
        items = [s for s in incident.document.get('data', {}).get('supplements', []) if s.get('status') == 'pending']
        if not items:
            return
        if incident.state in FOLLOWUP_ONLY_STATES:
            # Closed while waiting: keep the closure; record the request as a read-only follow-up on the same incident.
            for s in items:
                self.add_followup(session, incident, s, 'closed_before_apply')
            self._supplements(incident, {s['id']: {'status': 'recorded_as_followup', 'resolved_at': self.clock()} for s in items})
            return
        blocker = self.supplement_blocker(session, incident)
        if blocker:
            self._supplements(incident, {s['id']: {'blocked_by': blocker} for s in items})
            return
        changes = {}
        revise = [s for s in items if s['kind'] == 'revise']
        rerun = [s for s in items if s['kind'] == 'rerun_qa']
        data = incident.document['data']
        release = data.get('release') or {}
        if revise:
            note = '\n\n'.join(s['note_ko'] for s in revise)
            notes = [*data.get('operator_notes', []),
                     *({'id': s['id'], 'note_ko': s['note_ko'], 'subject': s['subject'], 'requested_at': s['requested_at'],
                        'round': data.get('round', 1)} for s in revise)]
            self.data(incident, 'operator_notes', notes)
            if data.get('source_snapshot') and incident.state not in ('detected', 'collecting', 'source_pinning', 'analysis_queued'):
                self.start_round(session, incident, 'operator_supplement', note, {'subject': revise[-1]['subject']},
                                 revise[-1]['request_id'])
                applied = {'status': 'applied', 'applied_as': 'new_round', 'applied_round': incident.document['data']['round']}
            else:
                applied = {'status': 'applied', 'applied_as': 'analysis_note'}
            changes.update({s['id']: {**applied, 'resolved_at': self.clock(), 'blocked_by': None} for s in revise})
        rerun_done = False
        for s in rerun:
            if not revise and not rerun_done and incident.state == 'qa_failed' and self.qa_protocol(release) and release.get('valid'):
                self.queue_qa(session, incident)
                rerun_done = True
                changes[s['id']] = {'status': 'applied', 'applied_as': 'qa_rerun', 'resolved_at': self.clock(), 'blocked_by': None}
            else:
                changes[s['id']] = {'status': 'superseded', 'resolved_at': self.clock(), 'blocked_by': None}
        self._supplements(incident, changes)
        self.event(session, incident, 'supplement_applied', {'ids': sorted(changes)})

    def add_followup(self, session, incident, item, basis):
        """Durable, read-traceable note on a closed incident. Never reopens it and never queues work."""
        data = incident.document.get('data', {})
        release = data.get('release') or {}
        closure = {'state': incident.state, 'closed_at': stage_time(incident.document, incident.state),
                   'recovery_receipt_id': (release.get('recovery') or {}).get('receipt_id'),
                   'error_code': (incident.document.get('error') or {}).get('code')}
        followup = {'id': item['id'], 'request_id': item['request_id'], 'kind': 'followup_note', 'note_ko': item['note_ko'],
                    'note_sha256': item['note_sha256'], 'subject': item['subject'], 'requested_at': item['requested_at'],
                    'basis': basis, 'closure': closure, 'round': data.get('round', 1), 'effects': 'none',
                    'semantics_ko': '종료 기록과 복구 근거는 그대로 유지됩니다. 이 후속 메모는 자동 분석·배포를 시작하지 않으며, '
                                    '추가 조치가 필요하면 담당자가 이 사건을 근거로 새 조치를 요청합니다.'}
        self.data(incident, 'followups', [*data.get('followups', []), followup])
        self.event(session, incident, 'followup_recorded', {'id': followup['id'], 'subject': followup['subject'],
                                                            'closure_state': incident.state})
        return followup

    def supplement(self, incident_id, body, actor):
        self.require_store()
        note = str(scrub(body.note_ko)).strip()
        if not note:
            fail(422, 'supplement_note_required', '보완 내용을 입력하세요.')
        with self.store.transaction() as session:
            incident = self.get(session, incident_id)
            data = incident.document.get('data', {})
            existing = next((s for s in [*data.get('supplements', []), *data.get('followups', [])]
                             if s['request_id'] == body.request_id), None)
            if existing:
                same_kind = existing['kind'] == body.kind or (existing['kind'] == 'followup_note' and body.kind == 'revise')
                if existing['note_sha256'] != text_sha(note) or not same_kind:
                    fail(409, 'supplement_request_conflict', '같은 요청 번호로 다른 보완 내용을 보낼 수 없습니다.')
                key = 'followup' if existing['kind'] == 'followup_note' else 'supplement'
                return {'incident_id': incident_id, 'state': incident.state, key: existing}
            release = data.get('release') or {}
            if body.kind == 'rerun_qa' and not (incident.state == 'qa_failed' and self.qa_protocol(release) and release.get('valid')):
                fail(409, 'qa_rerun_unavailable', 'QA 실패 상태의 현재 버전만 다시 시험할 수 있습니다.')
            item = {'id': uuid.uuid4().hex, 'request_id': body.request_id, 'kind': body.kind, 'note_ko': note,
                    'note_sha256': text_sha(note), 'subject': actor['subject'], 'requested_at': self.clock(),
                    'state_at_request': incident.state, 'round': data.get('round', 1), 'status': 'pending', 'blocked_by': None}
            if incident.state in FOLLOWUP_ONLY_STATES or (incident.state in ('failed', 'handoff') and not data.get('source_snapshot')):
                # Same historical incident; no new incident, no side effect, closure and recovery proof stay intact.
                followup = self.add_followup(session, incident, item, 'closed_incident')
                return {'incident_id': incident_id, 'state': incident.state, 'followup': followup}
            self.data(incident, 'supplements', [*data.get('supplements', []), item])
            self.event(session, incident, 'supplement_requested', {'id': item['id'], 'kind': item['kind'], 'subject': actor['subject']})
            self.apply_pending_supplements(session, incident)
            saved = next((s for s in incident.document['data'].get('supplements', []) if s['id'] == item['id']), None)
            return {'incident_id': incident_id, 'state': incident.state, 'supplement': saved}
