"""P15: the incident's agent flow as nodes and edges for the war-room graph.

Nodes come only from persisted facts (routing decisions, agent runs, run events, actions, operator messages and
commands); not-yet-run roles of the active group are shown as `planned`. Layout hints: `depth` is the column
(left -> right), `lane` the row inside a column. No hidden reasoning is shown, only recorded outputs.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import select

from .models import (INCIDENT_CLOSED_STATES, Action, AgentRun, Evidence, Incident, Interpretation, OperatorCommand, OperatorMessage, RoutingDecision,
                     RunEvent)
from .roles_ko import GROUP_KO, role_info

STAGES: dict[str, list[list[str]]] = {
    "SIMPLE": [["signal_analyst"], ["result_verifier"]],
    "NORMAL": [["k8s_analyst", "log_summarizer"], ["coordinator"], ["group_verifier"]],
    "COMPLEX": [["k8s_log_analyst"], ["cause_reasoner"], ["challenger", "recovery_planner"], ["group_verifier"]],
    "HIGH_RISK": [["evidence_curator"], ["safety_verifier"]],
}
VERIFY_ROLES = {"result_verifier", "group_verifier", "safety_verifier"}
ASSIST_ROLES = {"router", "interpreter", "steering", "operator_planner"}
FULL_REF = {"E1": "E1_deployment", "E2": "E2_pods", "E3": "E3_service", "E4": "E4_callers", "E5": "E5_recent_events",
            "E6": "E6_metrics", "E7": "E7_error_logs", "E8": "E8_node", "E9": "E9_config"}
ACTION_KO = {"rollout_undo": "이전 버전으로 롤백", "rollout_restart": "Deployment 재시작", "pod_delete": "Pod 재생성",
             "scale_replicas": "레플리카 수 조정", "set_memory_limit": "메모리 한도 조정", "configmap_restore": "ConfigMap 복원",
             "pvc_reference_restore": "Deployment의 PVC 참조 복원"}
SIGNAL_KO = {
    "TargetWorkloadOOMKilled": "메모리 초과 강제 종료(OOMKilled)", "OOMKilled": "메모리 초과 강제 종료(OOMKilled)",
    "TargetWorkloadImagePullFailing": "이미지 내려받기 실패", "ErrImagePull": "이미지 내려받기 실패",
    "ImagePullBackOff": "이미지 내려받기 실패", "TargetWorkloadCrashLooping": "반복 재시작(CrashLoop)",
    "CrashLoopBackOff": "반복 재시작(CrashLoop)", "NoEndpoints": "Service 연결 대상 없음",
    "TargetServiceNoEndpoints": "Service 연결 대상 없음", "TargetHighLatency": "응답 지연",
    "TargetRouteSlowRequests": "업무 경로 응답 지연",
    "TargetProxyListenerUpdateRejected": "Envoy 리스너 설정 반영 실패",
    "TargetConnectionPoolExhausted": "DB 연결 풀 고갈", "OrderHigh5xxRate": "5xx 오류 증가",
    "InventoryHigh5xxRate": "5xx 오류 증가", "NetworkDrop": "네트워크 차단",
    "TargetHigh5xxRate": "5xx 오류 증가", "ApplicationException": "애플리케이션 예외",
    "TargetWorkloadNotReady": "준비되지 않은 워크로드", "Unhealthy": "상태 확인(probe) 실패",
    "FailedScheduling": "Pod 노드 배치 실패", "RequiredContainerReferenceMissing": "컨테이너 필수 설정 누락",
    "ProgressDeadlineExceeded": "Deployment 롤아웃 기한 초과",
    "ReplicaCreationFailed": "ReplicaSet의 Pod 생성 실패", "FailedMount": "영속 볼륨 마운트 실패",
    "AdmissionWebhookCallFailed": "Kubernetes 승인 웹훅 호출 실패",
    "RetiredIngressApiRejected": "종료된 Ingress API 사용으로 적용 실패",
    "CniVersionMismatch": "노드 CNI 버전 불일치",
    "NodeStartupBlocked": "노드 시작 실패로 미등록",
    "NodeDiskPressureEviction": "노드 디스크 압박으로 Pod 퇴출",
    "ResourceMetricsHpaBlocked": "자원 지표 API 장애로 자동 확장 계산 중단",
}
COMMAND_STATUS = {"planning": "running", "awaiting_confirmation": "running", "awaiting_secret": "running",
                  "executing": "running", "succeeded": "done", "rolled_back": "done", "rolling_back": "running",
                  "failed": "failed", "rejected": "failed", "cancelled": "skipped"}


def _korean(text: str | None) -> bool:
    from .readings_ko import is_korean
    return is_korean(text or "")


def signal_ko(reasons: list[str] | None) -> str:
    seen: list[str] = []
    for r in reasons or []:
        ko = SIGNAL_KO.get(str(r), str(r))
        if ko not in seen:
            seen.append(ko)
    return ", ".join(seen) or "이상 신호"


def refs_of(output: dict | None) -> list[str]:
    out: list[str] = []
    o = output or {}
    raw = [e.get("ref") for e in (o.get("evidence") or []) if isinstance(e, dict)]
    for c in (o.get("root_cause_candidates") or o.get("candidates") or []):
        raw += list(c.get("evidence_refs") or [])
    for r in raw:
        r = str(r or "").strip()
        base = r.split("_", 1)[0].split("#", 1)[0]
        full = FULL_REF.get(base) or (r if r.startswith("R") else None)
        if full and full not in out:
            out.append(full)
    return out


def _conclusion(o: dict | None) -> str | None:
    if not o:
        return None
    text = str(o.get("summary") or "")
    for c in (o.get("root_cause_candidates") or o.get("candidates") or [])[:3]:
        cause = c.get("cause") or c.get("statement")
        if cause:
            text += f"\n- {cause} (confidence {c.get('confidence')})"
    return text.strip() or None


class _G:
    def __init__(self):
        self.nodes: list[dict] = []
        self.edges: list[dict] = []
        self.at_depth: dict[int, int] = defaultdict(int)

    def node(self, nid: str, type_: str, label: str, depth: int, *, lane: int | None = None, status: str = "done",
             role: str | None = None, group: str | None = None, **extra) -> dict:
        info = role_info(role) if role else {"role_ko": label, "job_ko": ""}
        if lane is None:
            lane = self.at_depth[depth]
        self.at_depth[depth] = max(self.at_depth[depth], lane + 1)
        n = {"id": nid, "type": type_, "label": label, "role": role, "role_ko": info["role_ko"], "job_ko": info["job_ko"],
             "status": status, "model": None, "effort": None, "tokens": None, "cost_usd": None, "duration_ms": None,
             "inputs": [], "conclusion_ko": None, "conclusion_raw": None, "lane": lane, "depth": depth, "group": group,
             "at": None, "card": None}
        n.update(extra)
        self.nodes.append(n)
        return n

    def edge(self, a: str, b: str, kind: str = "flow", label: str | None = None) -> None:
        eid = f"{a}->{b}:{kind}"
        if a != b and not any(e["id"] == eid for e in self.edges):
            self.edges.append({"id": eid, "source": a, "target": b, "kind": kind, "label": label})


def _run_node(g: _G, r: AgentRun, depth: int, lane: int | None, group: str | None, ko: dict[str, str],
              status: str | None = None) -> dict:
    o = r.output or {}
    st = status or ("failed" if r.status == "failed" else "done")
    return g.node(f"run:{r.id}", "verify" if r.role in VERIFY_ROLES else "agent", role_info(r.role)["role_ko"], depth,
                  lane=lane, status=st, role=r.role, group=group, model=r.model, effort=r.reasoning_effort,
                  tokens={"in": r.input_tokens, "out": r.output_tokens} if r.input_tokens is not None else None,
                  cost_usd=r.estimated_cost_usd, duration_ms=r.duration_ms, inputs=refs_of(o),
                  conclusion_raw=_conclusion(o) or (r.error if r.status == "failed" else None),
                  conclusion_ko=_run_ko(r, o, ko), agent_run_id=r.id)


def _run_ko(r: AgentRun, o: dict, ko: dict[str, str]) -> str | None:
    """표시용 한국어 결론: 모델이 함께 쓴 summary_ko → 캐시된 해석 → 한국어 원문 → 실패 사유 고정 문장 순서."""
    from .readings_ko import error_ko, is_korean, korean_self_ko
    if isinstance(o.get("summary_ko"), str) and is_korean(o["summary_ko"]):
        return o["summary_ko"].strip()
    if ko.get(f"agent:{r.id}"):
        return ko[f"agent:{r.id}"]
    own = korean_self_ko(_conclusion(o) or "")
    if own:
        return own
    if r.status == "failed" and r.error:
        return error_ko(r.error)
    return None


def build_flow(s, iid: int, items: list[dict] | None = None) -> dict[str, Any]:
    """`items` are the incident's display items with readings (interpret.readings); computed here when omitted."""
    inc = s.get(Incident, iid)
    if inc is None:
        return {"nodes": [], "edges": []}
    from .models import AuditLog
    routing = list(s.scalars(select(RoutingDecision).where(RoutingDecision.incident_id == iid).order_by(RoutingDecision.id)))
    runs = list(s.scalars(select(AgentRun).where(AgentRun.incident_id == iid).order_by(AgentRun.id)))
    events = list(s.scalars(select(RunEvent).where(RunEvent.incident_id == iid).order_by(RunEvent.seq)))
    actions = list(s.scalars(select(Action).where(Action.incident_id == iid).order_by(Action.id)))
    commands = list(s.scalars(select(OperatorCommand).where(OperatorCommand.incident_id == iid).order_by(OperatorCommand.id)))
    messages = list(s.scalars(select(OperatorMessage).where(OperatorMessage.incident_id == iid).order_by(OperatorMessage.id)))
    evidence = list(s.scalars(select(Evidence).where(Evidence.incident_id == iid).order_by(Evidence.id)))
    audits = list(s.scalars(select(AuditLog).where(AuditLog.incident_id == iid).order_by(AuditLog.ts, AuditLog.id)))
    if items is None:
        from .interpret import incident_items, readings
        items = readings(s, iid, incident_items(s, iid))
    n_evidence = bool(evidence)
    ko = {i["id"]: i["ko"] for i in items if i.get("ko") and i["id"].startswith("agent:")}
    finished = inc.status in INCIDENT_CLOSED_STATES
    g = _G()

    g.node("signal", "signal", f"감지 신호: {signal_ko(inc.signal_reasons)}", 0, status="done")
    ev_count = len(evidence)
    g.node("evidence", "evidence", f"근거 수집 {ev_count}건" if n_evidence else "근거 수집", 1,
           status="done" if n_evidence else ("failed" if finished else "running"))
    g.edge("signal", "evidence")
    from .routing_ko import classifier_was_skipped
    router_run = (None if routing and classifier_was_skipped(routing[0])
                  else next((r for r in runs if r.role == "router"), None))
    started = Counter(e.detail.get("role") for e in events if e.kind == "agent_started" and isinstance(e.detail, dict))
    finished_runs = Counter(r.role for r in runs)
    if router_run is not None:
        _run_node(g, router_run, 2, 0, None, ko)
        g.nodes[-1].update(id="router", type="router", label="사건 수준 판단")
    else:
        g.node("router", "router", "사건 수준 판단", 2, role="router",
               status="done" if routing else ("running" if started.get("router") else ("skipped" if finished else "planned")))
    g.edge("evidence", "router")

    escal_left = Counter(e.detail.get("role") for e in events if e.kind == "escalated" and isinstance(e.detail, dict))
    work = [r for r in runs if r.role not in ASSIST_ROLES]
    depth = 3
    prev_tail: list[str] = ["router"]
    last_role_node: dict[str, dict] = {}
    targeted_plans = {(m.intent_detail or {}).get("target_node") for m in messages
                      if isinstance(m.intent_detail, dict)}
    seg_nodes: list[tuple[Any, int, int, dict[str, dict]]] = []      # (routing, first node index, end index, role->node)
    for si, rd in enumerate(routing):
        seg_start = len(g.nodes)
        nxt = routing[si + 1].created_at if si + 1 < len(routing) else None
        seg = [r for r in work if (rd.created_at is None or r.created_at is None or r.created_at >= rd.created_at or si == 0)
               and (nxt is None or r.created_at is None or r.created_at < nxt)]
        if si > 0:
            seg = [r for r in seg if r.created_at is None or r.created_at >= rd.created_at]
        level = rd.level if rd.level in STAGES else "SIMPLE"
        gid = f"group:{rd.id}"
        g.node(gid, "group", f"{GROUP_KO.get(level, level)} 선택", depth, status="done" if (finished or si + 1 < len(routing)) else "running",
               group=level, decided_by=rd.decided_by, rationale=rd.rationale, confidence=rd.confidence)
        for t in prev_tail:
            g.edge(t, gid, "reroute" if si else "flow", ("운영자 재지정" if rd.decided_by == "operator" else "수준 재판단") if si else None)
        stages = STAGES[level]
        base = depth + 1
        pointer, shift = -1, 0
        stage_nodes: dict[int, list[dict]] = defaultdict(list)
        seg_role_node: dict[str, dict] = {}
        for r in seg:
            prev = seg_role_node.get(r.role)
            if prev is not None and escal_left.get(r.role, 0) > 0:
                escal_left[r.role] -= 1
                prev["status"] = "escalated"
                n = _run_node(g, r, prev["depth"], None, level, ko)
                g.edge(prev["id"], n["id"], "escalate", "상위 모델 승격")
                stage_nodes[prev["depth"]].append(n)
            else:
                sidx = next((i for i, roles in enumerate(stages) if r.role in roles and i > pointer), None)
                if sidx is None and prev is None:
                    sidx = next((i for i, roles in enumerate(stages) if r.role in roles), None)
                if sidx is None:      # a repeat outside its stage (e.g. COMPLEX revise) gets its own column
                    shift += 1
                    d = base + max(pointer, 0) + shift
                    lane = 0
                else:
                    pointer = max(pointer, sidx)
                    d = base + sidx + shift
                    lane = stages[sidx].index(r.role) if r.role in stages[sidx] else None
                    if lane is not None and any(x["lane"] == lane for x in stage_nodes[d]):
                        lane = None
                n = _run_node(g, r, d, lane, level, ko)
                stage_nodes[d].append(n)
            if prev is None and any(r.role in roles for roles in stages):
                n["aliases"] = [f"plan:{rd.id}:{r.role}"]
            seg_role_node[r.role] = n
            last_role_node[r.role] = n
        live = si + 1 == len(routing) and not finished
        if live or targeted_plans:
            for i, roles in enumerate(stages):
                for role in roles:
                    if role in seg_role_node or (not live and f"plan:{rd.id}:{role}" not in targeted_plans):
                        continue
                    running = started.get(role, 0) > finished_runs.get(role, 0)
                    d = base + i + shift
                    n = g.node(f"plan:{rd.id}:{role}", "verify" if role in VERIFY_ROLES else "agent", role_info(role)["role_ko"], d,
                               lane=roles.index(role) if not any(x["lane"] == roles.index(role) for x in stage_nodes[d]) else None,
                               status=("running" if running else "planned") if live else "skipped", role=role, group=level)
                    stage_nodes[d].append(n)
                    seg_role_node[role] = n
                    last_role_node.setdefault(role, n)
        cols = sorted(stage_nodes)
        tails = [gid]
        for d in cols:
            heads = [n for n in stage_nodes[d] if n["status"] != "escalated" or not any(
                e["source"] == n["id"] and e["kind"] == "escalate" for e in g.edges)]
            entry = [n for n in stage_nodes[d] if not any(e["target"] == n["id"] and e["kind"] == "escalate" for e in g.edges)]
            for t in tails:
                for n in entry:
                    g.edge(t, n["id"])
            tails = [n["id"] for n in heads] or tails
        prev_tail = tails
        depth = (max(cols) if cols else depth) + 1
        seg_nodes.append((rd, seg_start, len(g.nodes), seg_role_node))

    # operator steering ------------------------------------------------------------
    applied_to: dict[int, str | None] = {}
    for e in events:
        if e.kind == "operator_applied" and isinstance(e.detail, dict):
            for mid in e.detail.get("message_ids") or []:
                applied_to[mid] = e.detail.get("applied_to_role")
    for m in messages:
        target = None
        want = (m.intent_detail or {}).get("target_role") if isinstance(m.intent_detail, dict) else None
        role = applied_to.get(m.id)
        target_id = (m.intent_detail or {}).get("target_node") if isinstance(m.intent_detail, dict) else None
        if target_id:
            target = next((n for n in g.nodes if n["id"] == target_id or target_id in n.get("aliases", [])), None)
        if target is not None:
            pass
        elif want and want in last_role_node:
            target = last_role_node[want]
        elif role and role in last_role_node:
            target = last_role_node[role]
        elif m.applied_at is not None:
            target = next((n for n in g.nodes if n.get("agent_run_id") and any(
                r.id == n["agent_run_id"] and r.created_at and r.created_at >= m.applied_at for r in runs)), None)
        if target is None and m.applied_at is None:
            target = next((n for n in g.nodes if n["status"] in ("running", "planned") and n["type"] in ("agent", "verify")), None)
        if target is None:
            continue
        sn = g.node(f"steer:{m.id}", "steer", f"운영자 지시: {m.body[:40]}", target["depth"], lane=-1 - sum(
            1 for x in g.nodes if x["type"] == "steer" and x["depth"] == target["depth"]),
            status="done" if m.applied_at else "planned", intent=m.intent, conclusion_raw=m.body,
            conclusion_ko=m.body if _korean(m.body) else None,
            message_id=m.id, target_role=want, target_node=(m.intent_detail or {}).get("target_node") if want else None,
            applied_to_role=role)
        sn["at"] = _iso(m.created_at)
        g.edge(sn["id"], target["id"], "steer", "반영" if m.applied_at else "반영 예정")

    # actions and outcome -------------------------------------------------------------
    for a in actions:
        st = {"succeeded": "done", "failed": "failed", "denied": "failed", "rejected": "failed", "cancelled": "skipped",
              "pending_approval": "running", "executing": "running", "approved": "running"}.get(a.status, "planned")
        from .routing_ko import ACTION_STATUS_KO
        n = g.node(f"action:{a.id}", "action", f"조치: {ACTION_KO.get(a.type, a.type)}", depth, lane=0, status=st,
                   conclusion_raw=f"{a.type} {a.target} {a.params or ''} -> {a.status}", action_type=a.type,
                   conclusion_ko=f"{ACTION_KO.get(a.type, a.type)} · 대상 {a.target} · {ACTION_STATUS_KO.get(a.status, a.status)}")
        for t in prev_tail:
            g.edge(t, n["id"])
        prev_tail = [n["id"]]
        depth += 1
    label = {"resolved": "자동 복구 완료", "handed_off": "운영자 인계", "failed": "처리 실패", "closed": "운영자 종료", "recovery_review": "서비스 복구 관측 · 운영자 소명 확인 대기"}.get(inc.status, "결과 대기")
    from .hpa_metrics import RECOVERY_LABEL, RECOVERY_SCOPE
    if inc.status == 'recovery_review' and ((inc.handoff or {}).get('observed_recovery') or {}).get('scope') == RECOVERY_SCOPE:
        label = RECOVERY_LABEL
    if inc.status == 'resolved' and (inc.handoff or {}).get('observed_recovery'):
        label = inc.status_reason if (inc.status_reason or '').startswith('정상 ·') else '정상 상태 확인 · 원인 미확정'
    from .readings_ko import status_reason_ko
    g.node("outcome", "outcome", label, depth, lane=0,
           status="done" if inc.status in ("resolved", "handed_off", "closed", "recovery_review") else "failed" if inc.status == "failed" else "planned",
           conclusion_raw=inc.status_reason, conclusion_ko=status_reason_ko(inc.status_reason))
    for t in prev_tail:
        g.edge(t, "outcome")
    depth += 1
    prev = "outcome"
    for c in commands:
        n = g.node(f"command:{c.id}", "command", f"운영자 지시 실행: {c.instruction[:40]}", depth, lane=0,
                   status=COMMAND_STATUS.get(c.status, "planned"), conclusion_raw=c.instruction, command_status=c.status,
                   conclusion_ko=c.instruction if _korean(c.instruction) else None)
        n["at"] = _iso(c.created_at)
        g.edge(prev, n["id"], "steer")
        prev = n["id"]
        depth += 1
    attach_escalations(g.nodes, g.edges, events)
    _cards(g, inc, routing, runs, events, actions, evidence, audits, items, seg_nodes, routing_live=not finished,
           citations=_citations(s, runs))
    # Bind display records to this exact logical call, never just a repeated role.
    completions = {e.detail.get("agent_run_id"): e.detail.get("call_id") for e in events
                   if e.kind == "agent_finished" and isinstance(e.detail, dict) and e.detail.get("call_id")}
    ended = {e.detail.get("call_id") for e in events
             if e.kind == "agent_finished" and isinstance(e.detail, dict)}
    # dispatch/v1 (C3): the public record of what THIS call was asked, only when its agent_started carried one.
    # Older incidents have no record and the node gets no `dispatch` key -- it is never reconstructed.
    dispatches = {e.detail["call_id"]: e.detail["dispatch"] for e in events
                  if e.kind == "agent_started" and isinstance(e.detail, dict) and e.detail.get("call_id")
                  and isinstance(e.detail.get("dispatch"), dict) and e.detail["dispatch"].get("version") == "dispatch/v1"}
    for node in g.nodes:
        if node.get("agent_run_id"):
            node["call_id"] = completions.get(node["agent_run_id"])
        elif node["status"] == "running" and node.get("role"):
            start = next((e for e in reversed(events) if e.kind == "agent_started"
                          and isinstance(e.detail, dict) and e.detail.get("role") == node["role"]
                          and e.detail.get("call_id") and e.detail["call_id"] not in ended), None)
            node["call_id"] = start.detail["call_id"] if start else None
        if node.get("call_id") in dispatches:
            node["dispatch"] = dispatches[node["call_id"]]
    # Each recorded search attaches to its actual reviewer, then to calls consuming its result.
    by_run = {n["agent_run_id"]: n for n in g.nodes if n.get("agent_run_id")}
    by_call = {n["call_id"]: n for n in g.nodes if n.get("call_id")}
    searches = {}
    results = {e.detail.get("investigation_seq"): e for e in events if e.kind == "investigation_finished" and e.detail}
    for event in events:
        detail = event.detail or {}
        if event.kind == "investigation_started":
            parent = by_run.get(detail.get("parent_run_id"))
            dest = results.get(event.seq)
            node = g.node(f"investigation:{event.seq}", "investigation", "추가 근거 탐색",
                          parent["depth"] + 1 if parent else 2,
                          status="done" if dest else ("failed" if finished else "running"),
                          job_ko=event.summary, at=_iso(event.created_at),
                          conclusion_ko=dest.summary if dest else None,
                          inputs=(dest.detail.get("evidence_refs") or []) if dest else [])
            searches[event.seq] = node
            if parent:
                g.edge(parent["id"], node["id"], "review")
        elif event.kind == "investigation_progress" and detail.get("investigation_seq") in searches:
            investigation = searches[detail["investigation_seq"]]
            investigation.setdefault("source_activity", []).append({
                key: detail.get(key) for key in ("at", "phase", "file", "job_id", "seq", "attempt")
            })
        elif event.kind == "agent_started" and detail.get("investigation_seq") in searches:
            target = by_call.get(detail.get("call_id"))
            if target:
                g.edge(searches[detail["investigation_seq"]]["id"], target["id"], "review")
    return {"nodes": g.nodes, "edges": g.edges}


