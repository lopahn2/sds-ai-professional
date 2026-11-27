"""Prepare the scoped KW-03 verification Deployment and exact persistent volume claims."""
from argparse import ArgumentParser
from datetime import datetime, timezone
import fcntl
import json
from time import monotonic

from evaluation import k8s_dev_image_trial as live
from evaluation.dev_rollout_case import VOLUME as CASE, VOLUME_MISSING_PATH, VOLUME_NAME
from evaluation.k8s_dev_replica_trial import READABLE_ERRORS, hash_text


SCOPE = {"app": CASE.workload, "app.kubernetes.io/part-of": "liveprobe-verification"}
IMAGE = "liveprobe/k8s-verification@sha256:661e494b9e413e15cc0ad50eee9edf925f79630b087ee0df70ff259ad78a5209"
BASELINE = live.EVIDENCE / "dev-kw03-baseline.json"
PROGRAM = """
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

class Check(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/ready' or not Path('/fixture-volume').is_dir():
            self.send_error(404)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'kw03-ready')

    def log_message(self, *args):
        pass

HTTPServer(('0.0.0.0', 8080), Check).serve_forever()
"""


def manifest(hostname):
    def resource(kind, name, spec, namespace=True, api="v1"):
        return {"apiVersion": api, "kind": kind, "metadata": {
            "name": name, "labels": SCOPE, **({"namespace": "dev"} if namespace else {}),
        }, "spec": spec}

    claim = {"accessModes": ["ReadWriteOnce"], "volumeMode": "Filesystem", "resources": {"requests": {"storage": "1Mi"}}}
    affinity = [{"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": [hostname]}]}]
    volume = resource("PersistentVolume", VOLUME_NAME, {
        "capacity": {"storage": "1Mi"}, "accessModes": ["ReadWriteOnce"], "volumeMode": "Filesystem",
        "persistentVolumeReclaimPolicy": "Retain", "storageClassName": "",
        "local": {"path": VOLUME_MISSING_PATH}, "nodeAffinity": {"required": {"nodeSelectorTerms": affinity}},
        "claimRef": {"namespace": "dev", "name": CASE.broken},
    }, namespace=False)
    healthy = resource("PersistentVolumeClaim", CASE.healthy, {**claim, "storageClassName": "local-path"})
    failing = resource("PersistentVolumeClaim", CASE.broken, {**claim, "storageClassName": "", "volumeName": VOLUME_NAME})
    app = resource("Deployment", CASE.workload, {
        "replicas": 1, "revisionHistoryLimit": 3,
        "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}},
        "selector": {"matchLabels": {"app": CASE.workload}},
        "template": {"metadata": {"labels": SCOPE}, "spec": {
            "serviceAccountName": "default", "automountServiceAccountToken": False, "enableServiceLinks": False,
            "terminationGracePeriodSeconds": 5,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "seccompProfile": {"type": "RuntimeDefault"}},
            "volumes": [{"name": "check", "persistentVolumeClaim": {"claimName": CASE.healthy, "readOnly": True}}],
            "containers": [{
                "name": "verification", "image": IMAGE, "imagePullPolicy": "Never", "command": ["python", "-B", "-c"],
                "args": [PROGRAM], "readinessProbe": {"httpGet": {"path": "/ready", "port": 8080}, "periodSeconds": 2},
                "volumeMounts": [{"name": "check", "mountPath": "/fixture-volume", "readOnly": True}],
                "resources": {"requests": {"cpu": "10m", "memory": "24Mi"}, "limits": {"cpu": "100m", "memory": "64Mi"}},
                "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]}},
            }],
        }},
    }, api="apps/v1")
    app["metadata"]["labels"] = {**SCOPE, "liveprobe.io/managed": "true"}
    return {"apiVersion": "v1", "kind": "List", "items": [volume, healthy, failing, app]}


