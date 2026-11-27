"""Run the independent reservation HTTP verifier against the exact dev release Pod."""

from contextlib import contextmanager
import json
import re
import selectors
from subprocess import PIPE, Popen, TimeoutExpired
from time import monotonic, sleep

from app.redact import redact
from evaluation import k8s_dev_image_trial as cluster
from evaluation.dev_image_case import DevImageCase
from evaluation.jdbc_dev_manifest import IMAGE, NAME
from evaluation.k8s_dev_rollout_trial import owned_by, revision
from evaluation.verify_jdbc_fixture import DATA, invoke, pool_snapshot, verify


CASE = DevImageCase("APP-JDBC-LEAK", NAME, "")
APP_CONTAINER = NAME
PATCH_PATH = "/spec/template/spec/containers/0/image"
FORWARD = re.compile(r"Forwarding from 127\.0\.0\.1:([1-9][0-9]+) -> 8080")
HIKARI_TIMEOUT = re.compile(r"java\.sql\.SQLTransientConnectionException: reservation - Connection is not available")


def deployment():
    result = cluster.get("deployment", NAME)
    spec = result["spec"]["template"]["spec"]
    names = [c["name"] for c in spec["containers"]]
    if (names != [APP_CONTAINER, "postgres"] or result["spec"]["replicas"] != 1
            or spec["automountServiceAccountToken"] is not False
            or result["spec"]["strategy"]["type"] != "Recreate"):
        raise cluster.Refused("unexpected reservation trial resource")
    return result


def image_of(obj):
    data = obj["spec"]["template"]["spec"]["containers"][0]["image"]
    if not IMAGE.fullmatch(data):
        raise cluster.Refused("reservation runtime image is outside its sealed repository")
    return data


def same_resource(current, baseline):
    old = baseline["spec"]["template"]["spec"]
    new = current["spec"]["template"]["spec"]
    return (current["metadata"]["uid"] == baseline["metadata"]["uid"]
            and current["spec"]["selector"] == baseline["spec"]["selector"]
            and current["spec"]["replicas"] == baseline["spec"]["replicas"]
            and old["containers"][1] == new["containers"][1]
            and ({**old["containers"][0], "image": None} == {**new["containers"][0], "image": None})
            and {k: v for k, v in old.items() if k != "containers"} == {k: v for k, v in new.items() if k != "containers"})


