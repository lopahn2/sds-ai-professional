#!/usr/bin/env python3
"""삼성 전환의 단일 객체 CAS 요청을 생성한다. 가져오기만으로 외부 동작은 없다."""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import re
from typing import Any

CONTEXT = 'rancher-desktop'
ENVIRONMENTS = ('dev', 'stg', 'prd')
SERVICES = ('order', 'inventory', 'catalog', 'payment', 'trade-member',
            'delivery', 'notification', 'delivery-member', 'business-api')
DOMAINS = ('trade-member', 'delivery-member', 'business-api')
OLD_POLICIES = ('allow-same-namespace', 'allow-dns', 'allow-ingress-controller',
                'allow-observation', 'inventory-postgres', 'allow-istiod')
POLICIES = OLD_POLICIES + ('default-deny-all', 'samsung-trade-internal',
    'samsung-delivery-internal', 'samsung-business-internal', 'samsung-dns',
    'samsung-public-http', 'samsung-observation', 'samsung-domain-kafka',
    'samsung-business-readers-tls', 'samsung-istiod') + tuple(
        'samsung-' + s + '-database' for s in ('inventory',) + DOMAINS) + tuple(
        'samsung-' + s + '-business-tls' for s in DOMAINS[:2])
RESOURCES = {'Deployment': ('apps/v1', 'deployments', SERVICES + ('member',)),
    'ConfigMap': ('v1', 'configmaps', tuple(s + '-config' for s in SERVICES + ('member',))),
    'Service': ('v1', 'services', SERVICES + ('member',)),
    'Ingress': ('networking.k8s.io/v1', 'ingresses', tuple(s for s in SERVICES if s != 'inventory') + ('member',)),
    'NetworkPolicy': ('networking.k8s.io/v1', 'networkpolicies', POLICIES)}


class CutoverError(ValueError):
    """조건을 낮추거나 재시도해서는 안 되는 중단이다."""


def _require(condition, message):
    if not condition:
        raise CutoverError(message)


def _target(environment, obj):
    _require(environment in ENVIRONMENTS, '환경 하나만 지정해야 한다')
    _require(isinstance(obj, dict), '단일 객체가 필요하다')
    kind, meta = obj.get('kind'), obj.get('metadata', {})
    _require(kind in RESOURCES and isinstance(meta, dict), '허용된 앱 객체만 지정할 수 있다')
    api, plural, names = RESOURCES[kind]
    _require(obj.get('apiVersion') == api and meta.get('namespace') == environment and
             meta.get('name') in names, '종류·이름·환경 허용목록과 일치해야 한다')
    base = ('/api/v1' if api == 'v1' else '/apis/' + api) + '/namespaces/' + environment + '/' + plural
    return base, base + '/' + meta['name']


def _identity(obj):
    meta = obj.get('metadata', {})
    values = {key: meta.get(key) for key in ('uid', 'resourceVersion')}
    _require(all(isinstance(v, str) and v.strip() == v and v for v in values.values()),
             '관측한 UID와 resourceVersion 문자열이 모두 필요하다')
    _require(not meta.get('deletionTimestamp'), '이미 삭제 중인 객체는 변경할 수 없다')
    return values


