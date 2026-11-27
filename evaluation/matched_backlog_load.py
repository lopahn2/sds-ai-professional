"""Drive a fixed stream of real reservation reads and verify sustained recovery."""

from concurrent.futures import ThreadPoolExecutor
from itertools import cycle
from math import ceil
from threading import Event, Lock, Thread
from time import monotonic
from urllib.error import URLError

from evaluation.verify_backlog import ARRIVAL_MS
from evaluation.verify_jdbc_fixture import DATA, invoke


class ReservationTraffic:
    def __init__(self, origin):
        self.origin = origin
        self.stopping = Event()
        self.started = Event()
        self.lock = Lock()
        self.begin = None
        self.outcomes = []
        self.thread = Thread(target=self.dispatch, name="backlog-reservation-load")

    def start(self):
        self.thread.start()
        if not self.started.wait(5):
            raise OSError("the fixed-rate workload did not start")
        return self.begin

    def stop(self):
        if self.thread.ident is None:
            return
        self.stopping.set()
        self.thread.join(9)
        if self.thread.is_alive():
            raise OSError("the fixed-rate workload did not stop")

    def dispatch(self):
        with ThreadPoolExecutor(max_workers=24) as workers:
            pending = []
            self.begin = monotonic()
            self.started.set()
            for index, item in enumerate(cycle(DATA)):
                tick = self.begin + index * ARRIVAL_MS / 1000
                if self.stopping.wait(max(0, tick-monotonic())):
                    return
                pending = [future for future in pending if not future.done()]
                if len(pending) >= 24:
                    self.record(tick, {"status": None, "duration_ms": None, "contract_ok": False, "transport": "dropped"})
                else:
                    pending.append(workers.submit(self.read, tick, item))

    def record(self, tick, row):
        with self.lock:
            self.outcomes.append((tick, row))

    def read(self, tick, item):
        try:
            row = invoke(self.origin, item, "standard")
        except (OSError, URLError) as exc:
            reason = exc.reason if isinstance(exc, URLError) else exc
            row = {"status": None, "duration_ms": None, "contract_ok": False,
                   "transport": "timeout" if isinstance(reason, TimeoutError) else "unavailable"}
        self.record(tick, row)

    def window(self, start, stop):
        with self.lock:
            samples = sorted((tick, row) for tick, row in self.outcomes if start <= tick < stop)
        return [row for _, row in samples]


def stable_recovery(load, since, now, samples, pod_ids):
    """Two Ready backends must recover their queues and contracts under the original arrival rate."""
    if now - since < 25:
        return None
    windows = [item for item in samples if since <= item["time"] <= now and item["pods"] == pod_ids]
    if len(windows) < 3:
        return None
    tail = [max(item["queues"]) for item in windows[-3:]]
    rows = load.window(now-20, now-5)
    if len(rows) < 40 or not all(row["contract_ok"] for row in rows) or max(tail) > 2 or tail[-1] > 0:
        return None
    elapsed = sorted(row["duration_ms"] for row in rows)
    return {"pod_uids": pod_ids, "queue_windows": tail, "sample_requests": rows,
            "p95_ms": elapsed[ceil(len(elapsed)*.95)-1], "interval_ms": ARRIVAL_MS}
