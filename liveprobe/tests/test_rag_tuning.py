"""AutoRAG retrieval tuning: shared fusion, metrics, visibility, selection and the apply rule (no database)."""
from datetime import datetime, timedelta, timezone

import pytest

from app import rag as R
from app import rag_tuning as T

T0 = datetime(2026, 11, 20, tzinfo=timezone.utc)


def test_default_configuration_is_the_previous_ranking():
    assert T.DEFAULT.search_kwargs() == {"k": 3, "candidates": 30, "mode": "hybrid", "rrf_k": 60,
                                         "max_per_kind": {"past_incident": 1, "eval_memory": 1}}
    assert T.DEFAULT.query_mode == "template" and T.DEFAULT.key() == "template/hybrid/rrf60/c30/k3/p1"
    assert R.MAX_PER_KIND["past_incident"] == 1 and (R.CANDIDATES, R.RRF_K) == (30, 60)


def test_configuration_is_bounded_and_validated():
    with pytest.raises(ValueError):
        T.RetrievalConfig(k=9)
    with pytest.raises(ValueError):
        T.RetrievalConfig(mode="bm42")
    with pytest.raises(ValueError):
        T.RetrievalConfig.from_dict({"k": 3, "shell": "rm"})
    with pytest.raises(ValueError):
        T.RetrievalConfig(candidates=True)
    cfg = T.RetrievalConfig.from_dict({"query_mode": "findings", "mode": "vector", "k": 2})
    assert cfg.key() == "findings/vector/c30/k2/p1"          # rrf is not part of a single-list key
    space = T.candidates()
    assert len(space) == 48 and len({c.key() for c in space}) == 48 and T.DEFAULT.key() in {c.key() for c in space}
    assert {c.past_incident_cap for c in space} == {1}                     # memory is not a tuned channel
    assert T.document_kinds(["runbook", "public_doc", "past_incident", "eval_memory"]) == ["runbook", "public_doc"]


def test_fusion_ties_and_diversity_match_the_ranking_policy():
    dist = {1: 0.10, 2: 0.20, 3: 0.30, 4: 0.05, 5: 0.40}
    scored, vr, lr = R.fuse([1, 2, 3], [3, 4, 1], dist)
    # 1: 1/61 + 1/63, 3: 1/63 + 1/61 (tie -> smaller distance first), 4 and 2 single-list
    assert [c for _, _, c in scored] == [1, 3, 4, 2] and vr == {1: 1, 2: 2, 3: 3} and lr == {3: 1, 4: 2, 1: 3}
    assert [c for _, _, c in R.fuse([1, 2], [3], dist, mode="vector")[0]] == [1, 2]
    assert [c for _, _, c in R.fuse([1, 2], [3, 5], dist, mode="lexical")[0]] == [3, 5]
    assert [c for _, _, c in R.fuse([1, 2, 3], [], dist, candidates=2)[0]] == [1, 2]
    docs = {1: "a.md", 3: "a.md", 4: "inc-1.md", 2: "inc-2.md"}
    kinds = {1: "runbook", 3: "runbook", 4: "past_incident", 2: "past_incident"}
    chosen = R.diversify(scored, docs.get, kinds.get, 3, {"past_incident": 1})
    assert [c for _, _, c in chosen] == [1, 4]                 # one chunk per document, one past incident
    with pytest.raises(ValueError):
        R.fuse([1], [], dist, mode="dense")


def test_metrics():
    m = T.metrics(["a", "x", "b"], {"a", "b"}, 3)
    assert m["hit"] == 1 and m["mrr"] == 1 and m["recall"] == 1
    assert m["precision"] == pytest.approx(2 / 3)
    assert m["ndcg"] == pytest.approx((1 + 1 / 2) / (1 + 1 / 1.5849625007211562))
    assert m["objective"] == pytest.approx(0.5 * m["ndcg"] + 0.5 * m["precision"])
    none = T.metrics(["x"], {"a"}, 3)
    assert none == {"ndcg": 0.0, "precision": 0.0, "hit": 0.0, "mrr": 0.0, "recall": 0.0, "objective": 0.0}
    assert T.metrics([], {"a"}, 2)["precision"] == 0.0


def test_memory_is_visible_only_if_closed_before_the_query_incident():
    runbook = {"kind": "runbook", "incident_id": None, "closed_at": None}
    past = {"kind": "past_incident", "incident_id": 5, "closed_at": T0}
    assert T.visible(runbook, 9, T0)
    assert T.visible(past, 9, T0 + timedelta(minutes=1))
    assert not T.visible(past, 9, T0)                           # closed at the same time: not yet indexed
    assert not T.visible(past, 5, T0 + timedelta(days=1))       # its own memory
    assert not T.visible({**past, "closed_at": None}, 9, T0 + timedelta(days=1))


def test_families_share_one_vocabulary():
    assert T.canonical_families(["image_pull"]) == {"image_crash"}
    assert T.canonical_families(["CrashLoopBackOff", "no_endpoints", "OOMKilled", "cascading_5xx", None, 3]) == {
        "image_crash", "endpoint_loss", "oom", "http_5xx"}
    assert T.canonical_families(["TargetProxyListenerUpdateRejected"]) == set()


