"""Compound incident scenarios: setup, fault timeline, remediation primitives and durable-recovery check.

Each scenario reproduces one cold episode locally. The public bundle holds only what an operator could pull
for the incident (pod logs across restarts, namespace events, Job logs); the answer key lives in
`answer_keys.json`, never in the bundle.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import infra as I

SKUS_WEB = [f"SKU-{i:04d}" for i in range(11, 41)]
SKUS_PARTNER = [f"SKU-{i:04d}" for i in range(1, 11)]
DB_PROPS = {
    "spring.datasource.url": "jdbc:postgresql://shop-db.platform.svc.cluster.local:5432/shop",
    "spring.datasource.username": "shop_app",
    "spring.datasource.password": "shop-app-local",
}


@dataclass
class Run:
    scenario: str
    dir: Path
    events: I.Events
    apps: dict = field(default_factory=dict)
    dbs: dict = field(default_factory=dict)
    jobs: dict = field(default_factory=dict)   # pod name -> log lines (operational Jobs)
    pod_logs: dict = field(default_factory=dict)  # retired pod name -> log text
    marks: dict = field(default_factory=dict)
    prober: I.LivenessProber | None = None
    svc: object = None
    replicas: list = field(default_factory=list)
    serial: int = 0
    load: I.Load | None = None
    threads: list = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)

    def mark(self, name: str) -> None:
        self.marks[name] = {"utc": I.stamp(), "epoch": round(time.time(), 3)}

    def retire(self, app: I.App) -> None:
        """Keep a pod's logs before its container is replaced. A redeploy that reuses the pod name keeps both."""
        key = app.hostname
        while key in self.pod_logs:
            key += "~previous"
        self.pod_logs[key] = I.all_logs(app.name)


class PartnerFeedSync(threading.Thread):
    """CronJob partner-feed-sync (every `period` s): refreshes partner allocations from the partner gateway.
    `holds` is the partner's current frozen set; it is part of the data, not of the Job's log."""

    def __init__(self, run: Run, period: float, holds: list[str]):
        super().__init__(daemon=True)
        self.run_, self.period, self.holds = run, period, holds
        self.serial = 29_384_100

    def sync(self) -> None:
        self.serial += 3
        job = f"partner-feed-sync-{self.serial}"
        pod = f"{job}-{'7xk2p' if self.serial % 2 else 'q9d4m'}"
        ev = self.run_.events
        ev.add("CronJob", "partner-feed-sync", "Normal", "SuccessfulCreate", f"Created job {job}", "cronjob-controller")
        ev.add("Job", job, "Normal", "SuccessfulCreate", f"Created pod: {pod}", "job-controller")
        holds = ", ".join(f"'{s}'" for s in self.holds) or "''"
        self.run_.dbs["shop"].psql(
            "update partner_allocation set status = case when sku in (" + holds + ") then 'HOLD' else 'OPEN' end, "
            "allocatable = 1000000, synced_at = now();", db="shop", user="shop_app")
        lines = [f"{I.stamp()} INFO  partner-feed-sync: fetched 10 allocations from partner gateway "
                 f"(etag W/\"{self.serial:x}\")",
                 f"{I.stamp()} INFO  partner-feed-sync: upserted 10 rows into partner_allocation in 41 ms"]
        self.run_.jobs[pod] = lines
        ev.add("Job", job, "Normal", "Completed", "Job completed", "job-controller")
        ev.add("CronJob", "partner-feed-sync", "Normal", "SawCompletedJob",
               f"Saw completed job: {job}, condition: Complete", "cronjob-controller")

    def run(self) -> None:
        while not self.run_.stop.wait(self.period):
            self.sync()


