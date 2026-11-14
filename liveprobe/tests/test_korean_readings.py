"""한글 해석 누락 회귀: 실제 운영에서 관측한 원문 형태가 결정적 한국어 읽기를 갖는지 확인한다.

표본은 2026-11-07 운영 관제 DB의 최근 사건에서 형태만 추려 식별자를 합성값으로 바꾼 것이다
(docs/evidence/korean-coverage-20261107/). 모델·DB·네트워크를 쓰지 않는다.
"""
import json
import re

import pytest

from app.interpret import rule_ko

HANGUL = re.compile(r"[\uac00-\ud7a3]")


def log(raw):
    return {"id": "E7_error_logs#0", "ref": "E7_error_logs", "kind": "log", "source": "loki", "raw": raw}


def event(raw):
    return {"id": "E5_recent_events#0", "ref": "E5_recent_events", "kind": "event", "source": "kubernetes", "raw": raw}


def assert_korean(text, *must):
    assert text, "한국어 읽기가 없다"
    assert len(HANGUL.findall(text)) >= 6, text
    for m in must:
        assert m in text, (m, text)


LOG_CASES = [
    ('[order] {"@timestamp":"2026-10-07T13:27:44.762Z","log":{"level":"WARN","logger":"io.x.OrderController"},'
     '"service":{"name":"order"},"message":"inventory call retry item=b attempt=1 error=ServiceUnavailable"}',
     ("inventory", "재시도", "b")),
    ('[order] {"@timestamp":"2026-10-07T13:27:44.762Z","log":{"level":"ERROR"},"service":{"name":"order"},'
     '"message":"inventory call failed item=a error=ServiceUnavailable: 503 Service Unavailable: \\"no healthy upstream\\""}',
     ("inventory", "실패", "정상 대상")),
    ('[inventory] {"@timestamp":"2026-10-07T13:27:44.762Z","log":{"level":"ERROR"},'
     '"message":"stock lookup db_error item=gadget error=CannotGetJdbcConnectionException"}', ("DB 연결", "gadget")),
    ('[payment] {"timestamp": "2026-10-25T08:18:54+00:00", "level": "ERROR", "system": "payment", "service": "payment", '
     '"environment": "dev", "event": "http_access", "method": "GET", "uri": "/api/payments/{id}/receipt", "status": 503, '
     '"duration_seconds": 4.9e-05}', ("payment", "GET", "/api/payments/{id}/receipt", "503")),
    ('[delivery] {"time":"2026-10-25T08:52:02.015Z","level":"ERROR","system":"delivery","service":"delivery",'
     '"environment":"dev","method":"PATCH","uri":"/api/deliveries/{id}/status","status":502,"duration_ms":2013.306}',
     ("delivery", "PATCH", "502", "2013")),
    ('[notification] {"timestamp": "2026-10-25T08:52:02+00:00", "level": "WARN", "service": "notification", '
     '"event": "client_disconnected", "error": "BrokenPipeError"}', ("notification", "연결")),
    ('[order] {"event":"self_traffic_failed","service":"order"}\n (repeated 26 times)', ("order", "26")),
    ('[payment] {"timestamp": "2026-10-25T19:41:21+00:00", "level": "ERROR", "service": "payment", "event": "crash_exit", '
     '"exit_code": 1}', ("payment", "종료 코드 1")),
    ('[catalog] {"timestamp": "2026-10-30T14:05:25+00:00", "level": "WARN", "service": "catalog", '
     '"event": "chaos_restored", "active": ["liveness_fail"]}', ("catalog", "liveness_fail")),
    ('[order] {"event":"app_exception","service":"order","exception":"WorkerPoolExhausted"}\n (repeated 12 times)',
     ("WorkerPoolExhausted", "12")),
    ('[inventory] 2026-10-07T13:27:44.653676Z\terror\tprobes\tRequest to probe app failed: Get '
     '"http://10.42.0.98:8080/actuator/health/liveness": dial tcp 127.0.0.6:0->10.42.0.98:8080: connect: connection refused, '
     'original URL path = /app-health/inventory/startupz\n', ("상태 확인", "연결 거부")),
    ('[inventory] [2026-10-07T13:27:41.535Z] "GET /api/stock/a HTTP/1.1" 503 URX,UF upstream_reset_before_response_started'
     '{remote_connection_failure|delayed_connect_error:_Connection_refused} - "delayed_connect_error:_Connection_refused" '
     '0 121 29 - "-" "Java/21" "id" "inventory.svc:8080" "10.42.0.98:8080" inbound|8080||', ("GET /api/stock/a", "503")),
    ('[inventory] Picked up JAVA_TOOL_OPTIONS: -XX:MaxRAMPercentage=70 -XX:+ExitOnOutOfMemoryError\n', ("JVM",)),
    ('[inventory] 2026-10-07T13:53:56.918136Z\tinfo\tEnvoy command: [-c etc/istio/proxy/envoy-rev.json --drain-time-s 45]\n',
     ("Envoy",)),
    ('[inventory] info\tFLAG: --proxyComponentLogLevel="misc:error"\n', ("Envoy",)),
    ('[member] failed to create fsnotify watcher: too many open files (repeated 50 times)', ("열린 파일", "50")),
    ('[delivery] level=ERROR system=delivery service=delivery\n (repeated 4 times)', ("delivery", "오류")),
    ('[delivery]     at listOnTimeout (node:internal/timers:585:17)\n (repeated 4 times)', ("호출 경로", "listOnTimeout")),
    ('[delivery]     at Timeout._onTimeout (/app/dependency.js:41:23)\n', ("호출 경로", "/app/dependency.js:41")),
    ('[order] \tat org.apache.catalina.valves.ErrorReportValve.invoke(ErrorReportValve.java:83)\n (repeated 12 times)',
     ("호출 경로", "ErrorReportValve.java:83")),
    ('[notification]   File "/usr/local/lib/python3.12/urllib/request.py", line 639, in http_error_default\n',
     ("호출 경로", "request.py:639")),
    ('[notification]     with opener.open(request, timeout=5) as response:\n (repeated 3 times)', ("소스 코드",)),
    ('[notification] The above exception was the direct cause of the following exception:\n', ("원인",)),
    ('[notification] During handling of the above exception, another exception occurred:\n', ("예외",)),
    ('[delivery] Caused by: Error: 의존 호출 2초 제한 초과\n (repeated 4 times)', ("의존 호출 2초 제한 초과",)),
    ('[delivery] Caused by: Error: connect ECONNREFUSED 10.43.0.10:8080\n', ("연결 거부",)),
    ('[notification] urllib.error.URLError: <urlopen error [Errno 111] Connection refused>\n', ("URLError", "연결 거부")),
    ('[notification] ConnectionRefusedError: [Errno 111] Connection refused\n', ("ConnectionRefusedError", "연결 거부")),
    ('[notification] TimeoutError: timed out\n', ("TimeoutError", "시간 초과")),
    ('[payment] urllib.error.HTTPError: HTTP Error 503: Service Unavailable\n', ("HTTPError", "503")),
    ('[member] DependencyError: 같은 환경의 업무 연동에 실패했습니다.\n', ("DependencyError", "업무 연동에 실패")),
    ('[order] io.liveprobe.target.order.AppFaultRuntime$IntermittentException: intermittent business processing failure\n',
     ("IntermittentException", "간헐")),
    ('[notification] socket.gaierror: [Errno -3] Temporary failure in name resolution\n', ("이름",)),
    ('[delivery] Error: aborted\n', ("중단",)),
    ('[order] failed to watch directory "/var/lib/docker/containers/abc": no such file or directory', ("감시",)),
]


