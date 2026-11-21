"""Follow a reviewer's open question with bounded, read-only evidence collection."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

from sqlalchemy import select

from . import db, runlog
from .evidence import COLLECT_TIMEOUT_S, collect, compact_summary
from .k8s import parse_ts
from .models import AgentRun, Evidence
from .redact import redact_obj

QUESTIONS = {
    "E2_pods": "같은 Pod의 준비 상태나 종료 사유가 가설을 뒷받침합니까?",
    "E3_service": "서비스의 선택 대상이나 준비된 엔드포인트가 달라졌습니까?",
    "E6_metrics": "오류·부하 지표에 원인 후보를 구별할 변화가 있습니까?",
    "E7_error_logs": "사건 구간의 로그나 예외 기록에서 후보를 검증할 수 있습니까?",
    "E1_deployment": "배포 버전과 변경 이력이 실패 시점과 일치합니까?",
}
EXTRA = ("E5_recent_events", "E8_code_refs", "E9_config")
VOLATILE_KEYS = {"collected_at", "observed_at", "observed_ts", "resource_version"}


def fingerprint(value):
    def clean(v):
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items() if k not in VOLATILE_KEYS}
        if isinstance(v, list):
            return [clean(x) for x in v]
        return v
    return hashlib.sha256(json.dumps(clean(value), ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


class EvidenceInquiry:
    def __init__(self, engine, iid, bundle, incident, store, controller):
        self.engine, self.iid, self.bundle = engine, iid, bundle
        self.incident, self.store, self.controller = incident, store, controller
        self.attempts = []
        self.seen = {k: {fingerprint(store.sources.get(k))} for k in (*QUESTIONS, *EXTRA)}
        self.seen["source_snapshot"] = set()

    def __call__(self, state, role):
        ctl = self.controller
        deadline = ctl.t0 + ctl.policy.deadline_s
        if (len(self.attempts) >= ctl.policy.max_rounds or ctl.calls >= ctl.policy.max_agent_calls[ctl.level]
                or ctl.spent >= ctl.policy.max_cost_usd[ctl.level] or ctl.clock() + COLLECT_TIMEOUT_S >= deadline):
            return False
        cause = state["msgs"].get(state.get("primary_role")) or {}
        refs = [key for c in cause.get("root_cause_candidates") or [] for key in c.get("evidence_refs") or []]
        referenced_sources = {self.store.key(ref) for ref in refs}
        review = state["msgs"].get(role) or {}
        gaps = " ".join(str(v) for v in [*(review.get("recommended_steps") or []),
                        *(review.get("counter_evidence") or [])]).lower()
        topics = {"E6_metrics": ("metric", "prometheus", "latency", "지표", "부하"),
                  "E1_deployment": ("revision", "deploy", "배포", "버전"),
                  "E7_error_logs": ("log", "exception", "stack", "source", "로그", "소스", "예외"),
                  "E3_service": ("endpoint", "selector", "연결 대상", "선택자"),
                  "E2_pods": ("pod", "container", "메모리", "재시작")}
        requested = [k for k, words in topics.items() if any(word in gaps for word in words)]
        ordered = list(dict.fromkeys([*requested, *(k for k in QUESTIONS if k in referenced_sources)])) or ["E7_error_logs"]
        key = next((k for k in ordered if k not in [a["source"] for a in self.attempts]), None)
        if not key:
            return False
        runlog.gate(self.iid, "반론 근거 추가 탐색")
        with db.session() as session:
            reviewer = session.scalar(select(AgentRun.id).where(AgentRun.incident_id == self.iid,
                AgentRun.role == role).order_by(AgentRun.id.desc()).limit(1))
        question = QUESTIONS[key]
        start = runlog.emit(self.iid, "investigation_started", question, {
            "parent_run_id": reviewer, "source": key, "question_ko": question,
            "review_role": role})
        entry = {"source": key, "question_ko": question, "evidence_refs": []}
        self.attempts.append(entry)
        since = parse_ts(self.incident.get("created_at")) or datetime.now(timezone.utc)
        fresh = collect(self.engine.kube, self.engine.cfg, self.engine.settings,
                        self.bundle.namespace, self.bundle.workload, since=since)
        # The external source lease pins the image digest of this newly observed Deployment.
        self.engine._save_observation(self.iid, fresh, "counter_evidence_search")
        summary = compact_summary(fresh)
        keys = list(dict.fromkeys([key, *ordered, *EXTRA]))
        facts = {k: summary[k] for k in keys if summary.get(k) not in (None, {}, [])}
        # Leave a reviewer call inside the same group deadline after the external source read.
        source_deadline = deadline - min(ctl.cfg.llm.timeout_s, ctl.policy.deadline_s / 3)
        if (fresh.code_refs or {}).get("frames") and ctl.clock() < source_deadline:
            from .source_investigation import read_source_evidence
            try:
                receipt = read_source_evidence(self.iid, question, fresh.code_refs, source_deadline, investigation_seq=start)
            except runlog.RunStopped:
                raise
            except Exception:
                # External source storage can be unavailable independently of the incident database.
                receipt = None
            entry["source_status_ko"] = "배포 소스 대조 완료" if receipt else "기한 내 검증된 배포 소스 미관측"
            if receipt:
                facts["source_snapshot"] = receipt
        delta = {k: v for k, v in facts.items() if fingerprint(v) not in self.seen[k]}
        if delta:
            for k, v in delta.items():
                self.seen[k].add(fingerprint(v))
            # Preserve older evidence/citations. Every follow-up has a separate stable reference.
            ref = f"E{9 + len(self.attempts)}_investigation"
            receipt = redact_obj({"question_ko": question, "findings": delta})
            with db.session() as session:
                evidence = Evidence(incident_id=self.iid, source="investigation",
                    target=f"{self.bundle.namespace}/{self.bundle.workload}", payload={"ref": ref, **receipt})
                session.add(evidence)
                session.flush()
                entry["evidence_refs"] = [ref]
                entry["evidence_id"] = evidence.id
            self.store.sources[ref] = receipt
            self.store.invalidate_views()
            self.engine._investigation_seq = start
        result = "새 근거가 수집되어 원인 추론과 반론을 다시 검토합니다." if delta else "새로운 구별 근거가 없어 같은 조사를 반복하지 않습니다."
        entry["result_ko"] = result
        runlog.emit(self.iid, "investigation_finished", result, {"investigation_seq": start, **entry})
        self.engine._request_readings(self.iid)
        return bool(delta)
