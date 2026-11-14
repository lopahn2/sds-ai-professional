"""실제 Confluent 어댑터와 DB 우선 ACK·checkpoint 작업자."""
import json
import logging
from pathlib import Path
import ssl
import threading
import time

from confluent_kafka import Consumer, KafkaError, Producer, TopicPartition
import httpx

from .store import DomainError, TARGETS, canonical

LOGGER = logging.getLogger('samsung-domain')
QUIET_KAFKA = logging.getLogger('samsung-domain.kafka-private')
QUIET_KAFKA.addHandler(logging.NullHandler())
QUIET_KAFKA.propagate = False


class KafkaProducer:
    def __init__(self, config):
        safe = dict(config)
        safe.pop('group.id', None)
        safe.update({'enable.idempotence': True, 'acks': 'all', 'max.in.flight.requests.per.connection': 1,
            'message.timeout.ms': 10000, 'retry.backoff.ms': 200, 'linger.ms': 5, 'log_level': 0})
        self.producer = Producer(safe, logger=QUIET_KAFKA)

    def send(self, topic, key, wire):
        result = []
        def delivered(error, _message):
            result.append(error is None)
        self.producer.produce(topic, key=key.encode(), value=wire.encode(), on_delivery=delivered)
        deadline = time.monotonic() + 12
        while not result and time.monotonic() < deadline:
            self.producer.poll(0.2)
        if not result or not result[0]:
            raise DomainError(503, 'Kafka 발행 ACK를 확인하지 못했습니다')

    def close(self):
        # flush 시간 초과는 DB ACK로 바꾸지 않는다.
        self.producer.flush(2)


class OutboxPublisher:
    def __init__(self, store, transport):
        self.store, self.transport = store, transport

    def publish_target(self, target):
        key = f'publisher:{self.store.environment}:{target}'
        with self.store.tx() as conn:
            acquired = conn.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS locked', (key,)).fetchone()['locked']
            if not acquired:
                return 0
            try:
                count = 0
                topic = f'{self.store.environment}.business.{target.removeprefix("samsung-")}.master.v1'
                for row in self.store.pending(target):
                    self.transport.send(topic, row['event_key'], row['wire'])
                    self.store.ack(row['event_id'])
                    count += 1
                watermark = self.store.watermark(target)
                if watermark:
                    self.transport.send(topic, 'control:watermark', canonical(watermark))
                    self.store.ack_watermark(target, watermark['event_id'])
                return count
            finally:
                conn.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))', (key,))

    def run(self, stop):
        retry_at = dict.fromkeys(TARGETS, 0.0)
        failures = dict.fromkeys(TARGETS, 0)
        while not stop.is_set():
            for target in TARGETS:
                if stop.is_set() or time.monotonic() < retry_at[target]:
                    continue
                try:
                    self.publish_target(target)
                    failures[target] = 0
                except Exception:
                    failures[target] += 1
                    retry_at[target] = time.monotonic() + min(30, 2 ** min(failures[target], 5))
                    LOGGER.warning('원장 대상 발행 실패: 재시도 대기, 대상=%s', target)
            try:
                self.store.maintenance()
            except Exception:
                LOGGER.warning('보존 기간이 지난 원장 기록 정리를 완료하지 못했습니다')
            stop.wait(0.5)


