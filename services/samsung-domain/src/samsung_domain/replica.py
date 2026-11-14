"""불변 snapshot과 세대별 inbox·투영·checkpoint를 원자 관리한다."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import time
import uuid

from psycopg.types.json import Jsonb


def digest(value):
    from .store import canonical
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('시간대 누락')
    return parsed


def validate_event(event, environment, target):
    from .store import FIELDS, validate_id
    required = {'event_id', 'schema_version', 'entity_type', 'entity_id', 'entity_version', 'target_seq',
        'environment', 'source_system', 'target_system', 'occurred_at', 'operation', 'payload'}
    if not isinstance(event, dict) or not required <= event.keys():
        raise ValueError('필수 필드 누락')
    if event['schema_version'] != 1 or type(event['schema_version']) is not int or event['environment'] != environment or event['target_system'] != target or event['source_system'] != 'samsung-business':
        raise ValueError('스키마 또는 소속 불일치')
    validate_id(event['event_id'])
    timestamp(event['occurred_at'])
    if type(event['target_seq']) is not int or event['target_seq'] < 0:
        raise ValueError('대상 순번 오류')
    if event['entity_type'] == 'watermark':
        if event['operation'] != 'watermark' or event['entity_id'] != 'control' or event['payload'] != {'barrier_seq': event['target_seq']}:
            raise ValueError('watermark 오류')
        return
    kind = event['entity_type']
    if kind not in FIELDS or event['operation'] not in ('upsert', 'revoke'):
        raise ValueError('유형 또는 동작 오류')
    validate_id(event['entity_id'])
    if type(event['entity_version']) is not int or event['entity_version'] < 1 or event['target_seq'] < 1:
        raise ValueError('원장 버전 오류')
    payload = event['payload']
    if not isinstance(payload, dict) or not payload:
        raise ValueError('전체 상태 누락')
    if event['operation'] == 'revoke':
        if payload != {'revoked': True}:
            raise ValueError('철회 표식 오류')
    else:
        from .store import validate_data, visible
        validate_data(kind, dict(payload, target_systems=[target]))
        if not visible(kind, payload):
            raise ValueError('비활성 upsert 오류')


class ReplicationMixin:
    @contextmanager
    def export_snapshot_tx(self, target):
        from .store import DomainError
        key = f'export:{self.environment}:{target}'
        with self.pool.connection() as conn:
            acquired = conn.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS locked', (key,)).fetchone()['locked']
            # 세션 잠금을 먼저 소유한 다음 새 TX에서 snapshot을 생성한다.
            # RR snapshot을 잡은 채 잠금을 기다리면 용량 count가 과거 값일 수 있다.
            conn.commit()
            if not acquired:
                raise DomainError(503, '같은 대상의 export 생성이 진행 중입니다')
            try:
                with conn.transaction():
                    conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                    yield conn
            finally:
                try:
                    conn.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))', (key,))
                    conn.commit()
                except Exception:
                    # 잠금 해제가 불명인 연결을 pool에 재사용하지 않는다.
                    conn.close()
                    raise

    def create_export(self, target):
        from .store import DomainError, TARGETS, canonical
        if self.mode != 'business' or target not in TARGETS:
            raise DomainError(403, '허용되지 않은 export 대상입니다')
        started = time.monotonic()
        with self.export_snapshot_tx(target) as conn:
            if conn.execute('SELECT count(*) AS n FROM exports WHERE target=%s AND expires_at>now()', (target,)).fetchone()['n'] >= 8:
                raise DomainError(503, '활성 export 저장 한도에 도달했습니다. 만료 후 다시 복구해 주세요')
            boundary = conn.execute('SELECT last_committed_seq FROM target_state WHERE target=%s', (target,)).fetchone()['last_committed_seq']
            rows = conn.execute("SELECT entity_type,entity_id,entity_version,target_seq,payload FROM projections WHERE target=%s AND operation='upsert' ORDER BY entity_type,entity_id", (target,)).fetchall()
            if len(canonical(rows).encode()) > 50 * 1024 * 1024 or time.monotonic() - started > 60:
                raise DomainError(503, 'export 생성 한도를 초과했습니다')
            created = datetime.now(timezone.utc)
            meta = dict(export_id=str(uuid.uuid4()), environment=self.environment, target_system=target,
                boundary_seq=boundary, created_at=created.isoformat(), expires_at=(created + timedelta(minutes=15)).isoformat(),
                item_count=len(rows), content_hash=digest(rows), schema_version=1)
            conn.execute('DELETE FROM exports WHERE expires_at < now()')
            conn.execute('INSERT INTO exports VALUES (%s,%s,%s,%s,%s)', (meta['export_id'], target, Jsonb(meta), Jsonb(rows), meta['expires_at']))
            if time.monotonic() - started > 60:
                raise DomainError(503, 'export 생성 시간이 제한을 초과했습니다')
            return dict(meta, items=rows)

    def export_page(self, export_id, target, page=0, page_size=500):
        from .store import DomainError
        if type(page) is not int or page < 0 or not 1 <= page_size <= 500:
            raise DomainError(422, '페이지 범위가 올바르지 않습니다')
        with self.tx() as conn:
            row = conn.execute('SELECT * FROM exports WHERE export_id=%s AND target=%s', (export_id, target)).fetchone()
            if not row:
                raise DomainError(404, 'export가 없습니다')
            if row['expires_at'] <= datetime.now(timezone.utc):
                raise DomainError(503, 'export가 만료되어 복구를 다시 시작해야 합니다')
            begin = page * page_size
            return dict(row['metadata'], items=row['items'][begin:begin + page_size], page=page,
                next_page=page + 1 if begin + page_size < len(row['items']) else None)

    def begin_rebuild(self, *, start_offset, log_start):
        from .store import DomainError
        if self.mode == 'business' or type(start_offset) is not int or type(log_start) is not int or start_offset < log_start or log_start < 0:
            raise DomainError(503, '복구 시작 offset이 보존 범위를 벗어났습니다')
        generation = str(uuid.uuid4())
        with self.tx() as conn:
            conn.execute('SELECT * FROM replica_state FOR UPDATE')
            conn.execute('INSERT INTO generations(generation,start_offset,checkpoint) VALUES (%s,%s,%s)', (generation, start_offset, start_offset))
            conn.execute('UPDATE replica_state SET rebuilding_generation=%s,dependency_error=false', (generation,))
        return generation

    def load_snapshot(self, generation, export):
        from .store import DomainError, canonical
        try:
            items = export['items']
            if export['environment'] != self.environment or export['target_system'] != self.system or export.get('schema_version', 1) != 1:
                raise ValueError('범위 불일치')
            if not isinstance(items, list) or len(items) != export['item_count'] or digest(items) != export['content_hash'] or len(canonical(items).encode()) > 50 * 1024 * 1024:
                raise ValueError('내용 해시 또는 항목 수 불일치')
            if timestamp(export['expires_at']) <= datetime.now(timezone.utc):
                raise ValueError('만료')
            boundary = export['boundary_seq']
            if type(boundary) is not int or boundary < 0:
                raise ValueError('경계 오류')
            keys = set()
            for item in items:
                event = dict(item, event_id='snapshot', schema_version=1, environment=self.environment,
                    target_system=self.system, source_system='samsung-business', occurred_at=datetime.now(timezone.utc).isoformat(), operation='upsert')
                validate_event(event, self.environment, self.system)
                key = (item['entity_type'], item['entity_id'])
                if item['target_seq'] > boundary or key in keys:
                    raise ValueError('중복 또는 경계 초과')
                keys.add(key)
        except (KeyError, ValueError, TypeError, DomainError):
            raise DomainError(503, 'snapshot 검증에 실패하여 복구를 다시 시작해야 합니다') from None
        with self.tx() as conn:
            state = conn.execute('SELECT * FROM replica_state FOR UPDATE').fetchone()
            row = conn.execute('SELECT * FROM generations WHERE generation=%s FOR UPDATE', (generation,)).fetchone()
            if not row or state['rebuilding_generation'] != generation or row['state'] != 'loading':
                raise DomainError(503, '현재 복구 작업이 아닙니다')
            for item in items:
                conn.execute('INSERT INTO replicas VALUES (%s,%s,%s,%s,%s,%s,%s)', (generation, item['entity_type'], item['entity_id'], item['entity_version'], item['target_seq'], 'upsert', Jsonb(item['payload'])))
            conn.execute("UPDATE generations SET boundary_seq=%s,applied_seq=%s,snapshot_expires_at=%s,state='replaying' WHERE generation=%s", (boundary, boundary, export['expires_at'], generation))

    def consume(self, wire, *, offset, generation=None, topic=None, partition=0):
        from .store import DomainError
        topic = topic or f'{self.environment}.business.{self.mode}.master.v1'
        raw = wire.encode() if isinstance(wire, str) else wire
        content_hash = hashlib.sha256(raw or b'').hexdigest()
        with self.tx() as conn:
            state = conn.execute('SELECT * FROM replica_state FOR UPDATE').fetchone()
            generation = generation or state['active_generation']
            gen = conn.execute('SELECT * FROM generations WHERE generation=%s FOR UPDATE', (generation,)).fetchone()
            if not gen or gen['boundary_seq'] is None or generation not in (state['active_generation'], state['rebuilding_generation']):
                raise DomainError(503, '읽기 모델 초기 복구가 필요합니다')
            if offset < gen['checkpoint']:
                return 'checkpoint_duplicate'
            if (conn.execute('SELECT count(*) AS n FROM inbox WHERE generation=%s', (generation,)).fetchone()['n'] >= 10000
                    or conn.execute('SELECT count(*) AS n FROM quarantine WHERE generation=%s AND NOT resolved', (generation,)).fetchone()['n'] >= 1000):
                raise DomainError(503, 'inbox 또는 격리 저장 한도에 도달하여 snapshot 재구축이 필요합니다')
            diagnostic = None
            event = None
            try:
                if topic != f'{self.environment}.business.{self.mode}.master.v1' or partition != 0 or len(raw or b'') > 65536:
                    raise ValueError('토픽·파티션·크기 오류')
                event = json.loads(raw)
                validate_event(event, self.environment, self.system)
                existing = conn.execute('SELECT content_hash FROM inbox WHERE generation=%s AND event_id=%s', (generation, event['event_id'])).fetchone()
                if existing and existing['content_hash'] != content_hash:
                    raise ValueError('동일 이벤트 ID의 내용 변경')
            except (ValueError, KeyError, TypeError, DomainError, UnicodeError):
                diagnostic = '이벤트 스키마·소속·중복 해시 검증 실패'
            if diagnostic:
                conn.execute('INSERT INTO quarantine(generation,topic,partition,kafka_offset,content_hash,diagnostic) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING', (generation, topic, partition, offset, content_hash, diagnostic))
            else:
                conn.execute('INSERT INTO inbox VALUES (%s,%s,%s) ON CONFLICT DO NOTHING', (generation, event['event_id'], content_hash))
                if event['entity_type'] == 'watermark':
                    # 오래된 control 메시지가 더 새 watermark를 덮지 못한다.
                    conn.execute('UPDATE generations SET watermark_seq=%s,watermark_at=%s WHERE generation=%s AND (watermark_at IS NULL OR watermark_at<%s)', (event['target_seq'], event['occurred_at'], generation, event['occurred_at']))
                elif event['target_seq'] > gen['boundary_seq']:
                    conn.execute('INSERT INTO replicas VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(generation,entity_type,entity_id) DO UPDATE SET entity_version=excluded.entity_version,target_seq=excluded.target_seq,operation=excluded.operation,payload=excluded.payload WHERE replicas.entity_version<excluded.entity_version', (generation, event['entity_type'], event['entity_id'], event['entity_version'], event['target_seq'], event['operation'], Jsonb(event['payload'])))
                    conn.execute('INSERT INTO applied_sequences VALUES (%s,%s) ON CONFLICT DO NOTHING', (generation, event['target_seq']))
                    # 역순 수신도 허용하되 빈 순번을 최신으로 주장하지 않는다.
                    applied = gen['applied_seq']
                    while conn.execute('SELECT 1 FROM applied_sequences WHERE generation=%s AND target_seq=%s', (generation, applied + 1)).fetchone():
                        applied += 1
                    conn.execute('UPDATE generations SET applied_seq=%s WHERE generation=%s', (applied, generation))
            conn.execute('UPDATE generations SET checkpoint=%s WHERE generation=%s', (offset + 1, generation))
            current = conn.execute('SELECT * FROM generations WHERE generation=%s', (generation,)).fetchone()
            if (current['state'] == 'active' and current['watermark_seq'] is not None
                    and current['watermark_seq'] >= current['boundary_seq'] and current['applied_seq'] >= current['watermark_seq']
                    and not conn.execute('SELECT 1 FROM quarantine WHERE generation=%s AND NOT resolved', (generation,)).fetchone()):
                conn.execute('UPDATE quarantine SET resolved=true WHERE generation IN (SELECT generation FROM generations WHERE created_at<%s)', (current['created_at'],))
            return 'quarantined' if diagnostic else 'committed'

    def activate(self, generation, *, end_offset):
        from .store import DomainError
        with self.tx() as conn:
            state = conn.execute('SELECT * FROM replica_state FOR UPDATE').fetchone()
            gen = conn.execute('SELECT * FROM generations WHERE generation=%s FOR UPDATE', (generation,)).fetchone()
            if not gen or state['rebuilding_generation'] != generation or gen['state'] != 'replaying' or gen['checkpoint'] < end_offset or gen['snapshot_expires_at'] <= datetime.now(timezone.utc):
                raise DomainError(503, '재생 미완료 또는 만료된 generation입니다')
            conn.execute("UPDATE generations SET state='active',end_offset=%s WHERE generation=%s", (end_offset, generation))
            conn.execute('UPDATE replica_state SET active_generation=%s,rebuilding_generation=NULL,dependency_error=false,observed_end_offset=%s', (generation, end_offset))
            # 새 세대가 W까지 확정된 경우에만 이전 격리 차단을 해제한다.
            if (gen['watermark_seq'] is not None and gen['watermark_seq'] >= gen['boundary_seq'] and gen['applied_seq'] >= gen['watermark_seq']
                    and not conn.execute('SELECT 1 FROM quarantine WHERE generation=%s AND NOT resolved', (generation,)).fetchone()):
                conn.execute('UPDATE quarantine SET resolved=true WHERE generation IN (SELECT generation FROM generations WHERE created_at<%s)', (gen['created_at'],))

    def observe_end_offset(self, offset):
        from .store import DomainError
        if type(offset) is not int or offset < 0:
            raise DomainError(503, '관측 Kafka end offset이 올바르지 않습니다')
        with self.tx() as conn:
            conn.execute('UPDATE replica_state SET observed_end_offset=%s', (offset,))

    def advance_checkpoint(self, generation, offset):
        from .store import DomainError
        with self.tx() as conn:
            state = conn.execute('SELECT * FROM replica_state FOR UPDATE').fetchone()
            generation = generation or state['active_generation']
            if generation not in (state['active_generation'], state['rebuilding_generation']):
                raise DomainError(503, '현재 generation이 아닙니다')
            conn.execute('UPDATE generations SET checkpoint=greatest(checkpoint,%s) WHERE generation=%s', (offset, generation))

    def maintenance(self):
        with self.tx() as conn:
            conn.execute("DELETE FROM outbox WHERE event_id IN (SELECT event_id FROM outbox WHERE acked_at<now()-interval '72 hours' LIMIT 1000)")
            conn.execute('DELETE FROM exports WHERE expires_at<now()')
            rows = conn.execute("SELECT generation FROM generations WHERE created_at<now()-interval '24 hours' AND generation NOT IN (SELECT active_generation FROM replica_state WHERE active_generation IS NOT NULL) AND generation NOT IN (SELECT rebuilding_generation FROM replica_state WHERE rebuilding_generation IS NOT NULL) AND generation NOT IN (SELECT generation FROM quarantine WHERE NOT resolved) LIMIT 10").fetchall()
            for row in rows:
                for table in ('replicas', 'inbox', 'applied_sequences', 'quarantine', 'generations'):
                    # table은 고정 허용목록이며 외부 입력이 아니다.
                    conn.execute(f'DELETE FROM {table} WHERE generation=%s', (row['generation'],))

    def dependency_failure(self, failed=True):
        with self.tx() as conn:
            conn.execute('UPDATE replica_state SET dependency_error=%s', (failed,))

    def sync_status(self):
        with self.tx() as conn:
            if self.mode == 'business':
                targets = conn.execute('SELECT target,last_committed_seq,watermark_at FROM target_state ORDER BY target').fetchall()
                pending = conn.execute('SELECT count(*) AS n FROM outbox WHERE acked_at IS NULL').fetchone()['n']
                return dict(state='degraded' if pending else 'source', pending_outbox=pending, targets=targets,
                    note='단일 브로커 ACK는 복제 내구성을 보장하지 않습니다')
            state = conn.execute('SELECT * FROM replica_state').fetchone()
            gen = conn.execute('SELECT * FROM generations WHERE generation=%s', (state['active_generation'],)).fetchone()
            quarantine = conn.execute('SELECT count(*) AS n FROM quarantine WHERE NOT resolved').fetchone()['n']
            current_quarantine = conn.execute('SELECT count(*) AS n FROM quarantine WHERE NOT resolved AND generation=%s', (state['active_generation'],)).fetchone()['n']
            inbox_count = conn.execute('SELECT count(*) AS n FROM inbox WHERE generation=%s', (state['active_generation'],)).fetchone()['n']
            storage_rebuild = inbox_count >= 10000 or current_quarantine >= 1000
            observed_lag = max(0, state['observed_end_offset'] - gen['checkpoint']) if gen and state['observed_end_offset'] is not None else None
            fresh = bool(gen and observed_lag == 0 and not storage_rebuild and not state['rebuilding_generation'] and not state['dependency_error'] and not quarantine
                and gen['watermark_at'] and 0 <= (datetime.now(timezone.utc) - gen['watermark_at']).total_seconds() <= 90
                and gen['applied_seq'] >= gen['watermark_seq'])
            return dict(state='fresh' if fresh else ('rebuilding' if state['rebuilding_generation'] or not gen else 'degraded'),
                generation=state['active_generation'], rebuilding_generation=state['rebuilding_generation'],
                checkpoint=gen['checkpoint'] if gen else None, boundary_seq=gen['boundary_seq'] if gen else None,
                applied_seq=gen['applied_seq'] if gen else None, watermark_seq=gen['watermark_seq'] if gen else None,
                watermark_at=gen['watermark_at'].isoformat() if gen and gen['watermark_at'] else None,
                unresolved_quarantine=quarantine, current_generation_quarantine=current_quarantine,
                observed_end_offset=state['observed_end_offset'], observed_offset_lag=observed_lag,
                inbox_count=inbox_count, storage_rebuild_required=storage_rebuild, dependency_error=state['dependency_error'],
                note='기준 watermark까지의 수렴이며 현재 순간 원장의 권한 확정을 대신하지 않습니다')

    def list_entities(self, kind):
        from .store import DomainError
        if kind not in ('employee', 'contract'):
            raise DomainError(422, '잘못된 원장 유형입니다')
        with self.tx() as conn:
            if self.mode == 'business':
                rows = conn.execute('SELECT entity_id,entity_version,data AS payload FROM entities WHERE entity_type=%s ORDER BY entity_id', (kind,)).fetchall()
            else:
                rows = conn.execute("SELECT entity_id,entity_version,payload FROM replicas WHERE generation=(SELECT active_generation FROM replica_state) AND entity_type=%s AND operation='upsert' ORDER BY entity_id", (kind,)).fetchall()
            return [dict(id=r['entity_id'], entity_version=r['entity_version'], **r['payload']) for r in rows]
