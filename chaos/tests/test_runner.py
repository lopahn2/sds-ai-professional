"""Fault runner safety tests; no Kubernetes calls."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

RUNNER = Path(__file__).resolve().parents[1] / "run-case.py"
sys.path.insert(0, str(RUNNER.parent))
spec = importlib.util.spec_from_file_location("case_runner", RUNNER)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def test_kubelet_fallback_keeps_the_resource_gate(self):
        node_name = "fixture"
        stamp = datetime.now(timezone.utc)
        snapshot = {"items": [{"metadata": {"name": node_name}, "status": {
            "allocatable": {"cpu": "2", "memory": "4194304Ki"},
            "conditions": [{"type": key, "status": value} for key, value in {
                "Ready": "True", "MemoryPressure": "False", "DiskPressure": "False", "PIDPressure": "False",
            }.items()],
        }}]}
        report = {"node": {"nodeName": node_name,
                           "cpu": {"time": stamp.isoformat(), "usageNanoCores": 300_000_001},
                           "memory": {"time": stamp.isoformat(), "workingSetBytes": 530 * 1_048_576 + 1}}}

        def api(*args, **kwargs):
            if args == ("get", "nodes", "-o", "json"):
                return json.dumps(snapshot)
            if args == ("top", "node", "--no-headers"):
                raise runner.Stop("metrics unavailable")
            if args == ("get", "--raw", "/api/v1/nodes/fixture/proxy/metrics/resource"):
                raise runner.Stop("resource metrics unavailable")
            self.assertEqual(args, ("get", "--raw", "/api/v1/nodes/fixture/proxy/stats/summary"))
            return json.dumps(report)

        with patch.object(runner, "kubectl", side_effect=api):
            self.assertEqual(runner.node_gate(), {"cpu_m": 301, "cpu_cap_m": 2000, "mem_mi": 531,
                                                   "alloc_mi": 4096, "metrics_source": "kubelet_summary"})
            report["node"]["cpu"]["usageNanoCores"] = 1_400_000_000
            with self.assertRaisesRegex(runner.Stop, "node stop gate breached"):
                runner.node_gate()
            report["node"]["cpu"]["time"] = (stamp - timedelta(seconds=90)).isoformat()
            with self.assertRaisesRegex(runner.Stop, "node metrics unavailable"):
                runner.node_gate()
            report["node"]["cpu"]["time"] = stamp.isoformat()
            report["node"]["nodeName"] = "another-node"
            with self.assertRaisesRegex(runner.Stop, "node metrics unavailable"):
                runner.node_gate()

    def test_kubelet_resource_requires_fresh_node_samples_and_keeps_the_gate(self):
        node_name = "fixture"
        now = 1_795_445_000
        node = {"items": [{"metadata": {"name": node_name}, "status": {
            "allocatable": {"cpu": "2", "memory": "4194304Ki"},
            "conditions": [{"type": key, "status": value} for key, value in {
                "Ready": "True", "MemoryPressure": "False", "DiskPressure": "False", "PIDPressure": "False",
            }.items()],
        }}]}

        def sample(cpu, ago, memory="5.55745281e+08", error="0", memory_age=None):
            memory_time = now - (memory_age if memory_age is not None else ago)
            return (f"node_cpu_usage_seconds_total {cpu} {(now - ago) * 1000}\n"
                    f"node_memory_working_set_bytes {memory} {memory_time * 1000}\n"
                    f"resource_scrape_error {error}\n"
                    "container_memory_working_set_bytes{node=\"other\"} 999999999999\n")

        def gate(readings):
            readings = iter(readings)

            def api(*args, **kwargs):
                if args == ("get", "nodes", "-o", "json"):
                    return json.dumps(node)
                if args in (("top", "node", "--no-headers"),
                            ("get", "--raw", "/api/v1/nodes/fixture/proxy/stats/summary")):
                    raise runner.Stop("metrics unavailable")
                self.assertEqual(args, ("get", "--raw", "/api/v1/nodes/fixture/proxy/metrics/resource"))
                self.assertEqual(kwargs["timeout"], 16)
                return next(readings)

            with patch.object(runner, "kubectl", side_effect=api), \
                    patch.object(runner.time, "time", return_value=now), patch.object(runner.time, "sleep") as sleep:
                try:
                    return runner.node_gate()
                finally:
                    self.assertLessEqual(sleep.call_count, 2)

        first = sample("100.0", 30)
        healthy = sample("103.000001", 20, memory="5.55745280e+08")
        self.assertEqual(gate([first, first, healthy]), {"cpu_m": 301, "cpu_cap_m": 2000, "mem_mi": 531,
                                                       "alloc_mi": 4096, "metrics_source": "kubelet_resource"})
        for readings in ([first, sample("114", 20)], [first, sample("103", 20, memory=2147483649)]):
            with self.subTest(readings=readings), self.assertRaisesRegex(runner.Stop, "node stop gate breached"):
                gate(readings)
        for readings in ([sample("100", 61)], [sample("100", 30, error="1")], [sample("100", 30, memory_age=31)],
                         [first, sample("99", 20)], [first, first, first]):
            with self.subTest(readings=readings), self.assertRaisesRegex(runner.Stop, "node metrics unavailable"):
                gate(readings)

    def test_only_operator_owned_cases(self):
        self.assertEqual(runner.case_file("F1-01")["injection"]["mode"], "bad_image")
        self.assertEqual(runner.case_file("F4-15")["injection"]["mode"], "selector_mismatch")
        for cid in ("F1-02", "F2-01", "F3-01", "F4-02", "F1-99", "../../x"):
            with self.subTest(cid=cid), self.assertRaises(runner.Stop):
                runner.case_file(cid)

    def test_signal_requires_new_pod_and_real_waiting_reason(self):
        with patch.object(runner, "pod_status", side_effect=[
                [("old", "uid-old", {"state": {"waiting": {"reason": "ImagePullBackOff"}}})],
                [("new", "uid-new", {"state": {"waiting": {"reason": "Running"}}})],
                [("new", "uid-new", {"state": {"waiting": {"reason": "ErrImagePull"}}})]]), \
                patch.object(runner.time, "sleep"):
            self.assertEqual(runner.signal("inventory", "bad_image", {"uid-old"}),
                             {"new_pod": "new", "reason": "ErrImagePull"})

    def test_selector_requires_zero_ready_endpoints(self):
        with patch.object(runner, "endpoints", side_effect=[1, 0]), patch.object(runner.time, "sleep"):
            self.assertEqual(runner.signal("order", "selector_mismatch", set()), {"ready_endpoints": 0})

    def test_selector_rollback_replaces_the_whole_map(self):
        baseline = {"order": {"selector": {"app": "order"}}}
        for current in ({"app": "order", "extra": "wrong"}, None):
            with self.subTest(current=current), patch.object(runner, "obj", return_value={"spec": {
                **({"selector": current} if current else {}),
            }}), patch.object(runner, "kubectl") as kubectl:
                runner.restore(baseline, "order", "selector_mismatch")
                args = kubectl.call_args.args
                self.assertEqual(args[:5], ("patch", "service", "order", "--type=json", "-p"))
                self.assertEqual(json.loads(args[5]), [
                    {"op": "add", "path": "/spec/selector", "value": {"app": "order"}},
                ])

    def test_preflight_failure_never_injects_or_creates_case_journal(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(runner, "node_gate", side_effect=runner.Stop("node not ready")), \
                patch.object(runner, "kubectl") as kubectl, \
                patch.object(runner, "restore") as restore:
            with self.assertRaisesRegex(runner.Stop, "node not ready"):
                runner.run("F1-01", Path(folder))
            self.assertFalse((Path(folder) / "active.json").exists())
            self.assertFalse((Path(folder) / "results.jsonl").exists())
            kubectl.assert_not_called()
            restore.assert_not_called()

    def test_failure_restores_and_stops_campaign(self):
        base = {t: {"image": "liveprobe/" + t + "@sha256:" + "a" * 64, "selector": {"app": t}}
                for t in runner.TARGETS}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(runner, "node_gate", return_value={}), \
                patch.object(runner, "snapshot", return_value=base), \
                patch.object(runner, "healthy") as health, \
                patch.object(runner, "pg_sessions", return_value=1), \
                patch.object(runner, "pod_status", return_value=[]), \
                patch.object(runner, "kubectl") as kubectl, \
                patch.object(runner, "signal", side_effect=runner.Stop("no signal")), \
                patch.object(runner, "restore") as restore:
            with self.assertRaises(runner.Stop):
                runner.run("F1-01", Path(folder))
            restore.assert_called_once_with(base, "inventory", "bad_image")
            self.assertEqual(health.call_count, 2)
            self.assertFalse((Path(folder) / "active.json").exists())
            result = json.loads((Path(folder) / "results.jsonl").read_text())
            self.assertEqual(result["status"], "FAIL")
            self.assertTrue(result["baseline_restored"])
            self.assertFalse("ground_truth" in str(result))
            kubectl.assert_called()

    def test_failed_restore_retains_journal(self):
        base = {t: {"image": "digest", "selector": {"app": t}} for t in runner.TARGETS}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(runner, "node_gate", return_value={}), patch.object(runner, "snapshot", return_value=base), \
                patch.object(runner, "healthy"), patch.object(runner, "pg_sessions", return_value=1), \
                patch.object(runner, "pod_status", return_value=[]), patch.object(runner, "kubectl"), \
                patch.object(runner, "signal", return_value={"ready_endpoints": 0}), \
                patch.object(runner, "restore", side_effect=runner.Stop("failed")):
            with self.assertRaises(runner.Stop):
                runner.run("F4-01", Path(folder))
            self.assertTrue((Path(folder) / "active.json").exists())
            self.assertFalse(json.loads((Path(folder) / "results.jsonl").read_text())["baseline_restored"])


if __name__ == "__main__":
    unittest.main()