@pytest.mark.parametrize("raw,must", LOG_CASES, ids=[str(i) for i in range(len(LOG_CASES))])
def test_observed_log_shapes_have_korean_reading(raw, must):
    assert_korean(rule_ko(log(raw)), *must)


EVENT_CASES = [
    ("Normal Started Pod/order-7d9-d4xd: Container started", ("시작",)),
    ("Normal Created Pod/order-7d9-d4xd: Container created", ("만들",)),
    ('Normal Pulled Pod/order-7d9-d4xd: Container image "docker.io/istio/proxyv2:1.27.3" already present on machine '
     'and can be pulled', ("이미지", "이미 있")),
    ("Normal Killing Pod/order-7d9-d4xd: Stopping container istio-proxy", ("istio-proxy", "종료")),
    ("Warning Unhealthy Pod/order-7d9-d4xd: Readiness probe failed: HTTP probe failed with statuscode: 503 (x12)",
     ("준비 상태 확인", "503", "12")),
    ("Warning Unhealthy Pod/order-7d9-d4xd: Startup probe failed: Get \"http://10.42.0.1:15021/healthz/ready\": dial tcp "
     "10.42.0.1:15021: connect: connection refused", ("시작 상태 확인", "연결 거부")),
    ("Warning Unhealthy Pod/order-7d9-d4xd: Liveness probe failed: Get \"http://10.42.0.1:8080/healthz/live\": context "
     "deadline exceeded", ("생존 상태 확인", "시간 초과")),
    ("Normal SandboxChanged Pod/order-7d9-d4xd: Pod sandbox changed, it will be killed and re-created.", ("샌드박스",)),
    ("Normal ScalingReplicaSet Deployment/order: Scaled down replica set order-7d9 from 1 to 0",
     ("order-7d9", "1", "0", "줄")),
    ("Normal ScalingReplicaSet Deployment/order: Scaled up replica set order-8a1 from 0 to 1", ("order-8a1", "늘")),
    ("Normal Scheduled Pod/inventory-1-abcd: Successfully assigned liveprobe-target/inventory-1-abcd to lima-rancher-desktop",
     ("lima-rancher-desktop", "배치")),
    ("Normal SuccessfulCreate ReplicaSet/inventory-1: Created pod: inventory-1-abcd", ("inventory-1-abcd", "만들")),
    ("Normal SuccessfulDelete ReplicaSet/inventory-1: Deleted pod: inventory-1-abcd", ("inventory-1-abcd", "삭제")),
    ("Warning FailedSync Pod/order-1: error determining status: rpc error: code = Unknown desc = Error response from daemon",
     ("상태",)),
    ('Warning FailedMount Pod/order-1: MountVolume.SetUp failed for volume "istiod-ca-cert" : object "prd"/"istio-ca-root-cert"'
     ' not registered', ("istiod-ca-cert", "마운트")),
    ("Warning Failed Pod/order-1: Error: ImagePullBackOff", ("이미지",)),
    ('Normal Pulling Pod/order-1: Pulling image "liveprobe/order:does-not-exist-1"', ("liveprobe/order:does-not-exist-1",)),
    ('Normal BackOff Pod/order-1: Back-off pulling image "liveprobe/order:does-not-exist-1"', ("이미지", "재시도")),
    ("Warning BackOff Pod/order-1: Back-off restarting failed container order in pod order-1", ("재시작", "order")),
]


