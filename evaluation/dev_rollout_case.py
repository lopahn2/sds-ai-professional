"""Fixture identity and the one field each isolated Kubernetes trial may change."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RolloutCase:
    case_id: str = "KW-05"
    workload: str = "lp-eval-kw05"
    pointer: str = "/spec/template/spec/containers/0/readinessProbe/httpGet/path"
    healthy: str = "/ready"
    broken: str = "/kw05-not-ready"
    signal: str = "ProgressDeadlineExceeded"
    repair_key: str = "readiness_path"
    body: str = "kw05-ready"
    budget_seconds: int = 300

    @property
    def target(self):
        return f"dev/Deployment/{self.workload}"

    @property
    def selector(self):
        return f"app={self.workload}"

    def field(self, deployment):
        *parts, key = (part.replace("~1", "/").replace("~0", "~") for part in self.pointer.split("/")[1:])
        parent = deployment
        for part in parts:
            parent = parent[int(part) if isinstance(parent, list) else part]
        return parent, key


CASE = RolloutCase()
CONFIG = RolloutCase("KW-02", "lp-eval-kw02", "/spec/template/spec/containers/0/env/0/valueFrom/configMapKeyRef/name",
                     "lp-eval-kw02-configuration", "lp-eval-kw02-missing", "RequiredContainerReferenceMissing",
                     "configmap_reference", "kw02-ready", 360)
VOLUME = RolloutCase("KW-03", "lp-eval-kw03", "/spec/template/spec/volumes/0/persistentVolumeClaim/claimName",
                     "lp-eval-kw03-ready", "lp-eval-kw03-unprovisioned", "FailedMount",
                     "volume_claim_name", "kw03-ready", 390)
VOLUME_NAME = "lp-eval-kw03-unprovisioned-pv"
VOLUME_MISSING_PATH = "/var/lib/liveprobe-unprovisioned-eval/lp-eval-kw03"
VOLUME_ERROR = f'MountVolume.NewMounter initialization failed for volume "{VOLUME_NAME}" : path "{VOLUME_MISSING_PATH}" does not exist'
VOLUME_OBSERVATION_SECONDS = 130
SCHEDULING = RolloutCase("KW-01", "lp-eval-kw01", "/spec/template/spec/nodeSelector/kubernetes.io~1os",
                         "linux", "liveprobe-eval-no-node", "FailedScheduling", "node_selector_os", "kw01-ready", 420)
LIVENESS = RolloutCase("KW-06", "lp-eval-kw06", "/spec/template/spec/containers/0/livenessProbe/httpGet/path",
                       "/ready", "/kw06-bad-probe", "Unhealthy", "liveness_path", "kw06-ready", 450)
LIVENESS_ERROR = "Liveness probe failed: HTTP probe failed with statuscode: 404"
LIVENESS_OBSERVATION_SECONDS = 180
CASES = {case.case_id: case for case in (CASE, CONFIG, SCHEDULING, LIVENESS, VOLUME)}
