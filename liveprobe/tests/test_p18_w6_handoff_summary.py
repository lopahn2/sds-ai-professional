"""P18 W6 실운영 관측: 분석 Agent가 결과를 만들지 못한 인계는 요약이 '준비 중'으로 남지 않아야 한다.

#1082(prd delivery)에서 cause_reasoner가 시간 초과로 실패해 handoff.summary=None 이었고,
화면은 해석이 끝났는데도 '한글 요약을 준비 중입니다.'를 계속 보였다.
"""
from types import SimpleNamespace as N

from app.routing_ko import handoff_view


def _inc(handoff, status="handed_off"):
    return N(status=status, handoff=handoff)


def test_failed_group_without_summary_states_the_recorded_failure_instead_of_pending():
    h = {"summary": None, "workflow_status": "failed", "root_cause_candidates": [], "recommended_steps": [],
         "notes": ["primary agent produced no valid output", "COMPLEX: operator handoff (not independently verified)"],
         "remediation": {"why": "운영 완화 우선"}}
    v = handoff_view(_inc(h), {"handoff:why": "운영 완화 우선"})
    assert v["summary_raw"] is None
    assert v["summary_ko"], "요약 원문이 없으면 해석 대기가 아니라 기록된 사실을 말해야 한다"
    assert "준비" not in v["summary_ko"]
    assert "유효한 결과를 만들지 못" in v["summary_ko"]
    assert "원인" in v["summary_ko"] and "확정하지 못" in v["summary_ko"]
    assert v["summary_source"] == "rules"
    assert v["interpreting"] is False


def test_missing_summary_without_failure_record_says_not_recorded():
    v = handoff_view(_inc({"summary": None, "root_cause_candidates": []}), {})
    assert v["summary_ko"] == "AI 분석 요약이 기록되지 않았습니다."
    assert v["summary_source"] == "rules"


def test_recorded_summary_still_waits_for_its_own_translation():
    h = {"summary": "Delivery returned HTTP 502.", "workflow_status": "failed"}
    pending = handoff_view(_inc(h), {})
    assert pending["summary_ko"] is None and pending["interpreting"] is True
    assert pending["summary_source"] == "interpret"
    done = handoff_view(_inc(h), {"handoff:summary": "Delivery가 HTTP 502를 반환했습니다."})
    assert done["summary_ko"] == "Delivery가 HTTP 502를 반환했습니다."


def test_handoff_next_step_does_not_point_to_a_screen_side():
    """모바일에서는 대화가 오른쪽이 아니라 '대화' 탭이다. 위치가 아니라 영역·카드 이름으로 안내한다."""
    from app.narrative import _next
    text = _next(N(status="handed_off"), [], [])
    assert "오른쪽" not in text
    assert "분석 · 운영자 대화" in text and "단계 카드" in text