@pytest.mark.parametrize("raw,must", EVENT_CASES, ids=[str(i) for i in range(len(EVENT_CASES))])
def test_observed_event_shapes_have_korean_reading(raw, must):
    assert_korean(rule_ko(event(raw)), *must)


def test_unknown_english_still_has_no_invented_reading():
    assert rule_ko(log("[order] completely ordinary line")) is None
    assert rule_ko(event("Normal SomethingNew Pod/x: some unregistered text")) is None


def test_korean_agent_and_decision_text_is_its_own_reading():
    text = "inventory DB 연결 풀이 고갈되어 재고 조회가 실패했습니다.\n- DB 연결 풀 고갈 (confidence 0.82)"
    ko = rule_ko({"id": "agent:1", "ref": "agent:k8s_log_analyst", "kind": "agent", "source": "agent", "raw": text})
    assert ko.startswith("inventory DB 연결 풀이 고갈") and "신뢰도 0.82" in ko and "confidence" not in ko
    assert rule_ko({"id": "agent:2", "kind": "agent", "ref": "agent:x", "raw": "Delivery returns HTTP 502 when ..."}) is None
    assert rule_ko({"id": "handoff:summary", "kind": "handoff", "ref": "handoff",
                    "raw": "LLM classifier unavailable (RunStopped); operator handoff"}) == "AI 분류기를 사용할 수 없어 고위험 인계"


@pytest.mark.parametrize("raw,must", [
    ("COMPLEX group verified: independent verifier supports evidence-grounded candidates", ("복잡", "독립 검증")),
    ("NORMAL group unverified: independent verification verdict insufficient", ("보통", "근거 부족")),
    ("COMPLEX group stopped: limit reached (deadline): 180s elapsed; next call may exceed 180s", ("시간 한도",)),
    ("COMPLEX group failed: primary agent produced no valid output", ("유효한 결과",)),
    ("stopped_before_start", ("시작 전",)),
    ("rollout_restart succeeded and verified", ("Deployment 재시작", "검증")),
    ("action failed: AttributeError: request_rollback", ("조치 실패", "AttributeError")),
    ("single agent proposed no Kubernetes action (next_action=handoff)", ("조치",)),
    ("HIGH_RISK policy: handoff to operator", ("고위험",)),
    ("LiveProbe restarted while incident was 'analyzing'; marked failed (no replay)", ("재시작",)),
    ("action rejected by hwany", ("hwany", "거절")),
    ("waiting for operator approval", ("승인",)),
])
def test_status_reasons_have_korean_reading(raw, must):
    from app.readings_ko import status_reason_ko
    assert_korean(status_reason_ko(raw), *must)


