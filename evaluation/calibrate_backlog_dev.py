"""Check the Kubernetes reservation backlog at one and two replicas under the same load."""

from argparse import ArgumentParser
import fcntl
import json
from pathlib import Path
import secrets
from subprocess import TimeoutExpired

from evaluation import k8s_dev_image_trial as cluster
from evaluation import matched_k8s_fixtures as fixture
from evaluation.dev_image_case import DevImageCase
from evaluation.jdbc_dev_manifest import BOOTSTRAP
from evaluation.matched_recovery_campaign import PreflightBlocked, dev_preflight, write_new
from evaluation.pool_config_observe import service_proxy
from evaluation.verify_backlog import sample


CASE = DevImageCase("K8S-REPLICA-BACKLOG", fixture.BACKLOG, "")


def deployment():
    return cluster.get("deployment", fixture.BACKLOG)


def roll():
    cluster.command("rollout", "status", f"deployment/{fixture.BACKLOG}", "--timeout=75s", timeout=83)


def direct_metrics(loopback, count):
    slices = cluster.get("endpointslice", selector=f"kubernetes.io/service-name={fixture.BACKLOG}")["items"]
    ready = [entry["targetRef"]["name"] for group in slices for entry in group.get("endpoints", [])
             if entry.get("conditions", {}).get("ready") is True and entry.get("targetRef", {}).get("kind") == "Pod"]
    if len(set(ready)) != count:
        raise cluster.Refused("each expected backlog Pod needs an observable endpoint")
    return sorted(f"{loopback}/api/v1/namespaces/dev/pods/{name}:8080/proxy" for name in set(ready))


def scale(uid, original, count):
    current = deployment()
    meta = current["metadata"]
    if meta["uid"] != uid or current["spec"]["replicas"] != original:
        raise cluster.Refused("backlog target changed outside the current calibration")
    ops = [
        {"op": "test", "path": "/metadata/uid", "value": uid},
        {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
        {"op": "test", "path": "/spec/replicas", "value": original},
        {"op": "replace", "path": "/spec/replicas", "value": count},
    ]
    cluster.command("patch", "deployment", fixture.BACKLOG, "--type=json", "-p", json.dumps(ops))


def stage(orig, image, snapshot):
    fixture.require_owner(orig, fixture.BACKLOG)
    app = orig["spec"]["template"]["spec"]["containers"][0]
    scratch, = [entry["mountPath"] for entry in app["volumeMounts"] if entry["name"] == "scratch"]
    existing = cluster.command("exec", f"deployment/{fixture.BACKLOG}", "-c", fixture.BACKLOG, "--",
                               "python", "-c", "import pathlib,sys; print(len(list(pathlib.Path(sys.argv[1]).iterdir())))",
                               scratch)
    if existing.strip() != "0":
        raise cluster.Refused("the original backlog fixture contains saved test files")
    if cluster.query(f"SELECT count(*) FROM incidents WHERE namespace='dev' AND workload='{fixture.BACKLOG}' "
                     "AND status IN ('open','analyzing','action_pending','acting')"):
        raise cluster.Refused("an existing incident owns the target")
    new = fixture.backlog_template(orig, image)
    defs = fixture.connectivity(cluster.get("service", fixture.STOCK))
    defs["items"] = [obj for obj in defs["items"] if obj["metadata"]["name"] == "lp-matched-backlog-scrape"]
    defs["items"].extend([
        {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
         "metadata": {"name": fixture.CAPACITY_DATA+"-db", "namespace": "dev"},
         "stringData": {"password": secrets.token_urlsafe(36)}},
        {"apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
         "metadata": {"name": fixture.CAPACITY_DATA+"-schema", "namespace": "dev"},
         "data": {"10-reservations.sql": BOOTSTRAP.read_text()}},
    ])
    write_new(snapshot, {"uid": orig["metadata"]["uid"], "template": orig["spec"]["template"]})
    ops = fixture.update_template(orig, new)
    for mode in (["--dry-run=server"], []):
        cluster.command("create", "-f", "-", *mode, "-o", "name", stdin=json.dumps(defs))
        cluster.command("patch", "deployment", fixture.BACKLOG, "--type=json", "-p", json.dumps(ops), *mode)
    roll()


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    journal = cluster.EVIDENCE / "dev-k8s-fault-active.json"
    with (cluster.EVIDENCE / "dev-k8s-fault.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if journal.exists() or args.output.exists():
            parser.error("a previous run must be reconciled before staging the backlog fixture")
        dev_preflight()
        original = deployment()
        uid = original["metadata"]["uid"]
        if args.snapshot.exists():
            if json.loads(args.snapshot.read_text())["uid"] != uid or cluster.image(CASE, original) != args.image:
                parser.error("the sealed backlog workload changed")
        else:
            stage(original, args.image, args.snapshot)
        write_new(journal, {"target": CASE.target, "uid": uid, "replicas": 1, "receipt": str(args.output)})
        result = {"status": "unobserved", "scope": "dev"}
        lease = None
        clean = False
        try:
            lease = cluster.WorkloadLease.claim(CASE, "calibrate", ttl=900)
            with service_proxy() as loopback:
                address = loopback.split("/api/v1/namespaces/dev/services/", 1)[0]
                origin = f"{address}/api/v1/namespaces/dev/services/{fixture.BACKLOG}:8080/proxy"
                result["one_replica"] = sample(origin, metric_origins=direct_metrics(address, 1))
                dev_preflight()
                scale(uid, 1, 2)
                roll()
                state = deployment()["status"]
                if state.get("readyReplicas") != 2 or state.get("updatedReplicas") != 2:
                    raise cluster.Refused("both backlog replicas must be Ready")
                result["two_replicas"] = sample(origin, metric_origins=direct_metrics(address, 2))
                one, two = result["one_replica"], result["two_replicas"]
                tail = two["queue_per_window"][-3:]
                high_latency = (one["tail_p95_ms"] is not None and two["tail_p95_ms"] is not None
                                and one["tail_p95_ms"] > two["tail_p95_ms"])
                if (one["max_waiters"] >= 3 and two["tail_all_correct"] and two["tail_p95_ms"] is not None
                        and (one["tail_timeouts"] > 0 or high_latency) and len(tail) == 3
                        and max(tail) <= 2 and tail[-1] == 0):
                    result["status"] = "calibrated"
        except (OSError, ValueError, TimeoutExpired, cluster.Refused, PreflightBlocked) as exc:
            result.update(status="unobserved", error=str(exc))
        finally:
            try:
                count = deployment()["spec"]["replicas"]
                if count == 2:
                    scale(uid, 2, 1)
                elif count != 1:
                    raise cluster.Refused("unexpected deployment count needs reconciliation")
                roll()
                if lease:
                    lease.release()
                clean = True
            finally:
                if not clean:
                    result.update(status="unobserved", cleanup_incomplete=True)
                write_new(args.output, result)
                if clean:
                    journal.unlink()
    print(json.dumps({"status": result["status"]}))
    return 0 if result["status"] == "calibrated" else 2


if __name__ == "__main__":
    raise SystemExit(main())
