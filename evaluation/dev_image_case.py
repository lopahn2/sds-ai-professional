"""Approved bad-image cases shared by the dev trial and evidence reader."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IMAGE_WORKLOADS = {"F1-01": "inventory", "F1-07": "order", "F1-13": "inventory"}


@dataclass(frozen=True)
class DevImageCase:
    case_id: str
    workload: str
    bad_image: str

    @property
    def target(self):
        return f"dev/Deployment/{self.workload}"


def load_image_case(case_id):
    workload = IMAGE_WORKLOADS.get(case_id)
    if workload is None:
        raise ValueError("case is outside the bounded dev image-pull fixtures")
    source = f"chaos/cases/{case_id}.yaml"
    catalog = json.loads((ROOT / "evaluation/catalogs/comparative-2026-11-23.json").read_text())
    row, = [entry for entry in catalog["basic"]["cases"] if entry["id"] == case_id]
    raw = (ROOT / source).read_bytes()
    if row["source"] != source or row["case_sha256"] != hashlib.sha256(raw).hexdigest():
        raise ValueError("approved case digest changed")
    case = json.loads(raw)
    tag = f"liveprobe/{workload}:does-not-exist-{case_id[-2:]}"
    expected = {"mode": "bad_image", "parameters": {"tag": tag}}
    if case["id"] != case_id or case["target"] != workload or case["injection"] != expected:
        raise ValueError("case differs from the bounded dev image-pull fixture")
    return DevImageCase(case_id, workload, tag)


def successful_probes(rows):
    return (isinstance(rows, list) and len(rows) == 2
            and all(row.get("contract_ok") is True and row.get("ready") == row.get("business") == 200 for row in rows))