class BusinessClient:
    def __init__(self, settings):
        self.settings = settings
        context = ssl.create_default_context(cafile=settings.paths['BUSINESS_CA_FILE'])
        self.client = httpx.Client(base_url=settings.business_url, verify=context,
            headers={'Authorization': 'Bearer ' + Path(settings.paths['BUSINESS_TOKEN_FILE']).read_text().strip()},
            timeout=httpx.Timeout(20, connect=3), trust_env=False, follow_redirects=False)

    def close(self):
        self.client.close()

    def request(self, method, path, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
            if response.status_code in (401, 403, 404, 409):
                raise DomainError(response.status_code, '비즈니스 원장이 현재 요청을 허용하지 않았습니다')
            if response.status_code >= 300:
                raise DomainError(503, '비즈니스 원장을 확인할 수 없습니다')
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise DomainError(503, 'TLS 원장 확인이 불가능하여 요청을 거절했습니다') from None

    def get(self, path):
        return self.request('GET', path)

    def snapshot(self):
        export = self.request('POST', '/api/v1/sync/exports', json={})
        items = list(export['items'])
        page = export.get('next_page')
        expected = 1
        while page is not None:
            if page != expected or len(items) > export['item_count']:
                raise DomainError(503, 'export 페이지 순서 오류')
            more = self.get(f'/api/v1/sync/exports/{export["export_id"]}?page={page}')
            for key in ('export_id', 'environment', 'target_system', 'boundary_seq', 'created_at', 'expires_at', 'item_count', 'content_hash', 'schema_version'):
                if more[key] != export[key]:
                    raise DomainError(503, 'export 페이지 불변 조건 불일치')
            items.extend(more['items'])
            if len(canonical(items).encode()) > 50 * 1024 * 1024:
                raise DomainError(503, 'export 크기 제한 초과')
            page = more.get('next_page')
            expected += 1
        return dict(export, items=items)


class KafkaSource:
    def __init__(self, settings):
        config = dict(settings.kafka)
        config.update({'group.id': settings.group, 'enable.auto.commit': False, 'enable.auto.offset.store': False,
            'auto.offset.reset': 'error', 'allow.auto.create.topics': False, 'enable.partition.eof': True,
            'isolation.level': 'read_committed', 'log_level': 0})
        for key in ('enable.idempotence', 'acks'):
            config.pop(key, None)
        self.consumer = Consumer(config, logger=QUIET_KAFKA)
        self.topic = settings.topic

    def bounds(self):
        metadata = self.consumer.list_topics(self.topic, timeout=5)
        topic = metadata.topics.get(self.topic)
        if not topic or topic.error or set(topic.partitions) != {0}:
            raise DomainError(503, '승인된 단일 파티션 토픽이 준비되지 않았습니다')
        return self.consumer.get_watermark_offsets(TopicPartition(self.topic, 0), timeout=5, cached=False)

    def seek(self, offset):
        self.consumer.assign([TopicPartition(self.topic, 0, offset)])

    def poll(self, timeout=0.5):
        message = self.consumer.poll(timeout)
        if message is None:
            return None
        if message.error():
            if message.error().code() == KafkaError._PARTITION_EOF:
                return {'eof': True, 'offset': message.offset()}
            raise DomainError(503, 'Kafka offset 또는 소비 연결 오류입니다')
        return dict(wire=message.value(), offset=message.offset(), topic=message.topic(), partition=message.partition())

    def commit(self, offset):
        self.consumer.commit(offsets=[TopicPartition(self.topic, 0, offset)], asynchronous=False)

    def close(self):
        self.consumer.close()


class DirectoryConsumer:
    def __init__(self, store, source, business):
        self.store, self.source, self.business = store, source, business

    def rebuild(self, stop):
        low, start = self.source.bounds()
        generation = self.store.begin_rebuild(start_offset=start, log_start=low)
        self.store.load_snapshot(generation, self.business.snapshot())
        low, end = self.source.bounds()
        if start < low:
            raise DomainError(503, '복구 중 Kafka 보존 경계를 초과했습니다')
        self.source.seek(start)
        checkpoint = start
        deadline = time.monotonic() + 60
        while checkpoint < end:
            if stop.is_set() or time.monotonic() > deadline:
                raise DomainError(503, '복구가 중단되거나 시간이 초과되었습니다')
            message = self.source.poll()
            if message is None:
                continue
            if message.get('eof'):
                checkpoint = max(checkpoint, message['offset'])
                self.store.advance_checkpoint(generation, checkpoint)
            else:
                self.store.consume(**message, generation=generation)
                checkpoint = message['offset'] + 1
        self.store.activate(generation, end_offset=end)
        self.source.commit(checkpoint)
        return checkpoint

    def cycle(self, stop):
        status = self.store.sync_status()
        low, high = self.source.bounds()
        self.store.observe_end_offset(high)
        checkpoint = status['checkpoint']
        must_rebuild = (checkpoint is None or checkpoint < low or checkpoint > high or status['rebuilding_generation']
            or status['current_generation_quarantine'] > 0 or status['storage_rebuild_required']
            or (status['watermark_seq'] is not None and status['applied_seq'] < status['watermark_seq']))
        if must_rebuild:
            checkpoint = self.rebuild(stop)
        else:
            self.source.seek(checkpoint)
        self.store.dependency_failure(False)
        # 매 호출은 유한하며 시작할 때 DB checkpoint를 다시 기준으로 삼는다.
        for _ in range(100):
            if stop.is_set():
                break
            message = self.source.poll()
            if message is None:
                break
            if message.get('eof'):
                self.store.advance_checkpoint(None, message['offset'])
                break
            self.store.consume(**message)
            self.source.commit(message['offset'] + 1)

    def run(self, stop):
        key = f'consumer:{self.store.environment}:{self.store.system}'
        failures = 0
        while not stop.is_set():
            try:
                with self.store.tx() as conn:
                    if not conn.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS locked', (key,)).fetchone()['locked']:
                        stop.wait(2)
                        continue
                    try:
                        # 모든 replica가 같은 DB 잠금을 사용하므로 checkpoint 역전이 없다.
                        while not stop.is_set():
                            self.cycle(stop)
                            self.store.maintenance()
                            failures = 0
                            stop.wait(0.5)
                    finally:
                        conn.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))', (key,))
            except Exception:
                failures += 1
                try:
                    self.store.dependency_failure(True)
                except Exception:
                    pass
                LOGGER.warning('소비·원장 복구 실패: 기존 고객 업무를 유지하고 민감 처리를 거절합니다')
                stop.wait(min(30, 2 ** min(failures, 5)))
