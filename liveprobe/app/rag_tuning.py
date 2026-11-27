"""AutoRAG loop for LiveProbe retrieval: build an evaluation set from operations, score every candidate
retrieval configuration with the same metrics, apply the best one when it clearly beats the current one.

The AutoRAG ideas, mapped onto LiveProbe:

- **Dataset, built automatically.** Each finished production incident becomes one query, all of them up to
  `DATASET_LIMIT`. The first runs read only the newest 600 and never saw the OOM incidents, which are all older
  than a month. The query is rebuilt from what the engine had at analysis time: router features, findings and
  dependencies from the sealed collection (or the stored evidence rows). Evaluation incidents (`eval/`
  fingerprints) are never used: they are the benchmark's yardstick.
- **Ground truth.** A document is relevant to an incident in two cases:
  1. Its metadata family (`applies_to` / `family`) matches the detection family of the incident: the
     watcher's `family_hint`, set before any retrieval, or else the family of the incident's own signal
     reasons (`SIGNAL_FAMILY`). The watcher had no OOM class, so no OOM incident carried a family hint.
     An incident with neither also matches documents whose `applies_to` names one of its signal reasons
     (a runbook written for one alert, such as `TargetProxyListenerUpdateRejected`, has no family).
  2. It was actually cited for that incident.
  A past-incident document is a candidate only if its incident closed before the query incident was created,
  so the evaluation never ranks a memory the engine could not have had.
- **Nodes and modules.** Query construction (`rag_query.QUERY_MODES`), retrieval mode (hybrid | vector |
  lexical), RRF constant, candidate pool size and k. `SPACE` holds every combination. Only the document channel
  (runbooks, public documents, reference guide) is tuned. Resolution memory reaches the prompt through the
  signature-matched memory channel. The first run, with memory inside the document top-k, selected a
  past-incident cap of 0: memory there was noise, and the cap also switched off the precedents.
- **Metrics.** nDCG@k, precision@k (the share of documents put into the prompt that are relevant, i.e. noise
  and token cost), hit@k, MRR and capped recall. Per query: 0.5·nDCG@k + 0.5·precision@k. The objective is
  the macro average of that score over detection-family groups. Operations are dominated by one family
  (endpoint loss was 64% of the first dataset), and a plain mean hid regressions of the other families.
- **Strategy and deployment.** Split each family group in time order: its older 70% trains, its newer 30% is
  held out. A single global time split put every OOM incident on the train side, so the held-out bars never
  saw that family. Choose on the train split: the best objective among the configurations that already meet
  the hit@k bars below on that split. Confirm on the held-out split. Apply automatically only when all of
  these hold:
  - the held-out macro objective gains at least `MIN_GAIN` over the active configuration;
  - held-out hit@k does not drop overall, and no family group with `MIN_GROUP_HOLDOUT`+ held-out queries
    loses more than `HIT_TOLERANCE` of hit@k or more than `OBJECTIVE_TOLERANCE` of its objective. The
    objective bar keeps the ranking of a family: on 2026-11-27 a candidate kept http_5xx hit@k but moved its
    relevant runbook from first to second place (objective 0.691 -> 0.610), and a hit-only guard let it pass;
  - p95 latency of the live search stays within `LATENCY_FACTOR`× (or `LATENCY_SLACK_S` above) the current one;
  - the train and held-out splits reach `MIN_TRAIN` / `MIN_HOLDOUT` queries.

Every run is stored (`settings.rag_autotune_last`). Every application is stored with its previous
configuration (`settings.rag_retrieval`) and audited. `rollback`, `reset` and `disable` are one command each.

Offline ranking uses `rag.pool` / `rag.fuse` / `rag.diversify`, the very functions of the live search. A
configuration therefore ranks offline exactly as it will rank live.
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from . import rag as R
from .rag_query import QUERY_MODES, build as build_query

log = logging.getLogger(__name__)

SETTING_ACTIVE = "rag_retrieval"
SETTING_REPORT = "rag_autotune_last"
SETTING_CONTROL = "rag_autotune_control"
LOCK_KEY = 4_812_350_211          # pg advisory lock: one tuning run at a time across replicas
POOL_SIZE = 60                    # >= the largest candidate count in SPACE
TRAIN_SHARE = 0.7
MIN_TRAIN, MIN_HOLDOUT = 20, 10
MIN_GAIN = 0.02
HIT_TOLERANCE, MIN_GROUP_HOLDOUT, MIN_GROUP = 0.05, 5, 10   # non-inferiority per family; smaller groups pool
OBJECTIVE_TOLERANCE = 0.05        # per-family loss of 0.5·nDCG + 0.5·precision a candidate may cause
LATENCY_FACTOR, LATENCY_SLACK_S, LATENCY_SAMPLE = 1.5, 0.05, 20
DATASET_LIMIT = 5000              # all finished production incidents (1,328 on 2026-11-27); bounds run time
TERMINAL = ("resolved", "handed_off", "closed", "recovery_review", "failed")
PRODUCTION_KINDS = ["runbook", "public_doc", "past_incident"]
CACHE_TTL_S = 60.0
FIRST_RUN_DELAY_S, RUN_INTERVAL_S = 600.0, 24 * 3600.0

# Detection families and document families on one vocabulary.
FAMILY_ALIASES = {
    "image_crash": ("image_crash", "image_pull", "crashloop", "imagepullbackoff", "errimagepull", "crashloopbackoff"),
    "endpoint_loss": ("endpoint_loss", "no_endpoints", "selector_mismatch", "endpoints"),
    "oom": ("oom", "oomkilled"),
    "http_5xx": ("http_5xx", "http5xx", "upstream_failure", "cascading_5xx"),
}
_ALIAS = {alias: fam for fam, names in FAMILY_ALIASES.items() for alias in names}

# Signal reason -> detection family, for incidents the watcher gave no family hint. The first block is the
# watcher's own classification (incidents.REASON_CLASS -> FAMILY_HINT; a test keeps the two in step). The
# second block holds reasons the watcher does not classify.
SIGNAL_FAMILY = {
    "ImagePullBackOff": "image_crash", "ErrImagePull": "image_crash", "InvalidImageName": "image_crash",
    "TargetWorkloadImagePullFailing": "image_crash", "CrashLoopBackOff": "image_crash",
    "NoEndpoints": "endpoint_loss", "TargetServiceNoEndpoints": "endpoint_loss",
    "High5xx": "http_5xx", "OrderHigh5xxRate": "http_5xx", "InventoryHigh5xxRate": "http_5xx",
    "TargetHigh5xxRate": "http_5xx", "ApplicationException": "http_5xx",
    "LivenessProbeFailed": "image_crash",
    "TargetWorkloadOOMKilled": "oom", "OOMKilled": "oom", "TargetWorkloadCrashLooping": "image_crash",
}


@dataclass(frozen=True)
class RetrievalConfig:
    query_mode: str = "template"
    mode: str = "hybrid"
    rrf_k: int = R.RRF_K
    candidates: int = R.CANDIDATES
    k: int = 3
    past_incident_cap: int = 1

    def __post_init__(self):
        if self.query_mode not in QUERY_MODES or self.mode not in R.MODES:
            raise ValueError(f"unknown query or retrieval mode: {self.query_mode}/{self.mode}")
        for name, lo, hi in (("rrf_k", 1, 200), ("candidates", 5, POOL_SIZE), ("k", 1, 5), ("past_incident_cap", 0, 2)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise ValueError(f"{name} must be an integer in [{lo}, {hi}]")

    def key(self) -> str:
        rrf = f"/rrf{self.rrf_k}" if self.mode == "hybrid" else ""
        return f"{self.query_mode}/{self.mode}{rrf}/c{self.candidates}/k{self.k}/p{self.past_incident_cap}"

    def max_per_kind(self) -> dict[str, int]:
        return {"past_incident": self.past_incident_cap, "eval_memory": 1}

    def search_kwargs(self) -> dict:
        return {"k": self.k, "candidates": self.candidates, "mode": self.mode, "rrf_k": self.rrf_k,
                "max_per_kind": self.max_per_kind()}

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "RetrievalConfig":
        fields = {"query_mode", "mode", "rrf_k", "candidates", "k", "past_incident_cap"}
        if not isinstance(value, dict) or set(value) - fields:
            raise ValueError("retrieval configuration has unknown fields")
        return cls(**value)


DEFAULT = RetrievalConfig()
SPACE = {"query_mode": QUERY_MODES, "mode": R.MODES, "rrf_k": (20, 60), "candidates": (30, 60), "k": (2, 3, 4),
         "past_incident_cap": (1,)}      # memory is not in the tuned channel; the cap stays at its default
MEMORY_KINDS = ("past_incident", "eval_memory")


def document_kinds(kinds: list[str]) -> list[str]:
    return [k for k in kinds if k not in MEMORY_KINDS]


def candidates() -> list[RetrievalConfig]:
    out, seen = [], set()
    for qm, mode, rrf, cand, k, cap in itertools.product(*SPACE.values()):
        cfg = RetrievalConfig(qm, mode, rrf if mode == "hybrid" else R.RRF_K, cand, k, cap)
        if cfg.key() not in seen:
            seen.add(cfg.key())
            out.append(cfg)
    return out


# ---------------------------------------------------------------- active configuration
_cache: dict = {"at": -math.inf, "cfg": DEFAULT}


def _now() -> float:
    return time.monotonic()


def active_config() -> RetrievalConfig:
    """The applied configuration (cached for `CACHE_TTL_S`). Any read or validation problem means DEFAULT."""
    if _now() - _cache["at"] < CACHE_TTL_S:
        return _cache["cfg"]
    cfg = DEFAULT
    try:
        from . import db
        from .models import Setting
        with db.session() as s:
            row = s.get(Setting, SETTING_ACTIVE)
            if row and isinstance(row.value, dict) and row.value.get("active"):
                cfg = RetrievalConfig.from_dict(row.value["active"])
    except Exception as e:  # noqa: BLE001
        log.warning("retrieval configuration unavailable, using default: %s", type(e).__name__)
    _cache.update(at=_now(), cfg=cfg)
    return cfg


def invalidate() -> None:
    _cache["at"] = -math.inf


def production_kinds(cfg) -> list[str]:
    """Kinds production retrieval may see. Evaluation memory is never one of them."""
    return [*PRODUCTION_KINDS, "reference_guide"] if getattr(cfg, "use_reference_guide", False) else list(PRODUCTION_KINDS)


# ---------------------------------------------------------------- dataset
@dataclass
class Query:
    incident_id: int
    created_at: datetime
    workload: str
    features: dict
    findings: list
    dependencies: list
    relevant: frozenset
    label_sources: dict = field(default_factory=dict)
    group: str = "(none)"


def canonical_families(values) -> set[str]:
    out = set()
    for v in values or []:
        if isinstance(v, str) and v.strip().lower() in _ALIAS:
            out.add(_ALIAS[v.strip().lower()])
    return out


def signal_families(reasons) -> list[str]:
    """Detection families of signal reasons, in the order the signals were observed."""
    out: list[str] = []
    for r in reasons or []:
        fam = SIGNAL_FAMILY.get(r) if isinstance(r, str) else None
        if fam and fam not in out:
            out.append(fam)
    return out


def signal_tokens(values) -> set[str]:
    return {v.strip().lower() for v in values or [] if isinstance(v, str) and v.strip()}


def by_signal_name(catalog: dict, signal_reasons) -> set[str]:
    """Documents whose `applies_to` names one of the signal reasons (case-insensitive, exact name)."""
    names = signal_tokens(signal_reasons)
    return {k for k, d in catalog.items() if d.get("signals", set()) & names} if names else set()


def families_of(family_hint, signal_reasons) -> tuple[set[str], str, bool]:
    """(detection families, evaluation group, whether they came from the signal reasons) of one incident.

    The watcher's family hint wins. Without one, the incident's own signal reasons decide, and the group is
    the family of the earliest classified signal."""
    fams = canonical_families([family_hint])
    if fams:
        return fams, sorted(fams)[0], False
    ordered = signal_families(signal_reasons)
    return set(ordered), (ordered[0] if ordered else "(none)"), bool(ordered)


def _aware(value) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value if isinstance(value, datetime) else None


def doc_catalog(s, kinds: list[str]) -> dict[str, dict]:
    """Active documents of the searchable kinds: kind, canonical families, source incident and its close time."""
    from sqlalchemy import select
    from .models import RagChunk, RagDocument
    active = {d for (d,) in s.execute(select(RagChunk.doc).where(RagChunk.retired_at.is_(None)).distinct())}
    out = {}
    for d in s.scalars(select(RagDocument).where(RagDocument.kind.in_(kinds))):
        if d.doc_key not in active:
            continue
        meta = d.meta or {}
        tokens = [*(meta.get("applies_to") or []), *(meta.get("family") or [])]
        out[d.doc_key] = {"kind": d.kind, "families": canonical_families(tokens), "incident_id": d.incident_id,
                          "closed_at": _aware(meta.get("closed_at")), "signals": signal_tokens(tokens)}
    return out


def visible(doc: dict, q_incident: int, q_created: datetime) -> bool:
    """Whether the engine could have retrieved this document when the query incident was analysed."""
    if doc["kind"] not in ("past_incident", "eval_memory"):
        return True
    if doc.get("incident_id") == q_incident:
        return False
    closed = doc.get("closed_at")
    return closed is not None and closed < q_created


def build_dataset(s, kinds: list[str], limit: int = DATASET_LIMIT) -> tuple[list[Query], dict]:
    from sqlalchemy import select
    from .models import AgentRun, Citation, Incident, RagChunk, RoutingDecision, WorkflowRun
    from .resolution_memory import observations_from_evidence

    catalog = doc_catalog(s, kinds)
    incidents = list(s.scalars(select(Incident).where(~Incident.fingerprint.like("eval/%"),
                                                     Incident.status.in_(TERMINAL))
                               .order_by(Incident.id.desc()).limit(limit)))
    ids = [i.id for i in incidents]
    features: dict[int, dict] = {}
    for iid, f in s.execute(select(RoutingDecision.incident_id, RoutingDecision.features)
                            .where(RoutingDecision.incident_id.in_(ids)).order_by(RoutingDecision.id)):
        features[iid] = f or {}
    sealed: dict[int, tuple] = {}
    collection = WorkflowRun.snapshot["collection"]
    for iid, fnd, deps in s.execute(select(WorkflowRun.incident_id, collection["findings"], collection["dependencies"])
                                    .where(WorkflowRun.incident_id.in_(ids)).order_by(WorkflowRun.id)):
        if iid not in sealed and (fnd is not None or deps is not None):
            sealed[iid] = (fnd or [], deps or [])
    cited: dict[int, set] = {}
    for iid, doc in s.execute(select(AgentRun.incident_id, RagChunk.doc).join(Citation, Citation.agent_run_id == AgentRun.id)
                              .join(RagChunk, RagChunk.id == Citation.chunk_id).where(AgentRun.incident_id.in_(ids))):
        cited.setdefault(iid, set()).add(doc)

    out: list[Query] = []
    stats = {"incidents": len(incidents), "no_route": 0, "unlabeled": 0, "from_sealed": 0, "from_evidence": 0,
             "evidence_unreadable": 0, "label_family": 0, "label_signal": 0, "label_signal_name": 0,
             "label_cited": 0, "families": {}}
    for inc in sorted(incidents, key=lambda i: i.id):
        if inc.id not in features:
            stats["no_route"] += 1
            continue
        created = _aware(inc.created_at)
        fams, group, from_signal = families_of(inc.family_hint, inc.signal_reasons)
        by_family = {k for k, d in catalog.items() if d["families"] & fams and visible(d, inc.id, created)}
        if not fams:
            by_family = {k for k in by_signal_name(catalog, inc.signal_reasons) if visible(catalog[k], inc.id, created)}
            if by_family:
                group = "signal:" + sorted(t for t in signal_tokens(inc.signal_reasons)
                                           if any(t in catalog[k]["signals"] for k in by_family))[0]
                stats["label_signal_name"] += 1
        by_citation = {k for k in cited.get(inc.id, ()) if k in catalog and visible(catalog[k], inc.id, created)}
        relevant = frozenset(by_family | by_citation)
        if not relevant:
            stats["unlabeled"] += 1
            continue
        if inc.id in sealed:
            findings, deps = sealed[inc.id]
            stats["from_sealed"] += 1
        else:
            try:
                findings, deps = observations_from_evidence(s, inc)
            except Exception:  # noqa: BLE001 - an incident whose evidence cannot be read is skipped
                stats["evidence_unreadable"] += 1
                continue
            stats["from_evidence"] += 1
        stats["label_family"] += bool(by_family)
        stats["label_signal"] += bool(by_family) and from_signal
        stats["label_cited"] += bool(by_citation)
        stats["families"][group] = stats["families"].get(group, 0) + 1
        out.append(Query(inc.id, created, inc.workload, features[inc.id], list(findings or []), list(deps or []),
                         relevant, {"family": sorted(by_family), "cited": sorted(by_citation)}, group=group))
    return out, stats


# ---------------------------------------------------------------- evaluation
def metrics(docs: list[str], relevant, k: int) -> dict[str, float]:
    gains = [1 if d in relevant else 0 for d in docs]
    n_rel = len(relevant)
    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains))
    idcg = sum(1 / math.log2(i + 2) for i in range(min(n_rel, k)))
    first = next((i for i, g in enumerate(gains) if g), None)
    found = sum(gains)
    m = {"ndcg": dcg / idcg if idcg else 0.0, "precision": found / len(docs) if docs else 0.0,
         "hit": 1.0 if found else 0.0, "mrr": 1.0 / (first + 1) if first is not None else 0.0,
         "recall": found / min(n_rel, k) if n_rel else 0.0}
    m["objective"] = 0.5 * m["ndcg"] + 0.5 * m["precision"]
    return m


def rank(pool_: tuple, cfg: RetrievalConfig, allowed) -> list[str]:
    """Documents a configuration puts into the prompt for one pooled query (same functions as live search)."""
    vec_ids, lex_ids, meta = pool_
    vec = [c for c in vec_ids if allowed(meta[c][0])]
    lex = [c for c in lex_ids if allowed(meta[c][0])]
    scored, _, _ = R.fuse(vec, lex, {c: m[3] for c, m in meta.items()}, mode=cfg.mode, rrf_k=cfg.rrf_k,
                          candidates=cfg.candidates)
    chosen = R.diversify(scored, lambda c: meta[c][0], lambda c: meta[c][1], cfg.k, cfg.max_per_kind())
    return [meta[c][0] for _, _, c in chosen]


def _mean(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    out = {key: round(sum(r[key] for r in rows) / len(rows), 4) for key in rows[0]}
    out["n"] = len(rows)
    return out


def split(dataset: list[Query]) -> tuple[list[Query], list[Query]]:
    """Time split inside every family group (`groups_of`): the older `TRAIN_SHARE` trains, the rest is held out."""
    group = groups_of(dataset)
    by: dict[str, list[Query]] = {}
    for q in dataset:
        by.setdefault(group[q.incident_id], []).append(q)
    train_ids: set[int] = set()
    for qs in by.values():
        qs = sorted(qs, key=lambda q: (q.created_at, q.incident_id))
        train_ids.update(q.incident_id for q in qs[:int(len(qs) * TRAIN_SHARE)])
    return ([q for q in dataset if q.incident_id in train_ids],
            [q for q in dataset if q.incident_id not in train_ids])


def groups_of(dataset: list[Query]) -> dict[int, str]:
    """Family group per query; families with fewer than `MIN_GROUP` queries pool into `other`."""
    sizes: dict[str, int] = {}
    for q in dataset:
        sizes[q.group] = sizes.get(q.group, 0) + 1
    return {q.incident_id: q.group if sizes[q.group] >= MIN_GROUP else "other" for q in dataset}


def summarize(rows: list[tuple[str, dict]]) -> dict:
    """Mean metrics overall plus per family group; `objective` is the macro average over groups."""
    if not rows:
        return {"n": 0}
    out = _mean([m for _, m in rows])
    by: dict[str, list] = {}
    for g, m in rows:
        by.setdefault(g, []).append(m)
    out["groups"] = {g: _mean(ms) for g, ms in sorted(by.items())}
    out["micro_objective"] = out["objective"]
    out["objective"] = round(sum(v["objective"] for v in out["groups"].values()) / len(out["groups"]), 4)
    return out


def evaluate(pools: dict, dataset: list[Query], catalog: dict, configs: list[RetrievalConfig]) -> dict:
    """{config key: {"train": summary, "holdout": summary}} over pooled queries."""
    train, holdout = split(dataset)
    group = groups_of(dataset)
    out = {}
    for cfg in configs:
        per = {}
        for name, part in (("train", train), ("holdout", holdout)):
            rows = []
            for q in part:
                docs = rank(pools[(q.incident_id, cfg.query_mode)], cfg,
                            lambda doc, q=q: doc in catalog and visible(catalog[doc], q.incident_id, q.created_at))
                rows.append((group[q.incident_id], metrics(docs, q.relevant, cfg.k)))
            per[name] = summarize(rows)
        out[cfg.key()] = per
    return out


def distance(a: RetrievalConfig, b: RetrievalConfig) -> int:
    return sum(getattr(a, f) != getattr(b, f) for f in ("query_mode", "mode", "rrf_k", "candidates", "k",
                                                          "past_incident_cap"))


def regressions(best: dict, cur: dict) -> list[str]:
    """Non-inferiority bars: hit@k does not drop overall, and no family group with enough queries loses more
    than `HIT_TOLERANCE` of hit@k or more than `OBJECTIVE_TOLERANCE` of its objective (ranking quality)."""
    out = []
    if best.get("hit", 0) < cur.get("hit", 0):
        out.append(f"hit@k drops {cur.get('hit')} -> {best.get('hit')}")
    for g, cg in (cur.get("groups") or {}).items():
        bg = (best.get("groups") or {}).get(g)
        if not bg or cg.get("n", 0) < MIN_GROUP_HOLDOUT:
            continue
        if bg.get("hit", 0) < cg.get("hit", 0) - HIT_TOLERANCE:
            out.append(f"hit@k of family {g} drops {cg.get('hit')} -> {bg.get('hit')}")
        if "objective" in cg and "objective" in bg and bg["objective"] < cg["objective"] - OBJECTIVE_TOLERANCE:
            out.append(f"objective of family {g} drops {cg['objective']} -> {bg['objective']}")
    return out


def choose(results: dict, configs: list[RetrievalConfig], current: RetrievalConfig) -> RetrievalConfig:
    """Best train objective among configurations meeting the `regressions` bars on the train split (the current one
    always does). Ties: the fewest changes from the current configuration, then fewer documents."""
    by_key = {c.key(): c for c in configs}
    by_key.setdefault(current.key(), current)
    cur = results[current.key()]["train"]
    admissible = [c for c in by_key.values()
                  if c.key() == current.key() or not regressions(results[c.key()]["train"], cur)]
    return max(admissible, key=lambda c: (round(results[c.key()]["train"].get("objective", 0.0), 4),
                                          -distance(c, current), -c.k, c.key()))


def decide(results: dict, best: RetrievalConfig, current: RetrievalConfig, latency: dict, sizes: dict) -> tuple[bool, list[str]]:
    reasons = []
    if best.key() == current.key():
        return False, ["current configuration is already the best on the train split"]
    b, c = results[best.key()]["holdout"], results[current.key()]["holdout"]
    if sizes["train"] < MIN_TRAIN or sizes["holdout"] < MIN_HOLDOUT:
        reasons.append(f"not enough queries (train {sizes['train']} < {MIN_TRAIN} or holdout {sizes['holdout']} < {MIN_HOLDOUT})")
    gain = round(b.get("objective", 0) - c.get("objective", 0), 4)
    if gain < MIN_GAIN:
        reasons.append(f"held-out objective gain {gain} < {MIN_GAIN}")
    reasons += [f"held-out {r}" for r in regressions(b, c)]
    cap = max(latency["current_p95_s"] * LATENCY_FACTOR, latency["current_p95_s"] + LATENCY_SLACK_S)
    if latency["best_p95_s"] > cap:
        reasons.append(f"p95 latency {latency['best_p95_s']}s > cap {round(cap, 4)}s")
    return (not reasons), (reasons or [f"held-out macro objective +{gain}, hit@k kept overall and per family "
                                       f"(tolerance {HIT_TOLERANCE}), family objectives within {OBJECTIVE_TOLERANCE}, "
                                       f"latency within cap"])


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)], 4) if ordered else 0.0


def measure_latency(s, embedder, queries: list[Query], kinds: list[str], cfgs: list[RetrievalConfig]) -> list[float]:
    """p95 seconds of the real `rag.search` per configuration, interleaved over the same queries after a warm-up."""
    sample = queries[-LATENCY_SAMPLE:]
    times: dict[str, list[float]] = {c.key(): [] for c in cfgs}
    for warm in (True, False):
        for q in sample:
            for c in cfgs:
                text = build_query(c.query_mode, q.workload, q.features, q.findings, q.dependencies)
                t0 = time.perf_counter()
                R.search(s, embedder, text, kinds=kinds, **c.search_kwargs())
                if not warm:
                    times[c.key()].append(time.perf_counter() - t0)
    return [_p95(times[c.key()]) for c in cfgs]


# ---------------------------------------------------------------- run / apply
def _setting(s, key: str) -> dict:
    from .models import Setting
    row = s.get(Setting, key)
    return dict(row.value) if row and isinstance(row.value, dict) else {}


def _put(s, key: str, value: dict) -> None:
    from .models import Setting
    row = s.get(Setting, key)
    if row is None:
        s.add(Setting(key=key, value=value))
    else:
        row.value = value


def enabled(s) -> bool:
    return _setting(s, SETTING_CONTROL).get("enabled", True) is not False


def current_config(s) -> RetrievalConfig:
    value = _setting(s, SETTING_ACTIVE).get("active")
    try:
        return RetrievalConfig.from_dict(value) if value else DEFAULT
    except ValueError:
        return DEFAULT


def apply_config(s, cfg: RetrievalConfig, actor: str, reason: str, metrics_: dict | None = None) -> dict:
    from .db import audit
    state = _setting(s, SETTING_ACTIVE)
    previous = current_config(s)
    history = list(state.get("history") or [])[-9:]
    history.append({"active": cfg.as_dict(), "previous": previous.as_dict(), "at": datetime.now(timezone.utc).isoformat(),
                    "by": actor, "reason": reason[:300]})
    value = {"active": cfg.as_dict(), "previous": previous.as_dict(), "applied_at": history[-1]["at"],
             "applied_by": actor, "reason": reason[:300], "metrics": metrics_ or {}, "history": history}
    _put(s, SETTING_ACTIVE, value)
    audit(s, actor, "rag_retrieval_applied", None, active=cfg.key(), previous=previous.key(), reason=reason[:300])
    invalidate()
    return value


def run(s, embedder, kinds: list[str], *, apply: bool = True, actor: str = "rag_autotune") -> dict:
    """One AutoRAG pass in session `s`. Writes the report; applies the winner only if `decide` says so."""
    started = time.perf_counter()
    kinds = document_kinds(kinds)
    current = current_config(s)
    configs = candidates()
    dataset, stats = build_dataset(s, kinds)
    catalog = doc_catalog(s, kinds)
    vectors: dict[str, list[float]] = {}
    pools: dict[tuple, tuple] = {}
    by_text: dict[str, tuple] = {}
    for q in dataset:
        for qm in QUERY_MODES:
            text = build_query(qm, q.workload, q.features, q.findings, q.dependencies)
            if text not in by_text:
                if text not in vectors:
                    vectors[text] = embedder.embed([text])[0]
                by_text[text] = R.pool(s, vectors[text], text, kinds=kinds, candidates=POOL_SIZE, mode="hybrid")
            pools[(q.incident_id, qm)] = by_text[text]
    train, holdout = split(dataset)
    sizes = {"train": len(train), "holdout": len(holdout)}
    results = evaluate(pools, dataset, catalog, configs) if dataset else {}
    report = {"run_at": datetime.now(timezone.utc).isoformat(), "dataset": {**stats, **sizes, "queries": len(dataset),
              "distinct_query_texts": len(by_text)}, "current": current.key(), "candidates": len(configs)}
    if not dataset:
        report.update(decision="kept", reasons=["no labelled production incidents"])
    else:
        if current.key() not in results:
            results.update(evaluate(pools, dataset, catalog, [current]))
        best = choose(results, configs, current)
        cur_p95, best_p95 = measure_latency(s, embedder, holdout or train, kinds, [current, best])
        latency = {"current_p95_s": cur_p95, "best_p95_s": best_p95, "sample": min(LATENCY_SAMPLE, len(holdout or train))}
        ok, reasons = decide(results, best, current, latency, sizes)
        top = sorted(results.items(), key=lambda kv: -kv[1]["train"].get("objective", 0))[:10]
        report.update(best=best.key(), latency=latency, reasons=reasons,
                      metrics={"current": results[current.key()], "best": results[best.key()]},
                      top=[{"config": k, **v} for k, v in top])
        report["decision"] = "applied" if (ok and apply and enabled(s)) else ("eligible" if ok else "kept")
        if report["decision"] == "applied":
            apply_config(s, best, actor, "; ".join(reasons), {"current": results[current.key()],
                                                               "best": results[best.key()], "latency": latency})
    report["seconds"] = round(time.perf_counter() - started, 1)
    if apply:
        _put(s, SETTING_REPORT, report)
    return report


def run_locked(embedder, kinds: list[str], *, apply: bool = True) -> dict | None:
    from sqlalchemy import func, select
    from . import db
    with db.session() as s:
        if not s.execute(select(func.pg_try_advisory_xact_lock(LOCK_KEY))).scalar():
            return None
        if apply and not enabled(s):
            return {"decision": "disabled"}
        return run(s, embedder, kinds, apply=apply)


def loop(stop, embedder, kinds: list[str], first_delay_s: float = FIRST_RUN_DELAY_S,
         interval_s: float = RUN_INTERVAL_S) -> None:
    """Background schedule: first pass after `first_delay_s`, then every `interval_s`. Never raises."""
    delay = first_delay_s
    while not stop.wait(delay):
        delay = interval_s
        try:
            report = run_locked(embedder, kinds)
            if report:
                log.info("RAG autotune: %s (%s -> %s)", report.get("decision"), report.get("current"), report.get("best"))
        except Exception as e:  # noqa: BLE001
            log.warning("RAG autotune skipped: %s", type(e).__name__)


# ---------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.rag_tuning", description=__doc__.split("\n")[0])
    p.add_argument("command", choices=["evaluate", "run", "status", "rollback", "reset", "enable", "disable"])
    args = p.parse_args(argv)
    from . import db
    from .config import get_app_config, get_settings
    settings = get_settings()
    db.init_db(settings.sqlalchemy_url())
    kinds = production_kinds(get_app_config())
    if args.command in ("evaluate", "run"):
        from .rag import FastEmbedder
        emb = FastEmbedder(settings.embedding_model, settings.embedding_cache_dir)
        report = run_locked(emb, kinds, apply=args.command == "run")
        print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
        return 0 if report is not None else 3
    with db.session() as s:
        if args.command == "status":
            print(json.dumps({"active": current_config(s).key(), "enabled": enabled(s),
                              "state": _setting(s, SETTING_ACTIVE), "last_run": _setting(s, SETTING_REPORT)},
                             ensure_ascii=False, indent=1, default=str))
        elif args.command in ("enable", "disable"):
            _put(s, SETTING_CONTROL, {"enabled": args.command == "enable", "at": datetime.now(timezone.utc).isoformat()})
            print(args.command + "d")
        elif args.command == "rollback":
            prev = _setting(s, SETTING_ACTIVE).get("previous")
            cfg = RetrievalConfig.from_dict(prev) if prev else DEFAULT
            print(apply_config(s, cfg, "operator", "rollback")["active"])
        else:
            print(apply_config(s, DEFAULT, "operator", "reset to default")["active"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
