"""Run only the existing Application analysis queue consumer, without an HTTP server or runner credentials."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import shutil
import signal
import stat
import sys
import threading
import time

from .control import ControlPlane
from .settings import ApplicationSettings
from .storage import RunnerObservation, Store
from .worker import AnalysisWorker

PULSE_ID = 5  # Separate from external source/cluster/QA identities (1..4).
PULSE_SECONDS = 15
FRESH_SECONDS = 60
MARKER = Path('/tmp/liveprobe-model-worker-ready')


def cli_configured(settings):
    return bool(settings.worker_enabled and settings.codex_home and (Path(settings.codex_home) / 'auth.json').is_file()
                and shutil.which('codex'))


def observed(store, now):
    if not store.ready():
        return False
    with store.read() as session:
        pulse = session.get(RunnerObservation, PULSE_ID)
        return bool(pulse and 0 <= now - pulse.last_claim_at < FRESH_SECONDS)


def pulse(control, marker):
    if not cli_configured(control.settings) or not control.store.ready():
        return False
    login = (Path(control.settings.codex_home) / 'auth.json').lstat()
    if not stat.S_ISREG(login.st_mode) or login.st_uid != os.geteuid() or stat.S_IMODE(login.st_mode) != 0o600:
        return False
    stamp = control.clock()
    with control.store.transaction() as session:
        record = session.get(RunnerObservation, PULSE_ID)
        if record is None:
            session.add(RunnerObservation(id=PULSE_ID, last_claim_at=stamp))
        else:
            record.last_claim_at = stamp
    marker.touch(mode=0o600)
    return True


def run(control, stop, *, worker=None, marker=MARKER):
    worker = worker or AnalysisWorker(control)
    ready = threading.Event()
    def observe():
        while not stop.is_set():
            try:
                if pulse(control, marker):
                    ready.set()
                else:
                    ready.clear()
            except Exception:
                ready.clear()
                logging.getLogger(__name__).warning('model_worker_heartbeat_unavailable')
            stop.wait(PULSE_SECONDS)
    heartbeat = threading.Thread(target=observe, name='model-availability', daemon=True)
    heartbeat.start()
    from .display_readings import start_worker
    display = start_worker(control, stop, AnalysisWorker(control)._provider, ready) if isinstance(worker, AnalysisWorker) else None
    try:
        while not stop.is_set():
            if ready.is_set():
                try:
                    worker.run_once()
                except Exception:
                    logging.getLogger(__name__).warning('model_worker_analysis_unavailable')
            stop.wait(.5)
    finally:
        stop.set()
        heartbeat.join()
        if display:
            display.join()
        marker.unlink(missing_ok=True)


def main():
    if sys.argv[1:] == ['--ready']:
        return int(not (MARKER.is_file() and 0 <= time.time() - MARKER.stat().st_mtime < FRESH_SECONDS))
    if sys.argv[1:]:
        return 2
    settings = ApplicationSettings.from_env()
    if (not settings.worker_enabled or not settings.isolated_database or settings.machine_identity_file or
            settings.identity_file or settings.local_operator_key_file or settings.github_read_token_file or not settings.codex_home):
        raise SystemExit('isolated model worker configuration required')
    # The control-plane API owns schema initialization. This consumer only uses the existing model claim and fence.
    store = Store(settings)
    control = ControlPlane(store, settings)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        run(control, stop)
    finally:
        if store.engine is not None:
            store.engine.dispose()
    return 0


if __name__ == '__main__':
    sys.exit(main())
