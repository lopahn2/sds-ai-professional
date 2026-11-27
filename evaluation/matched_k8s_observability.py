"""Render native Prometheus rules for the two remaining existing dev test workloads."""

from argparse import ArgumentParser
import json
from pathlib import Path

import yaml


GROUP = "liveprobe-matched-dev"


def add_rules(raw):
    settings = yaml.safe_load(raw)
    groups = settings["groups"]
    if any(group["name"] == GROUP for group in groups):
        raise ValueError("the matched dev rules are already registered")
    caller_errors = ('sum by (namespace, app) (rate(http_server_requests_seconds_count'
                     '{namespace="dev",app="lp-process",method="GET",uri="/api/orders/check",status=~"5.."}[1m])) > 0.04')
    route = 'namespace="dev",app="lp-cpu",method="GET",uri="/api/reservations/availability"'
    latency = (f'(sum by (namespace,app) (rate(http_server_requests_seconds_sum{{{route}}}[1m])) / '
               f'sum by (namespace,app) (rate(http_server_requests_seconds_count{{{route}}}[1m])))')
    backlog = (f'{latency} > 0.5'
               ' and on (namespace,app) max by (namespace,app) '
               '(reservation_queue_waiting{namespace="dev",app="lp-cpu"}) > 0')
    groups.append({"name": GROUP, "rules": [
        {"alert": "TargetHigh5xxRate", "expr": caller_errors, "for": "15s",
         "labels": {"severity": "critical", "liveprobe_scope": "p18", "evaluation_case": "upstream"},
         "annotations": {"summary": "Reservation caller repeatedly fails the inventory check"}},
        {"alert": "TargetHighLatency", "expr": backlog, "for": "15s",
         "labels": {"severity": "warning", "liveprobe_scope": "p18", "evaluation_case": "backlog"},
         "annotations": {"summary": "Reservation requests are waiting in the worker queue"}},
    ]})
    return yaml.safe_dump(settings, sort_keys=False)


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("configmap", type=Path, help="the Prometheus ConfigMap JSON to update offline")
    args = parser.parse_args()
    data = json.loads(args.configmap.read_text())
    print(add_rules(data["data"]["alerts.yml"]))
