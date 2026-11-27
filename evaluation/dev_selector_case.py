"""Frozen Service selector faults from the approved basic comparison catalog."""
from dataclasses import dataclass
import hashlib
import json

from evaluation.dev_image_case import ROOT


SELECTOR_WORKLOADS = {"F4-01": "inventory", "F4-03": "order", "F4-09": "inventory", "F4-11": "order"}
ENDPOINT_GRACE_SECONDS = 600
PROTOCOL = "live-repair/v36"


@dataclass(frozen=True)
class DevSelectorCase:
    case_id: str
    workload: str
    bad_app: str
    case_sha256: str

    @property
    def target(self):
        return f"dev/Service/{self.workload}"

    @property
    def errors(self):
        return tuple(f'no endpoints available for service "{name}"' for name in
                     (self.workload, f"{self.workload}:8080"))


def load_selector_case(case_id):
    workload = SELECTOR_WORKLOADS.get(case_id)
    if workload is None:
        raise ValueError("case is outside the approved dev Service selector fixtures")
    source = f"chaos/cases/{case_id}.yaml"
    catalog = json.loads((ROOT / "evaluation/catalogs/comparative-2026-11-23.json").read_text())
    row, = [entry for entry in catalog["basic"]["cases"] if entry["id"] == case_id]
    raw = (ROOT / source).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    bad = f"{workload}-broken-{case_id[-2:]}"
    expected = {"mode": "selector_mismatch", "parameters": {"selector_value": bad}}
    definition = json.loads(raw)
    if (row["source"] != source or row["case_sha256"] != digest or definition["id"] != case_id
            or definition["target"] != workload or definition["injection"] != expected):
        raise ValueError("approved Service selector case changed")
    return DevSelectorCase(case_id, workload, bad, digest)