def cas_request(environment, change, *, dry_run):
    """저수준 단일 객체 요청이다. 실행 승인은 prepare_plan/execute_one이 담당한다."""
    _require(type(dry_run) is bool and isinstance(change, dict), '명시적 dry-run과 단일 변경이 필요하다')
    action = change.get('action')
    before: Any = change.get('before')
    _require(action in ('apply', 'delete', 'scale'), '허용하지 않는 작업이다')
    keys = {'action', 'before', 'desired'} if action == 'apply' else ({'action', 'before', 'replicas'} if action == 'scale' else {'action', 'before'})
    _require(set(change) == keys, '알 수 없는 옵션이나 생략한 관측값은 허용하지 않는다')
    desired: Any = change.get('desired') if action == 'apply' else before
    base, path = _target(environment, desired)
    if before is not None:
        _require(_target(environment, before)[1] == path, '관측 객체와 변경 객체가 다르다')
        identity = _identity(before)
    else:
        _require(action == 'apply', '기존 객체의 관측값이 필요하다')
        identity = None
    if action == 'delete':
        kind, name = before['kind'], before['metadata']['name']
        _require((kind == 'NetworkPolicy' and name in OLD_POLICIES) or name in ('member', 'member-config'),
                 '삭제는 명시적인 구 정책·은퇴 회원 객체만 허용한다')
        body = {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': identity,
                'propagationPolicy': 'Foreground'}
        if dry_run:
            body['dryRun'] = ['All']
        return {'method': 'DELETE', 'path': path, 'body': body, 'content_type': 'application/json'}
    if action == 'scale':
        replicas = change.get('replicas')
        _require(before['kind'] == 'Deployment' and type(replicas) is int and replicas in (0, 1),
                 'Deployment의 0 또는 1 크기만 허용한다')
        additions = [{'op': 'add', 'path': '/spec/replicas', 'value': replicas}]
    else:
        _require(set(desired) <= {'apiVersion', 'kind', 'metadata', 'spec', 'data'} and
                 set(desired['metadata']) <= {'name', 'namespace', 'labels', 'annotations'},
                 '선언에는 식별자·검토한 spec/data·라벨·주석만 허용한다')
        additions = []
        if 'data' in desired:
            additions.append({'op': 'add', 'path': '/data', 'value': deepcopy(desired['data'])})
        if 'spec' in desired:
            _require(isinstance(desired['spec'], dict), 'spec은 객체여야 한다')
            additions += [{'op': 'add', 'path': '/spec/' + key.replace('~', '~0').replace('/', '~1'),
                           'value': deepcopy(value)} for key, value in desired['spec'].items()]
        additions += [{'op': 'add', 'path': '/metadata/' + key,
                       'value': deepcopy(desired['metadata'][key])}
                      for key in ('labels', 'annotations') if key in desired['metadata']]
        _require(bool(additions), '빈 적용은 허용하지 않는다')
    suffix = '?dryRun=All' if dry_run else ''
    if identity is None:
        return {'method': 'POST', 'path': base + suffix, 'body': deepcopy(desired),
                'content_type': 'application/json'}
    tests = [{'op': 'test', 'path': '/metadata/' + key, 'value': value} for key, value in identity.items()]
    return {'method': 'PATCH', 'path': path + suffix, 'body': tests + additions,
            'content_type': 'application/json-patch+json'}


MEMORY_APPROVAL_SHA256 = 'e8c7dda3962de4618c4b9295ef770ea6a2e7aed4e5f977ccd6ee81a526e720b2'
LEGACY_SERVICES = frozenset(('order', 'inventory', 'catalog', 'payment', 'delivery', 'notification', 'member'))
ACCEPTANCE_CHECKS = frozenset(('data', 'single_writer', 'api', 'network', 'tls', 'resources', 'readback', 'rollback'))


def fingerprint(value):
    """JSON 값의 정확한 검토 지문이다. 비밀값이나 원문 증거는 넣지 않는다."""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _time(value):
    try:
        parsed = datetime.fromisoformat(value)
        _require(parsed.utcoffset() is not None, '시각에는 시간대가 필요하다')
        return parsed
    except (TypeError, ValueError):
        raise CutoverError('시간대가 있는 ISO 시각이 필요하다') from None


def _sha(value):
    _require(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value), 'SHA256 형식이 필요하다')


def _receipt(value, kind, environment, now, *, fresh=True):
    _require(isinstance(value, dict) and value.get('kind') == kind and
             value.get('environment') == environment and value.get('context') == CONTEXT,
             '증거 종류·환경·context가 정확히 일치해야 한다')
    _sha(value.get('artifact_sha256'))
    observed = _time(value.get('observed_at'))
    _require(observed <= now and (not fresh or now - observed <= timedelta(hours=1)),
             '미래 또는 오래된 증거는 사용할 수 없다')
    return observed