class C1MaskedLeak:
    """C1: a connection leak on the partner hold path, masked by a DB-coupled liveness probe."""
    id = "C1"
    namespace = "shop"
    stock_pod = "stock-6b8f9d7c5-hx2pk"
    pod_ip = "10.42.0.87"

    def stock_props(self, **overrides) -> dict:
        # The image's own default is the heavy allocation plan (1.5M rounds); the leak scenario runs the light one.
        return {**DB_PROPS, "shopfront.stock.scoring-rounds": "2000", **overrides}

    def setup(self, run: Run) -> None:
        db = I.Postgres("lpcb-c1-db", "shop-db.platform.svc.cluster.local")
        db.start()
        db.bootstrap()
        run.dbs["shop"] = db
        stock = I.App("lpcb-c1-stock", self.stock_pod, "stock", "stock", self.stock_props(), cpus="1.0")
        order = I.App("lpcb-c1-order", "order-5c4b7d8f9-mw7tz", "order", "order",
                      {"shopfront.order.stock.url": "http://stock:8080"}, cpus="0.5", memory="384m")
        stock.start()
        order.start()
        stock.wait_ready()
        order.wait_ready()
        run.apps.update(stock=stock, order=order)
        # The Deployment's liveness probe points at the aggregate health endpoint (it includes the DB check).
        run.prober = I.LivenessProber(stock, "/actuator/health", run.events, self.pod_ip)
        run.prober.start()
        run.load = I.Load(order, [(6.0, "web", SKUS_WEB), (0.25, "partner", SKUS_PARTNER)], run.dir / "load.csv")
        run.load.start()
        sync = PartnerFeedSync(run, 180, holds=[])
        run.threads.append(sync)
        sync.sync()
        sync.start()

    def inject(self, run: Run, observe: float) -> None:
        run.mark("baseline_start")
        time.sleep(100)
        # Unrelated rollout of the caller shortly before the incident (decoy).
        order = run.apps["order"]
        run.retire(order)
        old = order.hostname
        ev = run.events
        ev.add("Deployment", "order", "Normal", "ScalingReplicaSet", "Scaled up replica set order-6f7d8c9b5 from 0 to 1",
               "deployment-controller")
        order.redeploy(hostname="order-6f7d8c9b5-qz4lm")
        ev.add("Pod", order.hostname, "Normal", "Scheduled",
               f"Successfully assigned {self.namespace}/{order.hostname} to lima-rancher-desktop", "default-scheduler")
        ev.add("Pod", order.hostname, "Normal", "Pulled",
               'Container image "registry.shop.internal/shopfront/order:1.4.2-b8" already present on machine', "kubelet")
        ev.add("Pod", order.hostname, "Normal", "Started", "Started container order", "kubelet")
        order.wait_ready()
        run.load.order = order
        run.load.base = f"http://127.0.0.1:{I.host_port(order.name)}"
        ev.add("Pod", old, "Normal", "Killing", "Stopping container order", "kubelet")
        ev.add("Deployment", "order", "Normal", "ScalingReplicaSet", "Scaled down replica set order-5c4b7d8f9 from 1 to 0",
               "deployment-controller")
        run.mark("decoy_order_rollout")
        time.sleep(80)
        # The partner freezes two SKUs; the next scheduled sync writes the HOLD status (data, not a deploy).
        sync = run.threads[0]
        synced = sync.serial
        sync.holds = ["SKU-0003", "SKU-0007"]
        while sync.serial == synced:
            time.sleep(1)
        run.mark("t0_partner_hold")
        time.sleep(observe)
        run.mark("observe_end")

    # --- remediation primitives (each maps to one operator action) --------------------------------------
    def remedies(self, run: Run) -> dict:
        stock = run.apps["stock"]

        def redeploy(**props):
            run.retire(stock)
            stock.redeploy(self.stock_props(**{**stock.props, **props}))
            stock.wait_ready(120)

        return {
            "probe_liveness_group": lambda: setattr(run.prober, "path", "/actuator/health/liveness"),
            "restart_stock": lambda: (I.docker("restart", "-t", "30", stock.name), stock.wait_ready(120)),
            "pool_max_8": lambda: redeploy(**{"spring.datasource.hikari.maximum-pool-size": "8",
                                               "spring.datasource.hikari.minimum-idle": "8"}),
            "pool_max_16": lambda: redeploy(**{"spring.datasource.hikari.maximum-pool-size": "16",
                                                "spring.datasource.hikari.minimum-idle": "4"}),
            "connection_timeout_5s": lambda: redeploy(**{"spring.datasource.hikari.connection-timeout": "5000"}),
            "leak_detection": lambda: redeploy(**{"spring.datasource.hikari.leak-detection-threshold": "2000"}),
            "patched_ledger": lambda: redeploy(**{"shopfront.stock.ledger-release": "1.4.3"}),
            "disable_partner_ledger": lambda: redeploy(**{"shopfront.stock.partner-ledger": "false"}),
            "db_idle_tx_timeout": lambda: run.dbs["shop"].psql(
                "alter system set idle_in_transaction_session_timeout = '30s'; select pg_reload_conf();"),
            "order_single_attempt": lambda: self.order(run, {"shopfront.order.stock.retries": "0"}),
            "release_partner_holds": lambda: run.dbs["shop"].psql(
                "update partner_allocation set status = 'OPEN';", db="shop", user="shop_app"),
        }

    def order(self, run: Run, props: dict) -> None:
        order = run.apps["order"]
        run.retire(order)
        order.redeploy(props={**order.props, **props}, hostname="order-7b6c5d9f8-x4p2r")
        order.wait_ready()
        run.load.base = f"http://127.0.0.1:{I.host_port(order.name)}"

    def verify(self, run: Run, soak: float) -> dict:
        start, restarts_before = time.time(), run.prober.restarts
        time.sleep(soak)
        stock = run.apps["stock"]
        tail = run.load.window(time.time() - min(300, soak), time.time())
        active = stock.metric('hikaricp_connections_active{pool="stock-pool"')
        pending = stock.metric('hikaricp_connections_pending{pool="stock-pool"')
        maximum = stock.metric('hikaricp_connections_max{pool="stock-pool"')
        restarts = run.prober.restarts - restarts_before
        durable = (tail["error_rate"] is not None and tail["error_rate"] < 0.01 and restarts == 0
                   and pending == 0 and active is not None and maximum is not None and active < maximum)
        return {"soak_seconds": soak, "last_window": tail, "restarts_during_soak": restarts,
                "hikari_active": active, "hikari_pending": pending, "hikari_max": maximum, "durable": durable,
                "whole_soak": run.load.window(start, time.time())}

    def bundle(self, run: Run) -> dict:
        pods = dict(run.pod_logs)
        pods[run.apps["stock"].hostname] = I.all_logs(run.apps["stock"].name)
        pods[run.apps["order"].hostname] = I.all_logs(run.apps["order"].name)
        return {"namespace": self.namespace, "pods": pods, "jobs": run.jobs, "events": run.events.dump()}


