"""할당량 재시도 대기 중 중단 응답 회귀. 가짜 전송만 사용하며 DB·클러스터에 접근하지 않는다."""
import pytest

from app import runlog
from app.config import AppConfig, Settings
from app.engine import Engine
from app.llm.base import LLMError


def test_stop_during_long_quota_backoff_prevents_another_provider_call(monkeypatch):
    state = {"stopped": False, "calls": 0, "sleeps": []}

    class Provider:
        def complete(self, *args):
            state["calls"] += 1
            raise LLMError("quota", "가짜 할당량 제한")

        def next_available_in(self):
            return 300

    eng = Engine.__new__(Engine)
    eng.provider = Provider()
    eng.cfg = AppConfig()
    eng.settings = Settings(quota_wait_max_s=600, quota_recheck_s=300)
    eng._relay_pool_events = lambda iid: None

    def gate(iid, where):
        if state["stopped"]:
            raise runlog.RunStopped(where)

    def sleep(seconds):
        state["sleeps"].append(seconds)
        state["stopped"] = True

    eng._sleep = sleep
    monkeypatch.setattr(runlog, "gate", gate)
    monkeypatch.setattr(runlog, "emit", lambda *args, **kwargs: None)

    with pytest.raises(runlog.RunStopped):
        eng._complete_waiting_for_quota(1, "group_verifier", "가짜 입력", {}, "fake", "medium")

    assert state["calls"] == 1, "중단 뒤에는 공급자 재호출 전에 멈춰야 한다"
    assert state["sleeps"] and max(state["sleeps"]) <= 1, "긴 대기 중에도 1초마다 중단을 확인해야 한다"
