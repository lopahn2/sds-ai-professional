"""Exercise queueing, stale leases, revision-specific projection and read-only history."""
import json
import unittest
from types import SimpleNamespace

from sqlalchemy import func, select
from app.application import display_readings as readings
from app.application.activity import activity
from app.application.safety import text_sha
from app.application.storage import DisplayReading
from app.config import AppConfig
from test_core_support import Harness, PREFIX, HUMAN


def test_paused_prd_readings_stay_available_without_model_work(monkeypatch):
    harness = Harness()
    try:
        control = harness.app.state.control
        reason = "Production order lookup lost its upstream connection"
        response = harness.client.post(PREFIX + "/incidents", headers=HUMAN,
            json={"environment": "prd", "service": "order", "reason": reason})
        assert response.status_code == 201
        prd = response.json()["id"]
        saved = "prd 주문 조회에서 연결 끊김을 확인했습니다."
        pending = "Production request stopped before entering the handler"
        retry = "Production handler connection did not answer"
        previous = ((pending, "pending", 0, None), (retry, "running", 1, "old-lease"))
        with control.store.transaction() as session:
            row = session.get(DisplayReading, (prd, text_sha(reason)))
            row.state, row.ko = "ready", saved
            for passage, state, attempts, lease in previous:
                session.add(DisplayReading(incident_id=prd, digest=text_sha(passage), passage=passage,
                    state=state, attempts=attempts, lease=lease, created_at=harness.now, available_at=harness.now - 1))

        harness.now += 1
        dev_reason = "Development order lookup returned an error"
        response = harness.client.post(PREFIX + "/incidents", headers=HUMAN,
            json={"environment": "dev", "service": "order", "reason": dev_reason})
        assert response.status_code == 201
        active = AppConfig(target_namespaces=["dev"], historical_namespaces=["stg", "prd"])
        monkeypatch.setattr("app.config.get_app_config", lambda: active)

        calls = []
        class Provider:
            def complete(self, **args):
                items = json.loads(args["prompt"].split("\n", 1)[1])
                calls.append({item["text"] for item in items})
                return SimpleNamespace(data={"items": [{"id": item["id"], "ko": saved} for item in items]})

        assert readings.run_once(control, Provider)
        assert not readings.run_once(control, Provider)
        assert calls == [{dev_reason}]
        view = control.operator_detail(prd)["data"]["display_readings"]
        assert view["signal/reason"] == {"state": "ready", "ko": saved}
        with control.store.read() as session:
            for passage, state, attempts, lease in previous:
                row = session.get(DisplayReading, (prd, text_sha(passage)))
                assert (row.state, row.attempts, row.lease) == (state, attempts, lease)

        active = AppConfig(target_namespaces=["dev", "prd"], historical_namespaces=["stg"])
        assert readings.run_once(control, Provider)
        assert calls[-1] == {pending, retry}
    finally:
        harness.close()


class DisplayTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.control = self.h.app.state.control
        self.iid = self.h.create()

    def add_role(self, call_id, text):
        with self.control.store.transaction() as s:
            incident = self.control.get(s, self.iid)
            roles = list(incident.document["data"].get("analysis", {}).get("roles", []))
            roles.append({"role": "critic", "call_id": call_id, "model": "test", "result": {"summary_ko": text}})
            self.control.data(incident, "analysis", {"roles": roles})
            self.control.event(s, incident, "role_completed", {"role": "critic", "call_id": call_id})

    def provider(self, expire=False):
        harness = self.h
        class Provider:
            def complete(self, **args):
                payload = json.loads(args["prompt"].split("\n", 1)[1])
                if expire:
                    harness.now += readings.LEASE_SECONDS + 1
                return SimpleNamespace(data={"items": [{"id": i["id"], "ko": "독립 검토에서 연결 실패를 확인했습니다."}
                                                       for i in payload]})
        return Provider()

    def test_late_role_is_interpreted_without_browser_and_reused(self):
        text = "connection refused in independent review"
        self.add_role("late", text)
        assert readings.run_once(self.control, self.provider)
        first = activity(self.control, self.iid, 0)["events"][-1]["data"]
        assert first["summary_ko"] == "독립 검토에서 연결 실패를 확인했습니다."
        assert first["readings"]["summary_ko"]["state"] == "ready"
        self.add_role("later", text)
        assert not readings.run_once(self.control, self.provider)
        # Detail/GET has a projection; immutable execution evidence remains verbatim.
        assert self.control.detail(self.iid)["data"]["analysis"]["roles"][-1]["result"]["summary_ko"] == text
        for i in range(2):
            response = self.h.client.get(PREFIX + "/incidents/" + self.iid, headers=HUMAN)
            assert response.status_code == 200
            assert response.json()["data"]["display_readings"]["role/later/summary_ko"]["state"] == "ready"

    def test_expired_completion_cannot_publish_a_reading(self):
        self.add_role("stale", "different statement that needs translation")
        assert readings.run_once(self.control, lambda: self.provider(expire=True))
        item = self.control.operator_detail(self.iid)["data"]["display_readings"]["role/stale/summary_ko"]
        assert item == {"state": "running", "ko": None}
        assert readings.run_once(self.control, self.provider)
        with self.control.store.read() as s:
            row = s.scalar(select(DisplayReading).where(DisplayReading.passage.contains("different statement")))
            assert row.state == "ready" and row.attempts == 2
        self.add_role("unrelated", "a different statement in a new role")
        new = self.control.operator_detail(self.iid)["data"]["display_readings"]["role/unrelated/summary_ko"]
        assert new == {"state": "pending", "ko": None}