def _preservation(environment, receipts, now):
    _require(isinstance(receipts, dict) and set(receipts) == {'backup', 'writers_stopped', 'inventory_migration'},
             '백업·쓰기중지·inventory 이관 영수증이 모두 필요하다')
    backup, writers, migration = (receipts[k] for k in ('backup', 'writers_stopped', 'inventory_migration'))
    b = _receipt(backup, 'backup', environment, now)
    w = _receipt(writers, 'writers_stopped', environment, now)
    m = _receipt(migration, 'inventory_migration', environment, now)
    _require(backup.get('available_backup_saved') is True and backup.get('complete_memory_snapshot') is False and
             backup.get('memory_reset_approval_sha256') == MEMORY_APPROVAL_SHA256,
             '가능한 백업 선보관과 이번 메모리 보존 예외의 정확한 승인 지문이 필요하다')
    entries = backup.get('entries', [])
    _require(isinstance(entries, list) and len(entries) == len(LEGACY_SERVICES) and
             all(isinstance(e, dict) for e in entries) and
             {e.get('service') for e in entries} == LEGACY_SERVICES, '기존 모든 서비스의 백업 범위가 필요하다')
    for entry in entries:
        _require(type(entry.get('records')) is int and entry['records'] >= 0, '백업 조회 수량이 필요하다')
        _sha(entry.get('sha256'))
    _require(writers.get('services') and len(writers['services']) == len(LEGACY_SERVICES) and
             set(writers['services']) == LEGACY_SERVICES and
             all(writers.get(k) is True for k in ('old_writes_rejected', 'self_traffic_stopped', 'inventory_frozen', 'external_maintenance_blocked')) and
             writers.get('backup_receipt_sha256') == fingerprint(backup),
             '백업과 연결된 전체 writer·자체 트래픽·inventory 쓰기중지 증거가 필요하다')
    terminations = writers.get('terminations', [])
    _require(isinstance(terminations, list) and len(terminations) == len(LEGACY_SERVICES) and
             all(isinstance(t, dict) for t in terminations) and
             {t.get('service') for t in terminations} == LEGACY_SERVICES,
             '모든 구 Deployment의 소유 Pod·컨테이너 종료 확인이 필요하다')
    for entry in terminations:
        _identity({'metadata': {'uid': entry.get('deployment_uid'), 'resourceVersion': entry.get('resourceVersion')}})
        previous, absent = entry.get('previous_pod_uids'), entry.get('absent_pod_uids')
        _require(isinstance(previous, list) and isinstance(absent, list) and
                 all(isinstance(uid, str) and uid for uid in previous + absent) and
                 len(previous) == len(set(previous)) and len(absent) == len(set(absent)) and set(previous) == set(absent) and
                 entry.get('owned_pods') == [] and type(entry.get('running_containers')) is int and entry['running_containers'] == 0 and
                 type(entry.get('desired_replicas')) is int and entry['desired_replicas'] == 0 and
                 type(entry.get('generation')) is int and entry['generation'] > 0 and
                 type(entry.get('observed_generation')) is int and entry['observed_generation'] == entry['generation'] and
                 entry.get('owner_chain_complete') is True and entry.get('container_inventory_complete') is True,
                 'ready=0은 종료 증거가 아니다. 구 소유 Pod 완전 부재·컨테이너 종료가 필요하다')
    _require(migration.get('writer_receipt_sha256') == fingerprint(writers), '최종 이관은 해당 쓰기중지 증거와 연결돼야 한다')
    _sha(migration.get('final_dump_sha256'))
    _require(b <= w <= _time(migration.get('dumped_at')) <= _time(migration.get('restored_at')) <= m,
             '백업→쓰기중지→최종 dump→복원 순서가 필요하다')
    source, target = migration.get('source', {}), migration.get('target', {})
    _require(isinstance(source.get('database'), str) and bool(source['database']) and
             isinstance(source.get('server_ref'), str) and bool(source['server_ref']) and
             target.get('database') == 'inventory_' + environment and
             target.get('server_ref') == 'trade-db.liveprobe-data.svc:5432' and
             (source['server_ref'], source['database']) != (target['server_ref'], target['database']),
             '기존 DB와 환경별 신규 inventory DB의 실제 서버·이름을 구분해야 한다')
    _require(type(source.get('records')) is int and source['records'] >= 0 and
             type(target.get('records')) is int and target['records'] == source['records'], 'DB 수량이 다르다')
    for key in ('keys_sha256', 'content_sha256', 'versions_sha256'):
        _sha(source.get(key))
        _require(target.get(key) == source[key], 'DB 키·내용·버전 대조가 일치하지 않는다')


