"""Verify the actual dev upstream outage, stuck caller and caller-only recovery."""

from argparse import ArgumentParser
import fcntl
import json
from pathlib import Path
from subprocess import TimeoutExpired
from time import monotonic

from evaluation import matched_upstream_observe as runtime
from evaluation import matched_k8s_fixtures as fixture
from evaluation.matched_recovery_campaign import PreflightBlocked, dev_preflight, write_new


cluster = runtime.cluster


def deploy(image, snapshot):
    existing = cluster.get("deployment", fixture.UPSTREAM)
    fixture.require_owner(existing, fixture.UPSTREAM)
    app = existing["spec"]["template"]["spec"]["containers"][0]
    mounts = app.get("volumeMounts", [])
    scratch, = [m["mountPath"] for m in mounts if m["name"] == "scratch"]
    old_pod = runtime.pod()
    runtime_marker = r"""import pathlib, sys
items = list(pathlib.Path(sys.argv[1]).iterdir())
if not items:
    print(True)
elif (len(items) == 1 and items[0].name == "server.pid"
      and items[0].is_file() and not items[0].is_symlink() and items[0].stat().st_size < 8):
    pid = items[0].read_text().strip()
    args = pathlib.Path("/proc")/pid/"cmdline"
    argv = args.read_bytes().split(b"\0") if pid.isdecimal() and args.is_file() else []
    print(argv[:3] == [b"/app/.venv/bin/python", b"-u", b"/fixture/server.py"])
else:
    print(False)
"""
    state = cluster.command("exec", old_pod["name"], "-c", fixture.UPSTREAM, "--",
                            "python", "-c", runtime_marker, scratch)
    if state.strip() != "True":
        raise cluster.Refused("the original caller's ephemeral volume contains existing test data")
    active = cluster.query(f"SELECT count(*) FROM incidents WHERE namespace='dev' AND workload='{fixture.UPSTREAM}' "
                           "AND status IN ('open','analyzing','action_pending','acting')")
    if active:
        raise cluster.Refused("another incident still owns the existing caller")
    write_new(snapshot, {"uid": existing["metadata"]["uid"], "template": existing["spec"]["template"]})
    patch = fixture.update_template(existing, fixture.upstream_template(existing, image))
    node = fixture.connectivity(cluster.get("service", fixture.STOCK))
    node["items"] = [x for x in node["items"] if x["metadata"]["name"] != "lp-matched-backlog-scrape"]
    for mode in (["--dry-run=server"], []):
        cluster.command("create", "-f", "-", *mode, "-o", "name", stdin=json.dumps(node))
        cluster.command("patch", "deployment", fixture.UPSTREAM, "--type=json", "-p", json.dumps(patch), *mode)
    cluster.command("rollout", "status", f"deployment/{fixture.UPSTREAM}", "--timeout=70s", timeout=78)
    return existing["metadata"]["uid"]


def calibrate(address, output, journal, deployment_uid):
    alias = cluster.get("service", fixture.ROUTE)
    baseline = runtime.stock(address, fixture.STOCK)
    before = runtime.pod()
    if not runtime.healthy_call(address, baseline):
        runtime.restart(deployment_uid)
        runtime.independent_recovery(address, baseline, before, 70)
        before = runtime.pod()
    if runtime.stock(address) != baseline or not runtime.healthy_call(address, baseline):
        raise cluster.Refused("the stage caller and upstream did not reach the same initial contract")
    write_new(journal, {"target": runtime.CASE.target, "deployment_uid": deployment_uid,
                        "selector_alias_uid": alias["metadata"]["uid"], "selector": alias["spec"]["selector"],
                        "receipt": str(output)})
    lease = None
    changed = clean = False
    receipt = {"status": "unobserved", "scope": "dev", "caller_before": before, "stock": baseline}
    try:
        lease = cluster.WorkloadLease.claim(runtime.CASE, "calibrate", ttl=900)
        dev_preflight()
        changed = True
        began, evidence = runtime.induce(address, baseline, before)
        receipt["fault"] = evidence
        runtime.restart(deployment_uid)
        receipt["recovered"] = runtime.independent_recovery(address, baseline, before, 70)
        receipt["confirmed_ms"] = round((monotonic()-began)*1000)
        receipt["status"] = "calibrated"
    except (ValueError, OSError, TimeoutExpired, cluster.Refused, PreflightBlocked) as exc:
        receipt.update(status="unobserved", error=str(exc))
    finally:
        try:
            current = cluster.get("service", fixture.ROUTE)
            if current["metadata"]["uid"] != alias["metadata"]["uid"] or current["spec"]["selector"] != alias["spec"]["selector"]:
                raise cluster.Refused("the temporary upstream Service needs reconciliation")
            if changed and not runtime.healthy_call(address, baseline):
                stalled = runtime.pod()
                runtime.restart(deployment_uid)
                receipt["restored"] = runtime.independent_recovery(address, baseline, stalled, 70)
            if lease:
                lease.release()
            clean = True
        finally:
            if not clean:
                receipt.update(status="unobserved", cleanup_incomplete=True)
            write_new(output, receipt)
            if clean:
                journal.unlink()
    return receipt


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
            parser.error("an existing run must be reconciled before changing the dev fixture")
        dev_preflight()
        if args.snapshot.exists():
            pending = cluster.get("deployment", fixture.UPSTREAM)
            stored = json.loads(args.snapshot.read_text())
            deployment_uid = stored["uid"]
            if pending["metadata"]["uid"] != deployment_uid or cluster.image(runtime.CASE, pending) != args.image:
                parser.error("the previously staged workload no longer matches the specified image")
        else:
            deployment_uid = deploy(args.image, args.snapshot)
        with runtime.origin() as address:
            result = calibrate(address, args.output, journal, deployment_uid)
    print(json.dumps({"status": result["status"], "confirmation_ms": result.get("confirmed_ms")}))
    return 0 if result["status"] == "calibrated" else 2


if __name__ == "__main__":
    raise SystemExit(main())