# ------------------------------------------------------------------ P18 W6 stage cards
def _citations(s, runs: list) -> dict[int, list[dict]]:
    """Reference chunks (runbook, public doc, past incident) each agent run cited. Retrieved-but-uncited chunks
    are not stored, so this is exactly what the agent leaned on."""
    from .models import Citation, RagChunk
    ids = [r.id for r in runs]
    out: dict[int, list[dict]] = defaultdict(list)
    if not ids:
        return out
    for c, ch in s.execute(select(Citation, RagChunk).join(RagChunk, RagChunk.id == Citation.chunk_id)
                           .where(Citation.agent_run_id.in_(ids)).order_by(Citation.id)):
        out[c.agent_run_id].append({"chunk_id": ch.id, "doc": ch.doc, "source": ch.source,
                                    "chunk_ix": ch.chunk_ix, "score": c.score, "section": ch.section,
                                    "locator": ch.locator,
                                    "source_uri": (ch.provenance or {}).get("source_uri") if ch.document_id else None,
                                    "content": _clip(ch.content, 400)})
    return out


def _iso(t) -> str | None:
    return t.isoformat() if t is not None else None


def _first_ko(items: list[dict], ref: str | None, item_id: str | None = None) -> str | None:
    from .routing_ko import first_sentence
    if ref is None:
        return None
    if item_id:
        hit = next((i for i in items if i["id"] == item_id and i.get("ko")), None)
        if hit:
            return first_sentence(hit["ko"])
    hit = next((i for i in items if i.get("ref") == ref and i.get("ko")), None)
    return first_sentence(hit["ko"]) if hit else None


