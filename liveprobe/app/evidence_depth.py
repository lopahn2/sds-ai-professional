"""Deeper read-only evidence: effective Pod resources, namespace defaults, probes, dependencies and Pod log tails.

The base bundle sees the incident workload only. Compound failures put the decisive fact one hop away:
an admission default on the running Pod that the Deployment template does not show, the backing Pods of a
Service the workload depends on, or the container that the kubelet replaced. Every read here is bounded,
read-only and optional; a failure becomes an item error, never a missing source.

Secret values are never read. Env and ConfigMap values are scanned for in-namespace Service names only and
are never returned. Probe exec commands are not echoed because their arguments can carry credentials.
"""
from __future__ import annotations

import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

LOG_ERROR = re.compile(r"(?i)(error|exception|fail|refused|unavailable|timeout|timed out|fatal|denied|reject)")
LEADING_TS = re.compile(r"^\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\]?\s+")
LOG_LINE_CHARS = 400
POD_LOG_TAIL = 400
POD_LOG_KEEP = 10
MAX_DEPENDENCIES = 3
MAX_DEPENDENCY_PODS = 3
DEPENDENCY_LOG_KEEP = 6
SATURATED_UTILIZATION = 0.8
THROTTLED_RATIO = 0.2
RESTART_WINDOW_S = 900
NAME = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")


def _remaining(deadline: float | None) -> float:
    return math.inf if deadline is None else deadline - time.monotonic()


def _check(deadline: float | None) -> None:
    if _remaining(deadline) <= 0:
        raise TimeoutError("evidence depth deadline exceeded")


def _raw(kube):
    """The worker proxy wraps methods only; client attributes live on the wrapped Kube."""
    return getattr(kube, "kube", kube)


# ------------------------------------------------------------------ Pod and template views (pure)
def cpu_cores(quantity: Any) -> float | None:
    if quantity in (None, ""):
        return None
    text = str(quantity).strip()
    try:
        if text.endswith("m"):
            return float(text[:-1]) / 1000
        if text.endswith("n"):
            return float(text[:-1]) / 1e9
        if text.endswith("u"):
            return float(text[:-1]) / 1e6
        return float(text)
    except ValueError:
        return None


def pod_runtime(pod: dict) -> dict:
    """The resources the running Pod actually received. Admission can add limits the template never had."""
    spec, md = pod.get("spec") or {}, pod.get("metadata") or {}
    resources = {}
    for c in spec.get("containers") or []:
        r = c.get("resources") or {}
        resources[c.get("name")] = {"limits": dict(r.get("limits") or {}), "requests": dict(r.get("requests") or {})}
    out: dict[str, Any] = {"resources": resources}
    note = (md.get("annotations") or {}).get("kubernetes.io/limit-ranger")
    if isinstance(note, str) and note.strip():
        out["admission_defaults"] = note.strip()[:300]
    return out


def probe_view(template: dict) -> dict:
    out = {}
    for c in (template.get("spec") or {}).get("containers") or []:
        view = {}
        for kind in ("livenessProbe", "readinessProbe", "startupProbe"):
            p = c.get(kind)
            if not isinstance(p, dict):
                continue
            entry = {k: p[k] for k in ("initialDelaySeconds", "periodSeconds", "timeoutSeconds", "failureThreshold")
                     if p.get(k) is not None}
            if isinstance(p.get("httpGet"), dict):
                entry["http_get"] = {"path": str(p["httpGet"].get("path") or "/")[:200], "port": p["httpGet"].get("port")}
            elif isinstance(p.get("tcpSocket"), dict):
                entry["tcp_socket"] = {"port": p["tcpSocket"].get("port")}
            elif isinstance(p.get("grpc"), dict):
                entry["grpc"] = {"port": p["grpc"].get("port")}
            elif p.get("exec"):
                entry["exec"] = "command not shown"
            view[kind[:-len("Probe")]] = entry
        if view:
            out[c.get("name")] = view
    return out


