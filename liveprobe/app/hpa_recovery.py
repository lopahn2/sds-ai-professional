"""Persist read-only HPA recovery observations against the original incident."""
from sqlalchemy import select

from . import accountability, db, runlog
from .hpa_metrics import API_SERVICE, RECOVERY_LABEL, RECOVERY_SCOPE, Outage
from .incidents import HPA_METRICS_BLOCKED, HPA_METRICS_SOURCE
from .k8s import parse_ts
from .lifecycle import transition
from .models import Evidence, Incident
from .sse import broker


class HpaRecovery:
    def __init__(self, config):
        self.config = config

    def _latest(self, session, uid):
        return session.execute(select(Evidence.id.label("evidence_id"), Incident.id.label("incident_id"),
                                      Incident.status, Incident.namespace, Evidence.payload)
            .join(Incident, Incident.id == Evidence.incident_id)
            .where(Incident.workload == API_SERVICE, Evidence.source == HPA_METRICS_SOURCE,
                   Evidence.error.is_(None), Evidence.payload["provider_uid"].astext == uid,
                   Evidence.payload["provider_since"].astext.is_not(None))
            .order_by(Incident.id.desc(), Evidence.id.desc()).limit(1)).first()

    def pending(self, uid):
        with db.session() as session:
            row = self._latest(session, uid)
        if row and row.status == "handed_off" and self.config.is_active_environment(row.namespace):
            return Outage(row.incident_id, row.evidence_id, row.payload)
        return None

    def record(self, outage, samples):
        iid = outage.incident_id
        with db.session() as session:
            inc = session.scalar(select(Incident).where(Incident.id == iid).with_for_update())
            latest = self._latest(session, outage.detail["provider_uid"])
            if (inc is None or inc.status != "handed_off" or inc.signal_reasons != [HPA_METRICS_BLOCKED]
                    or ((inc.handoff or {}).get("handoff_classification") or {}).get("reason_code")
                    != "resource_metrics_cluster_authority"
                    or not all(self.config.is_active_environment(target["namespace"])
                               for target in outage.detail["affected_hpas"])
                    or not latest or latest.evidence_id != outage.evidence_id
                    or runlog.cleanup_blocker_locked(session, iid) is not None):
                return False

            proof = {"scope": RECOVERY_SCOPE, "api_service": API_SERVICE,
                     "provider_uid": outage.detail["provider_uid"], "outage_evidence_id": outage.evidence_id,
                     "observations": samples, "cause_confirmed": False, "ai_remediation": False,
                     "workload_impact_observed": False,
                     "initial_handoff_at": inc.closed_at.isoformat() if inc.closed_at else None}
            inc.handoff = {**(inc.handoff or {}), "observed_recovery": proof}
            transition(inc, "recovery_review")
            inc.status_reason = RECOVERY_LABEL
            last = samples[-1]
            observation_ids = []
            for sample in samples:
                row = Evidence(incident_id=iid, source=HPA_METRICS_SOURCE, target=f"APIService/{API_SERVICE}",
                               payload={"recovery": sample, "outage_evidence_id": outage.evidence_id},
                               collected_at=parse_ts(sample["observed_at"]))
                session.add(row)
                session.flush()
                observation_ids.append(row.id)

            names = ", ".join(f"{target['namespace']}/{target['name']}" for target in last["hpas"][:4])
            refs = [f"지표 공급 중단 근거 #{outage.evidence_id}"]
            refs.extend(f"복구 관측 #{evidence_id}" for evidence_id in observation_ids)
            report = {**accountability.view(session, inc), "status": "pending_operator",
                      "recovery_scope": RECOVERY_SCOPE,
                      "detection_ko": f"자원 지표 API가 응답하지 않아 HPA {len(last['hpas'])}개({names})의 자동 확장 계산이 중단됐습니다.",
                      "recovery_ko": f"자원 지표 API가 정상 상태로 전환됐고 HPA {len(last['hpas'])}개의 자원 지표 계산 재개를 두 번 관측했습니다.",
                      "limits_ko": "공급원이 정상화된 이유, 실제 복제본 변화와 업무 요청 영향은 미관측입니다.",
                      "evidence_refs": refs, "provider_recovered_at": last["provider_available_since"],
                      "hpa_recovered_at": max((target["since"] for target in last["hpas"]), key=parse_ts)}
            accountability.append(session, iid, report, "liveprobe")
            db.audit(session, "liveprobe", "observed_recovery", iid, **proof)
        broker.publish(iid)
        return True
