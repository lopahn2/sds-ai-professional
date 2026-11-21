"""Exercise queueing, stale leases, revision-specific projection and read-only history."""
import json
import unittest
from types import SimpleNamespace

from sqlalchemy import func, select
from app.application import display_readings as readings
from app.application.activity import activity
from app.application.storage import DisplayReading
from test_core_support import Harness, PREFIX, HUMAN


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