# ------------------------------------------------------------------ namespace defaults
def limit_ranges(kube, ns: str, deadline: float | None = None) -> list[dict]:
    _check(deadline)
    raw = _raw(kube)
    rows = raw.core.list_namespaced_limit_range(ns, _request_timeout=min(8, max(1, _remaining(deadline))))
    out = []
    for item in list(rows.items)[:5]:
        d = raw.to_dict(item)
        md = d.get("metadata") or {}
        limits = [{k: lim[k] for k in ("type", "default", "defaultRequest", "max", "min") if lim.get(k)}
                  for lim in (d.get("spec") or {}).get("limits") or []][:5]
        out.append({"name": md.get("name"), "created": md.get("creationTimestamp"), "limits": limits})
    return out


# ------------------------------------------------------------------ log tails
def _strip(line: str) -> str:
    line = line.rstrip()
    first = line.split(" ", 1)
    if len(first) == 2 and re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", first[0]):
        line = first[1]  # kubelet --timestamps prefix
    return LEADING_TS.sub("", line, count=1)


def error_lines(text: str, label: str, keep: int) -> list[str]:
    """Newest-first error lines, grouped by body; repetitions are counted, not repeated."""
    grouped: dict[str, int] = {}
    order: list[str] = []
    for line in reversed((text or "").splitlines()):
        body = _strip(line)
        if not body or not LOG_ERROR.search(body):
            continue
        if body not in grouped:
            order.append(body)
            grouped[body] = 0
        grouped[body] += 1
    return [f"[{label}] {body[:LOG_LINE_CHARS]}" + (f" (repeated {grouped[body]} times)" if grouped[body] > 1 else "")
            for body in order[:keep]]


def _pod_log(kube, ns: str, pod: str, deadline: float | None, *, previous: bool = False,
             since_seconds: int | None = None, tail: int = POD_LOG_TAIL) -> str:
    _check(deadline)
    kwargs: dict[str, Any] = {"tail_lines": tail, "previous": previous,
                              "_request_timeout": min(6, max(1, _remaining(deadline)))}
    if since_seconds and not previous:
        kwargs["since_seconds"] = int(since_seconds)
    return _raw(kube).core.read_namespaced_pod_log(pod, ns, **kwargs) or ""