def _evidence_card(evidence: list, items: list[dict]) -> dict:
    from .routing_ko import SOURCE_KO, SOURCE_REF
    from .interpret import evidence_items, rule_ko
    from .routing_ko import first_sentence
    readings_by_id = {i["id"]: i for i in items if i.get("evidence_id") is not None}
    rows = []
    for e in evidence:
        ref = (e.payload or {}).get("ref") if e.source == "investigation" and isinstance(e.payload, dict) else SOURCE_REF.get(e.source)
        ok = not e.error and e.payload is not None
        raw = []
        for item in evidence_items([e]):
            reading = readings_by_id.get(item["id"])
            ko = reading.get("ko") if reading is not None else rule_ko(item)
            raw.append({**item, "ko": ko if ok else None,
                        "ko_source": (reading.get("ko_source") if reading is not None else "rules") if ko and ok else None})
        summary = next((first_sentence(i["ko"]) for i in raw if i.get("ko")), None)
        rows.append({"id": e.id, "source": e.source, "source_ko": SOURCE_KO.get(e.source, e.source), "target": e.target or "",
                     "ok": ok, "error": e.error, "summary_ko": summary if ok else None, "ref": ref,
                     "collected_at": _iso(e.collected_at), "raw_items": raw})
    return {"kind": "evidence", "total": len(rows), "ok": sum(1 for r in rows if r["ok"]),
            "failed": sum(1 for r in rows if not r["ok"]), "items": rows}