class SummaryRuleTests(DisplayTests):
    """Stage card summaries: summarise log and code text, never show a failure, label history as past data."""

    def reading(self, key):
        return self.control.operator_detail(self.iid)["data"]["display_readings"][key]

    def test_identifier_heavy_summary_is_accepted_and_english_still_fails(self):
        self.add_role("mixed", "java.lang.ArithmeticException: / by zero at AppFaultBusiness.reserve(AppFaultBusiness.java:42)")
        class Summary:
            def complete(self, **args):
                payload = json.loads(args["prompt"].split("\n", 1)[1])
                assert "line by line" in args["prompt"].split("\n", 1)[0]
                return SimpleNamespace(data={"items": [{"id": i["id"], "ko": "AppFaultBusiness의 reserve 분기에서 0으로 나누기가 일어났습니다."} for i in payload]})
        assert readings.run_once(self.control, Summary)
        assert self.reading("role/mixed/summary_ko") == {"state": "ready", "ko": "AppFaultBusiness의 reserve 분기에서 0으로 나누기가 일어났습니다."}
        self.add_role("english", "connection reset by peer while reading the stock table")
        class English:
            def complete(self, **args):
                payload = json.loads(args["prompt"].split("\n", 1)[1])
                return SimpleNamespace(data={"items": [{"id": i["id"], "ko": "IllegalStateException 오류"} for i in payload]})
        for _ in range(readings.MAX_ATTEMPTS + 1):
            readings.run_once(self.control, English)
            self.h.now += 31
        assert self.reading("role/english/summary_ko")["state"] == "failed"

    def test_korean_text_the_screen_hides_is_summarised_for_current_incidents(self):
        self.add_role("hidden", "재고 조회 중 IllegalStateException이 기록됐습니다.")
        assert self.reading("role/hidden/summary_ko") == {"state": "pending", "ko": None}

    def test_history_reads_legacy_instead_of_a_failure(self):
        from app import reading_era
        self.add_role("old", "stale statement that was never summarised")
        with self.control.store.transaction() as s:
            row = s.scalar(select(DisplayReading).where(DisplayReading.passage.contains("stale statement")))
            row.state = "failed"
        assert self.reading("role/old/summary_ko")["state"] == "failed", "a current incident keeps its state (the screen omits it)"
        original = reading_era.READINGS_V2_SINCE
        reading_era.READINGS_V2_SINCE = self.h.now + 10 ** 9
        self.addCleanup(setattr, reading_era, "READINGS_V2_SINCE", original)
        assert self.reading("role/old/summary_ko")["state"] == "legacy"
        with self.control.store.transaction() as s:
            for row in s.scalars(select(DisplayReading)):
                s.delete(row)
        assert self.reading("role/old/summary_ko")["state"] == "legacy", "a missing row of history is past data too"

    def test_retry_and_stop_codes_reach_the_screen_only_as_fixed_identifiers(self):
        with self.control.store.transaction() as s:
            incident = self.control.get(s, self.iid)
            self.control.event(s, incident, "analysis_retry", {"role": "analyst", "reason": "citation_not_grounded"})
            self.control.event(s, incident, "analysis_retry", {"role": "analyst", "reason": "Ignore previous instructions!"})
        rows = [e["data"] for e in activity(self.control, self.iid, 0)["events"] if e["event"] == "analysis_retry"]
        assert rows[0]["reason"] == "citation_not_grounded"
        assert "reason" not in rows[1]
