"""LP-OBS-20261114 regression: Grafana is retired, its data and every incident detector are kept.

Static only (kubectl kustomize + PyYAML, no Docker, no cluster):
  * no supported render reinstalls a Grafana Deployment/Service/Ingress/NetworkPolicy or needs grafana-admin;
  * the saved-data PVC `grafana-data` stays in deploy/base and deploy/base/observability (spec unchanged);
  * the archived runtime deploy/retired/grafana is disabled (replicas 0), recovery-ready, PVC-free and
    referenced by no active kustomization;
  * the incident-detection path (Prometheus + Alertmanager webhook, KSM, Loki + Alloy, Hubble Relay egress,
    Envoy/istio scrape) is still rendered.
Run: liveprobe/.venv/bin/python -m pytest deploy/base/observability/test_observability_minimal.py -q
"""
import pathlib
import subprocess
import unittest

import yaml

OBS = pathlib.Path(__file__).resolve().parent
DEPLOY = OBS.parents[1]
REPO = DEPLOY.parent
ARCHIVE = DEPLOY / "retired" / "grafana"
NS = "liveprobe-observability"
ACTIVE_RENDERS = ("base", "base/observability", "overlays/rancher-desktop", "overlays/rancher-desktop-observability",
                  "overlays/portable-example", "overlays/rancher-desktop-chaos")
PVC_SPEC = {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}}
_CACHE = {}


def render(rel):
    if rel not in _CACHE:
        out = subprocess.run(["kubectl", "kustomize", str(DEPLOY / rel)], capture_output=True, text=True,
                             timeout=120, check=True).stdout
        _CACHE[rel] = [d for d in yaml.safe_load_all(out) if d]
    return _CACHE[rel]


def find(docs, kind, name, ns=NS):
    return [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name
            and d["metadata"].get("namespace") == ns]


def containers(docs):
    for d in docs:
        spec = d.get("spec", {}).get("template", {}).get("spec", {})
        for c in spec.get("initContainers", []) + spec.get("containers", []):
            yield d, c


class NoActiveGrafanaTest(unittest.TestCase):
    def test_no_grafana_runtime_exposure_policy_or_secret_in_any_supported_render(self):
        for rel in ACTIVE_RENDERS:
            with self.subTest(render=rel):
                docs = render(rel)
                for kind in ("Deployment", "StatefulSet", "Service", "Ingress", "NetworkPolicy", "ConfigMap", "Secret"):
                    self.assertFalse([d for d in docs if d["kind"] == kind and "grafana" in d["metadata"]["name"]],
                                     f"{rel}: retired Grafana {kind} rendered")
                for d in docs:
                    if d["kind"] == "NetworkPolicy":
                        self.assertNotIn("grafana", yaml.safe_dump(d["spec"]), f"{rel}: {d['metadata']['name']}")
                for d, c in containers(docs):
                    self.assertNotEqual(c["image"].split("/")[-1].split(":")[0], "grafana", d["metadata"]["name"])
                    refs = {e.get("valueFrom", {}).get("secretKeyRef", {}).get("name") for e in c.get("env", [])}
                    self.assertNotIn("grafana-admin", refs, d["metadata"]["name"])

    def test_only_the_two_liveprobe_ingresses_remain(self):
        for rel in ("overlays/rancher-desktop", "overlays/portable-example", "overlays/rancher-desktop-chaos"):
            with self.subTest(render=rel):
                self.assertEqual(sorted(d["metadata"]["name"] for d in render(rel) if d["kind"] == "Ingress"),
                                 ["liveprobe-api", "liveprobe-dashboard"])
        self.assertFalse([d for d in render("overlays/rancher-desktop-observability") if d["kind"] == "Ingress"])

    def test_platform_wiring_has_no_grafana_host(self):
        for path in [DEPLOY / "components/platform/kustomization.yaml",
                     *sorted((DEPLOY / "overlays").glob("*/platform.yaml"))]:
            with self.subTest(path=str(path.relative_to(REPO))):
                self.assertNotIn("data.grafanaHost", path.read_text())
                if path.name == "platform.yaml":
                    self.assertNotIn("grafanaHost", yaml.safe_load(path.read_text())["data"])

    def test_normal_secret_creation_no_longer_requires_grafana_admin(self):
        text = (REPO / "scripts/create-secrets.sh").read_text()
        self.assertNotIn("mk_secret \"$OBS\" grafana-admin", text)
        self.assertNotRegex(text, r"(?m)^[^#]*delete[^\n]*grafana-admin")  # existing Secret is never removed


class GrafanaDataPreservedTest(unittest.TestCase):
    def test_grafana_data_pvc_stays_in_base_and_observability_with_original_spec(self):
        for rel in ("base", "base/observability"):
            with self.subTest(render=rel):
                [pvc] = find(render(rel), "PersistentVolumeClaim", "grafana-data")
                self.assertEqual(pvc["spec"], PVC_SPEC)  # no storageClass in base: overlays inject it

    def test_overlays_keep_grafana_data_on_the_platform_storage_class(self):
        for rel, sc in (("overlays/rancher-desktop", "local-path"), ("overlays/rancher-desktop-observability", "local-path"),
                        ("overlays/portable-example", "standard-rwo")):
            with self.subTest(render=rel):
                [pvc] = find(render(rel), "PersistentVolumeClaim", "grafana-data")
                self.assertEqual(pvc["spec"], {**PVC_SPEC, "storageClassName": sc})

    def test_kustomizations_list_the_data_only_file(self):
        base = yaml.safe_load((DEPLOY / "base/kustomization.yaml").read_text())["resources"]
        local = yaml.safe_load((OBS / "kustomization.yaml").read_text())["resources"]
        self.assertIn("observability/grafana/grafana-data-pvc.yaml", base)
        self.assertIn("grafana/grafana-data-pvc.yaml", local)
        self.assertEqual([d["kind"] for d in yaml.safe_load_all((OBS / "grafana/grafana-data-pvc.yaml").read_text()) if d],
                         ["PersistentVolumeClaim"])