def fixture_manifest():
    node, = live.get("nodes")["items"]
    hostname = node["metadata"]["labels"]["kubernetes.io/hostname"]
    storage = live.get("storageclass", "local-path")
    if (node["metadata"]["labels"].get("kubernetes.io/os") != "linux"
            or storage.get("provisioner") != "rancher.io/local-path" or storage.get("volumeBindingMode") != "WaitForFirstConsumer"):
        raise live.Refused("isolated local volume prerequisites do not match the fixture")
    return manifest(hostname)


def volume_bindings():
    receipts = {}
    for name in (CASE.healthy, CASE.broken):
        pvc = live.get("pvc", name)
        pv = live.get("pv", pvc["spec"]["volumeName"])
        ref = pv["spec"].get("claimRef") or {}
        if (pvc["metadata"]["labels"].get("app") != CASE.workload or pvc["status"]["phase"] != "Bound"
                or ref.get("uid") != pvc["metadata"]["uid"] or ref.get("namespace") != "dev" or ref.get("name") != name
                or pv["status"]["phase"] != "Bound"):
            raise live.Refused("persistent volume binding is not scoped to the approved dev fixture")
        if name == CASE.broken:
            if (pv["metadata"]["name"] != VOLUME_NAME or pv["metadata"]["labels"].get("app") != CASE.workload
                    or pv["spec"].get("local") != {"path": VOLUME_MISSING_PATH}
                    or pv["spec"].get("persistentVolumeReclaimPolicy") != "Retain"
                    or pvc["spec"].get("storageClassName") != ""):
                raise live.Refused("broken test volume does not match its designated path and ownership")
        elif pvc["spec"].get("storageClassName") != "local-path":
            raise live.Refused("healthy claim did not use the approved local provisioner")
        receipts[name] = {"claim_uid": pvc["metadata"]["uid"], "pv": pv["metadata"]["name"], "pv_uid": pv["metadata"]["uid"]}
    return receipts


def main():
    from evaluation.k8s_dev_rollout_trial import verify

    parser = ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    data = fixture_manifest()
    body = json.dumps(data)
    dry = live.command("create", "-f", "-", "--dry-run=server", "-o", "name", stdin=body)
    if len(dry.strip().splitlines()) != 4:
        raise live.Refused("server dry-run did not admit the four scoped fixture objects")
    if args.preflight:
        print(json.dumps({"case": CASE.case_id, "server_dry_run": True, "objects": dry.strip().splitlines()}))
        return 0
    journal = live.EVIDENCE / "dev-k8s-fault-active.json"
    with (live.EVIDENCE / "dev-k8s-fault.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if journal.exists() or BASELINE.exists():
            raise live.Refused("a fault or an existing volume baseline requires review")
        gate = live.CHAOS.node_gate()
        created = live.command("create", "-f", "-", "-o", "name", stdin=body).strip().splitlines()
        live.command("rollout", "status", f"deployment/{CASE.workload}", "--timeout=85s", timeout=90)
        dep = live.get("deployment", CASE.workload)
        baseline = {"at": datetime.now(timezone.utc).isoformat(), "case": CASE.case_id, "namespace": "dev",
                    "isolated_deployment": CASE.workload, "uid": dep["metadata"]["uid"],
                    "fixture_sha256": live.fixture_digest(dep), "manifest_sha256": hash_text(body), "manifest": data,
                    "volume_bindings": volume_bindings(), "created": created, "node_gate": gate}
        baseline.update(ready=True, baseline_http=verify(baseline, monotonic() + 20, CASE)["requests"])
        live.write_once(BASELINE, baseline)
    print(json.dumps({"case": CASE.case_id, "ready": True, "baseline": str(BASELINE), "created": created}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (*READABLE_ERRORS, live.CHAOS.Stop) as error:
        print(json.dumps({"eligible": False,
                          "reason": str(error) if isinstance(error, (live.Refused, live.CHAOS.Stop)) else type(error).__name__}))
        raise SystemExit(2) from None
