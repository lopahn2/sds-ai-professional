"""Use a separate read identity, plus the single allowed DNS repair, for KI-10."""
import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
from select import select
import socket
from socketserver import BaseRequestHandler, ThreadingUnixStreamServer
import sys
from threading import Thread
from time import monotonic, sleep

from ki10_dns_fixture import (COMMAND, LABELS, LEGACY, NAMESPACE, REPOSITORY, STATE, VERSIONS,
                              WORKLOAD, cleanup, failed, kubectl, prepare, probes, put, ready_state, scope, state)


READ = "liveprobe-ki10-reader"
EXECUTOR = "liveprobe-ki10-executor"
POLICY = "ki10-version-preserving-repair"
SOCKET = Path("/tmp/lp-ki10-api.sock")
APP_NS = f"system:serviceaccount:{NAMESPACE}:{EXECUTOR}"


class Pipe(BaseRequestHandler):
    def handle(self):
        with socket.create_connection(("127.0.0.1", 6443), timeout=5) as remote:
            remote.settimeout(None)
            peers = {self.request: remote, remote: self.request}
            while True:
                active, _, _ = select(tuple(peers), (), (), 15)
                for conn in active:
                    payload = conn.recv(65536)
                    if not payload:
                        return
                    peers[conn].sendall(payload)


@contextmanager
def api_bridge():
    with ThreadingUnixStreamServer(str(SOCKET), Pipe) as server:
        server.daemon_threads = True
        os.chown(SOCKET, int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))
        SOCKET.chmod(0o600)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield
        finally:
            server.shutdown()
            worker.join(timeout=2)
            SOCKET.unlink(missing_ok=True)


def identity(name, rules):
    meta = {"namespace": NAMESPACE, "name": name}
    put({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": meta})
    rbac = "rbac.authorization.k8s.io"
    put({"apiVersion": f"{rbac}/v1", "kind": "Role", "metadata": meta, "rules": rules})
    put({"apiVersion": f"{rbac}/v1", "kind": "RoleBinding", "metadata": meta,
         "roleRef": {"apiGroup": rbac, "kind": "Role", "name": name},
         "subjects": [{"kind": "ServiceAccount", **meta}]})
    return kubectl("-n", NAMESPACE, "create", "token", name, "--duration=10m").strip()


def access():
    put({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE, "labels": LABELS}})
    reads = ["get", "list", "watch"]
    reader = identity(READ, [
        {"apiGroups": [""], "resources": ["pods", "services", "endpoints", "events"], "verbs": reads},
        {"apiGroups": [""], "resources": ["pods/log", "configmaps"], "verbs": ["get"]},
        {"apiGroups": ["apps"], "resources": ["deployments", "replicasets"], "verbs": reads},
        {"apiGroups": ["discovery.k8s.io"], "resources": ["endpointslices"], "verbs": reads},
    ])
    executor = identity(EXECUTOR, [{"apiGroups": ["apps"], "resources": ["deployments"],
                                     "resourceNames": [WORKLOAD], "verbs": ["get", "patch"]}])
    admission = "admissionregistration.k8s.io/v1"
    put({"apiVersion": admission, "kind": "ValidatingAdmissionPolicy", "metadata": {"name": POLICY},
         "spec": {"failurePolicy": "Fail", "matchConstraints": {"resourceRules": [{
             "apiGroups": ["apps"], "apiVersions": ["v1"], "operations": ["UPDATE"],
             "resources": ["deployments"], "scope": "Namespaced"}]},
             "matchConditions": [
                 {"name": "fixture-identity", "expression": f"request.namespace == '{NAMESPACE}' && "
                                                              f"request.userInfo.username == '{APP_NS}'"}],
             "validations": [
                 {"expression": f"object.spec.template.spec.containers[0].image == '{REPOSITORY}:{VERSIONS[1]}' && "
                                f"object.spec.template.spec.containers[0].args == {json.dumps(COMMAND)} && "
                                "object.spec.replicas == oldObject.spec.replicas",
                  "message": "KI-10 permits only the recovered new-version DNS configuration"}]}})
    put({"apiVersion": admission, "kind": "ValidatingAdmissionPolicyBinding", "metadata": {"name": POLICY},
         "spec": {"policyName": POLICY, "validationActions": ["Deny"]}})
    ca = (STATE / "state/server/tls/server-ca.crt").read_bytes()
    return {"reader": reader, "executor": executor, "authority": base64.b64encode(ca).decode()}


def authorization():
    prefix = ["--as", APP_NS, "-n", NAMESPACE, "patch", "deployment", WORKLOAD, "--dry-run=server", "--type=json", "-p"]
    container = "/spec/template/spec/containers/0/"
    good = [{"op": "replace", "path": container + "args", "value": COMMAND}]
    bad = [*good, {"op": "replace", "path": container + "image", "value": f"{REPOSITORY}:{VERSIONS[0]}"}]
    deadline = monotonic() + 12
    while monotonic() < deadline:
        denied = kubectl(*prefix, json.dumps(bad), required=False)
        if denied.returncode and POLICY in denied.stderr:
            break
        sleep(.25)
    else:
        raise RuntimeError("isolated admission did not enforce the fixed DNS version")
    assert kubectl(*prefix, json.dumps(good), required=False).returncode == 0
    assert state()["args"] == [*LEGACY, *COMMAND]
    return {"same_version_args_allowed": True, "older_version_denied": True}


def main():
    assert os.geteuid() == 0 and os.environ["K3S_DATA_DIR"] == str(STATE / "state")
    assert json.loads(sys.stdin.readline()) == {"tool": "connect"}
    try:
        credentials = access()
        with api_bridge():
            print(json.dumps(credentials), flush=True)
            assert json.loads(sys.stdin.readline()) == {"tool": "inject"}
            baseline, started = prepare(namespace_prepared=True)
            print(json.dumps({**baseline, "authority": authorization()}), flush=True)
            for line in sys.stdin:
                operation = json.loads(line)
                if operation == {"tool": "shutdown"}:
                    break
                if operation != {"tool": "verify"}:
                    raise ValueError("unsupported LiveProbe fixture operation")
                current = state()
                if current["args"] == COMMAND:
                    current = ready_state()
                checks = probes()
                print(json.dumps({"recovery": {"state": current, "probes": checks},
                                  "scope_after": scope(), "wall_duration_ms": round((monotonic()-started)*1000)}), flush=True)
    finally:
        kubectl("delete", "validatingadmissionpolicybinding", POLICY, "--ignore-not-found")
        kubectl("delete", "validatingadmissionpolicy", POLICY, "--ignore-not-found")
        cleanup()
    assert failed(probes())
    print(json.dumps({"removed": True, "dns_absent": True}), flush=True)


if __name__ == "__main__":
    main()