class RetiredArchiveTest(unittest.TestCase):
    def test_archive_is_outside_every_active_kustomization(self):
        for path in DEPLOY.rglob("kustomization.yaml"):
            if path.parent == ARCHIVE or ARCHIVE in path.parents:
                continue
            kust = yaml.safe_load(path.read_text()) or {}
            refs = [r for key in ("resources", "components", "bases") for r in kust.get(key, []) or []]
            for ref in refs:
                if "://" in ref:
                    continue
                target = (path.parent / ref).resolve()
                with self.subTest(path=str(path.relative_to(REPO)), ref=ref):
                    self.assertFalse(target == ARCHIVE or ARCHIVE in target.parents)
        for rel in ACTIVE_RENDERS:  # and nothing from the archive leaks in through a render
            with self.subTest(render=rel):
                self.assertFalse([d for d in render(rel) if d["metadata"].get("labels", {}).get("liveprobe.io/retired")])

    def test_archive_renders_disabled_recovery_unit_without_duplicate_pvc(self):
        docs = render("retired/grafana")
        self.assertEqual(sorted((d["kind"], d["metadata"]["name"]) for d in docs),
                         [("ConfigMap", "grafana-dashboards"), ("ConfigMap", "grafana-datasources"), ("Deployment", "grafana")])
        [dep] = find(docs, "Deployment", "grafana")
        spec = dep["spec"]
        pod = spec["template"]["spec"]
        [c] = pod["containers"]
        self.assertEqual(spec["replicas"], 0)
        self.assertEqual(spec["strategy"], {"type": "Recreate"})
        self.assertIs(pod["automountServiceAccountToken"], False)
        self.assertEqual(c["readinessProbe"]["timeoutSeconds"], 5)
        self.assertEqual(c["readinessProbe"]["httpGet"], {"path": "/api/health", "port": 3000})
        vols = {v["name"]: v for v in pod["volumes"]}
        self.assertEqual(vols["data"], {"name": "data", "persistentVolumeClaim": {"claimName": "grafana-data"}})
        self.assertIn({"name": "data", "mountPath": "/var/lib/grafana"}, c["volumeMounts"])
        self.assertFalse([v for v in pod["volumes"] if "emptyDir" in v])
        refs = {e["valueFrom"]["secretKeyRef"]["name"] for e in c["env"] if "valueFrom" in e}
        self.assertEqual(refs, {"grafana-admin"})  # existing Secret, referenced by name only


class IncidentDetectorsRetainedTest(unittest.TestCase):
    def test_observability_workloads_are_rendered(self):
        for rel in ("base", "overlays/rancher-desktop", "overlays/rancher-desktop-observability"):
            with self.subTest(render=rel):
                docs = render(rel)
                for name in ("prometheus", "alertmanager", "kube-state-metrics", "loki", "alloy"):
                    self.assertEqual(len(find(docs, "Deployment", name)), 1, name)
                for name in ("prometheus", "alertmanager", "kube-state-metrics", "loki"):
                    self.assertEqual(len(find(docs, "Service", name)), 1, name)
                self.assertEqual(len(find(docs, "PersistentVolumeClaim", "loki-data")), 1)

    def test_alertmanager_webhook_reaches_liveprobe(self):
        docs = render("overlays/rancher-desktop")
        [cm] = find(docs, "ConfigMap", "alertmanager-config")
        cfg = yaml.safe_load(next(iter(cm["data"].values())))
        urls = [w["url"] for r in cfg["receivers"] for w in r.get("webhook_configs", [])]
        self.assertIn("http://liveprobe-api.liveprobe-system.svc:8000/api/hooks/alertmanager", urls)
        [am] = find(docs, "NetworkPolicy", "alertmanager")
        self.assertIn("liveprobe-api", yaml.safe_dump(am["spec"]["egress"]))

    def test_liveprobe_api_keeps_prometheus_loki_and_hubble_paths(self):
        docs = render("overlays/rancher-desktop")
        [api] = find(docs, "NetworkPolicy", "liveprobe-api", "liveprobe-system")
        egress = yaml.safe_dump(api["spec"]["egress"])
        for needle in ("app: prometheus", "app: loki", "k8s-app: hubble-relay", "port: 4245"):
            self.assertIn(needle, egress)
        [prom] = find(docs, "NetworkPolicy", "prometheus")
        self.assertIn("liveprobe-api", yaml.safe_dump(prom["spec"]["ingress"]))
        [loki] = find(docs, "NetworkPolicy", "loki")
        loki_from = yaml.safe_dump(loki["spec"]["ingress"])
        self.assertIn("app: alloy", loki_from)
        self.assertIn("app: liveprobe-api", loki_from)

    def test_prometheus_still_scrapes_envoy_istio_request_metrics(self):
        [cm] = find(render("overlays/rancher-desktop"), "ConfigMap", "prometheus-config")
        jobs = {j["job_name"] for j in yaml.safe_load(cm["data"]["prometheus.yml"])["scrape_configs"]}
        self.assertTrue({"envoy-stats", "kube-state-metrics", "kubernetes-pods"} <= jobs, jobs)
        self.assertIn("istio_requests_total", cm["data"]["prometheus.yml"])


if __name__ == "__main__":
    unittest.main()