def pod_error_tail(kube, ns: str, pods: list[dict], app: str, since: datetime | None,
                   deadline: float | None, keep: int = POD_LOG_KEEP) -> list[str]:
    """Kubelet log tails for the workload's Pods, including the container the kubelet replaced."""
    since_s = None
    if since is not None:
        since_s = max(60, int((datetime.now(timezone.utc) - since).total_seconds()))
    lines: list[str] = []
    for p in pods[:MAX_DEPENDENCY_PODS]:
        name = p.get("name")
        if not name:
            continue
        lines += error_lines(_pod_log(kube, ns, name, deadline, since_seconds=since_s), app, keep)
        restarted = any((c.get("restart_count") or 0) > 0 for c in p.get("containers") or [])
        if restarted:
            try:
                lines += error_lines(_pod_log(kube, ns, name, deadline, previous=True, tail=200),
                                     f"{app} previous container", max(3, keep // 2))
            except TimeoutError:
                raise
            except Exception:  # noqa: BLE001 - no previous container retained
                pass
    return lines[:keep * 2]


def loki_pod_errors(settings, ns: str, pods: list[str], since: datetime | None, deadline: float | None,
                    keep: int = DEPENDENCY_LOG_KEEP) -> dict[str, list[str]]:
    """Error lines of named Pods from the log store LiveProbe already reads (no kubelet access needed)."""
    import httpx
    from .observation_sources import adapter_url

    names = [p for p in pods if NAME.fullmatch(p or "")]
    if not names or settings is None:
        return {}
    _check(deadline)
    end = datetime.now(timezone.utc)
    start = max(since, end - timedelta(minutes=10)) if since else end - timedelta(minutes=10)
    # Names are DNS labels ([a-z0-9-]); a hyphen is literal in RE2, and a backslash would be an invalid LogQL escape.
    query = (f'{{namespace="{ns}", pod=~"{"|".join(names)}"}}'
             ' |~ "(?i)(error|exception|fail|fatal|denied|reject|refused|timeout)"')
    r = httpx.get(f"{adapter_url(settings, 'loki')}/loki/api/v1/query_range", timeout=min(8, max(1, _remaining(deadline))),
                  params={"query": query, "limit": 200, "direction": "backward",
                          "start": str(int(start.timestamp() * 1e9)), "end": str(int(end.timestamp() * 1e9))})
    r.raise_for_status()
    text: dict[str, list[tuple[int, str]]] = {}
    for stream in r.json()["data"]["result"]:
        pod = stream["stream"].get("pod")
        if pod in names:
            text.setdefault(pod, []).extend((int(ts), line) for ts, line in stream["values"])
    return {pod: error_lines("\n".join(line for _, line in sorted(rows)), pod, keep) for pod, rows in text.items()}


# ------------------------------------------------------------------ dependency traversal
def _hosts(value: str) -> set[str]:
    out = set()
    for token in re.split(r"[^a-z0-9.-]+", (value or "").lower()):
        token = token.strip(".-")
        for suffix in (".svc.cluster.local", ".svc"):
            if token.endswith(suffix):
                token = token[: -len(suffix)]
        if token:
            out.add(token)
    return out


def dependency_names(kube, ns: str, template: dict, services: set[str], own: str,
                     deadline: float | None = None, static: list[str] | tuple[str, ...] = (),
                     cache: dict | None = None) -> list[str]:
    """In-namespace Services named by the workload's env or ConfigMaps. Values are scanned, never returned."""
    values: list[str] = []
    cm_keys: dict[str, set[str] | None] = {}
    for c in (template.get("spec") or {}).get("containers") or []:
        for env in c.get("env") or []:
            if isinstance(env.get("value"), str):
                values.append(env["value"])
            ref = ((env.get("valueFrom") or {}).get("configMapKeyRef") or {})
            if ref.get("name") and ref.get("key"):
                keys = cm_keys.setdefault(ref["name"], set())
                if keys is not None:
                    keys.add(ref["key"])
        for src in c.get("envFrom") or []:
            name = (src.get("configMapRef") or {}).get("name")
            if name:
                cm_keys[name] = None  # every key
    raw = _raw(kube)
    cache = {} if cache is None else cache
    for name, keys in list(cm_keys.items())[:4]:
        if name not in cache:
            try:
                _check(deadline)
                cm = raw.to_dict(raw.core.read_namespaced_config_map(
                    name, ns, _request_timeout=min(6, max(1, _remaining(deadline)))))
                cache[name] = cm.get("data") or {}
            except TimeoutError:
                break
            except Exception:  # noqa: BLE001 - optional read
                cache[name] = {}
        data = cache[name]
        values += [str(v) for k, v in data.items() if keys is None or k in keys]
    found: list[str] = []
    for value in values:
        for host in _hosts(value):
            name, _, rest = host.partition(".")
            if rest and rest != ns:
                continue
            if name != own and name in services and name not in found:
                found.append(name)
    for name in static:
        if name != own and name in services and name not in found:
            found.append(name)
    return found[:MAX_DEPENDENCIES]


def _endpoint_rows(ep: dict | None) -> list[dict]:
    rows = []
    for subset in (ep or {}).get("subsets") or []:
        for ready, key in ((True, "addresses"), (False, "notReadyAddresses")):
            for a in subset.get(key) or []:
                ref = a.get("targetRef") or {}
                rows.append({"pod": ref.get("name"), "ready": ready, "kind": ref.get("kind")})
    return rows


def _owner(pod: dict) -> str | None:
    refs = [r for r in (pod.get("metadata") or {}).get("ownerReferences") or [] if r.get("controller")]
    return f"{refs[0].get('kind')}/{refs[0].get('name')}" if refs else None


def dependency_view(kube, ns: str, name: str, svc: dict, all_pods: list[dict], since: datetime | None,
                    deadline: float | None, settings=None) -> dict:
    selector = (svc.get("spec") or {}).get("selector") or {}
    pods = {p["metadata"]["name"]: p for p in all_pods if p.get("metadata", {}).get("name")}
    matching = sorted(n for n, p in pods.items()
                      if selector and all((p["metadata"].get("labels") or {}).get(k) == v for k, v in selector.items()))
    view: dict[str, Any] = {"service": name, "selector": selector, "selector_matches": matching,
                            "ports": [{"port": p.get("port"), "target": p.get("targetPort")}
                                      for p in (svc.get("spec") or {}).get("ports") or []][:4]}
    try:
        _check(deadline)
        rows = _endpoint_rows(_raw(kube).get_endpoints(ns, name))
        view["endpoints"] = rows
        view["ready_endpoints"] = sum(1 for r in rows if r["ready"])
    except TimeoutError:
        raise
    except Exception as exc:  # noqa: BLE001
        view["endpoints_error"] = type(exc).__name__
    backing = [r["pod"] for r in view.get("endpoints") or [] if r.get("pod")] or matching
    # Pods of the same controller that the Service does not select. After a failover or relabel, clients with
    # long-lived connections can keep talking to one of them; their logs show it.
    owners = {_owner(pods.get(n) or {}) for n in backing} - {None}
    siblings = sorted(n for n, p in pods.items() if _owner(p) in owners and n not in backing)[:MAX_DEPENDENCY_PODS]
    view["pods"] = []
    since_s = max(60, int((datetime.now(timezone.utc) - since).total_seconds())) if since else 600
    chosen = list(dict.fromkeys(backing))[:MAX_DEPENDENCY_PODS]
    selected = set(chosen)
    chosen += siblings
    stored: dict[str, list[str]] | None = None
    if settings is not None:
        try:
            stored = loki_pod_errors(settings, ns, chosen, since, deadline)
            view["log_source"] = "loki"
        except TimeoutError:
            raise
        except Exception as exc:  # noqa: BLE001 - the kubelet tail below is the fallback
            view["loki_error"] = type(exc).__name__
    for pod_name in chosen:
        p = pods.get(pod_name) or {}
        md, st = p.get("metadata") or {}, p.get("status") or {}
        row: dict[str, Any] = {"name": pod_name, "selected_by_service": pod_name in selected,
                               "labels": md.get("labels") or {}, "phase": st.get("phase"),
                               "ready": all(cs.get("ready") for cs in st.get("containerStatuses") or []) or None,
                               "restarts": sum(cs.get("restartCount") or 0 for cs in st.get("containerStatuses") or [])}
        if stored is not None:
            row["error_logs"] = stored.get(pod_name, [])
            view["pods"].append(row)
            continue
        try:
            row["error_logs"] = error_lines(_pod_log(kube, ns, pod_name, deadline, since_seconds=since_s),
                                            pod_name, DEPENDENCY_LOG_KEEP)
        except TimeoutError:
            row["error_logs_error"] = "deadline"
        except Exception as exc:  # noqa: BLE001
            row["error_logs_error"] = type(exc).__name__
        view["pods"].append(row)
    return view


def caller_names(kube, ns: str, workload: str, own_services: set[str], deadline: float | None,
                 cache: dict | None = None) -> list[str]:
    """Deployments whose env or ConfigMaps name this workload's Service: the callers that drive its load."""
    out = []
    for d in _raw(kube).list_deployments(ns):
        name = (d.get("metadata") or {}).get("name")
        if not name or name == workload:
            continue
        template = (d.get("spec") or {}).get("template") or {}
        if dependency_names(kube, ns, template, own_services, name, deadline, cache=cache):
            out.append(name)
        if len(out) >= MAX_DEPENDENCIES:
            break
    return out


def query_request_rates(settings, ns: str, apps: list[str], deadline: float | None) -> dict:
    import httpx
    from .observation_sources import adapter_url

    apps = [a for a in apps if NAME.fullmatch(a or "")]
    if not apps or not NAME.fullmatch(ns or ""):
        return {}
    regex = "|".join(apps)
    out: dict = {}
    for key, extra in (("requests_per_s", ""), ("errors_5xx_per_s", ',status=~"5.."')):
        _check(deadline)
        q = f'sum by (app) (rate(http_server_requests_seconds_count{{namespace="{ns}",app=~"{regex}"{extra}}}[1m]))'
        r = httpx.get(f"{adapter_url(settings, 'prometheus')}/api/v1/query", params={"query": q},
                      timeout=min(8, max(1, _remaining(deadline))))
        r.raise_for_status()
        for row in r.json()["data"]["result"]:
            value = float(row["value"][1])
            if math.isfinite(value):
                out.setdefault(row["metric"].get("app"), {})[key] = round(value, 3)
    return out


# ------------------------------------------------------------------ deterministic findings (pure)
def _pod_cpu_limit(resources: dict) -> float | None:
    if not resources:
        return None
    total = 0.0
    for r in resources.values():
        v = cpu_cores((r.get("limits") or {}).get("cpu"))
        if v is None:
            return None  # one unbounded container leaves the Pod unbounded
        total += v
    return total


def resource_findings(pods: list[dict], metrics: dict, deployment: dict | None,
                      ranges: list[dict] | None) -> list[dict]:
    """CPU limits that bind the running Pods, and limits that came from namespace admission defaults."""
    usage = (metrics or {}).get("pod_cpu_usage_cores") or {}
    throttled = (metrics or {}).get("pod_cpu_throttled_ratio") or {}
    template = (deployment or {}).get("resources") or {}
    out, defaulted = [], False
    for p in pods or []:
        resources = p.get("resources") or {}
        limit = _pod_cpu_limit(resources)
        if limit is None or limit <= 0:
            continue
        name = p.get("name")
        template_cpu = {c: cpu_cores((template.get(c) or {}).get("limits", {}).get("cpu")) for c in resources}
        from_admission = all(v is None for v in template_cpu.values())
        u, t = usage.get(name), throttled.get(name)
        utilization = round(u / limit, 2) if isinstance(u, (int, float)) else None
        saturated = ((utilization is not None and utilization >= SATURATED_UTILIZATION)
                     or (isinstance(t, (int, float)) and t >= THROTTLED_RATIO))
        if not saturated and not from_admission:
            continue
        entry: dict[str, Any] = {"kind": "cpu_limit_binding" if saturated else "cpu_limit_from_admission",
                                 "pod": name, "cpu_limit_cores": round(limit, 3),
                                 "cpu_usage_cores": u, "utilization_of_limit": utilization,
                                 "throttled_period_ratio": t,
                                 "limit_in_deployment_template": not from_admission}
        if p.get("admission_defaults"):
            entry["admission_defaults"] = p["admission_defaults"]
        defaulted |= from_admission
        out.append(entry)
    if defaulted and ranges:
        out.append({"kind": "namespace_default_limits", "limit_ranges": ranges})
    return out


def _liveness_kills(events: list[dict], pod: str, now: datetime) -> int:
    total = 0
    for e in events or []:
        message = str(e.get("message") or "").lower()
        if not str(e.get("object") or "").endswith("/" + str(pod)):
            continue
        if not ((e.get("reason") == "Unhealthy" and "liveness probe failed" in message)
                or (e.get("reason") == "Killing" and "failed liveness probe" in message)):
            continue
        stamp = _ts(e.get("last"))
        if stamp is not None and (now - stamp).total_seconds() <= RESTART_WINDOW_S:
            total += int(e.get("count") or 1)
    return total


def restart_findings(pods: list[dict], events: list[dict], probes: dict | None,
                     now: datetime | None = None) -> list[dict]:
    """Recent kubelet restarts, with the liveness failures that drove them."""
    now = now or datetime.now(timezone.utc)
    out = []
    for p in pods or []:
        for c in p.get("containers") or []:
            restarts = c.get("restart_count")
            if type(restarts) is not int or restarts < 1:
                continue
            ended = _ts(c.get("last_terminated_at"))
            if ended is None or (now - ended).total_seconds() > RESTART_WINDOW_S:
                continue
            out.append({"kind": "recent_restart", "pod": p.get("name"), "container": c.get("name"),
                        "restart_count": restarts, "last_terminated_reason": c.get("last_terminated_reason"),
                        "last_exit_code": c.get("last_exit_code"), "last_terminated_at": c.get("last_terminated_at"),
                        "liveness_failures": _liveness_kills(events, p.get("name"), now),
                        "liveness_probe": ((probes or {}).get(c.get("name")) or {}).get("liveness")})
    return out


def probe_restart_masking(pods: list[dict], events: list[dict], now: datetime | None = None) -> list[dict]:
    """Restarts the kubelet made because liveness failed, inside the window. The fresh container hides the
    condition that failed the probe, so a clean reading right after one is not evidence of recovery."""
    return [f for f in restart_findings(pods, events, None, now) if f["liveness_failures"] > 0]


AMPLIFIED = 1.5


def retry_findings(caller_load: dict | None) -> list[dict]:
    """Requests arriving at the workload per request its callers received: above 1, callers repeat calls."""
    if not caller_load:
        return []
    workload, callers, rates = caller_load.get("workload"), caller_load.get("callers") or [], caller_load.get("rates") or {}
    inbound = (rates.get(workload) or {}).get("requests_per_s")
    upstream = sum((rates.get(c) or {}).get("requests_per_s") or 0 for c in callers)
    if not inbound or not upstream:
        return []
    ratio = round(inbound / upstream, 2)
    if ratio < AMPLIFIED:
        return []
    return [{"kind": "caller_retry_amplification", "workload": workload, "callers": callers,
             "workload_requests_per_s": inbound, "caller_requests_per_s": round(upstream, 3),
             "requests_per_caller_request": ratio,
             "workload_5xx_per_s": (rates.get(workload) or {}).get("errors_5xx_per_s"),
             "caller_5xx_per_s": {c: (rates.get(c) or {}).get("errors_5xx_per_s") for c in callers}}]


def pool_findings(metrics: dict, trend: dict | None, workload: str) -> list[dict]:
    active = ((metrics or {}).get("db_pool_active") or {}).get(workload)
    limit = ((metrics or {}).get("db_pool_max") or {}).get(workload)
    pending = ((metrics or {}).get("db_pool_pending_threads") or {}).get(workload)
    if active is None or limit is None:
        return []
    entry = {"kind": "db_pool_state", "active": active, "max": limit, "pending": pending}
    if trend:
        entry["active_trend"] = trend
    if active >= limit or (trend and trend.get("never_released")):
        return [entry]
    return [entry] if trend and trend.get("rises") else []


def pool_trend(samples: list[tuple[float, float]]) -> dict | None:
    """Shape of Hikari active connections over the window: does it only climb between drops?"""
    pts = [(t, v) for t, v in samples if math.isfinite(v)]
    if len(pts) < 4:
        return None
    values = [v for _, v in pts]
    rises = sum(1 for a, b in zip(values, values[1:]) if b > a)
    falls = sum(1 for a, b in zip(values, values[1:]) if b < a)
    drops_to_low = sum(1 for a, b in zip(values, values[1:]) if b < a and b <= min(values) + 0.5)
    return {"window_minutes": round((pts[-1][0] - pts[0][0]) / 60, 1), "first": values[0], "last": values[-1],
            "min": min(values), "max": max(values), "step_rises": rises, "step_falls": falls,
            "resets_to_minimum": drops_to_low, "rises": rises > 0,
            "never_released": falls == 0 and values[-1] > values[0]}


def _ts(value) -> datetime | None:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------ collection worker
POOL_TREND_MINUTES = 15


def query_pool_trend(settings, ns: str, workload: str, deadline: float | None) -> dict | None:
    import httpx
    from .observation_sources import adapter_url

    if not all(NAME.fullmatch(v or "") for v in (ns, workload)):
        return None
    _check(deadline)
    end = time.time()
    r = httpx.get(f"{adapter_url(settings, 'prometheus')}/api/v1/query_range", timeout=min(8, max(1, _remaining(deadline))),
                  params={"query": f'max(hikaricp_connections_active{{namespace="{ns}",app="{workload}"}})',
                          "start": str(end - POOL_TREND_MINUTES * 60), "end": str(end), "step": "30"})
    r.raise_for_status()
    rows = r.json()["data"]["result"]
    if not rows:
        return None
    return pool_trend([(float(t), float(v)) for t, v in rows[0]["values"]])


def query_cpu_peak(settings, ns: str, workload: str, deadline: float | None) -> dict | None:
    """Peak CPU of the workload's Pods over 20 minutes, replaced Pods included: the demand before a limit bound."""
    import httpx
    from .observation_sources import adapter_url

    if not all(NAME.fullmatch(v or "") for v in (ns, workload)):
        return None
    _check(deadline)
    q = ('max(max_over_time((sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="%s",'
         'pod=~"%s-[a-z0-9]+-[a-z0-9]+",container=""}[1m])))[20m:30s]))' % (ns, workload))
    r = httpx.get(f"{adapter_url(settings, 'prometheus')}/api/v1/query", params={"query": q},
                  timeout=min(8, max(1, _remaining(deadline))))
    r.raise_for_status()
    rows = r.json()["data"]["result"]
    if not rows:
        return None
    value = float(rows[0]["value"][1])
    return {"peak_cpu_cores_20m": round(value, 3)} if math.isfinite(value) else None


def collect_depth(kube, cfg, settings, ns: str, workload: str, since: datetime | None, deadline: float):
    """Fifth evidence worker. Returns a private Bundle; every optional read is isolated."""
    from .evidence import Bundle, _resource_err, _err

    b = Bundle(namespace=ns, workload=workload)
    raw = _raw(kube)
    if not hasattr(raw, "core"):
        return b  # a client without the Kubernetes core API (offline replay) has nothing deeper to read
    all_pods: list[dict] = []
    template: dict = {}
    try:
        _check(deadline)
        all_pods = raw.list_pods(ns)
        d = raw.get_deployment(ns, workload)
        template = ((d or {}).get("spec") or {}).get("template") or {}
        selector = (((d or {}).get("spec") or {}).get("selector") or {}).get("matchLabels") or {"app": workload}
    except Exception as e:  # noqa: BLE001
        for source in ("pod_log", "limitrange", "dependency"):
            b.add(source, f"{ns}/{workload}", error=_resource_err(e), supporting=source != "pod_log")
        return b
    own = [p for p in all_pods if all((p["metadata"].get("labels") or {}).get(k) == v for k, v in selector.items())]
    services: dict = {}
    cm_cache: dict = {}
    from .k8s import pod_summary
    own_view = [pod_summary(p) for p in own]
    try:
        b.pod_logs = pod_error_tail(kube, ns, own_view, workload, since, deadline)
        b.add("pod_log", f"{ns}/pods(app={workload})", {"lines": b.pod_logs, "_query": {
            "kind": "kubelet_tail", "tail_lines": POD_LOG_TAIL, "previous_container": True}})
    except Exception as e:  # noqa: BLE001
        b.add("pod_log", f"{ns}/pods(app={workload})", error=_err(e))
    try:
        b.limit_ranges = limit_ranges(kube, ns, deadline)
        b.add("limitrange", f"{ns}/LimitRange", b.limit_ranges, supporting=True)
    except Exception as e:  # noqa: BLE001
        b.add("limitrange", f"{ns}/LimitRange", error=_resource_err(e), supporting=True)
    try:
        _check(deadline)
        services = {s["metadata"]["name"]: s for s in raw.list_services(ns)}
        static = list((getattr(cfg, "dependencies", None) or {}).get(workload) or [])
        names = dependency_names(kube, ns, template, set(services), workload, deadline, static, cache=cm_cache)
        for name in names:
            try:
                view = dependency_view(kube, ns, name, services[name], all_pods, since, deadline, settings)
                b.dependencies.append(view)
                b.add("dependency", f"{ns}/Service/{name}", view, supporting=True)
            except Exception as e:  # noqa: BLE001
                b.add("dependency", f"{ns}/Service/{name}", error=_resource_err(e), supporting=True)
        if not names:
            b.add("dependency", f"{ns}/{workload}", {"services": [], "note": "no in-namespace Service named in env or ConfigMaps"},
                  supporting=True)
    except Exception as e:  # noqa: BLE001
        b.add("dependency", f"{ns}/{workload}", error=_resource_err(e), supporting=True)
    try:
        own_services = {n for n, svc in services.items()
                        if ((svc.get("spec") or {}).get("selector") or {}) and any(
                            all((p["metadata"].get("labels") or {}).get(k) == v
                                for k, v in svc["spec"]["selector"].items()) for p in own)}
        callers = caller_names(kube, ns, workload, own_services or {workload}, deadline, cm_cache)
        if callers:
            b.caller_load = {"workload": workload, "callers": callers,
                             "rates": query_request_rates(settings, ns, [workload, *callers], deadline)}
            b.add("caller_load", f"{ns}/{workload}", b.caller_load, supporting=True)
    except Exception as e:  # noqa: BLE001
        b.add("caller_load", f"{ns}/{workload}", error=_err(e), supporting=True)
    try:  # source and configuration behind the workload, its callers and its dependencies
        from . import source_analysis as S
        registry = S.load_registry()
        images = [c.get("image") for c in (template.get("spec") or {}).get("containers") or [] if c.get("image")]
        callers_view = {}
        for name in (b.caller_load or {}).get("callers") or []:
            _check(deadline)
            ct = (((raw.get_deployment(ns, name) or {}).get("spec") or {}).get("template")) or {}
            cimages = [c.get("image") for c in (ct.get("spec") or {}).get("containers") or [] if c.get("image")]
            if cimages:
                callers_view[name] = S.caller_config(cimages[0], ct, registry)
        by_name = {p["metadata"]["name"]: p for p in all_pods if p.get("metadata", {}).get("name")}
        deps = {}
        for dep in b.dependencies:
            specs = [(by_name.get(row["name"]) or {}).get("spec") or {} for row in dep.get("pods") or []]
            deps[dep["service"]] = {"access_config": S.dependency_access(raw, ns, specs, lambda: _check(deadline))}
        b.source_view = {"workload": S.workload_source(images[0], template, registry) if images else None,
                         "callers": callers_view, "dependencies": deps}
        b.add("source", f"{ns}/{workload}", b.source_view, supporting=True)
    except Exception as e:  # noqa: BLE001
        b.add("source", f"{ns}/{workload}", error=_err(e), supporting=True)
    try:
        b.usage_history = query_cpu_peak(settings, ns, workload, deadline)
        if b.usage_history:
            b.add("usage_history", f"{ns}/{workload}", b.usage_history, supporting=True)
    except Exception as e:  # noqa: BLE001
        b.add("usage_history", f"{ns}/{workload}", error=_err(e), supporting=True)
    try:
        b.pool_trend = query_pool_trend(settings, ns, workload, deadline)
        if b.pool_trend:
            b.add("pool_trend", f"{ns}/{workload}", b.pool_trend, supporting=True)
    except Exception as e:  # noqa: BLE001
        b.add("pool_trend", f"{ns}/{workload}", error=_err(e), supporting=True)
    return b


def findings(bundle, probes: dict | None, now: datetime | None = None) -> list[dict]:
    out = resource_findings(bundle.pods, bundle.metrics, bundle.deployment, bundle.limit_ranges)
    peak = (getattr(bundle, "usage_history", None) or {}).get("peak_cpu_cores_20m")
    for f in out:
        if f.get("kind") in ("cpu_limit_binding", "cpu_limit_from_admission") and peak is not None:
            f["workload_peak_cpu_cores_20m"] = peak  # demand seen before and during the bind, replaced Pods included
    out += restart_findings(bundle.pods, bundle.events, probes, now)
    out += pool_findings(bundle.metrics, bundle.pool_trend, bundle.workload)
    out += retry_findings(getattr(bundle, "caller_load", None))
    from .source_analysis import source_findings
    out += source_findings(getattr(bundle, "source_view", None))
    return out
