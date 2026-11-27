"""Independently check queue pressure and reservation results at a fixed request rate."""

from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import re
from time import monotonic, sleep
from urllib.error import URLError

from evaluation.matched_recovery_campaign import write_new
from evaluation.verify_jdbc_fixture import DATA, get, invoke


ARRIVAL_MS = 350
WINDOW_S = 30


def values(origin):
    status, body = get(origin, "/actuator/prometheus")
    queue = re.search(rb"^reservation_queue_waiting(?:\{[^}]+\})?\s+([.0-9]+)$", body, re.M)
    elapsed = re.search(rb"^reservation_database_execution_seconds_sum(?:\{[^}]+\})?\s+([.0-9]+)$", body, re.M)
    observed = re.search(rb"^reservation_database_execution_seconds_count(?:\{[^}]+\})?\s+([.0-9]+)$", body, re.M)
    if not (status == 200 and queue and elapsed and observed):
        raise ValueError("queue and database timings are not available on the fixture")
    return {"queued": float(queue[1]), "database_seconds": float(elapsed[1]), "database_calls": float(observed[1])}


def sample(origin, steady_seconds=WINDOW_S, *, metric_origins=None):
    if not 10 <= steady_seconds <= 90:
        raise ValueError("steady load window is outside the calibrated bounds")
    requests, snapshots = [], []
    started = monotonic()
    with ThreadPoolExecutor(max_workers=18) as workers:
        for index in range(math.ceil(steady_seconds * 1000 / ARRIVAL_MS)):
            sleep(max(0, started + index * ARRIVAL_MS / 1000 - monotonic()))
            requests.append(workers.submit(invoke, origin, DATA[index % len(DATA)], "standard"))
            if (index > 0 and index % 10 == 0):
                snapshots.append([workers.submit(values, address)
                                  for address in (metric_origins or (origin,))])
        outcomes = []
        for future in requests:
            try:
                outcomes.append(future.result())
            except (OSError, URLError) as exc:
                reason = exc.reason if isinstance(exc, URLError) else exc
                outcomes.append({"status": None, "contract_ok": False, "duration_ms": None,
                                 "transport": "timeout" if isinstance(reason, TimeoutError) else "unavailable"})
        windows = [[future.result() for future in group] for group in snapshots]
    counters = [row for group in windows for row in group]
    tail = outcomes[len(outcomes)//2:]
    durations = sorted(row["duration_ms"] for row in tail if row["duration_ms"] is not None)
    return {"requests": outcomes, "snapshots": counters, "interval_ms": ARRIVAL_MS, "window_s": steady_seconds,
            "tail_all_correct": all(row["contract_ok"] for row in tail),
            "tail_p95_ms": durations[max(0, math.ceil(len(durations)*.95)-1)] if durations else None,
            "tail_timeouts": sum(row.get("transport") == "timeout" for row in tail),
            "queue_per_window": [max(row["queued"] for row in group) for group in windows],
            "max_waiters": max((row["queued"] for row in counters), default=None)}


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("origin")
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if not args.origin.startswith("http://127.0.0.1:"):
        parser.error("use the prepared loopback fixture proxy")
    receipt = sample(args.origin)
    write_new(args.receipt, receipt)
    print(json.dumps({k: receipt[k] for k in ("tail_all_correct", "tail_p95_ms", "max_waiters")}))
