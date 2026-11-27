"""Collect provisioning failures that occur before Kubernetes has a Node object."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .incidents import NODE_STARTUP_BLOCKED, NODE_STARTUP_SOURCE, Signal
from .node_startup_feed import KUBELET_VERSION, NODE_NAME, SCHEMA
from .observation_log import ObservationLog


class NodeStartupWatcher(ObservationLog):
    def __init__(self, path: Path, cfg, targets: dict[str, str], kube, emit, *, clock=None):
        super().__init__(path, NODE_STARTUP_SOURCE)
        self.cfg, self.targets, self.kube, self.emit = cfg, targets, kube, emit
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _consume(self, entry):
        if not isinstance(entry, dict) or entry.get("schema") != SCHEMA:
            return False
        node, component, failure = (entry.get(key) for key in ("node", "component", "failure"))
        if not all(isinstance(value, str) for value in (node, component, failure)) or not NODE_NAME.fullmatch(node):
            return False
        namespace = self.targets.get(node)
        if not namespace or not self.cfg.is_active_environment(namespace):
            return False
        version = entry.get("kubelet_version")
        if failure == "kubelet_version":
            if component != "kubeadm" or not isinstance(version, str) or not re.fullmatch(KUBELET_VERSION, version):
                return False
        elif failure not in {"cri_v1", "cgroup_v1"} or component not in {"kubelet", "k3s-agent"}:
            return False
        stamp = entry.get("observed_at")
        if not isinstance(stamp, str) or len(stamp) > 40:
            return False
        try:
            observed = datetime.fromisoformat(stamp)
            if observed.tzinfo is None or not -30 <= (self.clock() - observed).total_seconds() <= 300:
                return False
        except (ValueError, OverflowError):
            return False
        if self.kube.get_node(node) is not None:
            return False
        detail = {"node": node, "component": component, "failure": failure,
                  "observed_at": observed.astimezone(timezone.utc).isoformat(), "node_registered": False}
        if failure == "kubelet_version":
            detail["kubelet_version"] = version
        self.emit(Signal(namespace, "Node", node, NODE_STARTUP_BLOCKED, NODE_STARTUP_SOURCE, detail))
        return True

    def poll_once(self) -> int:
        emitted = self._poll(self._consume)
        self.status = {"state": "running", "emitted": emitted}
        return emitted