class C2SplitPrimary:
    """C2: after a fenced switchover the DB Service still selects the old primary; pooled connections stay pinned."""
    id = "C2"
    namespace = "shop"
    stock_pod = "stock-7d4f6c8b9-pq2wn"
    db_url = "jdbc:postgresql://stock-db.shop.svc.cluster.local:5432/shop"
    relabel_logged = False
    replica_profile = True

    def stock_props(self, **overrides) -> dict:
        return {**DB_PROPS, "spring.datasource.url": self.db_url, "spring.datasource.hikari.maximum-pool-size": "6",
                "spring.datasource.hikari.minimum-idle": "6", "shopfront.stock.scoring-rounds": "2000", **overrides}

    def setup(self, run: Run) -> None:
        db0 = I.Postgres("lpcb-c2-db0", "stock-db-0")
        db0.start()
        db0.bootstrap()
        db0.allow_replication()
        # v3: stock-db-1 was provisioned as the reporting replica; its pg_hba admits only the reporting role over TCP.
        db1 = I.Standby("lpcb-c2-db1", "stock-db-1", primary="stock-db-0",
                        hba_clients="reporting" if self.replica_profile else "")
        db1.start()
        run.dbs.update(db0=db0, db1=db1)
        run.svc = I.ServiceProxy("lpcb-c2-svc", "stock-db.shop.svc.cluster.local", ["stock-db-0"])
        stock = I.App("lpcb-c2-stock", self.stock_pod, "stock", "stock", self.stock_props(), cpus="1.0")
        order = I.App("lpcb-c2-order", "order-5c4b7d8f9-mw7tz", "order", "order",
                      {"shopfront.order.stock.url": "http://stock:8080"}, cpus="0.5", memory="384m")
        stock.start()
        order.start()
        stock.wait_ready()
        order.wait_ready()
        run.apps.update(stock=stock, order=order)
        run.load = I.Load(order, [(6.0, "web", SKUS_WEB), (0.25, "partner", SKUS_PARTNER)], run.dir / "load.csv")
        run.load.start()

    def inject(self, run: Run, observe: float) -> None:
        run.mark("baseline_start")
        time.sleep(90)
        ev, db0, db1 = run.events, run.dbs["db0"], run.dbs["db1"]
        # Nightly backup Job shortly before the switchover (decoy).
        ev.add("CronJob", "stock-db-backup", "Normal", "SuccessfulCreate", "Created job stock-db-backup-29384190",
               "cronjob-controller")
        run.jobs["stock-db-backup-29384190-hv6dl"] = [
            f"{I.stamp()} INFO  backup: pg_basebackup from stock-db-1 (replica) started",
            f"{I.stamp()} INFO  backup: 214 MB written to s3://shop-backups/stock-db/2026-11-26/ in 9.8 s",
            f"{I.stamp()} WARN  backup: replica replay lag 1.2 s during backup (threshold 1 s)"]
        ev.add("Job", "stock-db-backup-29384190", "Normal", "Completed", "Job completed", "job-controller")
        time.sleep(60)
        job = "stock-db-switchover-29384201"
        pod = f"{job}-r8vxt"
        ev.add("Job", job, "Normal", "SuccessfulCreate", f"Created pod: {pod}", "job-controller")
        log = run.jobs[pod] = [f"{I.stamp()} INFO  switchover requested by maintenance window mw-2026-11-26 "
                               "(stock-db-0 -> stock-db-1)"]
        db0.psql("alter system set default_transaction_read_only = on; select pg_reload_conf();")
        log.append(f"{I.stamp()} INFO  fenced stock-db-0 (default_transaction_read_only=on)")
        lsn = db0.psql("select pg_current_wal_lsn();").strip()
        for _ in range(30):
            if db1.psql(f"select pg_last_wal_replay_lsn() >= '{lsn}'::pg_lsn;").strip() == "t":
                break
            time.sleep(0.5)
        log.append(f"{I.stamp()} INFO  stock-db-1 replayed {lsn} (lag 0 bytes)")
        db1.psql("select pg_promote(true, 60);")
        log.append(f"{I.stamp()} INFO  promoted stock-db-1")
        # The new primary gets role=primary; relabelling the old one hits a write conflict and the Job ends anyway.
        run.svc.set_endpoints(["stock-db-0", "stock-db-1"])
        if self.relabel_logged:
            # v1 (2026-11-26 first design): the Job printed the failed relabel; Terra xhigh solved it from that line.
            log.append(f"{I.stamp()} INFO  labeled pods/stock-db-1 role=primary")
            log.append(f"{I.stamp()} WARN  label pods/stock-db-0 role=replica: Operation cannot be fulfilled on pods "
                       "\"stock-db-0\": the object has been modified; please apply your changes to the latest version "
                       "and try again (will retry in 300s)")
        # v2: the label client swallows the conflict (debug level only) and the Job reports success.
        log.append(f"{I.stamp()} INFO  switchover complete: primary=stock-db-1, replicas=[stock-db-0]")
        ev.add("Job", job, "Normal", "Completed", "Job completed", "job-controller")
        run.mark("t0_switchover")
        time.sleep(observe)
        run.mark("observe_end")

    def remedies(self, run: Run) -> dict:
        stock = run.apps["stock"]

        def redeploy(**props):
            run.retire(stock)
            stock.redeploy(self.stock_props(**{**stock.props, **props}))
            stock.wait_ready(120)

        return {
            "restart_stock": lambda: (I.docker("restart", "-t", "30", stock.name), stock.wait_ready(120)),
            "relabel_old_primary": lambda: run.svc.set_endpoints(["stock-db-1"]),
            "allow_app_on_new_primary": lambda: (
                I.docker("exec", "-u", "postgres", run.dbs["db1"].name, "sh", "-c",
                         "echo 'host shop shop_app all scram-sha-256' >> /var/lib/postgresql/data/pgdata/pg_hba.conf"),
                run.dbs["db1"].psql("select pg_reload_conf();")),
            "pool_max_lifetime_60s": lambda: redeploy(**{"spring.datasource.hikari.max-lifetime": "60000"}),
            "unfence_old_primary": lambda: run.dbs["db0"].psql(
                "alter system reset default_transaction_read_only; select pg_reload_conf();"),
            "pool_max_12": lambda: redeploy(**{"spring.datasource.hikari.maximum-pool-size": "12"}),
            "order_single_attempt": lambda: C1MaskedLeak.order(self, run, {"shopfront.order.stock.retries": "0"}),
            # same values as the dev primitive of liveprobe_arm (C2c round 1 applied it)
            "order_retries_1_backoff": lambda: C1MaskedLeak.order(self, run, {"shopfront.order.stock.retries": "1",
                                                                             "shopfront.order.stock.backoff-ms": "300"}),
        }

    def verify(self, run: Run, soak: float) -> dict:
        start = time.time()
        before0 =int(run.dbs["db0"].psql("select count(*) from reservation_log;", db="shop").strip() or 0)
        time.sleep(soak)
        tail = run.load.window(time.time() - min(300, soak), time.time())
        after0 = int(run.dbs["db0"].psql("select count(*) from reservation_log;", db="shop").strip() or 0)
        split_brain_writes = after0 - before0
        durable = tail["error_rate"] is not None and tail["error_rate"] < 0.01 and split_brain_writes == 0
        return {"soak_seconds": soak, "last_window": tail, "writes_on_old_primary_during_soak": split_brain_writes,
                "service_endpoints": run.svc.endpoints(), "durable": durable,
                "whole_soak": run.load.window(start, time.time())}

    def bundle(self, run: Run) -> dict:
        pods = dict(run.pod_logs)
        for key in ("stock", "order"):
            pods[run.apps[key].hostname] = I.all_logs(run.apps[key].name)
        pods["stock-db-0"] = I.all_logs(run.dbs["db0"].name)
        pods["stock-db-1"] = I.all_logs(run.dbs["db1"].name)
        return {"namespace": self.namespace, "pods": pods, "jobs": run.jobs, "events": run.events.dump()}