def _settings(environment, stage, change):
    obj = change['desired'] if change['action'] == 'apply' else change['before']
    name = obj['metadata']['name']
    if name in ('member', 'member-config'):
        _require(change['action'] == 'delete' or (change['action'] == 'scale' and change['replicas'] == 0),
                 '구 회원 writer 재활성·구 저장소 재연결은 지원하지 않는다')
    if stage == 'activate':
        _require(change['action'] == 'apply' and obj['kind'] == 'ConfigMap' and
                 name in tuple(s + '-config' for s in DOMAINS), '쓰기 활성은 domain ConfigMap의 별도 검토로만 가능하다')
    if change['action'] == 'apply' and obj['kind'] == 'Deployment':
        containers = obj.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [])
        main = [c for c in containers if c.get('name') == name]
        _require(len(main) == 1 and main[0].get('envFrom') == [{'configMapRef': {'name': name + '-config'}}],
                 '주 컨테이너는 해당 서비스 ConfigMap 하나만 참조해야 한다')
        _require(not any(c.get('env') for c in containers) if name in DOMAINS else True,
                 'domain 환경변수 덮어쓰기는 허용하지 않는다')
    if change['action'] != 'apply' or obj['kind'] != 'ConfigMap':
        return
    data = obj.get('data', {})
    _require(isinstance(data, dict) and data.get('SELF_TRAFFIC') == '0', '자체 트래픽은 명시적으로 꺼야 한다')
    if name in tuple(s + '-config' for s in DOMAINS):
        expected = dict(zip((s + '-config' for s in DOMAINS), ('trade', 'delivery', 'business')))[name]
        _require(data.get('DOMAIN_MODE') == expected and data.get('READ_ONLY_MODE') == ('0' if stage == 'activate' else '1'),
                 'domain 쓰기는 cutover에서 차단하고 별도 activate 단계에서만 허용한다')
    if name == 'inventory-config':
        _require(data.get('INVENTORY_DB_URL') == 'jdbc:postgresql://trade-db.liveprobe-data.svc:5432/inventory_' + environment and
                 data.get('INVENTORY_DB_USER') == 'inventory_' + environment, '이관 검증한 신규 inventory DB만 연결할 수 있다')


def prepare_plan(environment, stage, change, *, receipts, previous, now):
    """한 환경의 한 객체만 계획한다. 파일·클러스터 조회 및 자동 전환은 하지 않는다."""
    current = _time(now)
    cas_request(environment, change, dry_run=True)
    _require(stage in ('cutover', 'activate'), '지원하지 않는 단계다')
    _settings(environment, stage, change)
    _require(isinstance(previous, list) and len(previous) == ENVIRONMENTS.index(environment),
             '앞선 환경 모두의 독립 인수 영수증이 필요하다')
    for env, receipt in zip(ENVIRONMENTS, previous):
        _receipt(receipt, 'environment_acceptance', env, current, fresh=False)
        _require(receipt.get('state') == 'accepted' and set(receipt.get('checks', [])) == ACCEPTANCE_CHECKS,
                 '앞선 환경의 필수 인수 검사가 모두 필요하다')
    _preservation(environment, receipts, current)
    before = change.get('before')
    if before is not None and before['kind'] == 'Deployment' and before['metadata']['name'] in LEGACY_SERVICES:
        stopped = next(t for t in receipts['writers_stopped']['terminations'] if t['service'] == before['metadata']['name'])
        _require(stopped['deployment_uid'] == before['metadata']['uid'], '쓰기중지 후 재생성된 Deployment는 다시 검증해야 한다')
    selected = deepcopy(change)
    if selected.get('before') is not None:
        obj = selected['before']
        selected['before'] = {'apiVersion': obj['apiVersion'], 'kind': obj['kind'],
            'metadata': {k: obj['metadata'][k] for k in ('name', 'namespace', 'uid', 'resourceVersion')}}
    return {'schema': 'samsung-cutover/v1', 'context': CONTEXT, 'environment': environment,
            'stage': stage, 'prepared_at': now, 'change': selected,
            'receipts': deepcopy(receipts), 'previous': deepcopy(previous)}


