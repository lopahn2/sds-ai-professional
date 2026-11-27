"""Retrieval query construction: the query-expansion node of the retrieval search space (see `rag_tuning`).

`template` is the historical query. It joins a few fixed phrases chosen from router features with the
structured evidence signature. `findings` adds one plain phrase for each structured observation (E10 finding
kinds, dependency state) and the words of the alerting signal names. Runbooks are written in that vocabulary,
while a signature token such as `pool_saturated` only matches itself.

Both modes read structured fields only: never log text, and never label-like keys such as `family_hint`. Those
keys are excluded for the same reason that `agent_groups.HIDDEN_KEYS` keeps them out of prompts.
"""
from __future__ import annotations

import re

from .resolution_memory import signature

QUERY_MODES = ("template", "findings")
PROXY_LDS_REJECTED = "TargetProxyListenerUpdateRejected"   # incidents.PROXY_LDS_REJECTED

# One phrase per structured observation token produced by `resolution_memory.signature`.
FINDING_PHRASES = {
    "cpu_limit_binding": "cpu limit throttling",
    "namespace_default_limits": "namespace default limits limitrange",
    "limit_from_admission": "limit injected by admission default",
    "recent_restart": "container restarted recently",
    "liveness_restart": "liveness probe failures restart",
    "pool_saturated": "connection pool exhausted saturated",
    "pool_never_released": "connections never released leak",
    "pool_resets": "connection pool resets after restart",
    "caller_retry_amplification": "caller retries amplify load",
    "dependency_errors": "dependency errors in logs",
    "dependency_unselected_errors": "errors on an endpoint the service does not select",
    "dependency_multi_endpoint": "service selects several endpoints",
}
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_SAFE_SIGNAL = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


def signal_words(signals) -> list[str]:
    """`TargetConnectionPoolExhausted` -> `target connection pool exhausted` (bounded, safe names only)."""
    out: list[str] = []
    for name in signals or []:
        if not isinstance(name, str) or not _SAFE_SIGNAL.match(name):
            continue
        words = " ".join(w.lower() for w in _CAMEL.findall(name))
        if words and words not in out:
            out.append(words)
    return out[:8]


def _tokens(findings, dependencies, features) -> list[str]:
    return [t for t in signature(findings, dependencies, (features or {}).get("signal_reasons") or [])
            if not t.startswith("signal:")]


def template_query(workload: str, features: dict | None, findings, dependencies) -> str:
    f = features or {}
    parts = [f"{workload} incident"]
    if f.get("waiting_reasons"):
        parts.append("pods " + " ".join(f["waiting_reasons"]) + " image pull failure after rollout")
    if f.get("service_selector_matches_zero_pods") or f.get("service_has_no_endpoints"):
        parts.append("service has no endpoints selector mismatch")
    if f.get("dependent_callers_failing"):
        parts.append("callers return 5xx " + " ".join(f["dependent_callers_failing"]))
    if f.get("oom_killed"):
        parts.append("container OOMKilled exit code 137 memory limit")
    if f.get("latency_signal"):
        parts.append("high latency connection pool exhaustion slow responses")
    if PROXY_LDS_REJECTED in (f.get("signal_reasons") or ()):
        parts.append("Istio Envoy LDS listener update rejected NACK proxy compatibility route verification")
    tokens = _tokens(findings, dependencies, f)
    if tokens:
        parts.append("signature " + " ".join(tokens))
    return "; ".join(parts)


def findings_query(workload: str, features: dict | None, findings, dependencies) -> str:
    base = template_query(workload, features, findings, dependencies)
    phrases = [FINDING_PHRASES[t] for t in _tokens(findings, dependencies, features) if t in FINDING_PHRASES]
    words = signal_words((features or {}).get("signal_reasons"))
    extra = ([f"observed {', '.join(phrases)}"] if phrases else []) + ([f"signals {', '.join(words)}"] if words else [])
    return "; ".join([base, *extra])


def build(mode: str, workload: str, features: dict | None, findings, dependencies) -> str:
    if mode == "findings":
        return findings_query(workload, features, findings, dependencies)
    if mode != "template":
        raise ValueError(f"unknown query mode {mode!r}")
    return template_query(workload, features, findings, dependencies)
