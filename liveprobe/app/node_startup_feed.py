"""Read live host startup output and write only recognized facts to JSON Lines."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone


SCHEMA = "liveprobe/node-startup/v1"
FAILURES = {
    "kubelet_version": "kubeadm이 설치된 kubelet 버전을 지원하지 않아 등록을 거부했습니다.",
    "cri_v1": "컨테이너 런타임이 kubelet에 필요한 CRI v1 서비스를 제공하지 않았습니다.",
    "cgroup_v1": "kubelet이 cgroup v1 호스트에서 시작을 거부했습니다.",
}
KUBELET_VERSION = r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,4}"
_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
NODE_NAME = re.compile(rf"(?=.{{1,253}}\Z){_DNS_LABEL}(?:\.{_DNS_LABEL})*")
_KUBEADM = re.compile(r'\[ERROR KubeletVersion\]: Kubelet version "(' + KUBELET_VERSION + r')" is lower than kubeadm can support\.')
_CRI = "rpc error: code = Unimplemented desc = unknown service runtime.v1.RuntimeService"
_CGROUP = "kubelet is configured to not run on a host using cgroup v1."


def observe_line(node: str, component: str, line: str, at: datetime) -> dict | None:
    if not NODE_NAME.fullmatch(node) or not line or len(line) > 8192:
        return None
    fields = {"schema": SCHEMA, "node": node, "component": component, "observed_at": at.isoformat()}
    if component == "kubeadm" and (match := _KUBEADM.search(line)):
        return {**fields, "failure": "kubelet_version", "kubelet_version": match[1]}
    if component in {"kubelet", "k3s-agent"} and _CRI in line:
        return {**fields, "failure": "cri_v1"}
    if component in {"kubelet", "k3s-agent"} and _CGROUP in line:
        return {**fields, "failure": "cgroup_v1"}
    return None


def main():
    parser = argparse.ArgumentParser(description="Convert live node startup errors into scoped observation records.")
    parser.add_argument("--node", required=True)
    parser.add_argument("--component", required=True, choices=("kubeadm", "kubelet", "k3s-agent"))
    args = parser.parse_args()
    if not NODE_NAME.fullmatch(args.node):
        parser.error("invalid Kubernetes node name")
    for line in sys.stdin:
        record = observe_line(args.node, args.component, line, datetime.now(timezone.utc))
        if record:
            print(json.dumps(record, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
