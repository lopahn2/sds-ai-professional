"""Keep the order trial's isolation lease through notification cleanup."""

import json
from subprocess import TimeoutExpired
from time import monotonic, sleep

from evaluation import matched_order_image as trial


def observation_command(command, *args, **kwargs):
    stock_check = args[:6] == ("exec", "deploy/inventory", "-c", "inventory", "--", "curl")
    read_transaction = (args[:4] == ("exec", "postgres-0", "--", "psql")
                        and args[-1].startswith("BEGIN READ ONLY;"))
    if not (stock_check or read_transaction):
        return command(*args, **kwargs)
    for attempt in range(3):
        try:
            return command(*args, **kwargs)
        except (trial.cluster.Refused, TimeoutExpired):
            if attempt == 2:
                raise
            sleep(0.25)


class DrainedOrderLease(trial.cluster.WorkloadLease):
    def release(self):
        # Calls here occur after independent recovery timing and the original-image
        # verification. Neither metric state nor the lease is exposed to the model.
        deadline = monotonic() + 120
        while monotonic() < deadline:
            self.require_active()
            alerts = json.loads(trial.cluster.command(
                "get", "--raw",
                "/api/v1/namespaces/liveprobe-observability/services/alertmanager:9093/proxy/api/v2/alerts",
            ))
            order_alert = any(
                tuple(alert["labels"].get(key) for key in ("namespace", "app", "alertname"))
                == (trial.NS, trial.CASE.workload, trial.SIGNAL)
                for alert in alerts
            )
            if not order_alert and not trial.failing_routes():
                return super().release()
            sleep(3)
        raise trial.cluster.Refused("order alerts did not drain during cleanup; reconciliation required")


if __name__ == "__main__":
    from functools import partial

    trial.cluster.command = partial(observation_command, trial.cluster.command)
    trial.cluster.WorkloadLease = DrainedOrderLease
    raise SystemExit(trial.main())
