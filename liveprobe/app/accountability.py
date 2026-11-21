"""Append-only explanations of why an incident opened and how service recovered."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import exists, func, select

from . import db
from .auth import require_user
from .sse import broker
from .flowgraph import signal_ko
from .k8s import parse_ts
from .lifecycle import transition, update_run
from .models import Action, AgentRun, AuditLog, Evidence, Incident
from .redact import redact

REPORT = "recovery_accountability"
HISTORICAL = ("resolved", "closed", "failed")
AWAITING = "recovery_review"
WAITING_LABEL = "서비스 복구 관측 · 운영자 소명 확인 대기"


def _stamp(value):
    return value.isoformat() if value else None


def _last_report(session, iid):
    return session.scalar(select(AuditLog).where(AuditLog.incident_id == iid, AuditLog.event == REPORT)
                          .order_by(AuditLog.id.desc()).limit(1))


def _initial(incident):
    observations = ((incident.handoff or {}).get("observed_recovery") or {}).get("observations") or []
    return {
        "status": "unreviewed" if incident.status in HISTORICAL or incident.status == AWAITING else "not_applicable",
        "awaiting_closure": incident.status == AWAITING,
        "detection_ko": (f"{incident.namespace}/{incident.workload}에서 {signal_ko(incident.signal_reasons)} 신호가 기록됐습니다."
                         if incident.signal_reasons else "당시 감지 신호 미기록"),
        "recovery_ko": "정상화 원인을 설명하는 소명 기록이 없습니다.",
        "recovery_observed_at": observations[-1].get("observed_at") if observations else None,
        "limits_ko": "종료 상태만으로 원인을 확정할 수 없습니다.",
        "evidence_refs": [], "decision_at": None, "decision_by": None, "decision_note": None, "revision": 0,
    }


def view(session, incident):
    last = _last_report(session, incident.id)
    if last:
        return {**last.detail, "revision": last.id}
    if incident.status in HISTORICAL:
        recovered = investigate(session, incident)
        if recovered["status"] == "explained":
            return recovered
    return _initial(incident)


def append(session, iid, report, actor):
    data = {k: v for k, v in report.items() if k != "revision"}
    row = db.audit(session, actor, REPORT, iid, **data)
    return {**row.detail, "revision": row.id}


def action_explanation(incident, action):
    """Prove a matching symptom, mutation of its causal input and a verified target state."""
    if (not action or not action.executed or action.status != "succeeded" or action.incident_id != incident.id
            or action.target != f"{incident.namespace}/Deployment/{incident.workload}"):
        return None
    if (not incident.created_at or not action.created_at or not action.updated_at
            or not (incident.created_at <= action.created_at <= action.updated_at)
            or (incident.closed_at and action.updated_at > incident.closed_at)):
        return None
    before, after, params = action.before_snapshot or {}, action.after_state or {}, action.params or {}
    signals = set(incident.signal_reasons or ())
    if not signals or not before or not after or not action.updated_at:
        return None
    oom_signals = {"OOMKilled", "OOMKilling", "TargetWorkloadOOMKilled"}
    rollout_symptoms = {"TargetWorkloadNotReady"}
    oom_symptoms = rollout_symptoms | {"CrashLoopBackOff", "TargetWorkloadCrashLooping"}
    if action.type == "set_memory_limit" and signals & oom_signals and signals <= oom_signals | oom_symptoms:
        checks = after.get("checks") or {}
        changed = (params.get("applied") or {}).get("memory_limit")
        from .actions import Denied, parse_mem_mi
        try:
            increased = parse_mem_mi(changed) > parse_mem_mi(before.get("memory_limit"))
        except Denied:
            return None
        if increased and checks.get("rollout_healthy") is True and checks.get("limit_applied") is True:
            return f"메모리 부족 종료가 기록된 {action.target}의 한도를 {before['memory_limit']}에서 {changed}로 변경하고 새 값과 Pod 준비 상태를 검증했습니다."
    image_signals = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "TargetWorkloadImagePullFailing"}
    if action.type == "rollout_undo" and signals & image_signals and signals <= image_signals | rollout_symptoms:
        images = params.get("to_images")
        if (params.get("to_revision") and before.get("revision") and params["to_revision"] != before["revision"]
                and images and before.get("images") and before["images"] != images and after.get("images") == images
                and after.get("waiting_reasons") == [] and (after.get("replicas") or 0) > 0
                and after.get("ready_pods") == after["replicas"]):
            return f"이미지 가져오기에 실패한 {action.target}을 리비전 {params.get('to_revision')}으로 되돌렸고 변경된 이미지에서 모든 Pod가 준비됐습니다."
    return None


def _selector_repair(session, incident, observations):
    # A selector change with different or recreated Pods is ambiguous. Match the Service
    # UID and unchanged Pod identities/labels at the failed and two recovered observations.
    first, final = observations[0], observations[-1]
    uid, selector, labels = first.get("service_uid"), first.get("service_selector"), first.get("pod_labels")
    if (not uid or not selector or not labels or not all(isinstance(p, dict) for p in labels.values())
            or any(final.get(k) != first.get(k) for k in ("service_uid", "service_selector", "pod_labels"))):
        return None
    def matches(criteria, pod):
        return all(pod.get(k) == v for k, v in criteria.items())
    if not all(matches(selector, pod) for pod in labels.values()):
        return None
    end = parse_ts(first.get("observed_at"))
    if not end:
        return None
    query = select(Evidence).where(Evidence.incident_id == incident.id, Evidence.error.is_(None),
        Evidence.collected_at >= incident.created_at, Evidence.collected_at <= end)
    outages = session.scalars(query.where(Evidence.source == "service",
        Evidence.target == f"{incident.namespace}/Service/{incident.workload}").order_by(Evidence.collected_at.desc()))
    for row in outages:
        prior = row.payload or {}
        old_selector = prior.get("selector")
        if (prior.get("uid") != uid or prior.get("ready_endpoints") != 0 or prior.get("matching_pods") != []
                or not old_selector or any(matches(old_selector, pod) for pod in labels.values())):
            continue
        # Pod and Service come from one Kubernetes collection but are timestamped separately.
        snapshot = session.scalar(query.where(Evidence.source == "pod_status",
            Evidence.collected_at.between(row.collected_at - timedelta(seconds=3), row.collected_at + timedelta(seconds=3)))
            .order_by(func.abs(func.extract("epoch", Evidence.collected_at - row.collected_at))).limit(1))
        if not snapshot or not isinstance(snapshot.payload, list):
            continue
        same_pods = {p.get("uid"): p.get("labels") for p in snapshot.payload if isinstance(p, dict)}
        if same_pods == labels:
            return {"reason": "Service 선택자가 준비된 Pod의 라벨과 일치하도록 변경됐습니다. 같은 Pod가 두 후속 관측에서 연결 대상으로 확인됐습니다.",
                    "refs": [f"선택 대상이 없던 Service #{row.id}", f"당시 Pod 라벨 #{snapshot.id}"]}
    return None


def investigate(session, incident):
    """Use incident-time records. A currently healthy resource proves no historical cause."""
    result = _initial(incident)
    signal = session.scalar(select(AuditLog).where(AuditLog.incident_id == incident.id,
        AuditLog.event.in_(("signal_observed", "incident_created"))).order_by(AuditLog.id).limit(1))
    if signal:
        result["evidence_refs"].append(f"감지 기록 #{signal.id}")
    action = session.scalar(select(Action).where(Action.incident_id == incident.id,
        Action.status == "succeeded", Action.executed.is_(True)).order_by(Action.id.desc()).limit(1))
    why = action_explanation(incident, action)
    if why:
        result.update(status="explained", recovery_observed_at=_stamp(action.updated_at),
                      recovery_ko=why,
                      limits_ko="")
        result["evidence_refs"].append(f"검증된 조치 #{action.id}")
        return result

    proof = (incident.handoff or {}).get("observed_recovery") or {}
    observations = proof.get("observations") or []
    result.update(status="pending_operator", recovery_ko="정상 상태는 관측했지만 정상화의 원인은 입증되지 않았습니다."
                  if observations else "저장된 자료에서 정상화 시점과 원인을 입증하지 못했습니다.",
                  limits_ko="저장된 감지 기록, 변경 전후가 검증된 조치, 사건 시간대의 Pod 이벤트를 대조했습니다.")
    # Report only exclusions which the stored independent review actually recorded.
    review = session.scalar(select(AgentRun).where(AgentRun.incident_id == incident.id,
        AgentRun.role.in_(("group_verifier", "result_verifier")), AgentRun.status == "succeeded")
        .order_by(AgentRun.id.desc()).limit(1))
    decisions = (review.output or {}).get("candidate_verdicts") if review else None
    rejected = [redact(str(row.get("reason_ko") or row.get("reason") or ""))[:240] for row in decisions or []
                if isinstance(row, dict) and row.get("verdict") == "contradicted"]
    if any(rejected):
        result["limits_ko"] += " 독립 검증에서 배제한 근거: " + " / ".join(r for r in rejected[:3] if r)
        result["evidence_refs"].append(f"독립 검증 #{review.id}")
    if len(observations) < 2:
        return result
    if incident.signal_reasons and set(incident.signal_reasons) <= {"NoEndpoints", "TargetServiceNoEndpoints"}:
        repair = _selector_repair(session, incident, observations)
        if repair:
            result.update(status="explained", recovery_ko=repair["reason"], limits_ko="")
            result["evidence_refs"].extend(repair["refs"])
        return result
    rows = list(session.scalars(select(Evidence).where(Evidence.incident_id == incident.id,
        Evidence.source == "k8s_event", Evidence.error.is_(None)).order_by(Evidence.id)))
    result["evidence_refs"].extend(f"이벤트 수집 #{row.id}" for row in rows[-2:])
    # Establish the same image and Pod UID across a documented failed pull then successful pull.
    # A generic Ready event or a coincident rollout alone is insufficient.
    if not incident.signal_reasons or set(incident.signal_reasons) - {"ImagePullBackOff", "ErrImagePull", "TargetWorkloadImagePullFailing"}:
        return result
    start, end = incident.created_at, parse_ts(observations[-1].get("observed_at"))
    if not end or not start:
        return result
    original_pods = {(c.get("pod"), c.get("pod_uid")) for c in observations[0].get("containers") or []}
    pods = original_pods & {(c.get("pod"), c.get("pod_uid")) for c in observations[-1].get("containers") or []}
    events = {}
    for row in rows:
        for event in row.payload if isinstance(row.payload, list) else []:
            if not isinstance(event, dict):
                continue
            obj, uid = event.get("object") or "", event.get("uid")
            if not uid or (obj.removeprefix("Pod/"), uid) not in pods or not obj.startswith("Pod/"):
                continue
            at = parse_ts(event.get("last"))
            if at is None or not (start <= at <= end):
                continue
            msg = event.get("message") or ""
            match = re.search(r'(?:Failed to pull image|Successfully pulled image) "([^"]+)"', msg)
            if not match:
                continue
            key = (uid, obj, match.group(1))
            events.setdefault(key, []).append((at, event.get("reason"), msg, row.id))
    for (_, obj, image), history in events.items():
        for fail, reason, msg, first_id in history:
            if reason != "Failed" or not msg.startswith("Failed to pull image"):
                continue
            success = next((item for item in history if item[0] > fail and item[1] == "Pulled"
                            and item[2].startswith("Successfully pulled image")), None)
            if success:
                result.update(status="explained", limits_ko="레지스트리가 최초 요청을 거절한 내부 사유는 별도 관측입니다.",
                              recovery_ko=f"{obj}에서 실패했던 이미지 {image}를 다시 받아 실행했고 정상 상태가 이어졌습니다.")
                result["evidence_refs"] += [f"이미지 가져오기 실패 #{first_id}", f"동일 이미지 수신 #{success[3]}"]
                return result
    return result


class ReviewDecision(BaseModel):
    revision: int = Field(ge=0)
    note: str | None = Field(None, max_length=2000)


def accountability_router():
    router = APIRouter(dependencies=[Depends(require_user)])

    @router.get("/api/reviews/unexplained")
    def unexplained(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        with db.session() as s:
            done = exists(select(AuditLog.id).where(AuditLog.incident_id == Incident.id,
                AuditLog.event == REPORT, AuditLog.detail["status"].astext.in_(("explained", "confirmed"))))
            query = select(Incident).where(Incident.status.in_((*HISTORICAL, AWAITING)), ~done)
            # Legacy cases have no explanation row. Only successful recorded changes and
            # observations with Pod identities can satisfy either form of causal proof.
            has_report = exists(select(AuditLog.id).where(AuditLog.incident_id == Incident.id, AuditLog.event == REPORT))
            last_action = (select(func.max(Action.id)).where(Action.incident_id == Incident.id,
                           Action.status == "succeeded", Action.executed.is_(True)).correlate(Incident).scalar_subquery())
            actions = s.execute(select(Incident, Action).join(Action, Action.id == last_action)
                .where(Incident.status.in_(HISTORICAL), ~has_report, Action.type.in_(("set_memory_limit", "rollout_undo"))))
            explained = {case.id for case, action in actions if action_explanation(case, action)}
            observation = Incident.handoff["observed_recovery"]["observations"][0]["containers"][0]["pod_uid"].astext
            observed = s.scalars(select(Incident).where(Incident.status.in_(HISTORICAL), ~has_report, observation.is_not(None)))
            for case in observed:
                if case.id not in explained and investigate(s, case)["status"] == "explained":
                    explained.add(case.id)
            if explained:
                query = query.where(Incident.id.not_in(explained))
            rows = s.scalars(query.order_by(Incident.id.desc()).offset(offset).limit(limit))
            return {"total": s.scalar(select(func.count()).select_from(query.subquery())), "items": [
                {"incident_id": i.id, "title": i.title, "namespace": i.namespace, "workload": i.workload,
                 "status": i.status, "closed_at": _stamp(i.closed_at)} for i in rows]}

    @router.get("/api/incidents/{iid}/accountability")
    def get(iid: int):
        with db.session() as s:
            incident = s.get(Incident, iid)
            if incident is None:
                raise HTTPException(404, "incident not found")
            return view(s, incident)

    @router.post("/api/incidents/{iid}/accountability/{action}")
    def decide(iid: int, action: str, body: ReviewDecision, user: str = Depends(require_user)):
        if action not in ("reinvestigate", "confirm"):
            raise HTTPException(404, "unknown review action")
        with db.session() as s:
            incident = s.scalar(select(Incident).where(Incident.id == iid).with_for_update())
            if incident is None:
                raise HTTPException(404, "incident not found")
            current = view(s, incident)
            if current["revision"] != body.revision or current["status"] not in ("unreviewed", "pending_operator"):
                raise HTTPException(409, "explanation changed or unavailable")
            if action == "reinvestigate":
                report = investigate(s, incident)
            elif current["status"] == "pending_operator":
                report = {**current, "status": "confirmed", "decision_at": _stamp(datetime.now(timezone.utc)),
                          "decision_by": user, "decision_note": redact(body.note.strip()) if body.note else None}
            else:
                raise HTTPException(409, "investigation required before confirmation")
            if incident.status == AWAITING and report["status"] in ("confirmed", "explained"):
                transition(incident, "resolved")
                incident.status_reason = "정상 · 복구 소명 확인"
                update_run(s, iid, "succeeded", "resolved", incident.status_reason)
            result = append(s, iid, report, user)
        broker.publish(iid, "updated")
        return result

    return router