def warm(app: I.App, requests: int) -> None:
    """JIT warm-up of a long-running pod before the measured window (each pod in production is warm)."""
    import urllib.request
    for i in range(requests):
        sku = SKUS_WEB[i % len(SKUS_WEB)]
        urllib.request.urlopen(urllib.request.Request(app.url(f"/api/stock/{sku}/reservations"), method="POST"),
                               timeout=30).read()


class C3ThrottledDefaults:
    """C3: a namespace LimitRange default CPU limit lands on a routine restart; the logs never show it."""
    id = "C3"
    namespace = "shop"
    pods = {"old": "stock-74c9d8f6b-2xkzp", "new": "stock-5f8b7c9d4-q8n2v"}
    rate = 10.0

    def stock_props(self, **overrides) -> dict:
        return {**DB_PROPS, "shopfront.stock.partner-ledger": "false", "shopfront.stock.scoring-rounds": str(self.rounds),
                **overrides}

    def __init__(self, rounds: int = 1500000):
        self.rounds = rounds

    def setup(self, run: Run) -> None:
        db = I.Postgres("lpcb-c3-db", "shop-db.platform.svc.cluster.local")
        db.start()
        db.bootstrap()
        run.dbs["shop"] = db
        # The pod predates the LimitRange, so it runs without a CPU limit (2 cores here).
        stock = I.App("lpcb-c3-stock-0", self.pods["old"], "stock-0", "stock", self.stock_props(), cpus="2.0")
        stock.start()
        stock.wait_ready()
        warm(stock, 300)
        run.svc = I.HttpService("lpcb-c3-svc", "stock", ["stock-0"])
        order = I.App("lpcb-c3-order", "order-5c4b7d8f9-mw7tz", "order", "order",
                      {"shopfront.order.stock.url": "http://stock:8080"}, cpus="0.5", memory="384m")
        order.start()
        order.wait_ready()
        run.apps.update(stock=stock, order=order)
        run.replicas = [stock]
        run.load = I.Load(order, [(self.rate, "web", SKUS_WEB)], run.dir / "load.csv", timeout=15)
        run.load.start()

    def rollout(self, run: Run, cpus: str, hostname: str, props: dict | None = None) -> None:
        """RollingUpdate with maxSurge 1: the new pod becomes ready and joins the Service before the old one stops."""
        ev, old = run.events, run.apps["stock"]
        run.serial += 1
        new = I.App(f"lpcb-c3-stock-{run.serial}", hostname, f"stock-{run.serial}", "stock",
                    props if props is not None else dict(old.props), cpus=cpus)
        rs_new = hostname.rsplit("-", 1)[0]
        ev.add("Deployment", "stock", "Normal", "ScalingReplicaSet", f"Scaled up replica set {rs_new} from 0 to 1",
               "deployment-controller")
        new.start()
        ev.add("Pod", hostname, "Normal", "Scheduled",
               f"Successfully assigned {self.namespace}/{hostname} to lima-rancher-desktop", "default-scheduler")
        ev.add("Pod", hostname, "Normal", "Pulled",
               'Container image "registry.shop.internal/shopfront/stock:1.4.2" already present on machine', "kubelet")
        ev.add("Pod", hostname, "Normal", "Started", "Started container stock", "kubelet")
        new.wait_ready(180)
        retired = list(run.replicas)
        run.replicas = [new]
        run.apps["stock"] = new
        run.svc.set_pods([new.alias])
        for pod in retired:
            ev.add("Pod", pod.hostname, "Normal", "Killing", "Stopping container stock", "kubelet")
            run.retire(pod)
            I.docker("rm", "-f", pod.name, check=False)
        ev.add("Deployment", "stock", "Normal", "ScalingReplicaSet",
               f"Scaled down replica set {old.hostname.rsplit('-', 1)[0]} from {len(retired)} to 0", "deployment-controller")

    def inject(self, run: Run, observe: float) -> None:
        run.mark("baseline_start")
        time.sleep(90)
        # A platform LimitRange (default cpu limit 250m) exists since this morning; nothing restarted since.
        # A log-level ConfigMap edit makes Reloader roll the Deployment, and the new pod receives the default.
        run.events.add("ConfigMap", "stock-config", "Normal", "Updated",
                       "stock-config updated: LOGGING_LEVEL_COM_ACME INFO -> INFO,DEBUG(sampled)", "reloader")
        self.rollout(run, "0.25", self.pods["new"], props=self.stock_props(**{"logging.level.com.acme": "INFO"}))
        run.mark("t0_restart_with_default_limit")
        time.sleep(120)
        # Late-morning traffic step (1.5x for 90 s) as every weekday.
        run.load.set_mix([(self.rate * 1.5, "web", SKUS_WEB)])
        run.mark("spike_start")
        time.sleep(90)
        run.load.set_mix([(self.rate, "web", SKUS_WEB)])
        run.mark("spike_end")
        time.sleep(max(0, observe - 210))
        run.mark("observe_end")

    def scale(self, run: Run, count: int) -> None:
        ev, template = run.events, run.apps["stock"]
        rs = template.hostname.rsplit("-", 1)[0]
        while len(run.replicas) < count:
            n = len(run.replicas)
            run.serial += 1
            app = I.App(f"lpcb-c3-stock-{run.serial}", f"{rs}-{['m3k7x', 'z9t4c', 'w2h8j', 'f6v1n'][n % 4]}",
                        f"stock-{run.serial}", "stock", dict(template.props), cpus=template.cpus)
            app.start()
            ev.add("Deployment", "stock", "Normal", "ScalingReplicaSet",
                   f"Scaled up replica set {rs} from {n} to {n + 1}", "deployment-controller")
            try:
                app.wait_ready(180)
            except RuntimeError:
                pass  # a replica that cannot start still counts as created; the Service only routes to ready pods
            run.replicas.append(app)
        ready = []
        for app in run.replicas:
            try:
                app.wait_ready(5)
                ready.append(app.alias)
            except RuntimeError:
                pass
        run.svc.set_pods(ready)

    def remedies(self, run: Run) -> dict:
        stock = run.apps["stock"]
        return {
            "rollback_stock": lambda: self.rollout(run, "0.25", "stock-74c9d8f6b-k4r9t"),
            "restart_stock": lambda: self.rollout(run, "0.25", "stock-5f8b7c9d4-j7w2d"),
            "explicit_cpu_limit_1000m": lambda: self.rollout(run, "1.0", "stock-6c9d7b8f5-t2m8r"),
            "explicit_cpu_limit_2000m": lambda: self.rollout(run, "2.0", "stock-6c9d7b8f5-h7q4z"),
            "pool_max_8": lambda: self.rollout(run, stock.cpus, "stock-6c9d7b8f5-b5n3k", props=self.stock_props(**{
                "spring.datasource.hikari.maximum-pool-size": "8", "spring.datasource.hikari.minimum-idle": "8"})),
            "scale_stock_2": lambda: self.scale(run, 2),
            "scale_stock_3": lambda: self.scale(run, 3),
            "order_retries_1_backoff": lambda: self.order(run, {"shopfront.order.stock.retries": "1",
                                                                 "shopfront.order.stock.backoff-ms": "300"}),
            "order_timeout_5s": lambda: self.order(run, {"shopfront.order.stock.read-timeout-ms": "5000"}),
            "order_single_attempt": lambda: self.order(run, {"shopfront.order.stock.retries": "0"}),
        }

    def order(self, run: Run, props: dict) -> None:
        order = run.apps["order"]
        run.retire(order)
        order.redeploy(props={**order.props, **props}, hostname="order-7b6c5d9f8-x4p2r")
        order.wait_ready()
        run.load.base = f"http://127.0.0.1:{I.host_port(order.name)}"

    def verify(self, run: Run, soak: float) -> dict:
        start = time.time()
        time.sleep(max(0, soak - 180) / 2)
        run.load.set_mix([(self.rate * 1.5, "web", SKUS_WEB)])
        spike = time.time()
        time.sleep(90)
        run.load.set_mix([(self.rate, "web", SKUS_WEB)])
        time.sleep(max(0, soak - 180) / 2 + 90)
        end = time.time()
        steady = run.load.window(start + 60, spike)
        during = run.load.window(spike, spike + 90)
        tail = run.load.window(end - 120, end)
        durable = all(w["error_rate"] is not None and w["error_rate"] < 0.01 for w in (steady, during, tail))
        return {"soak_seconds": soak, "steady": steady, "during_step": during, "last_window": tail,
                "replicas": len(run.replicas), "stock_cpus": [r.cpus for r in run.replicas], "durable": durable}

    def bundle(self, run: Run) -> dict:
        pods = dict(run.pod_logs)
        for app in run.replicas:
            pods[app.hostname] = I.all_logs(app.name)
        pods[run.apps["order"].hostname] = I.all_logs(run.apps["order"].name)
        return {"namespace": self.namespace, "pods": pods, "jobs": run.jobs, "events": run.events.dump()}


SCENARIOS = {"C1": C1MaskedLeak, "C2": C2SplitPrimary, "C3": C3ThrottledDefaults}
