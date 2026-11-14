"""사건 이력·조치의 기존 소유권 계약을 DB 없이 보존한다."""
from __future__ import annotations
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.config import AppConfig
from app.incidents import Signal, ingest
from app.models import Incident
from app.safety import check_action, Denied, effective_policy
from app.systems import SYSTEM_NAMES, SERVICE_SYSTEMS
from test_topology import deployment

class OwnershipPolicyTests(unittest.TestCase):
    def test_catalogue_is_the_same_as_runtime_display_and_contract_map(self):
        catalog = json.loads((Path(__file__).resolve().parents[3] / "deploy/samsung/system-catalog.json").read_text())
        self.assertEqual(SERVICE_SYSTEMS, catalog["service_to_system"])
        self.assertEqual(SYSTEM_NAMES, {s["id"]: s["display_name"] for s in catalog["systems"]})

    def test_old_incident_merge_does_not_reclassify_ownership(self):
        old = Incident(id=1, system="order", namespace="prd", workload="order", signal_count=1, signal_reasons=["CrashLoopBackOff"], status="open")
        session = Mock()
        session.scalars.return_value.first.return_value = old
        kube = Mock()
        with patch("app.incidents.audit"):
            inc, outcome = ingest(session, AppConfig(), Signal("prd", "Deployment", "order", "CrashLoopBackOff", "pod_watch"), kube=kube)
        assert inc is not None
        self.assertEqual((outcome, inc.system), ("merged", "order"))
        kube.get_deployment.assert_not_called()

    def test_new_incident_uses_only_current_live_ownership_label(self):
        session = Mock()
        session.scalars.return_value.first.return_value = None
        kube = Mock()
        kube.get_deployment.return_value = deployment("order", "samsung-delivery")
        with patch("app.incidents.audit"):
            inc, outcome = ingest(session, AppConfig(), Signal("prd", "Deployment", "order", "CrashLoopBackOff", "pod_watch"), now=datetime.now(timezone.utc), kube=kube)
        assert inc is not None
        self.assertEqual((outcome, inc.system), ("created", "samsung-delivery"))

    def test_renaming_does_not_allow_old_incident_to_act_on_new_system(self):
        kube = SimpleNamespace(get_deployment=lambda ns, name: deployment(name, "samsung-trade", ns, **{"liveprobe.io/managed": "true"}))
        cfg = AppConfig()
        self.assertEqual(check_action(cfg, kube, "rollout_undo", "prd", "Deployment", "order", system="samsung-trade")["metadata"]["labels"]["app.kubernetes.io/part-of"], "samsung-trade")
        for forbidden_system in ("order", "samsung-delivery", "samsung-business"):
            with self.assertRaises(Denied):
                check_action(cfg, kube, "rollout_undo", "prd", "Deployment", "order", system=forbidden_system)
        with self.assertRaises(Denied):
            check_action(cfg, kube, "rollout_undo", "liveprobe-messaging", "Deployment", "kafka", system="samsung-trade")
        self.assertEqual(effective_policy("HIGH_RISK", "auto"), "handoff")
        self.assertEqual(cfg.safe_actions.replicas_max, 2)
        self.assertEqual(cfg.safe_actions.memory_limit_max_mi, 768)

if __name__ == "__main__":
    unittest.main()
