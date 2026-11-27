"""Kubernetes observation helpers; writes go through a separate rollback controller."""
from __future__ import annotations

import copy
import json
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from .endpoint_slices import SERVICE_LABEL, current_slice_addresses, endpoint_key, is_selector_service

log = logging.getLogger(__name__)

_api = None
ENDPOINT_SLICE_PAGE_SIZE = 3
ENDPOINT_SLICE_CLUSTER_PAGE_SIZE = 50


class Kube:
    """Thin wrapper around the official client so tests can substitute a fake."""

    def __init__(self, in_cluster: bool = True):
        from kubernetes import client, config
        if in_cluster:
            config.load_incluster_config()
        else:
            config.load_kube_config()
        self.client = client
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
        self.api_client = client.ApiClient()

    def to_dict(self, obj: Any) -> dict:
        return self.api_client.sanitize_for_serialization(obj)

    def served_api_versions(self) -> set[str]:
        discovery = self.client.ApisApi(self.api_client).get_api_versions(_request_timeout=4)
        return {version.group_version for group in discovery.groups for version in group.versions}

    @staticmethod
    def _list_document(load, *args, **kwargs) -> dict:
        """Parse the wire response once for read-only collectors that use Kubernetes fields directly."""
        response = load(*args, _preload_content=False, _request_timeout=8, **kwargs)
        try:
            body = response.read(cache_content=False)
        except Exception:
            response.close()
            raise
        finally:
            response.release_conn()
        document = json.loads(body)
        if not isinstance(document, dict) or not isinstance(document.get("items"), list):
            raise ValueError("Kubernetes list response is missing its items")
        return document

    def executor(self, token_file: str, ca_file: str = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
                 namespace: str | None = None, cfg=None):
        """Write client for the dedicated safe-action-executor identity, or None when its token is not mounted.

        The API's own service account stays read-only (and is denied writes by admission); only this projected,
        separately-bound token can patch the two target Deployments / delete controller-owned target Pods."""
        import os
        from .actions import SafeActionKube
        from .config import get_app_config
        app_cfg = cfg or get_app_config()
        if not os.path.isfile(token_file):
            return None
        cfg = self.client.Configuration()
        cfg.host = "https://%s:%s" % (os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc"),
                                      os.environ.get("KUBERNETES_SERVICE_PORT", "443"))
        cfg.ssl_ca_cert = ca_file
        cfg.api_key = {"authorization": "Bearer " + open(token_file).read().strip()}

        class _Tok:   # re-read per request so the projected token's rotation needs no restart
            pass
        api = self.client.ApiClient(cfg)
        return SafeActionKube(self, self.client.CoreV1Api(api), self.client.AppsV1Api(api), self.client.ApiException,
                              namespace=namespace or app_cfg.target_namespaces[0], cfg=app_cfg)

    # --- reads -------------------------------------------------------------
    def list_pods(self, ns: str, selector: dict[str, str] | None = None) -> list[dict]:
        ls = ",".join(f"{k}={v}" for k, v in (selector or {}).items()) or None
        return [self.to_dict(p) for p in self.core.list_namespaced_pod(ns, label_selector=ls, _request_timeout=8).items]

    def get_pod(self, ns: str, name: str) -> dict | None:
        try:
            return self.to_dict(self.core.read_namespaced_pod(name, ns))
        except self.client.ApiException as e:
            if e.status == 404:
                return None
            raise

    def get_deployment(self, ns: str, name: str) -> dict | None:
        try:
            return self.to_dict(self.apps.read_namespaced_deployment(name, ns, _request_timeout=8))
        except self.client.ApiException as e:
            if e.status == 404:
                return None
            raise

    def list_deployments(self, ns: str) -> list[dict]:
        return [self.to_dict(d) for d in self.apps.list_namespaced_deployment(ns, _request_timeout=8).items]

    def get_resource_metrics_api(self) -> dict | None:
        from .hpa_metrics import API_SERVICE

        registry = self.client.ApiregistrationV1Api(self.api_client)
        try:
            return self.to_dict(registry.read_api_service(API_SERVICE, _request_timeout=8))
        except self.client.ApiException as error:
            if error.status == 404:
                return None
            raise

    def list_autoscalers(self, ns: str) -> list[dict]:
        api = self.client.AutoscalingV2Api(self.api_client)
        return self._list_document(api.list_namespaced_horizontal_pod_autoscaler, ns)["items"]

    def get_replicaset(self, ns: str, name: str) -> dict | None:
        try:
            return self.to_dict(self.apps.read_namespaced_replica_set(name, ns, _request_timeout=8))
        except self.client.ApiException as e:
            if e.status == 404:
                return None
            raise

    def list_replicasets(self, ns: str) -> list[dict]:
        return [self.to_dict(r) for r in self.apps.list_namespaced_replica_set(ns, _request_timeout=8).items]

    def get_service(self, ns: str, name: str) -> dict | None:
        try:
            return self.to_dict(self.core.read_namespaced_service(name, ns, _request_timeout=8))
        except self.client.ApiException as e:
            if e.status == 404:
                return None
            raise

    def list_services(self, ns: str) -> list[dict]:
        return [self.to_dict(s) for s in self.core.list_namespaced_service(ns, _request_timeout=8).items]

    def get_endpoints(self, ns: str, name: str) -> dict | None:
        try:
            return self.to_dict(self.core.read_namespaced_endpoints(name, ns, _request_timeout=8))
        except self.client.ApiException as e:
            if e.status == 404:
                return None
            raise

    def list_endpoint_slices(self, ns: str | None, *, name: str | None = None) -> Iterator[dict] | None:
        discovery = self.client.DiscoveryV1Api(self.api_client)
        load = discovery.list_namespaced_endpoint_slice if ns is not None else discovery.list_endpoint_slice_for_all_namespaces
        scope = (ns,) if ns is not None else ()
        selector = f"{SERVICE_LABEL}={name}" if name is not None else SERVICE_LABEL
        page_size = ENDPOINT_SLICE_PAGE_SIZE if ns is not None else ENDPOINT_SLICE_CLUSTER_PAGE_SIZE

        def read_page(continuation):
            return self._list_document(load, *scope, label_selector=selector, limit=page_size, _continue=continuation)

        def items(page):
            version = page["metadata"].get("resourceVersion")
            seen_tokens = set()
            while True:
                metadata = page["metadata"]
                if metadata.get("resourceVersion") != version:
                    raise RuntimeError("EndpointSlice pagination changed resource version")
                yield from page["items"]
                continuation = metadata.get("continue")
                if not continuation:
                    return
                if continuation in seen_tokens:
                    raise RuntimeError("EndpointSlice pagination did not advance")
                seen_tokens.add(continuation)
                del page
                page = read_page(continuation)

        try:
            first = read_page(None)
        except self.client.ApiException as error:
            if error.status == 404:
                return None
            raise
        return items(first)

    def _endpoint_slice_counts(self, selected: list[dict], ns: str | None) -> dict[tuple[str, str], int] | None:
        name = selected[0]["metadata"]["name"] if ns is not None and len(selected) == 1 else None

        def snapshot():
            slices = self.list_endpoint_slices(ns, name=name)
            if slices is None:
                return None
            return {key: len(addresses) for key, addresses in current_slice_addresses(selected, slices).items()}

        try:
            return snapshot()
        except self.client.ApiException as error:
            if error.status != 410:
                raise
        # A replacement continuation from a 410 response can mix snapshots.
        fresh = snapshot()
        if fresh is None:
            raise RuntimeError("EndpointSlice API became unavailable during pagination retry")
        return fresh

    def service_endpoint_counts(self, services: list[dict], ns: str | None = None) -> dict[tuple[str, str], int]:
        target = [service for service in services if (service.get("spec") or {}).get("type") != "ExternalName"]
        selected = [service for service in target if is_selector_service(service)]
        endpoints = {}
        legacy = target
        slice_api_available = False
        if selected:
            counts = self._endpoint_slice_counts(selected, ns)
            if counts is not None:
                slice_api_available = True
                endpoints = counts
                legacy = [service for service in target if not is_selector_service(service)]
        if not legacy:
            return endpoints
        keys = {endpoint_key(service) for service in legacy}
        if slice_api_available or (ns is not None and len(keys) == 1):
            legacy_rows = [self.get_endpoints(*key) for key in sorted(keys)]
        else:
            load = self.core.list_namespaced_endpoints if ns is not None else self.core.list_endpoints_for_all_namespaces
            scope = (ns,) if ns is not None else ()
            legacy_rows = [self.to_dict(item) for item in load(*scope, _request_timeout=8).items]
        for data in legacy_rows:
            if data is None or (data["metadata"].get("annotations") or {}).get(
                "endpoints.kubernetes.io/over-capacity"
            ) == "truncated":
                continue
            key = endpoint_key(data)
            if key in keys:
                endpoints[key] = ready_addresses(data)
        return endpoints

    def list_events(self, ns: str) -> list[dict]:
        return [self.to_dict(e) for e in self.core.list_namespaced_event(ns, _request_timeout=8).items]

    def get_pvc(self, ns: str, name: str) -> dict | None:
        try:
            return self.to_dict(self.core.read_namespaced_persistent_volume_claim(name, ns, _request_timeout=8))
        except self.client.ApiException as e:
            if e.status == 404:
                return None
            raise

    def get_pv(self, name: str) -> dict | None:
        try:
            return self.to_dict(self.core.read_persistent_volume(name, _request_timeout=8))
        except self.client.ApiException as e:
            if e.status == 404:
                return None
            raise

    def get_node(self, name: str) -> dict | None:
        try:
            return self.to_dict(self.core.read_node(name, _request_timeout=8))
        except self.client.ApiException as e:
            if e.status == 404:
                return None
            raise

    def configmap_meta(self, ns: str, name: str) -> dict | None:
        """Read one referenced ConfigMap; values and annotations never cross this boundary."""
        try:
            cm = self.to_dict(self.core.read_namespaced_config_map(name, ns, _request_timeout=8))
        except self.client.ApiException as e:
            if e.status == 404:
                return None
            raise
        return configmap_fingerprint(cm)

    def get_namespace_uid(self, name: str) -> str | None:
        """Cluster identity probe: the UID of a namespace that exists in every cluster (read-only)."""
        try:
            return (self.to_dict(self.core.read_namespace(name)).get("metadata") or {}).get("uid")
        except self.client.ApiException as e:
            if e.status == 404:
                return None
            raise

    # --- cluster-wide, read-only inventory for the topology map (P3) --------
    def cluster_inventory(self) -> dict:
        """Nodes, pods, services and endpoints of every namespace. Read-only; fields are trimmed to what the map needs."""
        def node_view(n: dict) -> dict:
            md, sp, st = n.get("metadata") or {}, n.get("spec") or {}, n.get("status") or {}
            return {"metadata": {"name": md.get("name")},
                    "spec": {"podCIDRs": sp.get("podCIDRs"), "podCIDR": sp.get("podCIDR")},
                    "status": {"capacity": st.get("capacity"), "addresses": st.get("addresses"),
                               "conditions": st.get("conditions")}}

        def pod_view(p: dict) -> dict:
            md, st = p.get("metadata") or {}, p.get("status") or {}
            return {"metadata": {"namespace": md.get("namespace"), "name": md.get("name"), "labels": md.get("labels") or {},
                                 "ownerReferences": md.get("ownerReferences") or []},
                    "spec": {"nodeName": (p.get("spec") or {}).get("nodeName"),
                             "images": [c.get("image") for c in (p.get("spec") or {}).get("containers") or []]},
                    "status": {"phase": st.get("phase"), "podIP": st.get("podIP"),
                               "containerStatuses": [{"ready": c.get("ready")} for c in st.get("containerStatuses") or []]}}
        def svc_view(s: dict) -> dict:
            md, sp = s.get("metadata") or {}, s.get("spec") or {}
            return {"metadata": {"namespace": md.get("namespace"), "name": md.get("name"), "labels": md.get("labels") or {}},
                    "spec": {"type": sp.get("type"), "clusterIP": sp.get("clusterIP"), "selector": sp.get("selector") or {},
                             "ports": [{"port": x.get("port")} for x in sp.get("ports") or []]}}
        def ing_view(i: dict) -> dict:
            md, sp = i.get("metadata") or {}, i.get("spec") or {}
            rules = []
            for r in sp.get("rules") or []:
                for pth in ((r.get("http") or {}).get("paths") or []):
                    svc = ((pth.get("backend") or {}).get("service") or {})
                    rules.append({"host": r.get("host"), "path": pth.get("path") or "/", "service": svc.get("name"),
                                  "port": (svc.get("port") or {}).get("number") or (svc.get("port") or {}).get("name")})
            dflt = ((sp.get("default_backend") or sp.get("defaultBackend") or {}).get("service") or {})
            if dflt.get("name"):
                rules.append({"host": None, "path": "/", "service": dflt["name"], "port": (dflt.get("port") or {}).get("number")})
            return {"metadata": {"namespace": md.get("namespace"), "name": md.get("name"), "labels": md.get("labels") or {}},
                    "spec": {"ingressClassName": sp.get("ingress_class_name") or sp.get("ingressClassName"), "rules": rules}}
        def dep_view(d: dict) -> dict:
            md, sp, st = d.get("metadata") or {}, d.get("spec") or {}, d.get("status") or {}
            return {"metadata": {"namespace": md.get("namespace"), "name": md.get("name"), "labels": md.get("labels") or {}},
                    "spec": {"replicas": sp.get("replicas")},
                    "pod_labels": ((sp.get("template") or {}).get("metadata") or {}).get("labels") or {},
                    "status": {"readyReplicas": st.get("readyReplicas", 0)}}
        reads = (
            ("ingresses", self.client.NetworkingV1Api(self.api_client).list_ingress_for_all_namespaces, ing_view, False),
            ("deployments", self.apps.list_deployment_for_all_namespaces, dep_view, False),
            ("namespaces", self.core.list_namespace,
             lambda n: {"metadata": {"name": (n.get("metadata") or {}).get("name")}}, False),
            ("services", self.core.list_service_for_all_namespaces, svc_view, True),
            ("nodes", self.core.list_node, node_view, True),
            ("pods", self.core.list_pod_for_all_namespaces, pod_view, True),
        )

        def read(task):
            key, load, trim, required = task
            try:
                rows = self._list_document(load)["items"]
                section = {key: [trim(item) for item in rows]}
            except Exception as e:  # noqa: BLE001 - retain partial inventory with explicit source errors
                if required:
                    raise
                return {key: []}, f"{key}: {type(e).__name__}"
            if key != "services":
                return section, None
            try:
                endpoints = self.service_endpoint_counts(rows)
            except Exception as e:  # noqa: BLE001 - service status remains unobserved if discovery fails
                return {**section, "endpoints": []}, f"endpoints: {type(e).__name__}"
            section["endpoints"] = [{"metadata": {"namespace": ns, "name": name}, "ready_endpoints": count}
                                    for (ns, name), count in endpoints.items()]
            return section, None

        inventory: dict = {"errors": []}
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="overview-kube") as pool:
            for section, error in pool.map(read, reads):
                inventory.update(section)
                if error:
                    inventory["errors"].append(error)
        return inventory

    def cluster_warning_events(self, *, limit: int = 500) -> list[dict]:
        """Bounded raw Warning events for the operations overview; no pod logs or Secret reads."""
        return self._list_document(self.core.list_event_for_all_namespaces,
                                   field_selector="type=Warning", limit=limit)["items"]

    # --- bounded request to independent, admission-guarded controller --------
    def request_rollback(self, ns: str, name: str, *, grant: dict | None = None) -> dict:
        import os
        from pathlib import Path
        import httpx
        from .config import get_app_config
        cfg = get_app_config()
        if ns not in cfg.target_namespaces:
            raise ValueError("rollback target not allowlisted")
        url = os.environ.get("ROLLBACK_CONTROLLER_URL")
        if url != "https://rollback-controller.liveprobe-system.svc:443/undo":
            raise RuntimeError("rollback controller is not configured")
        if grant is None or grant.get("name") != name or grant.get("namespace") != ns:
            raise RuntimeError("approved action grant required")
        try:
            token = Path("/var/run/secrets/rollback-caller/token").read_text().strip()
            with httpx.Client(verify="/var/run/rollback-ca/ca.crt", timeout=10) as client:
                response = client.post(url, headers={"Authorization": "Bearer " + token}, json=grant)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError, OSError) as exc:
            raise RuntimeError("rollback controller rejected or unavailable") from None