def test_production_kinds_never_include_evaluation_memory():
    class Cfg:
        use_reference_guide = True
    assert "eval_memory" not in T.production_kinds(Cfg()) and "reference_guide" in T.production_kinds(Cfg())
    assert T.production_kinds(object()) == ["runbook", "public_doc", "past_incident"]


def _pool(order: list[str]):
    """One pooled query whose vector and lexical lists both rank documents in `order` (one chunk each)."""
    ids = list(range(1, len(order) + 1))
    meta = {i: (doc, "past_incident" if doc.startswith("inc") else "runbook", None, i / 100) for i, doc in zip(ids, order)}
    return ids, list(ids), meta


def _query(i: int, relevant: set[str]) -> T.Query:
    return T.Query(i, T0 + timedelta(hours=i), "w", {}, [], [], frozenset(relevant))


def test_rank_drops_memory_the_engine_could_not_have_had():
    catalog = {"a.md": {"kind": "runbook", "incident_id": None, "closed_at": None},
               "inc-9.md": {"kind": "past_incident", "incident_id": 9, "closed_at": T0 + timedelta(days=9)},
               "b.md": {"kind": "runbook", "incident_id": None, "closed_at": None}}
    pool = _pool(["inc-9.md", "a.md", "b.md"])
    q = _query(1, {"a.md"})
    allowed = lambda doc: T.visible(catalog[doc], q.incident_id, q.created_at)  # noqa: E731
    assert T.rank(pool, T.RetrievalConfig(k=2), allowed) == ["a.md", "b.md"]
    assert T.rank(pool, T.RetrievalConfig(k=2), lambda doc: True) == ["inc-9.md", "a.md"]


def test_selection_prefers_the_configuration_that_ranks_relevant_documents_first():
    catalog = {d: {"kind": "runbook", "incident_id": None, "closed_at": None} for d in ("good.md", "noise.md", "n2.md")}
    dataset = [_query(i, {"good.md"}) for i in range(40)]
    pools = {}
    for q in dataset:
        pools[(q.incident_id, "template")] = _pool(["noise.md", "n2.md", "good.md"])
        pools[(q.incident_id, "findings")] = _pool(["good.md", "noise.md", "n2.md"])
    configs = [T.DEFAULT, T.RetrievalConfig(query_mode="findings"), T.RetrievalConfig(query_mode="findings", k=2)]
    results = T.evaluate(pools, dataset, catalog, configs)
    assert results[T.DEFAULT.key()]["holdout"]["n"] == 12 and results[T.DEFAULT.key()]["train"]["n"] == 28
    best = T.choose(results, configs, T.DEFAULT)
    assert best.key() == "findings/hybrid/rrf60/c30/k2/p1"       # same nDCG, less noise in the prompt
    latency = {"current_p95_s": 0.1, "best_p95_s": 0.11}
    ok, reasons = T.decide(results, best, T.DEFAULT, latency, {"train": 28, "holdout": 12})
    assert ok, reasons


def test_apply_rule_keeps_the_current_configuration_unless_every_bar_is_met():
    cur, new = T.DEFAULT, T.RetrievalConfig(k=2)
    res = {cur.key(): {"holdout": {"objective": 0.50, "hit": 0.9}}, new.key(): {"holdout": {"objective": 0.51, "hit": 0.9}}}
    fast = {"current_p95_s": 0.10, "best_p95_s": 0.10}
    big = {"train": 30, "holdout": 12}
    assert T.decide(res, cur, cur, fast, big) == (False, ["current configuration is already the best on the train split"])
    ok, why = T.decide(res, new, cur, fast, big)
    assert not ok and "gain" in why[0]
    res[new.key()]["holdout"] = {"objective": 0.60, "hit": 0.8}
    assert any("hit@k drops" in r for r in T.decide(res, new, cur, fast, big)[1])
    res[new.key()]["holdout"] = {"objective": 0.60, "hit": 0.9}
    assert any("latency" in r for r in T.decide(res, new, cur, {"current_p95_s": 0.1, "best_p95_s": 0.2}, big)[1])
    assert any("not enough" in r for r in T.decide(res, new, cur, fast, {"train": 5, "holdout": 2})[1])
    assert T.decide(res, new, cur, {"current_p95_s": 0.02, "best_p95_s": 0.06}, big)[0]   # +50 ms slack


def test_active_configuration_falls_back_to_default_and_is_cached(monkeypatch):
    from app import db
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("db down")
    T.invalidate()
    monkeypatch.setattr(db, "session", broken)
    assert T.active_config() == T.DEFAULT and T.active_config() == T.DEFAULT
    assert len(calls) == 1
    T.invalidate()


