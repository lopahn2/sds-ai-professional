"""Frozen member release defects with independent registration and profile journeys."""
from __future__ import annotations

import json
import re
from select import select
from time import monotonic

from evaluation.live_repair import DeliveryReleaseTrial


class MemberReleaseTrial(DeliveryReleaseTrial):
    source_symbols = {
        "normalizeMemberName": ("function normalizeMemberName(", "function serializeProfile("),
        "serializeProfile": ("function serializeProfile(", "function createBusiness("),
    }
    fixture_files = (*DeliveryReleaseTrial.fixture_files, "static/index.html", "static/app.css", "static/app.js",
                     "package.json", "test/index.js", "test/campaign-state.js", "test/ttl-persistence.js",
                     "test/campaign-http.js", "test/campaign-resources.js")

    def __init__(self, case):
        self.dual = case == "member-23"
        self.protocol = "live-repair/v33" if self.dual else "live-repair/v32"
        self.patch_expressions = {"serializeProfile.profileCreatedAt": {"member.createdAt"}}
        if self.dual:
            self.patch_expressions["normalizeMemberName.submittedName"] = {"body.name"}
        self.assignments = tuple(self.patch_expressions)
        super().__init__(case)

    def freeze(self, source):
        profile = " return {...member,createdAt:new Date(codeBug?'invalid-member-date':member.createdAt).toISOString()};"
        signup = " return codeBug?body.profile.name.trim():body.name.trim();"
        if source.count(profile) != 1 or source.count(signup) != 1:
            raise ValueError("the member fixture no longer matches the frozen release")
        frozen = source.replace(profile, " const profileCreatedAt = member.name.startsWith('LP-EVAL-PROFILE-') "
                                "? 'invalid-member-date' : member.createdAt;\n"
                                + profile.replace(":member.createdAt", ":profileCreatedAt"))
        if self.dual:
            frozen = frozen.replace(signup, " const submittedName = body.name.startsWith('LP-EVAL-SIGNUP-') "
                                    "? body.account.displayName : body.name;\n"
                                    + signup.replace(":body.name.trim()", ":submittedName.trim()"))
        return frozen

    def probe(self, _variant=None):
        self.sequence += 1
        suffix = str(self.sequence)
        ready = self.request("/healthz/ready")[0]
        live = self.request("/healthz/live")[0]
        seed_status, seed = self.request("/api/members/1/profile")
        missing = self.request("/api/members/lp-eval-missing/profile")[0]
        signup_status, signup_intact, rejected_unstored = None, None, None
        if self.dual:
            target = "LP-EVAL-SIGNUP-" + suffix
            signup_status, member = self.request("/api/members", {"name": target})
            found_status, lookup = self.request("/api/members?search=" + target)
            signup_intact = (signup_status == 201 and found_status == 200 and member.get("name") == target
                             and lookup.get("members") == [member])
            rejected_unstored = signup_status == 500 and found_status == 200 and lookup.get("members") == []
        name = "LP-EVAL-PROFILE-" + suffix
        created, created_member = self.request("/api/members", {"name": name})
        identifier = created_member.get("id")
        profile, fetched = self.request("/api/members/" + identifier + "/profile") if isinstance(identifier, str) else (None, {})
        intact = (profile == 200 and fetched.get("id") == identifier and fetched.get("name") == name
                  and fetched.get("createdAt") == created_member.get("createdAt"))
        seed_intact = seed_status == 200 and seed.get("id") == "1" and seed.get("name") == "김하늘"
        return {"ready": ready, "liveness": live, "business": profile, "created": created,
                "profile_intact": intact, "seed_intact": seed_intact, "missing_status": missing,
                "signup_status": signup_status, "signup_intact": signup_intact, "rejected_signup_unstored": rejected_unstored,
                "profile_id": identifier, "contract_ok": ready == live == profile == 200 and created == 201
                and intact and seed_intact and missing == 404 and (not self.dual or signup_intact)}

    def initial(self, failed):
        if (not failed["seed_intact"] or failed["created"] != 201 or failed["business"] != 500
                or self.dual and not failed["rejected_signup_unstored"]):
            raise RuntimeError("the frozen code did not reproduce the selected member failures")
        expected = {"serializeProfile": "RangeError"}
        if self.dual:
            expected["normalizeMemberName"] = "TypeError"
        initial, error = None, None
        deadline = monotonic() + 3
        stdout = self.process.stdout
        while select([stdout], [], [], max(0, deadline - monotonic()))[0]:
            line = stdout.readline()
            if not line or monotonic() >= deadline:
                break
            exception = re.fullmatch(rb"(RangeError|TypeError): ([^\r\n]{1,240})\r?\n", line)
            frame = re.fullmatch(rb"\s+at (\w+) \([^\r\n]+/business\.js:(\d+):\d+\)\r?\n", line)
            if exception:
                error = tuple(item.decode() for item in exception.groups())
            elif frame and error and expected.get(frame[1].decode()) == error[0]:
                self.traces.append({"path": self.source_file, "line": int(frame[2]), "function": frame[1].decode(),
                                    "exception": error[0], "message": error[1]})
                error = None
            elif line.startswith(b"{"):
                event = json.loads(line)
                if event.get("status") == 500 and event.get("event") == "http_access":
                    if initial is None:
                        initial = json.dumps({"service": "test-application", **{k: event[k] for k in
                                             ("level", "event", "method", "uri", "status")}})
                        self.trace_observed_at = event["time"]
                    if {(trace["function"], trace["exception"]) for trace in self.traces} == set(expected.items()):
                        return initial
        raise RuntimeError("the expected member runtime exception frames were unavailable")
