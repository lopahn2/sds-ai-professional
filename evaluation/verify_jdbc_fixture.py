"""Independently check the same reservation journey and pool state before or after a repair."""

from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, build_opener

from evaluation.matched_recovery_campaign import write_new


POOL = re.compile(r'^hikaricp_connections_(active|pending|max)\{pool="reservation",?\}\s+(\d+(?:\.\d+)?)$', re.M)
PROXY_FREE = build_opener(ProxyHandler({}))
DATA = (("stock-cedar", 5, 11), ("stock-maple", 4, 3), ("stock-cedar", 13, 11))
REQUEST_INTERVAL_S = 0.2


def get(origin: str, path: str) -> tuple[int, bytes]:
    try:
        with PROXY_FREE.open(origin + path, timeout=5) as response:
            return response.status, response.read(262_144)
    except HTTPError as response:
        return response.code, response.read(65_536)


def pool_snapshot(origin: str) -> dict:
    status, body = get(origin, "/actuator/prometheus")
    values = {name: float(value) for name, value in POOL.findall(body.decode())}
    if status != 200 or set(values) != {"active", "pending", "max"} or values["max"] != 4:
        raise ValueError("Hikari metrics or agreed pool capacity not observed")
    return values


def invoke(origin: str, workload: tuple[str, int, int], channel: str) -> dict:
    sku, quantity, available = workload
    began = monotonic()
    status, body = get(origin, "/api/reservations/availability?" + urlencode({
        "sku": sku, "quantity": quantity, "channel": channel,
    }))
    expected = {"sku": sku, "requested": quantity, "available": available, "fulfillable": quantity <= available}
    try:
        contract = status == 200 and json.loads(body) == expected
    except (UnicodeDecodeError, ValueError):
        contract = False
    return {"status": status, "contract_ok": contract, "duration_ms": round((monotonic() - began) * 1000)}


def verify(origin: str) -> dict:
    url = urlsplit(origin)
    if (url.scheme, url.hostname, url.path, url.username, url.query, url.fragment) != (
            "http", "127.0.0.1", "", None, "", "") or not url.port:
        raise ValueError("use the independent loopback probe of the isolated target")
    warmup = [invoke(origin, item, "standard") for item in DATA]
    base = pool_snapshot(origin)
    if not all(row["contract_ok"] for row in warmup) or base["active"] != 0 or base["pending"] != 0:
        raise ValueError("control journey must be healthy and pool idle before the load")
    futures = []
    with ThreadPoolExecutor(max_workers=len(DATA) * 3) as requests_in_flight:
        start = monotonic()
        for index, item in enumerate(DATA * 3):
            sleep(max(0, start + index * REQUEST_INTERVAL_S - monotonic()))
            futures.append(requests_in_flight.submit(invoke, origin, item, "partner"))
        requests = [future.result() for future in futures]
    sleep(1.2)  # Hikari's Micrometer value can cache its previous sample for up to one second.
    after = pool_snapshot(origin)
    verified = all(row["contract_ok"] for row in requests) and after == base
    reproduced = any(row["status"] == 500 for row in requests) and after["active"] == after["max"]
    return {"status": "recovered" if verified else "leak_reproduced" if reproduced else "unverified",
            "warmup": warmup, "baseline": base, "requests": requests, "after": after,
            "request_interval_ms": round(REQUEST_INTERVAL_S * 1000)}


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("origin")
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    evidence = verify(args.origin)
    write_new(args.receipt, evidence)
    raise SystemExit(2 if evidence["status"] == "unverified" else 0)
