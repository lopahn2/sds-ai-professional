"""원장·투영·대상 순번·outbox를 한 PostgreSQL TX로 저장한다."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import uuid

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

TARGETS = ('samsung-delivery', 'samsung-trade')
SYSTEMS = {'business': 'samsung-business', 'trade': 'samsung-trade', 'delivery': 'samsung-delivery'}
FIELDS = {
    'employee': {'name', 'title', 'department', 'target_systems', 'active'},
    'contract': {'title', 'employee_id', 'status', 'target_systems', 'active'},
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def now():
    return datetime.now(timezone.utc).isoformat()


class DomainError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def validate_id(entity_id):
    if not isinstance(entity_id, str) or not re.fullmatch(r'[A-Za-z0-9가-힣_-]{1,80}', entity_id):
        raise DomainError(422, '식별자 형식이 올바르지 않습니다')


def validate_data(kind, data):
    if kind not in FIELDS or not isinstance(data, dict) or set(data) - FIELDS[kind]:
        raise DomainError(422, '허용되지 않은 원장 필드입니다')
    targets = data.get('target_systems')
    if not isinstance(targets, list) or not targets or len(targets) != len(set(targets)) or any(t not in TARGETS for t in targets):
        raise DomainError(422, '대상 시스템을 정확히 지정해 주세요')
    for key, value in data.items():
        if key == 'target_systems':
            continue
        if key == 'active':
            if type(value) is not bool:
                raise DomainError(422, '활성 상태는 참 또는 거짓이어야 합니다')
        elif not isinstance(value, str) or not value.strip() or len(value) > 160:
            raise DomainError(422, '원장 문자열은 1~160자여야 합니다')
    if not data.get('name' if kind == 'employee' else 'title'):
        raise DomainError(422, '이름 또는 계약 제목이 필요합니다')
    if kind == 'contract' and data.get('status') not in ('active', 'draft', 'approved', 'terminated', 'cancelled', 'revoked'):
        raise DomainError(422, '지원하지 않는 계약 상태입니다')


def visible(kind, data):
    return data.get('active', True) and not (kind == 'contract' and data.get('status') in ('terminated', 'cancelled', 'revoked'))


from .replica import ReplicationMixin


class Store(ReplicationMixin):
    def __init__(self, dsn, *, mode, environment):
        if mode not in SYSTEMS or environment not in ('dev', 'stg', 'prd'):
            raise ValueError('런타임 모드 또는 환경이 올바르지 않습니다')
        self.mode, self.environment, self.system = mode, environment, SYSTEMS[mode]
        self.pool = ConnectionPool(dsn, min_size=1, max_size=4, timeout=3, open=False,
            kwargs={'row_factory': dict_row, 'options': '-c search_path=samsung_domain -c statement_timeout=60000 -c lock_timeout=3000'})
        self.pool.open(wait=True, timeout=5)

    def close(self):
        self.pool.close()

    @contextmanager
    def tx(self):
        with self.pool.connection() as conn:
            with conn.transaction():
                yield conn

    def initialize(self):
        with self.tx() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(716232881)')
            conn.execute(Path(__file__).with_name('schema.sql').read_text())
            conn.execute('INSERT INTO identity(environment, system_name) VALUES (%s,%s) ON CONFLICT DO NOTHING', (self.environment, self.system))
            row = conn.execute('SELECT * FROM identity').fetchone()
            if (row['environment'], row['system_name']) != (self.environment, self.system):
                raise DomainError(503, 'DB 소유 환경 또는 시스템이 일치하지 않습니다')

    def ready(self):
        with self.tx() as conn:
            return conn.execute('SELECT 1 AS ok').fetchone()['ok'] == 1

    def write(self, kind, entity_id, changes, *, create=False, expected_version=None, content=None):
        if self.mode != 'business':
            raise DomainError(403, '소비 시스템은 원장을 변경할 수 없습니다')
        validate_id(entity_id)
        if kind not in FIELDS or not isinstance(changes, dict) or set(changes) - FIELDS[kind]:
            raise DomainError(422, '허용되지 않은 원장 필드입니다')
        if not create and (type(expected_version) is not int or expected_version < 1):
            raise DomainError(422, 'expected_version이 필요합니다')
        if content is not None and (kind != 'contract' or not isinstance(content, str) or len(content.encode()) > 1024 * 1024):
            raise DomainError(422, '계약 원문 크기 또는 유형이 올바르지 않습니다')
        with self.tx() as conn:
            # 대상 잠금이 TX 종료까지 유지되므로 순번은 실제 커밋 순서를 따른다.
            conn.execute('SELECT target FROM target_state ORDER BY target FOR UPDATE').fetchall()
            if conn.execute('SELECT count(*) AS n FROM outbox WHERE acked_at IS NULL').fetchone()['n'] >= 10000:
                raise DomainError(503, '미발행 원장이 많아 새 쓰기를 중지했습니다')
            old = conn.execute('SELECT * FROM entities WHERE entity_type=%s AND entity_id=%s FOR UPDATE', (kind, entity_id)).fetchone()
            if create and old:
                raise DomainError(409, '이미 존재하는 식별자입니다')
            if not create and not old:
                raise DomainError(404, '원장 항목이 없습니다')
            if old and old['entity_version'] != expected_version:
                raise DomainError(409, '원장 버전이 변경되었습니다')
            data = {**(old['data'] if old else {'active': True, **({'status': 'active'} if kind == 'contract' else {})}), **changes}
            validate_data(kind, data)
            version = old['entity_version'] + 1 if old else 1
            document = content if content is not None else (old['content'] if old else '')
            conn.execute('INSERT INTO entities VALUES (%s,%s,%s,%s,%s) ON CONFLICT(entity_type,entity_id) DO UPDATE SET entity_version=excluded.entity_version,data=excluded.data,content=excluded.content', (kind, entity_id, version, Jsonb(data), document))
            before = set(old['data']['target_systems']) if old else set()
            after = set(data['target_systems'])
            for target in sorted(before | after):
                operation = 'upsert' if target in after and visible(kind, data) else 'revoke'
                seq = conn.execute('UPDATE target_state SET last_committed_seq=last_committed_seq+1 WHERE target=%s RETURNING last_committed_seq', (target,)).fetchone()['last_committed_seq']
                payload = {k: v for k, v in data.items() if k != 'target_systems'} if operation == 'upsert' else {'revoked': True}
                event = dict(event_id=str(uuid.uuid4()), schema_version=1, entity_type=kind, entity_id=entity_id,
                    entity_version=version, target_seq=seq, event_type=f'{kind}.{operation}', operation=operation,
                    environment=self.environment, source_system='samsung-business', target_system=target, occurred_at=now(), payload=payload)
                wire = canonical(event)
                if len(wire.encode()) > 65536:
                    raise DomainError(503, '연계 이벤트 크기 제한을 초과했습니다')
                conn.execute('INSERT INTO projections VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(target,entity_type,entity_id) DO UPDATE SET entity_version=excluded.entity_version,target_seq=excluded.target_seq,operation=excluded.operation,payload=excluded.payload', (target, kind, entity_id, version, seq, operation, Jsonb(payload)))
                conn.execute('INSERT INTO outbox(event_id,target,target_seq,event_key,wire) VALUES (%s,%s,%s,%s,%s)', (event['event_id'], target, seq, f'{kind}:{entity_id}', wire))
            return dict(id=entity_id, entity_type=kind, entity_version=version, **data)

    def pending(self, target, limit=100):
        with self.tx() as conn:
            rows = conn.execute('SELECT * FROM outbox WHERE target=%s AND acked_at IS NULL ORDER BY target_seq LIMIT %s', (target, limit)).fetchall()
            return [dict(row, event=json.loads(row['wire'])) for row in rows]

    def watermark(self, target):
        if self.mode != 'business' or target not in TARGETS:
            raise DomainError(403, '원장 watermark 대상이 올바르지 않습니다')
        with self.tx() as conn:
            state = conn.execute('SELECT * FROM target_state WHERE target=%s FOR UPDATE', (target,)).fetchone()
            if state['watermark_wire']:
                return json.loads(state['watermark_wire'])
            if conn.execute('SELECT 1 FROM outbox WHERE target=%s AND acked_at IS NULL LIMIT 1', (target,)).fetchone():
                return None
            if state['watermark_at'] and (datetime.now(timezone.utc) - state['watermark_at']).total_seconds() < 30:
                return None
            seq = state['last_committed_seq']
            event = dict(event_id=str(uuid.uuid4()), schema_version=1, entity_type='watermark', entity_id='control',
                entity_version=0, target_seq=seq, event_type='watermark', operation='watermark',
                environment=self.environment, source_system='samsung-business', target_system=target,
                occurred_at=now(), payload={'barrier_seq': seq})
            conn.execute('UPDATE target_state SET watermark_wire=%s,watermark_barrier=%s WHERE target=%s', (canonical(event), seq, target))
            return event

    def ack_watermark(self, target, event_id):
        with self.tx() as conn:
            conn.execute("UPDATE target_state SET watermark_at=now(),watermark_wire=NULL WHERE target=%s AND watermark_wire::jsonb->>'event_id'=%s", (target, event_id))

    def ack(self, event_id):
        with self.tx() as conn:
            conn.execute('UPDATE outbox SET acked_at=now() WHERE event_id=%s AND acked_at IS NULL', (event_id,))

    def members(self, search=''):
        with self.tx() as conn:
            rows = conn.execute("SELECT id,name,created_at FROM members WHERE position(%s in name)>0 ORDER BY created_at,id LIMIT 1000", (search[:80],)).fetchall()
            total = conn.execute('SELECT count(*) AS n FROM members').fetchone()['n']
            return dict(members=[dict(id=r['id'], name=r['name'], createdAt=r['created_at'].isoformat()) for r in rows], total=total)

    def create_member(self, name):
        from psycopg.errors import UniqueViolation
        if self.mode == 'business':
            raise DomainError(403, '고객 회원은 각 소비 시스템이 소유합니다')
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
            raise DomainError(422, '이름은 1~40자로 입력해 주세요')
        try:
            with self.tx() as conn:
                conn.execute('SELECT pg_advisory_xact_lock(716232882)')
                if conn.execute('SELECT count(*) AS n FROM members').fetchone()['n'] >= 1000:
                    raise DomainError(409, '합성 고객 회원 한도입니다')
                row = conn.execute('INSERT INTO members(id,name) VALUES (%s,%s) RETURNING *', (str(uuid.uuid4()), name.strip())).fetchone()
                return dict(id=row['id'], name=row['name'], createdAt=row['created_at'].isoformat())
        except UniqueViolation:
            raise DomainError(409, '이미 등록된 이름입니다') from None

    def confirm(self, kind, entity_id, target=None, *, include_content=False):
        if self.mode != 'business':
            raise DomainError(503, '원장 확인은 비즈니스에서 수행해야 합니다')
        with self.tx() as conn:
            row = conn.execute('SELECT * FROM entities WHERE entity_type=%s AND entity_id=%s FOR SHARE', (kind, entity_id)).fetchone()
            if not row:
                raise DomainError(404, '원장 항목이 없습니다')
            if not visible(kind, row['data']) or (target and target not in row['data']['target_systems']):
                raise DomainError(403, '현재 원장에서 허용되지 않는 조직 또는 비활성 항목입니다')
            response = dict(id=entity_id, entity_version=row['entity_version'], confirmed_at=now(), authoritative=True)
            if include_content:
                response['content'] = row['content']
            return response
