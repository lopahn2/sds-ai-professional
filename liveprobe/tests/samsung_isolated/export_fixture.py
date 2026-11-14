"""실제 조립 함수를 사용하되 합성 inventory만으로 화면 시험 표본을 만든다."""
from __future__ import annotations
import json
from pathlib import Path
from app.config import AppConfig
from app.flows import FlowStore
from app.overview import assemble
from test_topology import inventory, service

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[3]
    inv = inventory()
    inv["services"].append(service("kafka", None, "liveprobe-messaging"))
    inv["endpoints"] = [{"metadata": dict(s["metadata"]), "subsets": [{"addresses": [{"ip": "192.0.2.1"}]}]} for s in inv["services"]]
    result = assemble(inv, {}, store=FlowStore(), cfg=AppConfig())
    result["sources"] = {"kubernetes": "합성 화면 시험 표본 · 실제 클러스터 아님", "prometheus": "미관측", "hubble": "미관측", "envoy": "미관측"}
    for target in (root / "dashboard/e2e/fixtures/samsung-overview.mock.json", root / "docs/evidence/samsung/dashboard/samsung-overview.mock.json"):
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print("합성 inventory로 실제 assemble을 실행한 모의 표본 2개 생성 · 실배포 증거 아님")