def _subset(expected, observed):
    if isinstance(expected, dict):
        return isinstance(observed, dict) and all(k in observed and _subset(v, observed[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(observed, list) and len(expected) == len(observed) and all(
            _subset(a, b) for a, b in zip(expected, observed))
    return type(expected) is type(observed) and expected == observed


def _send(transport, request):
    _require(transport.context() == CONTEXT, '전송 context가 rancher-desktop가 아니다')
    try:
        status, body = transport.request(deepcopy(request))
    except Exception:
        raise CutoverError('전송 결과가 불명이다. 자동 재시도 없이 중단한다') from None
    _require(type(status) is int, 'HTTP 상태를 확인할 수 없다')
    if status == 409:
        raise CutoverError('409 충돌이다. 재조회·재시도·조건 완화 없이 중단한다')
    return status, body


def _verify_object(environment, change, obj):
    desired = change['desired'] if change['action'] == 'apply' else change['before']
    _require(_target(environment, obj)[1] == _target(environment, desired)[1], '응답 대상이 다르다')
    identity = _identity(obj)
    if change.get('before') is not None:
        _require(identity['uid'] == _identity(change['before'])['uid'], '응답 UID가 다르다')
    projection = change['desired'] if change['action'] == 'apply' else {'spec': {'replicas': change['replicas']}}
    _require(_subset(projection, obj), '검토한 변경과 응답·재조회 값이 다르다')
    return identity


def execute_one(plan, *, reviewed_sha256, transport, now, dry_run=True, dry_run_receipt: Any = None):
    """검토 지문 하나의 객체만 실행한다. 충돌·실패·삭제 대기에 재시도하지 않는다."""
    plan, dry_run_receipt = deepcopy(plan), deepcopy(dry_run_receipt)
    _require(isinstance(plan, dict) and set(plan) == {'schema', 'context', 'environment', 'stage',
             'prepared_at', 'change', 'receipts', 'previous'} and plan.get('schema') == 'samsung-cutover/v1' and
             plan.get('context') == CONTEXT and type(dry_run) is bool, '계획 형식·context가 잘못되었다')
    _require(fingerprint(plan) == reviewed_sha256, '계획 지문이 검토한 값과 다르다')
    current, prepared = _time(now), _time(plan['prepared_at'])
    _require(timedelta(0) <= current - prepared <= timedelta(minutes=15), '계획이 만료됐거나 미래 시각이다')
    environment, change = plan['environment'], plan['change']
    prepare_plan(environment, plan['stage'], change, receipts=plan['receipts'], previous=plan['previous'], now=now)
    dry_request = cas_request(environment, change, dry_run=True)
    if not dry_run:
        _require(isinstance(dry_run_receipt, dict) and dry_run_receipt.get('state') == 'server_dry_run' and
                 dry_run_receipt.get('nonpersistent_readback') is True and
                 dry_run_receipt.get('context') == CONTEXT and dry_run_receipt.get('plan_sha256') == reviewed_sha256 and
                 dry_run_receipt.get('request_sha256') == fingerprint(dry_request), '같은 계획의 실제 server dry-run 영수증이 필요하다')
        _require(prepared <= _time(dry_run_receipt.get('observed_at')) <= current and
                 current - _time(dry_run_receipt.get('observed_at')) <= timedelta(minutes=15), 'dry-run 영수증 시각이 잘못되었다')
    desired = change['desired'] if change['action'] == 'apply' else change['before']
    path = _target(environment, desired)[1]
    get = {'method': 'GET', 'path': path, 'body': None, 'content_type': 'application/json'}
    status, before = _send(transport, get)
    if change.get('before') is None:
        _require(status == 404, '생성 대상이 없음이 확인되지 않았다. 덮어쓰지 않는다')
    else:
        _require(status == 200 and _target(environment, before)[1] == path and
                 _identity(before) == _identity(change['before']), '검토 이후 UID/RV가 바뀌었다. 기존 조건을 갱신하지 않는다')
    status, result = _send(transport, cas_request(environment, change, dry_run=dry_run))
    _require(status in (200, 201, 202), '서버가 변경을 성공으로 확인하지 않았다')
    receipt = {'context': CONTEXT, 'plan_sha256': reviewed_sha256, 'observed_at': now,
               'request_sha256': fingerprint(dry_request), 'response_sha256': fingerprint(result)}
    if change['action'] != 'delete':
        receipt['response'] = _verify_object(environment, change, result)
    else:
        _require(isinstance(result, dict), 'DELETE 성공 본문을 확인할 수 없다')
        if result.get('kind') == 'Status':
            _require(result.get('status') == 'Success', 'DELETE 응답이 성공 상태가 아니다')
            uid = result.get('details', {}).get('uid')
            _require(uid is None or uid == _identity(change['before'])['uid'], 'DELETE 응답 UID가 다르다')
        else:
            _require(_target(environment, result)[1] == path and
                     result.get('metadata', {}).get('uid') == _identity(change['before'])['uid'], 'DELETE 응답 대상이 다르다')
    status, readback = _send(transport, get)
    if dry_run:
        if change.get('before') is None:
            _require(status == 404, 'server dry-run 뒤 생성 대상이 존재한다')
        else:
            _require(status == 200 and _target(environment, readback)[1] == path and
                     _identity(readback) == _identity(change['before']), 'server dry-run 뒤 UID/RV가 변했다')
        return dict(receipt, state='server_dry_run', nonpersistent_readback=True)
    if change['action'] == 'delete':
        _require(status == 404, '삭제 완료를 확인하지 못했다. 대기·재생성 객체를 삭제하거나 재시도하지 않는다')
        receipt['readback'] = {'absent': True}
    else:
        _require(status == 200, '정확한 대상의 재조회에 실패했다')
        identity = _verify_object(environment, change, readback)
        _require(identity['uid'] == receipt['response']['uid'], '변경 응답과 재조회 UID가 다르다')
        receipt['readback'] = identity
        receipt['readback_sha256'] = fingerprint(readback)
    return dict(receipt, state='readback_verified')


class KubernetesTransport:
    """부모가 명시적으로 생성할 때만 고정 context의 API 클라이언트를 연다.

    kubernetes 패키지는 실제 실행 경로에만 필요하다. 자격·서버 오류 본문은 반환하지 않는다.
    """
    def __init__(self, *, context=CONTEXT):
        _require(context == CONTEXT, 'rancher-desktop context만 허용한다')
        try:
            import importlib
            package = importlib.import_module('kubernetes')
            client, config = package.client, package.config
        except ImportError:
            raise CutoverError('부모 실행 환경에 kubernetes 패키지가 필요하다') from None
        try:
            contexts, _ = config.list_kube_config_contexts()
            _require(sum(c.get('name') == CONTEXT for c in contexts or []) == 1, '고정 context를 유일하게 확인할 수 없다')
            configuration = client.Configuration()
            config.load_kube_config(context=CONTEXT, client_configuration=configuration, persist_config=False)
            from urllib.parse import urlsplit
            _require(configuration.verify_ssl is True and urlsplit(configuration.host).scheme == 'https',
                     'HTTPS 인증서 검증 없는 API 연결은 허용하지 않는다')
            configuration.retries = 0
            self._client = client.ApiClient(configuration=configuration)
        except CutoverError:
            raise
        except Exception:
            raise CutoverError('고정 context 클라이언트 준비에 실패했다') from None

    def context(self):
        return CONTEXT

    def request(self, request):
        """검토 실행기가 만든 메서드·경로·본문을 변형 없이 API로 보낸다."""
        from urllib.parse import parse_qsl, urlsplit
        parsed = urlsplit(request['path'])
        allowed = set()
        collections = set()
        for env in ENVIRONMENTS:
            for kind, (api, _, names) in RESOURCES.items():
                for name in names:
                    base, path = _target(env, {'apiVersion': api, 'kind': kind,
                                              'metadata': {'namespace': env, 'name': name}})
                    allowed.add(path)
                    collections.add(base)
        _require(not parsed.scheme and not parsed.netloc and not parsed.fragment and parsed.query in ('', 'dryRun=All') and
                 ((request['method'] in ('GET', 'PATCH', 'DELETE') and parsed.path in allowed) or
                  (request['method'] == 'POST' and parsed.path in collections)), '전송 경로·메서드가 허용목록 밖이다')
        body: Any = request['body']
        if request['method'] == 'PATCH':
            _require(isinstance(body, list) and len(body) >= 3 and
                     all(isinstance(op, dict) for op in body[:2]) and
                     [op.get('path') for op in body[:2]] == ['/metadata/uid', '/metadata/resourceVersion'] and
                     all(op.get('op') == 'test' for op in body[:2]), '기존 변경에는 UID/RV 원자 검사가 먼저 필요하다')
            _identity({'metadata': {'uid': body[0].get('value'), 'resourceVersion': body[1].get('value')}})
            _require(request['content_type'] == 'application/json-patch+json', 'JSON Patch 전송만 허용한다')
        if request['method'] == 'DELETE':
            _require(isinstance(body, dict) and body.get('kind') == 'DeleteOptions' and
                     isinstance(body.get('preconditions'), dict) and
                     ('dryRun' not in body or body['dryRun'] == ['All']) and not parsed.query,
                     '삭제에는 본문 DeleteOptions와 올바른 dryRun이 필요하다')
            _identity({'metadata': body['preconditions']})
        try:
            data, status, _ = self._client.call_api(parsed.path, request['method'],
                query_params=parse_qsl(parsed.query), body=request['body'],
                header_params={'Content-Type': request['content_type'], 'Accept': 'application/json'},
                response_types_map={200: 'object', 201: 'object', 202: 'object'}, auth_settings=['BearerToken'],
                _return_http_data_only=False, _preload_content=True, _request_timeout=20)
            return status, data
        except Exception as exc:
            status = getattr(exc, 'status', None)
            if type(status) is int and status >= 400:
                return status, {}
            raise CutoverError('API 전송 결과가 불명이다. 재시도하지 않는다') from None