def test_signal_titles_and_sources_are_korean():
    from app.readings_ko import signal_source_ko, title_ko
    assert title_ko("TargetServiceNoEndpoints on Service/delivery-member", ["TargetServiceNoEndpoints"]) == \
        "Service 연결 대상 없음 · Service/delivery-member"
    assert "애플리케이션 예외" in title_ko("ApplicationException on Deployment/order", ["ApplicationException"])
    assert "5xx" in title_ko("TargetHigh5xxRate on App/payment", ["TargetHigh5xxRate"])
    assert "Prometheus" in signal_source_ko("alertmanager") and "로그" in signal_source_ko("app_log")
    assert signal_source_ko("unknown_source") == "unknown_source"


def test_not_applicable_verdict_has_korean_label():
    from app.routing_ko import VERDICT_KO
    assert VERDICT_KO["not_applicable"] == "해당 없음(검토 역할 아님)"


def test_model_prompts_require_korean_prose_and_verbatim_quotes():
    from app.agent_groups import GROUP_OUTPUT_RULES
    from app.router import ROUTER_PROMPT
    from app.workflows import OUTPUT_RULES
    for text in (GROUP_OUTPUT_RULES, OUTPUT_RULES):
        assert "한국어" in text and "번역하지" in text
    assert "rationale" in ROUTER_PROMPT and "한국어" in ROUTER_PROMPT
    assert "{features}" in ROUTER_PROMPT


def test_reading_never_returns_raw_secret_like_text():
    raw = '[order] {"level":"ERROR","service":"order","message":"inventory call failed item=a error=ServiceUnavailable"}'
    ko = rule_ko(log(raw))
    assert "{" not in ko and json.dumps(raw) not in ko


def test_korean_fields_are_requested_but_never_required_to_accept_output():
    from jsonschema import Draft202012Validator
    from app.agent_groups import GROUP_SCHEMA
    from app.llm import AGENT_SCHEMA, ROUTER_SCHEMA
    from app.llm.base import strict_schema, validation_schema
    for schema, key in ((AGENT_SCHEMA, "summary_ko"), (GROUP_SCHEMA, "summary_ko"), (ROUTER_SCHEMA, "rationale_ko")):
        assert key in strict_schema(schema)["required"]          # 생성 계약은 한국어 필드를 요청한다
        assert key not in validation_schema(schema).get("required", [])
    legacy = {"summary": "s", "evidence": [], "root_cause_candidates": [{"cause": "c", "confidence": .5, "evidence_refs": []}],
              "counter_evidence": [], "confidence": .5, "verdict": "not_applicable", "candidate_verdicts": [],
              "next_action": "handoff", "recommended_steps": [], "action": None}
    Draft202012Validator(validation_schema(GROUP_SCHEMA)).validate(legacy)


def test_model_written_korean_summary_is_the_reading_and_raw_stays_verbatim():
    from app.interpret import agent_items, handoff_items
    run = {"id": 7, "role": "cause_reasoner", "created_at": None,
           "output": {"summary": "Delivery returns HTTP 502.", "summary_ko": "delivery가 의존 호출 2초 제한을 넘겨 HTTP 502를 반환합니다.",
                      "root_cause_candidates": []}}
    item = agent_items([run])[0]
    assert item["raw"] == "Delivery returns HTTP 502." and rule_ko(item).startswith("delivery가 의존 호출")
    items = handoff_items({"summary": "Delivery returns HTTP 502.", "summary_ko": "delivery가 HTTP 502를 반환합니다.",
                           "root_cause_candidates": [{"cause": "timeout", "cause_ko": "의존 호출 시간 초과입니다."}]})
    by = {i["id"]: rule_ko(i) for i in items}
    assert by["handoff:summary"] == "delivery가 HTTP 502를 반환합니다." and by["handoff:cause#0"] == "의존 호출 시간 초과입니다."
    # 한글이 없는 '한국어' 힌트는 읽기로 쓰지 않는다.
    run["output"]["summary_ko"] = "Delivery returns 502"
    assert rule_ko(agent_items([run])[0]) is None