def _changes(a) -> list[str]:
    p = a.params or {}
    if isinstance(p.get("args"), dict):
        p = p["args"]
    out = []
    for k, v in p.items():
        if k in ("kind", "name", "api_group"):
            continue
        if k == "memory_limit_mi":
            out.append(f"메모리 한도 → {v}Mi")
        elif k == "replicas":
            out.append(f"레플리카 수 → {v}")
        else:
            out.append(f"{k} → {v}")
    return out


def _clip(text: Any, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def source_labels(evidence: list) -> dict[str, str]:
    """Ref id (E1_deployment / E1) -> Korean source label, only for sources that were actually collected."""
    from .routing_ko import SOURCE_KO, SOURCE_REF
    out: dict[str, str] = {}
    for e in evidence:
        ref = (e.payload or {}).get("ref") if e.source == "investigation" and isinstance(e.payload, dict) else SOURCE_REF.get(e.source)
        if ref:
            out.setdefault(ref, SOURCE_KO.get(e.source, e.source))
            out.setdefault(ref.split("_", 1)[0], out[ref])
    return out


def explain_fields(output: dict | None, sources: dict[str, str], citations: list[dict]) -> dict:
    """P20: what an agent based its conclusion on, taken only from its recorded output.

    Quotes, counter-evidence and reasons are the agent's own words and stay untranslated (`*_raw`); the UI shows
    them as source material. `ref_known` says whether the cited id resolves to evidence collected for this
    incident or to a retrieved runbook (R#) - it is not a re-run of the verbatim citation check."""
    import re
    from .routing_ko import VERDICT_KO
    from .readings_ko import is_korean
    o = output or {}
    cands = [c for c in (o.get("root_cause_candidates") or o.get("candidates") or []) if isinstance(c, dict)]
    causes = [" ".join(str(c.get("cause") or "").split()).lower() for c in cands]
    # Only the reference chunks an agent actually cited are stored, without their R# numbers, so an R# quote can
    # be tied to "a cited reference document" but not to one specific chunk.
    is_runbook = lambda ref: bool(re.fullmatch(r"R\d+", ref))  # noqa: E731
    quotes = []
    from .interpret import quote_entries
    for q in quote_entries(o):
        ref = str(q.get("ref") or "")
        supports = " ".join(str(q.get("supports") or "").split()).lower()
        quotes.append({"ref": ref, "ref_ko": sources.get(ref) or ("참고 문서" if is_runbook(ref) else None),
                       "ref_known": ref in sources or (is_runbook(ref) and bool(citations)),
                       "quote": _clip(q.get("quote"), 240), "supports_raw": _clip(q.get("supports"), 200),
                       "candidate_index": causes.index(supports) if supports and supports in causes else None})
    counter = [{"text_raw": _clip(x, 300), "text_ko": _clip(x, 300) if is_korean(str(x)) else None}
               for x in (o.get("counter_evidence") or [])[:10] if str(x or "").strip()]
    verdicts = [{"index": v.get("index"), "verdict": v.get("verdict"), "verdict_ko": VERDICT_KO.get(v.get("verdict")),
                 "reason_raw": _clip(v.get("reason"), 300),
                 "reason_ko": _clip(v.get("reason"), 300) if is_korean(str(v.get("reason") or "")) else None}
                for v in (o.get("candidate_verdicts") or [])[:3] if isinstance(v, dict)]
    rejected = o.get("_acceptance_rejected")
    return {"quotes": quotes, "counter_evidence": counter, "candidate_verdicts": verdicts,
            "citations": citations,
            "acceptance": ({"rejected": True, "reason_raw": _clip(rejected, 240), "reason_ko": escalation_reason_ko(str(rejected))}
                           if rejected else None)}


def escalation_reason_ko(reason: str | None) -> str:
    """Korean reading of GroupController's acceptance/escalation reasons (fixed phrases produced by code)."""
    import re
    r = str(reason or "")
    if "contradicted by reviewer" in r:
        return "검토 Agent가 결론을 반박했습니다."
    m = re.search(r"(\d+) quote\(s\) not found", r)
    if m:
        return f"인용문 {m.group(1)}건이 원본 근거에서 확인되지 않았습니다."
    if r.startswith("unknown evidence refs"):
        return "수집되지 않은 근거 번호를 인용했습니다."
    if "no root-cause candidate backed" in r:
        return "원본 근거로 뒷받침된 원인 후보가 없습니다."
    m = re.search(r"confidence ([0-9.]+) below ([0-9.]+)", r)
    if m:
        return f"신뢰도 {m.group(1)}이(가) 기준 {m.group(2)}보다 낮습니다."
    if r.startswith("agent failed"):
        return "Agent 응답을 받지 못했습니다."
    return "근거 검사를 통과하지 못했습니다."


def attach_escalations(nodes: list[dict], edges: list[dict], events: list) -> None:
    """Put the recorded reason on each escalated node and its successor, in the order the events were written."""
    by_id = {n["id"]: n for n in nodes}
    per_role: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        if e.kind == "escalated" and isinstance(e.detail, dict) and e.detail.get("role"):
            per_role[str(e.detail["role"])].append(e.detail)
    for edge in edges:
        if edge["kind"] != "escalate":
            continue
        src, dst = by_id.get(edge["source"]), by_id.get(edge["target"])
        queue = per_role.get(str((src or {}).get("role")))
        if not src or not dst or not queue:
            continue
        d = queue.pop(0)
        info = {"reason_raw": _clip(d.get("reason"), 240), "reason_ko": escalation_reason_ko(d.get("reason")),
                "from_model": d.get("from_model"), "from_effort": d.get("from_effort"),
                "to_model": d.get("to_model"), "to_effort": d.get("to_effort")}
        src["escalation"] = info
        dst["escalated_from"] = info


def _agent_card(n: dict, run, inc_open: bool, later_left: bool, sources: dict[str, str] | None = None,
                citations: list[dict] | None = None) -> dict:
    from .routing_ko import NEXT_ACTION_KO, VERDICT_KO
    o = (run.output if run is not None else None) or {}
    from .readings_ko import error_ko, is_korean
    cands = [{"cause_raw": str(c.get("cause") or c.get("statement") or ""), "confidence": c.get("confidence"),
              "cause_ko": (c.get("cause_ko") if is_korean(str(c.get("cause_ko") or "")) else
                           str(c.get("cause") or "") if is_korean(str(c.get("cause") or "")) else None),
              "verifier": c.get("verifier"),
              "evidence_refs": [str(r) for r in (c.get("evidence_refs") or [])[:8]]}
             for c in (o.get("root_cause_candidates") or o.get("candidates") or [])
             if isinstance(c, dict)]
    verdict, nxt = o.get("verdict"), o.get("next_action")
    return {"kind": "agent", "agent_run_id": run.id if run is not None else None, "summary_raw": o.get("summary"),
            "summary_ko": n.get("conclusion_ko"), "candidates": cands, "verdict": verdict,
            "verdict_ko": VERDICT_KO.get(verdict) if verdict else None, "next_action": nxt,
            "next_action_ko": NEXT_ACTION_KO.get(nxt) if nxt else None,
            "counter_evidence_count": len(o.get("counter_evidence") or []),
            "confidence": o.get("confidence") if isinstance(o.get("confidence"), (int, float)) else None,
            **explain_fields(o, sources or {}, citations or []),
            "error": run.error if run is not None else None,
            "error_ko": error_ko(run.error) if run is not None else None,
            "accepts_steering": inc_open and n["status"] != "skipped" and (n["status"] in ("planned", "running") or later_left),
            "steer_target_role": n.get("role")}


def _window(rows: list, t0, t1, attr: str) -> list:
    return [x for x in rows if (t0 is None or getattr(x, attr) is None or getattr(x, attr) >= t0)
            and (t1 is None or getattr(x, attr) is None or getattr(x, attr) < t1)]


def _cards(g: _G, inc, routing, runs, events, actions, evidence, audits, items, seg_nodes, routing_live: bool,
           citations: dict[int, list[dict]] | None = None) -> None:
    from .proposals import PLAN_FIELDS, resolve
    from .routing_ko import (ACTION_STATUS_KO, DECIDED_BY_KO, WF_STATUS_KO, handoff_view, pair_router_runs, progress_ko,
                             routing_card)
    by_id = {n["id"]: n for n in g.nodes}
    ko = {i["id"]: i.get("ko") for i in items}
    run_by_id = {r.id: r for r in runs}
    inc_open = inc.status not in INCIDENT_CLOSED_STATES
    sources, citations = source_labels(evidence), citations or {}
    created = next((a for a in audits if a.event == "incident_created"), None)
    cd = (created.detail if created else None) or {}

    sig = by_id["signal"]
    sig["at"] = _iso(inc.created_at)
    where = f"{inc.namespace}/{inc.workload}"
    from .readings_ko import signal_reason_ko, signal_source_ko
    from .systems import system_ko
    sig["card"] = {"kind": "signal", "what_ko": f"{where}에서 {signal_ko(inc.signal_reasons)} 감지",
                   "place_ko": (f"{system_ko(inc.system)} · " if inc.system else "") + where,
                   "source": cd.get("source"), "reason": cd.get("reason"),
                   "source_ko": signal_source_ko(cd.get("source")), "reason_ko": signal_reason_ko(cd.get("reason")),
                   "signal_count": inc.signal_count or 0, "first_at": _iso(inc.created_at)}
    ev = by_id["evidence"]
    ev["at"] = _iso(min((e.collected_at for e in evidence if e.collected_at), default=None))
    ev["card"] = _evidence_card(evidence, items)

    pairs = pair_router_runs(routing, runs)
    cards = {rd.id: routing_card(rd, pairs.get(rd.id), ko.get(f"routing:{rd.id}:llm")) for rd in routing}
    router = by_id["router"]
    if routing:
        router["at"] = _iso(routing[0].created_at)
        router["card"] = cards[routing[0].id]
    else:
        router["card"] = {"kind": "routing", "routing_id": None, "level": None, "level_ko": "판단 전", "decided_by": None,
                          "decided_by_ko": None, "confidence": None, "steps": [], "final_reason_ko": None, "reason_raw": "",
                          "llm": None, "model_policy": None, "interpreting": False,
                          "pending_ko": "아직 사건 수준 판단 기록이 없습니다."}

    for si, (rd, start, end, role_nodes) in enumerate(seg_nodes):
        nxt = routing[si + 1].created_at if si + 1 < len(routing) else None
        seg_audits = _window(audits, rd.created_at, nxt, "ts")
        started = next((a for a in seg_audits if a.event == "wf_group_started"), None)
        sd = (started.detail if started else None) or {}
        planned_roles = [r for stage in STAGES.get(rd.level, STAGES["SIMPLE"]) for r in stage]
        live = routing_live and si + 1 == len(routing)
        roles = []
        for role in planned_roles:
            node = role_nodes.get(role)
            rm = (sd.get("roles") or {}).get(role) or {}
            info = role_info(role)
            roles.append({"role": role, "role_ko": info["role_ko"], "job_ko": info["job_ko"],
                          "model": rm.get("model") or (node or {}).get("model"),
                          "effort": rm.get("effort") or (node or {}).get("effort"),
                          "status": node["status"] if node else ("planned" if live else "skipped")})
        b = sd.get("budget")
        budget = {"max_agent_calls": b.get("max_agent_calls"), "max_cost_usd": b.get("max_cost_usd"),
                  "deadline_s": b.get("deadline_s")} if isinstance(b, dict) else None
        finished_wf = next((a for a in seg_audits if a.event == "wf_group_finished"), None)
        gnode = by_id[f"group:{rd.id}"]
        if nxt is not None:
            status_ko = "다음 판단으로 Group 전환(" + DECIDED_BY_KO.get(routing[si + 1].decided_by, routing[si + 1].decided_by) + ")"
        elif finished_wf is not None:
            status_ko = "Group 종료: " + str(WF_STATUS_KO.get((finished_wf.detail or {}).get("status"),
                                                           (finished_wf.detail or {}).get("status")))
        elif live:
            status_ko = "분석 진행 중"
        else:
            status_ko = "분석 종료"
        rc = cards[rd.id]
        gnode["at"] = _iso(rd.created_at)
        gnode["card"] = {"kind": "group", "routing_id": rd.id, "level": rd.level, "group_ko": GROUP_KO.get(rd.level, rd.level),
                         "decision": {"level_ko": rc["level_ko"], "decided_by_ko": rc["decided_by_ko"],
                                      "confidence": rc["confidence"], "final_reason_ko": rc["final_reason_ko"],
                                      "steps": rc["steps"]},
                         "roles": roles, "budget": budget, "progress_ko": progress_ko(seg_audits), "status_ko": status_ko}
        seg = [x for x in g.nodes[start:end] if x["type"] in ("agent", "verify")]
        for i, n in enumerate(seg):
            later = any(x["status"] in ("planned", "running") for x in seg if x is not n)
            run = run_by_id.get(n.get("agent_run_id")) if n.get("agent_run_id") else None
            n["at"] = _iso(run.created_at) if run is not None else (
                _iso(next((e.created_at for e in reversed(events) if e.kind == "agent_started" and isinstance(e.detail, dict)
                           and e.detail.get("role") == n.get("role")), None)) if n["status"] == "running" else None)
            n["card"] = _agent_card(n, run, inc_open, later, sources,
                                      citations.get(run.id, []) if run is not None else [])
            if run is not None:
                card = n["card"]
                prefix = f"agent:{run.id}:"
                for section, field, text in (("cause", "candidates", "cause_ko"),
                                             ("counter", "counter_evidence", "text_ko"),
                                             ("verdict", "candidate_verdicts", "reason_ko")):
                    for i, entry in enumerate(card.get(field) or []):
                        entry[text] = entry.get(text) or ko.get(f"{prefix}{section}:{i}")
                from .reading_era import legacy_at
                for i, quote in enumerate(card.get("quotes") or []):
                    quote["quote_ko"] = ko.get(f"{prefix}quote:{i}")
                card["reading_era"] = "legacy" if legacy_at(run.created_at) else "current"
                proposal = resolve(run.output, inc.workload)
                if proposal:
                    card["proposed_action"] = {
                        "type_ko": ACTION_KO[proposal["type"]], "target": f'{inc.namespace}/{proposal["kind"]}/{proposal["name"]}',
                        **{field: ko.get(f"{prefix}action:{field}") for field in PLAN_FIELDS},
                    }
                card["recommended_steps_ko"] = [ko.get(f"{prefix}plan:{i}") for i, _ in
                                                enumerate(((run.output or {}).get("recommended_steps") or [])[:10])]
    for a in actions:
        n = by_id.get(f"action:{a.id}")
        if n is None:
            continue
        n["at"] = _iso(a.created_at)
        n["card"] = {"kind": "action", "action_id": a.id, "type": a.type, "type_ko": ACTION_KO.get(a.type, a.type),
                     "target": a.target, "status": a.status, "status_ko": ACTION_STATUS_KO.get(a.status, a.status),
                     "changes": _changes(a)}
    out = by_id["outcome"]
    out["at"] = _iso(inc.updated_at)
    handoff_record = next((entry for entry in reversed(audits) if entry.event == "incident_status"
                           and (entry.detail or {}).get("status") == "handed_off"), None)
    handoff = inc.handoff if isinstance(inc.handoff, dict) else {}
    out["card"] = {"handoff_recorded_at": _iso(handoff_record.ts) if handoff_record else None,
                   "handoff_claimed": bool(handoff.get("claimed_by")), "closed_from": handoff.get("closed_from"),
                   "kind": "outcome", "status": inc.status, "status_ko": out["label"],
                   "handoff": handoff_view(inc, ko, actions)}
