"""단일 프로세스의 HTTP·TLS 리스너와 모드별 내부 작업자를 실행한다."""
import asyncio
from contextlib import suppress
import logging
import signal
import sys
import threading

import uvicorn

from .api import create_apps
from .config import Settings
from .store import Store
from .workers import BusinessClient, DirectoryConsumer, KafkaProducer, KafkaSource, OutboxPublisher

LOGGER = logging.getLogger('samsung-domain')


class ManagedServer(uvicorn.Server):
    def capture_signals(self):
        from contextlib import nullcontext
        return nullcontext()


async def serve(settings, store, stop):
    client = BusinessClient(settings) if settings.mode != 'business' else None
    transport = KafkaProducer(settings.kafka) if settings.mode == 'business' else KafkaSource(settings)
    worker = OutboxPublisher(store, transport) if settings.mode == 'business' else DirectoryConsumer(store, transport, client)
    thread = threading.Thread(target=worker.run, args=(stop,), name='domain-internal-worker', daemon=True)
    public, protected = create_apps(settings, store, client)
    common = dict(host='0.0.0.0', proxy_headers=False, access_log=False, log_level='warning',
        log_config=None, server_header=False, timeout_graceful_shutdown=10, limit_concurrency=100)
    http_server = ManagedServer(uvicorn.Config(public, port=settings.http_port, **common))
    tls_server = ManagedServer(uvicorn.Config(protected, port=settings.tls_port,
        ssl_certfile=settings.paths['TLS_CERT_FILE'], ssl_keyfile=settings.paths['TLS_KEY_FILE'], **common))
    loop = asyncio.get_running_loop()
    def shutdown():
        stop.set()
        http_server.should_exit = True
        tls_server.should_exit = True
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown)
    thread.start()
    tasks = [asyncio.create_task(http_server.serve()), asyncio.create_task(tls_server.serve())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        shutdown()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(thread.join, 17)
        if thread.is_alive():
            LOGGER.warning('내부 작업자 종료 확인 전 프로세스를 종료합니다. 불명 ACK는 DB 성공으로 바꾸지 않습니다')
        else:
            transport.close()
            if client:
                client.close()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
    for task in tasks:
        if task.cancelled() or task.exception():
            raise RuntimeError('서비스 리스너가 비정상 종료했습니다')


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    stop = threading.Event()
    store = None
    try:
        settings = Settings.from_env()
        store = Store(settings.dsn, mode=settings.mode, environment=settings.environment)
        store.initialize()
        asyncio.run(serve(settings, store, stop))
    except (Exception, SystemExit):
        # 예외 문자열·설정 객체에는 보호된 연결 정보가 있을 수 있어 출력하지 않는다.
        LOGGER.error('런타임 시작 또는 실행 실패: 보호된 설정·DB·TLS 상태를 확인해 주세요')
        return 1
    finally:
        stop.set()
        if store:
            with suppress(Exception):
                store.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