def test_objective_is_the_macro_average_over_family_groups():
    rows = [("endpoint_loss", T.metrics(["a"], {"a"}, 1))] * 8 + [("image_crash", T.metrics(["x"], {"a"}, 1))] * 2
    s = T.summarize(rows)
    assert s["micro_objective"] == 0.8 and s["objective"] == 0.5           # majority family no longer dominates
    assert s["groups"]["image_crash"]["n"] == 2 and s["n"] == 10
    qs = [T.Query(i, T0, "w", {}, [], [], frozenset({"a"}), group="endpoint_loss" if i < 12 else "oom") for i in range(15)]
    groups = T.groups_of(qs)
    assert groups[0] == "endpoint_loss" and groups[13] == "other"             # 3 oom queries pool into `other`


def test_a_family_regression_beyond_tolerance_blocks_application():
    cur, new = T.DEFAULT, T.RetrievalConfig(query_mode="findings")
    fast, big = {"current_p95_s": 0.1, "best_p95_s": 0.1}, {"train": 30, "holdout": 30}
    res = {cur.key(): {"holdout": {"objective": 0.4, "hit": 0.7, "groups": {
               "endpoint_loss": {"n": 20, "hit": 0.6}, "image_crash": {"n": 6, "hit": 0.8}, "oom": {"n": 2, "hit": 1.0}}}},
           new.key(): {"holdout": {"objective": 0.6, "hit": 0.9, "groups": {
               "endpoint_loss": {"n": 20, "hit": 1.0}, "image_crash": {"n": 6, "hit": 0.7}, "oom": {"n": 2, "hit": 0.0}}}}}
    ok, why = T.decide(res, new, cur, fast, big)
    assert not ok and why == ["held-out hit@k of family image_crash drops 0.8 -> 0.7"]   # oom: too few to judge
    res[new.key()]["holdout"]["groups"]["image_crash"]["hit"] = 0.76                       # within tolerance
    assert T.decide(res, new, cur, fast, big)[0]


def test_ties_prefer_the_fewest_changes_from_the_current_configuration():
    a = T.RetrievalConfig(query_mode="findings", mode="lexical", k=2)
    b = T.RetrievalConfig(query_mode="findings", mode="lexical", k=2, candidates=60, past_incident_cap=2)
    res = {c.key(): {"train": {"objective": 0.7}} for c in (a, b)}
    res[T.DEFAULT.key()] = {"train": {"objective": 0.5}}
    assert T.choose(res, [b, a], T.DEFAULT) == a and T.distance(a, T.DEFAULT) == 3


def test_selection_only_considers_configurations_meeting_the_bars_on_train():
    cur, greedy, safe = T.DEFAULT, T.RetrievalConfig(k=2), T.RetrievalConfig(query_mode="findings")
    g = lambda hit: {"endpoint_loss": {"n": 30, "hit": 0.9}, "(none)": {"n": 8, "hit": hit}}  # noqa: E731
    res = {cur.key(): {"train": {"objective": 0.5, "hit": 0.8, "groups": g(0.9)}},
           greedy.key(): {"train": {"objective": 0.8, "hit": 0.9, "groups": g(0.5)}},      # best score, regresses
           safe.key(): {"train": {"objective": 0.7, "hit": 0.9, "groups": g(0.9)}}}
    assert T.choose(res, [greedy, safe], cur) == safe
    assert T.hit_regressions(res[greedy.key()]["train"], res[cur.key()]["train"]) == [
        "hit@k of family (none) drops 0.9 -> 0.5"]


def test_signal_families_follow_the_watcher_and_cover_oom():
    from app.incidents import FAMILY_HINT, REASON_CLASS
    for reason, rclass in REASON_CLASS.items():                    # the watcher's own classes, unchanged
        assert T.signal_families([reason]) == sorted(T.canonical_families([FAMILY_HINT[rclass]])), reason
    assert T.signal_families(["NetworkDrop", "TargetWorkloadOOMKilled", "TargetHigh5xxRate", "OOMKilled", 7]) == [
        "oom", "http_5xx"]
    assert T.families_of("endpoint_loss", ["TargetWorkloadOOMKilled"]) == ({"endpoint_loss"}, "endpoint_loss", False)
    assert T.families_of(None, ["NetworkDrop", "TargetWorkloadOOMKilled"]) == ({"oom"}, "oom", True)
    assert T.families_of("application", ["NetworkDrop"]) == (set(), "(none)", False)


def test_split_holds_out_the_newest_share_of_every_family():
    def q(i, hours, group):
        return T.Query(i, T0 + timedelta(hours=hours), "w", {}, [], [], frozenset({"a"}), group=group)
    old_oom = [q(i, i, "oom") for i in range(20)]                               # a month older than the rest
    new_ep = [q(100 + i, 720 + i, "endpoint_loss") for i in range(20)]
    small = [q(200 + i, 960 + i, g) for i, g in enumerate(["x"] * 3 + ["y"] * 4)]  # pool into `other`
    dataset = old_oom + new_ep + small
    train, holdout = T.split(dataset)
    assert {x.incident_id for x in holdout} == set(range(14, 20)) | set(range(114, 120)) | {204, 205, 206}
    assert len(train) + len(holdout) == len(dataset) and [x.incident_id for x in train] == sorted(x.incident_id for x in train)