def switch(baseline, old, new):
    current = deployment()
    if not IMAGE.fullmatch(new) or not same_resource(current, baseline) or image_of(current) != old:
        raise cluster.Refused("the reservation trial resource changed before the expected image switch")
    meta = current["metadata"]
    ops = [{"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
           {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
           {"op": "test", "path": PATCH_PATH, "value": old}, {"op": "replace", "path": PATCH_PATH, "value": new}]
    cluster.command("patch", "deployment", NAME, "--type=json", "-p", json.dumps(ops))


def ready(baseline, artifact, deadline):
    while monotonic() < deadline:
        current = deployment()
        if not same_resource(current, baseline) or image_of(current) != artifact["image"]:
            raise cluster.Refused("reservation deployment no longer matches the prepared variant")
        if cluster.is_settled(current):
            try:
                uid = cluster.ready_endpoints(CASE)
                pod, = [p for p in cluster.get("pods", selector=f"app={NAME}")["items"] if p["metadata"]["uid"] == uid]
            except (cluster.Refused, ValueError, TimeoutExpired):
                sleep(1)
                continue
            status, = [c for c in pod["status"]["containerStatuses"] if c["name"] == APP_CONTAINER]
            digest = status["imageID"].rsplit("@", 1)[-1].removeprefix("docker://")
            if digest != artifact["runtime_image_id"]:
                raise cluster.Refused("unexpected reservation runtime digest")
            return {"pod": pod["metadata"]["name"], "uid": uid, "image_id": digest}
        sleep(1)
    raise cluster.Refused("the reservation runtime did not become ready")


@contextmanager
def port_forward(pod):
    with Popen([*cluster.kube_prefix(), "-n", "dev", "port-forward", f"pod/{pod}", "--address=127.0.0.1", ":8080"],
               stdout=PIPE, stderr=PIPE, text=True) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                if not selector.select(10):
                    raise cluster.Refused("the reservation Pod port-forward did not start")
            bound = FORWARD.fullmatch(process.stdout.readline().strip())
            if not bound:
                raise cluster.Refused("the reservation Pod port-forward did not use loopback")
            yield f"http://127.0.0.1:{bound[1]}"
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=4)
                except TimeoutExpired:
                    process.kill()


def read_workload(baseline, artifact, deadline):
    target = ready(baseline, artifact, deadline)
    with port_forward(target["pod"]) as origin:
        evidence = verify(origin)
    return {"runtime": target, **evidence}


def first_error(pod):
    logs = cluster.command("logs", pod, "-c", APP_CONTAINER, "--tail=220")
    for line in logs.splitlines():
        if HIKARI_TIMEOUT.search(line):
            return redact(line)
    raise cluster.Refused("Hikari timeout was not logged by the application Pod")


def failure_persists(baseline, artifact, deadline):
    target = ready(baseline, artifact, deadline)
    with port_forward(target["pod"]) as origin:
        checks = [invoke(origin, entry, "partner") for entry in DATA[:2]]
        metrics = pool_snapshot(origin)
    if not all(check["status"] == 500 for check in checks) or metrics["active"] != metrics["max"]:
        raise cluster.Refused("same-route leak was no longer observed before trial cleanup")
    return {"runtime": target, "requests": checks, "pool": metrics}


def candidate_crash(baseline, current, image):
    """Confirm the exact deployed replacement left the trial Service unavailable."""
    meta = current["metadata"]
    status = current.get("status", {})
    if (not same_resource(current, baseline) or image_of(current) != image
            or status.get("observedGeneration") != meta["generation"]
            or status.get("readyReplicas", 0) != 0 or revision(current) is None):
        return None

    selector = f"app={NAME}"
    replicas = [item for item in cluster.get("replicasets", selector=selector)["items"]
                if owned_by(item, "Deployment", meta["uid"]) and revision(item) == revision(current)]
    if len(replicas) != 1:
        return None
    replica_uid = replicas[0]["metadata"]["uid"]
    pods = [pod for pod in cluster.get("pods", selector=selector)["items"]
            if not pod["metadata"].get("deletionTimestamp") and owned_by(pod, "ReplicaSet", replica_uid)
            and any(c["name"] == APP_CONTAINER and c["image"] == image for c in pod["spec"]["containers"])]
    if len(pods) != 1:
        return None

    pod, = pods
    app = next((c for c in pod.get("status", {}).get("containerStatuses", []) if c["name"] == APP_CONTAINER), {})
    waiting = app.get("state", {}).get("waiting", {}).get("reason")
    exit_code = app.get("lastState", {}).get("terminated", {}).get("exitCode")
    if (waiting != "CrashLoopBackOff" or app.get("ready") is not False or app.get("restartCount", 0) < 2
            or not app.get("imageID") or type(exit_code) is not int or exit_code == 0):
        return None

    slices = cluster.get("endpointslices", selector=f"kubernetes.io/service-name={NAME}")["items"]
    endpoints = [endpoint["conditions"] for group in slices for endpoint in group["endpoints"]]
    if (not slices or any(c.get("ready") is True and c.get("terminating") is not True for c in endpoints)
            or deployment()["metadata"]["resourceVersion"] != meta["resourceVersion"]):
        return None
    return {"deployment_uid": meta["uid"], "pod_uid": pod["metadata"]["uid"], "image": image,
            "runtime_image_id": app["imageID"], "container_state": waiting, "restart_count": app["restartCount"],
            "previous_exit_code": exit_code, "ready_endpoints": 0}