# ---------------------------------------------------------------------------
# pure helpers (unit-tested)
# ---------------------------------------------------------------------------

def parse_ts(v: Any) -> datetime | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def owner_of(obj: dict, kind: str) -> str | None:
    for o in (obj.get("metadata") or {}).get("ownerReferences") or []:
        if o.get("kind") == kind:
            return o.get("name")
    return None


def configmap_fingerprint(cm: dict) -> dict:
    """Value-free change metadata; data hash matches the approved-version contract."""
    import hashlib
    import json
    md, data = cm.get("metadata") or {}, cm.get("data") or {}
    times = [f["time"] for f in md.get("managedFields") or [] if f.get("time")]
    return {
        "name": md.get("name"), "keys": sorted(data), "binary_keys": sorted(cm.get("binaryData") or {}),
        "data_sha256": hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "content_sha256": hashlib.sha256(json.dumps({"data": data, "binaryData": cm.get("binaryData") or {}},
                                                    sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "created": md.get("creationTimestamp"), "last_changed": max(times) if times else md.get("creationTimestamp"),
        "resource_version": md.get("resourceVersion"),
    }


def configmap_refs(tpl: dict) -> list[str]:
    """Only actual ConfigMap references, including init containers and projected volumes."""
    spec = tpl.get("spec") or {}
    names = set()
    for c in [*(spec.get("containers") or []), *(spec.get("initContainers") or [])]:
        for env in c.get("envFrom") or []:
            if (env.get("configMapRef") or {}).get("name"):
                names.add(env["configMapRef"]["name"])
        for env in c.get("env") or []:
            ref = ((env.get("valueFrom") or {}).get("configMapKeyRef") or {}).get("name")
            if ref:
                names.add(ref)
    for vol in spec.get("volumes") or []:
        if (vol.get("configMap") or {}).get("name"):
            names.add(vol["configMap"]["name"])
        for src in (vol.get("projected") or {}).get("sources") or []:
            if (src.get("configMap") or {}).get("name"):
                names.add(src["configMap"]["name"])
    return sorted(names)


def node_summary(node: dict) -> dict:
    """Only scheduling/pressure evidence, never node annotations, addresses or messages."""
    st, spec = node.get("status") or {}, node.get("spec") or {}
    keep = {"Ready", "MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable"}
    return {
        "name": (node.get("metadata") or {}).get("name"),
        "conditions": {c["type"]: {"status": c.get("status"), "reason": c.get("reason"),
                                    "since": c.get("lastTransitionTime")}
                       for c in st.get("conditions") or [] if c.get("type") in keep},
        "allocatable": {k: (st.get("allocatable") or {}).get(k) for k in ("cpu", "memory", "pods")},
        "taints": [{k: t[k] for k in ("key", "value", "effect") if k in t} for t in spec.get("taints") or []],
    }


def revision_of(obj: dict) -> int:
    ann = (obj.get("metadata") or {}).get("annotations") or {}
    try:
        return int(ann.get("deployment.kubernetes.io/revision", "0"))
    except ValueError:
        return 0


def strip_template(tpl: dict) -> dict:
    """Pod template without pod-template-hash and with env *values* removed (for storage/compare)."""
    t = copy.deepcopy(tpl or {})
    labels = (t.get("metadata") or {}).get("labels") or {}
    labels.pop("pod-template-hash", None)
    t.get("metadata", {}).pop("creationTimestamp", None)
    return t


def sanitize_template(tpl: dict) -> dict:
    t = strip_template(tpl)
    for c in ((t.get("spec") or {}).get("containers") or []) + ((t.get("spec") or {}).get("initContainers") or []):
        for e in c.get("env") or []:
            if "value" in e:
                e["value"] = "[dropped]"
    return t


def container_images(tpl: dict) -> list[str]:
    return [c.get("image", "") for c in ((tpl or {}).get("spec") or {}).get("containers") or []]


def pod_waiting_reasons(pod: dict) -> list[str]:
    out = []
    for cs in ((pod.get("status") or {}).get("containerStatuses") or []) + \
              ((pod.get("status") or {}).get("initContainerStatuses") or []):
        w = (cs.get("state") or {}).get("waiting")
        if w and w.get("reason"):
            out.append(w["reason"])
    return out


def pod_summary(pod: dict) -> dict:
    st = pod.get("status") or {}
    cs_out = []
    spec_images = {c.get("name"): c.get("image") for c in (pod.get("spec") or {}).get("containers") or []}
    for cs in st.get("containerStatuses") or []:
        state = cs.get("state") or {}
        last = (cs.get("lastState") or {}).get("terminated") or {}
        spec_image = spec_images.get(cs.get("name"))
        image_id = cs.get("imageID")
        # Runtime status.image can be a local tag alias even for a digest-pinned
        # Pod. Only an exact repository+digest imageID match proves provenance;
        # absent or differently formatted IDs remain unverified, not safe.
        pinned = spec_image if spec_image and "@sha256:" in spec_image else None
        resolved = image_id.removeprefix("docker-pullable://") if image_id else None
        provenance = ("digest_matches_spec" if pinned and resolved == pinned else
                      "digest_mismatch" if pinned and resolved and "@sha256:" in resolved else
                      "unverified")
        cs_out.append({
            "name": cs.get("name"), "spec_image": spec_image, "image": cs.get("image"),
            "image_id": image_id, "image_provenance": provenance, "ready": cs.get("ready"),
            "restart_count": cs.get("restartCount"),
            "waiting_reason": (state.get("waiting") or {}).get("reason"),
            "waiting_message": ((state.get("waiting") or {}).get("message") or "")[:300] or None,
            "last_terminated_reason": last.get("reason"),
            "last_exit_code": last.get("exitCode"),
            "last_terminated_at": last.get("finishedAt"),
        })
    md = pod.get("metadata") or {}
    return {
        "name": md.get("name"), "uid": md.get("uid"), "labels": md.get("labels") or {}, "phase": st.get("phase"),
        "node": (pod.get("spec") or {}).get("nodeName"),
        "replicaset": owner_of(pod, "ReplicaSet"), "terminating": bool(md.get("deletionTimestamp")),
        "containers": cs_out,
    }


def labels_match(selector: dict[str, str], labels: dict[str, str]) -> bool:
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def ready_addresses(ep: dict | int | None) -> int:
    if isinstance(ep, int):
        return ep
    if not ep:
        return 0
    if "ready_endpoints" in ep:
        return ep["ready_endpoints"]
    return sum(len(s.get("addresses") or []) for s in ep.get("subsets") or [])


def deployment_for_pod(kube: Kube, ns: str, pod: dict) -> str | None:
    rs_name = owner_of(pod, "ReplicaSet")
    if not rs_name:
        return None
    rs = kube.get_replicaset(ns, rs_name)
    return owner_of(rs, "Deployment") if rs else None
