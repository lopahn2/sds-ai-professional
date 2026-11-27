"""운영자가 바로 읽을 결정적 한국어 읽기.

* 실제 운영 관제에서 관측한 로그·Kubernetes 이벤트·상태 사유·신호 형태만 다룬다
  (docs/evidence/korean-coverage-20261107/). 형태를 모르면 None을 돌려 표시용 모델 해석에 맡긴다.
* 원문에 없는 원인·조언을 만들지 않는다. 식별자(서비스·Pod·예외 클래스·이벤트 사유·오류 코드)는 원문 표기를 유지한다.
* 표시 전용이다. 분류·분석·조치 판단에는 쓰지 않는다.
"""
from __future__ import annotations

import json
import re
from typing import Callable

HANGUL = re.compile(r"[\uac00-\ud7a3]")
_PREFIX = re.compile(r"^\[([^\]\s]+)\]\s?")
_REPEATED = re.compile(r"\s*\(repeated (\d+) times\)\s*$")


def hangul_ratio(text: str) -> float:
    letters = re.findall(r"[A-Za-z\uac00-\ud7a3]", text or "")
    return len(HANGUL.findall(text or "")) / len(letters) if letters else 0.0


def is_korean(text: str | None, ratio: float = 0.3) -> bool:
    return bool(text) and len(HANGUL.findall(text)) >= 2 and hangul_ratio(text) >= ratio


def _rep(n: str | None) -> str:
    return f" 같은 줄이 {n}회 반복되었습니다." if n else ""


def _cause_ko(text: str) -> str | None:
    t = text.lower()
    if "no healthy upstream" in t:
        return "호출 대상에 정상 대상(엔드포인트)이 없습니다"
    if "cannotgetjdbcconnection" in t or "connection is not available" in t or "hikaripool" in t:
        return "DB 연결 풀에서 연결을 얻지 못했습니다"
    if "econnrefused" in t or "connection refused" in t:
        return "대상이 연결을 거부했습니다(연결 거부)"
    if "econnreset" in t or "connection reset" in t:
        return "상대방이 연결을 초기화했습니다(연결 끊김)"
    if "timed out" in t or "timeout" in t or "deadline exceeded" in t:
        return "제한 시간 안에 응답이 없었습니다(시간 초과)"
    if "name resolution" in t or "gaierror" in t:
        return "DNS 이름 해석에 실패했습니다"
    if "serviceunavailable" in t or "service unavailable" in t:
        return "호출 대상이 503(일시적으로 사용할 수 없음)을 반환했습니다"
    return None


def _status_text(code) -> str:
    try:
        n = int(code)
    except (TypeError, ValueError):
        return f"HTTP {code}"
    kind = "서버 오류" if n >= 500 else "클라이언트 오류" if n >= 400 else "정상 응답" if n < 300 else "리다이렉트"
    return f"HTTP {n}({kind})"


def _duration(j: dict) -> str:
    if isinstance(j.get("duration_ms"), (int, float)):
        return f", 처리 {j['duration_ms']:.0f}ms"
    if isinstance(j.get("duration_seconds"), (int, float)):
        return f", 처리 {j['duration_seconds'] * 1000:.1f}ms"
    return ""


_MESSAGES: list[tuple[re.Pattern, Callable[[re.Match], str]]] = [
    (re.compile(r"^(\w[\w-]*) call retry(?: item=(\S+))?(?: attempt=(\d+))?(?: error=(\S+))?"),
     lambda m: (f"{m.group(1)} 호출을 재시도합니다" + (f"(품목 {m.group(2)}" if m.group(2) else "(")
                + (f", {m.group(3)}번째 시도" if m.group(3) else "") + (f", 오류 {m.group(4)}" if m.group(4) else "") + ").")),
    (re.compile(r"^(\w[\w-]*) call failed(?: item=(\S+))?(?: error=(\w+))?"),
     lambda m: (f"{m.group(1)} 호출이 실패했습니다" + (f"(품목 {m.group(2)}" if m.group(2) else "(")
                + (f", 오류 {m.group(3)}" if m.group(3) else "") + ").")),
    (re.compile(r"^stock lookup db_error(?: item=(\S+))?(?: error=(\w+))?"),
     lambda m: f"재고 조회 중 DB 오류가 발생했습니다(품목 {m.group(1) or '미기록'}, {m.group(2) or '오류 미기록'})."),
    (re.compile(r"^order db lookup failed(?: item=(\S+))?(?: error=(\w+))?"),
     lambda m: f"주문 DB 조회가 실패했습니다(품목 {m.group(1) or '미기록'}, {m.group(2) or '오류 미기록'})."),
]


def _message_ko(msg: str) -> str | None:
    msg = (msg or "").strip()
    for pat, fn in _MESSAGES:
        m = pat.search(msg)
        if m:
            base = fn(m)
            cause = _cause_ko(msg)
            return base + (f" {cause}." if cause else "")
    if is_korean(msg):
        return msg
    cause = _cause_ko(msg)
    return f"{cause}." if cause else None


def _json_log_ko(j: dict, svc: str | None) -> str | None:
    service = j.get("service")
    if isinstance(service, dict):
        service = service.get("name")
    service = service or svc or "서비스"
    event = j.get("event")
    if event in ("http_access", None) and j.get("method") and j.get("uri") and j.get("status") is not None:
        return f"{service} 서비스가 {j['method']} {j['uri']} 요청에 {_status_text(j['status'])}로 응답했습니다{_duration(j)}."
    if event == "client_disconnected":
        err = f"({j['error']})" if j.get("error") else ""
        return f"{service} 서비스가 응답을 보내는 중 클라이언트 연결이 끊겼습니다{err}."
    if event == "self_traffic_failed":
        return f"{service} 서비스의 자체 점검 트래픽 요청이 실패했습니다."
    if event == "self_traffic_transport":
        return f"{service} 서비스의 자체 점검 트래픽 전송 중 오류가 발생했습니다" + (f"({j['error']})." if j.get("error") else ".")
    if event == "crash_exit":
        return f"{service} 프로세스가 종료 코드 {j.get('exit_code')}로 종료되었습니다."
    if event in ("chaos_changed", "chaos_restored"):
        active = ", ".join(str(a) for a in j.get("active") or []) or "없음"
        verb = "복원" if event == "chaos_restored" else "변경"
        return f"{service} 서비스의 장애 실험 설정이 {verb}되었습니다(활성 항목: {active})."
    if event == "app_exception":
        return f"{service} 서비스에서 애플리케이션 예외 {j.get('exception') or '미기록'}가 발생했습니다."
    msg = j.get("message") or j.get("msg")
    if isinstance(msg, str) and msg.strip():
        ko = _message_ko(msg)
        return ko
    return None


_STACK_NODE = re.compile(r"^\s*at\s+(?:(\S+)\s+)?\(?([^()\s]+?):(\d+):\d+\)?\s*$")
_STACK_JAVA = re.compile(r"^\s*at\s+([\w.$<>]+)\(([^:()]+):(\d+)\)\s*$")
_STACK_PY = re.compile(r'^\s*File "([^"]+)", line (\d+), in (\S+)')
_EXC = re.compile(r"^((?:[A-Za-z_][\w.$]*)?(?:Error|Exception|error|Exit|Interrupt))(?::\s*(.*))?$", re.S)
_ENVOY_ACCESS = re.compile(r'"([A-Z]+) (\S+) HTTP/[\d.]+"\s+(\d{3})\s+(\S+)')
_LOGFMT = re.compile(r"^level=(\w+)\s+system=(\S+)\s+service=(\S+)")
_CODE_LINE = re.compile(r"^\s{2,}(raise |return |with |if |for |self\.|await |[\w.]+\s*=\s|[\w.]+\()")


def _exc_ko(name: str, msg: str | None) -> str:
    short = name.rsplit(".", 1)[-1].rsplit("$", 1)[-1]
    msg = (msg or "").strip()
    if short == "HTTPError":
        m = re.search(r"HTTP Error (\d{3})", msg)
        if m:
            return f"{short}: 호출 대상이 {_status_text(m.group(1))}를 반환했습니다."
    if short == "IntermittentException" or "intermittent" in msg.lower():
        return f"{short}: 간헐적 업무 처리 실패 예외가 발생했습니다."
    if msg and is_korean(msg):
        return f"{short}: {msg}"
    cause = _cause_ko(msg or short)
    if cause:
        return f"{short}: {cause}."
    if msg.lower() == "aborted":
        return f"{short}: 요청이 중단(aborted)되었습니다."
    return f"{short} 예외가 발생했습니다(메시지는 원문 참조)."


def log_ko(raw: str) -> str | None:
    text = (raw or "").rstrip()
    m = _PREFIX.match(text)
    svc = m.group(1) if m else None
    body = text[m.end():] if m else text
    rep = _REPEATED.search(body)
    times = rep.group(1) if rep else None
    body = (body[:rep.start()] if rep else body).rstrip()
    stripped = body.strip()
    if not stripped:
        return None
    if stripped.startswith("{"):
        try:
            j = json.loads(stripped)
        except ValueError:
            j = None
        if isinstance(j, dict):
            ko = _json_log_ko(j, svc)
            return (ko + _rep(times)) if ko else None
    ko = _plain_ko(stripped, body, svc)
    return (ko + _rep(times)) if ko else None


def _plain_ko(s: str, body: str, svc: str | None) -> str | None:
    low = s.lower()
    if "request to probe app failed" in low:
        cause = _cause_ko(s)
        return "Istio 사이드카가 앱 상태 확인(probe) 요청을 전달했지만 실패했습니다" + (f": {cause}." if cause else ".")
    m = _ENVOY_ACCESS.search(s)
    if m:
        flags = m.group(4)
        detail = []
        if "UF" in flags:
            detail.append("상류 서비스 연결 실패(UF)")
        if "URX" in flags:
            detail.append("재시도 한도 초과(URX)")
        if "UT" in flags.split(","):
            detail.append("상류 응답 시간 초과(UT)")
        return (f"사이드카 접근 기록: {m.group(1)} {m.group(2)} 요청이 {_status_text(m.group(3))}로 끝났습니다"
                + (f" — {', '.join(detail)}." if detail else "."))
    if "java_tool_options" in low:
        return "JVM이 시작하며 JAVA_TOOL_OPTIONS 설정을 적용했다는 정보 로그입니다."
    if "envoy command" in low or re.search(r"\bflag: --", low):
        return "Istio 사이드카(Envoy) 시작 설정을 기록한 정보 로그입니다."
    if "fsnotify" in low and "too many open files" in low:
        return "파일 변경 감시(fsnotify)를 만들지 못했습니다. 열린 파일 한도를 넘었습니다."
    if "failed to watch directory" in low:
        return "로그 디렉터리 감시를 시작하지 못했습니다(경로 없음)."
    m = _LOGFMT.match(s)
    if m:
        level = {"ERROR": "오류", "WARN": "경고", "INFO": "정보"}.get(m.group(1).upper(), m.group(1))
        return f"{m.group(3)} 서비스의 {level} 로그 머리글입니다. 상세 내용은 다음 줄에 이어집니다."
    m = _STACK_PY.match(s)
    if m:
        name = m.group(1).rsplit("/", 1)[-1]
        return f"예외 호출 경로(스택)의 한 줄입니다: {m.group(3)} ({name}:{m.group(2)})."
    m = _STACK_JAVA.match(s)
    if m:
        method = ".".join(m.group(1).split(".")[-2:])
        return f"예외 호출 경로(스택)의 한 줄입니다: {method} ({m.group(2)}:{m.group(3)})."
    m = _STACK_NODE.match(s)
    if m:
        where = f"{m.group(2)}:{m.group(3)}"
        return f"예외 호출 경로(스택)의 한 줄입니다: " + (f"{m.group(1)} ({where})." if m.group(1) else f"{where}.")
    if low.startswith("the above exception was the direct cause"):
        return "위 예외가 다음 예외의 직접 원인입니다(예외 연쇄)."
    if low.startswith("during handling of the above exception"):
        return "위 예외를 처리하는 중 다른 예외가 발생했습니다."
    if low.startswith("traceback (most recent call last)"):
        return "예외 호출 경로(스택)가 시작됩니다."
    for pat, fn in _MESSAGES:
        mm = pat.search(s)
        if mm:
            cause = _cause_ko(s)
            return fn(mm) + (f" {cause}." if cause else "")
    m = re.match(r"^Caused by:\s*(?:[\w.$]*Error:\s*|[\w.$]*Exception:\s*)?(.*)$", s)
    if m:
        msg = m.group(1).strip()
        cause = _cause_ko(msg)
        return f"원인 예외: {msg if is_korean(msg) else cause or msg}" + ("" if (is_korean(msg) or cause) else " (원문)") + "."
    if _CODE_LINE.match(body):
        if s.startswith("raise "):
            exc = re.match(r"raise\s+([\w.]+)", s)
            return f"예외를 발생시킨 소스 코드 한 줄입니다(raise {exc.group(1) if exc else ''})."
        return "예외 호출 경로에 표시된 소스 코드 한 줄입니다."
    m = _EXC.match(s)
    if m:
        return _exc_ko(m.group(1), m.group(2))
    return None


# ------------------------------------------------------------------ Kubernetes events
_EVENT = re.compile(r"^(Normal|Warning)\s+(\w+)\s+(\S+):\s*(.*?)(?:\s+\(x(\d+)\))?$", re.S)
_PROBE = {"readiness": "준비 상태 확인", "liveness": "생존 상태 확인", "startup": "시작 상태 확인"}


def _probe_detail(msg: str) -> str:
    m = re.search(r"statuscode:\s*(\d{3})", msg)
    if m:
        return f"앱이 HTTP {m.group(1)}를 반환했습니다"
    cause = _cause_ko(msg)
    if cause:
        return cause
    if "read tcp" in msg.lower() or "eof" in msg.lower():
        return "응답을 읽는 중 연결이 끊겼습니다"
    return "실패 사유는 원문 참조"


def event_ko(raw: str) -> str | None:
    m = _EVENT.match((raw or "").strip())
    if not m:
        return None
    _type, reason, obj, msg, times = m.groups()
    rep = f" ({times}회 반복)" if times else ""
    ref = obj.split("/", 1)[-1]
    out = None
    if reason == "Started":
        out = f"{ref}의 컨테이너가 시작되었습니다."
    elif reason == "Created":
        out = f"{ref}의 컨테이너를 만들었습니다."
    elif reason == "Pulled":
        img = re.search(r'image "([^"]+)"', msg)
        if "already present" in msg:
            out = f"컨테이너 이미지 {img.group(1) if img else ''}가 노드에 이미 있어 새로 내려받지 않았습니다."
        else:
            out = f"컨테이너 이미지 {img.group(1) if img else ''}를 정상적으로 내려받았습니다."
    elif reason == "Pulling":
        img = re.search(r'image "([^"]+)"', msg)
        out = f"컨테이너 이미지 {img.group(1) if img else ''}를 내려받는 중입니다."
    elif reason == "Killing":
        c = re.search(r"Stopping container (\S+)", msg)
        out = f"{ref}의 {c.group(1) if c else ''} 컨테이너를 종료하는 중입니다."
    elif reason == "Unhealthy":
        p = re.match(r"(\w+) probe failed", msg)
        kind = _PROBE.get((p.group(1) if p else "").lower(), "상태 확인")
        out = f"{ref}의 {kind}(probe)이 실패했습니다: {_probe_detail(msg)}."
    elif reason == "SandboxChanged":
        out = f"{ref}의 Pod 샌드박스가 바뀌어 Pod를 종료하고 다시 만듭니다."
    elif reason == "ScalingReplicaSet":
        s = re.search(r"Scaled (up|down) replica set (\S+) from (\d+) to (\d+)", msg)
        if s:
            verb = "늘렸습니다" if s.group(1) == "up" else "줄였습니다"
            out = f"ReplicaSet {s.group(2)}의 Pod 수를 {s.group(3)}개에서 {s.group(4)}개로 {verb}(롤아웃 진행)."
    elif reason == "Scheduled":
        s = re.search(r"assigned (\S+) to (\S+)", msg)
        if s:
            out = f"Pod {s.group(1).split('/')[-1]}를 노드 {s.group(2)}에 배치했습니다."
    elif reason == "SuccessfulCreate":
        s = re.search(r"Created pod: (\S+)", msg)
        out = f"{ref}가 Pod {s.group(1) if s else ''}를 만들었습니다."
    elif reason == "SuccessfulDelete":
        s = re.search(r"Deleted pod: (\S+)", msg)
        out = f"{ref}가 Pod {s.group(1) if s else ''}를 삭제했습니다."
    elif reason == "FailedSync":
        out = f"{ref}의 상태를 컨테이너 런타임에서 확인하지 못했습니다(동기화 실패)."
    elif reason == "FailedMount":
        v = re.search(r'volume "([^"]+)"', msg)
        tail = " 참조하는 객체가 아직 등록되지 않았습니다." if "not registered" in msg else ""
        out = f"{ref}의 볼륨 {v.group(1) if v else ''}을(를) 마운트하지 못했습니다.{tail}"
    elif reason == "BackOff":
        img = re.search(r'Back-off pulling image "([^"]+)"', msg)
        c = re.search(r"Back-off restarting failed container (\S+)", msg)
        if img:
            out = f"이미지 {img.group(1)} 내려받기에 실패해 잠시 기다렸다가 재시도합니다."
        elif c:
            out = f"{c.group(1)} 컨테이너가 계속 종료되어 Kubernetes가 대기 시간을 늘리며 재시작을 반복합니다."
    return (out + rep) if out else None


# ------------------------------------------------------------------ incident status, titles, signals
def _lv(level):
    from .roles_ko import LEVEL_KO
    return LEVEL_KO.get(level or "", level or "미정")


_VERDICT_TEXT = {"supported": "'근거로 지지'", "insufficient": "'근거 부족'", "contradicted": "'반박됨'"}
_LIMIT_KO = {"deadline": "시간 한도", "loop": "Agent 호출 한도", "cost": "비용 한도", "budget": "사건 예산"}


def _group_reason_ko(reason: str) -> str:
    r = reason.strip()
    if r.startswith("independent verifier supports evidence-grounded candidates"):
        return "독립 검증이 근거 있는 원인 후보를 지지했습니다."
    m = re.match(r"independent verification verdict (\w+)", r)
    if m:
        v = m.group(1)
        if v == "supported":
            return "독립 검증 판정은 '근거로 지지'였지만 인용·신뢰도 등 최종 수용 기준을 충족하지 못했습니다."
        if v == "unavailable":
            return "독립 검증 결과가 없어 원인을 확정하지 못했습니다."
        return f"독립 검증 판정이 {_VERDICT_TEXT.get(v, v)}이라 원인을 확정하지 못했습니다."
    if r.startswith("primary agent produced no valid output"):
        return "주 분석 Agent가 유효한 결과를 만들지 못했습니다."
    if r.startswith("no evidence-grounded"):
        return "근거로 확인되고 반박되지 않은 원인 후보가 없습니다."
    m = re.match(r"limit reached \((\w+)\)(?::\s*(\d+)s elapsed)?", r)
    if m:
        kind = _LIMIT_KO.get(m.group(1), "한도")
        return f"{kind}에 도달해 분석을 멈췄습니다" + (f"({m.group(2)}초 경과)." if m.group(2) else ".")
    if is_korean(r):
        return r
    return "사유는 원문 참조."


def status_reason_ko(raw: str | None) -> str | None:
    r = (raw or "").strip()
    if not r:
        return None
    if is_korean(r):
        return r
    from .flowgraph import ACTION_KO
    m = re.match(r"^(SIMPLE|NORMAL|COMPLEX|HIGH_RISK) group (verified|unverified|stopped|failed): (.*)$", r, re.S)
    if m:
        st = {"verified": "검증 통과", "unverified": "검증 미통과", "stopped": "중단", "failed": "실패"}[m.group(2)]
        return f"{_lv(m.group(1))} Group 분석 {st} — {_group_reason_ko(m.group(3))}"
    if r == "stopped_before_start":
        return "분석 시작 전에 운영자 중단으로 멈췄습니다."
    if r == "stopped":
        return "분석 도중 중단되었습니다."
    m = re.match(r"^(\w+) succeeded and verified$", r)
    if m:
        return f"{ACTION_KO.get(m.group(1), m.group(1))} 조치가 성공했고 사후 검증도 통과했습니다."
    m = re.match(r"^action failed: (.*)$", r, re.S)
    if m:
        return f"조치 실패: 실행 중 오류가 발생했습니다({m.group(1)[:120]})."
    m = re.match(r"^action (\w+): (.*)$", r, re.S)
    if m:
        from .routing_ko import ACTION_STATUS_KO
        return f"조치 {ACTION_STATUS_KO.get(m.group(1), m.group(1))}: {m.group(2)[:160]}"
    m = re.match(r"^single agent proposed no Kubernetes action \(next_action=(\w+)\)", r)
    if m:
        from .routing_ko import NEXT_ACTION_KO
        return f"단일 Agent가 Kubernetes 조치를 제안하지 않았습니다(다음 행동: {NEXT_ACTION_KO.get(m.group(1), m.group(1))})."
    if r.startswith("HIGH_RISK policy"):
        return "고위험 정책에 따라 자동 조치 없이 운영자에게 인계했습니다."
    m = re.match(r"^LiveProbe restarted while incident was '(\w+)'", r)
    if m:
        state = {"analyzing": "분석", "acting": "조치", "routing": "수준 판단"}.get(m.group(1), m.group(1))
        return f"LiveProbe가 {state} 처리 중에 재시작되어 이 장애를 실패로 기록했습니다(다시 실행하지 않음)."
    m = re.match(r"^action rejected by (.+)$", r)
    if m:
        return f"{m.group(1)}님이 조치를 거절했습니다."
    if r == "waiting for operator approval":
        return "운영자 승인을 기다리고 있습니다."
    if r.startswith("action was not executable"):
        return "조치를 실행할 수 없어 운영자에게 인계했습니다."
    from .routing_ko import _RULE_PREFIX
    for pat, ko in _RULE_PREFIX:
        if pat.search(r):
            return ko
    return None


def error_ko(raw: str | None) -> str | None:
    r = (raw or "").strip()
    if not r:
        return None
    if is_korean(r):
        return r
    m = re.match(r"^(\w+):\s*(.*)$", r, re.S)
    kind, detail = (m.group(1), m.group(2)) if m else ("", r)
    if kind == "timeout":
        s = re.search(r"exceeded (\d+)s", detail)
        return f"모델 호출이 제한 시간{f'({s.group(1)}초)' if s else ''}을 넘어 실패했습니다."
    return {"quota": "모델 사용량 한도에 걸려 실패했습니다.", "budget": "사건 예산(호출·비용·시간)이 소진되어 호출하지 않았습니다.",
            "invalid_output": "모델 출력이 형식 계약을 지키지 않아 거부했습니다.", "auth": "모델 인증에 실패했습니다.",
            "transport": "모델 공급자 호출 중 통신 오류가 발생했습니다.", "provider": "모델 공급자 호출 중 오류가 발생했습니다.",
            "config": "모델 설정 오류로 호출하지 못했습니다.", "RunStopped": "운영자 중단으로 실행을 멈췄습니다."}.get(
        kind, "실행 오류가 기록되었습니다(원문 참조).")


SIGNAL_SOURCE_KO = {"alertmanager": "Prometheus 경보(Alertmanager)", "app_log": "애플리케이션 로그 감시",
                    "k8s_admission_audit": "Kubernetes API 감사", "k8s_cni_event": "kubelet CNI 경고",
                    "k8s_disk_pressure": "노드 디스크 압박과 Pod 퇴출",
                    "k8s_hpa_metrics": "자원 지표 API와 HPA 상태",
                    "pod_watch": "Pod 상태 감시", "event_watch": "Kubernetes 이벤트 감시",
                    "workload_poller": "워크로드 상태 점검",
                    "endpoint_poller": "Service 엔드포인트 점검", "network": "네트워크 관측", "hubble": "네트워크 관측(Hubble)",
                    "evaluation": "평가 실행", "operator": "운영자"}


def signal_source_ko(source: str | None) -> str | None:
    if not source:
        return None
    return SIGNAL_SOURCE_KO.get(source, source)


def signal_reason_ko(reason: str | None) -> str | None:
    if not reason:
        return None
    from .flowgraph import SIGNAL_KO
    return SIGNAL_KO.get(reason)


def title_ko(title: str | None, reasons: list[str] | None = None) -> str | None:
    """'TargetServiceNoEndpoints on Service/x' → 'Service 연결 대상 없음 · Service/x'. 모르는 신호는 None."""
    t = (title or "").strip()
    if not t:
        return None
    if is_korean(t):
        return t
    m = re.match(r"^(\w+) on (\S+)$", t)
    reason, obj = (m.group(1), m.group(2)) if m else ((reasons or [None])[0], None)
    ko = signal_reason_ko(reason)
    if not ko:
        return None
    return f"{ko} · {obj}" if obj else ko


def korean_self_ko(raw: str) -> str | None:
    """이미 한국어로 쓴 Agent 결론·판단·인계 문장은 그대로 읽기다. 후보 줄의 영문 표기만 바꾼다."""
    if not is_korean(raw):
        return None
    return re.sub(r"\(confidence ([\d.]+|None)\)", lambda m: f"(신뢰도 {m.group(1) if m.group(1) != 'None' else '미기록'})", raw.strip())


# ------------------------------------------------------------------ what a screen can show
# Mirrors the dashboard display boundary (dashboard/src/presentation.ts TECHNICAL): a summary that carries a URL,
# braces, a stack frame, a hash or an exception class name is not shown, so it is not accepted as a summary either.
_TECHNICAL = re.compile(
    r"(?:https?://|\b(?:kubectl|curl|wget|Traceback|Exception|Error:|sha256|resourceVersion)\b|\b\d{1,3}(?:\.\d{1,3}){3}\b"
    r"|[a-f\d]{32,}|[{}]|</?[a-z]|\b\w*(?:Error|Exception)\b|HTTP/\d|\bat \S+\([^)]*:\d+|\n\s*(?:at |File |[{$]))",
    re.I | re.A)
# Identifiers an operator still needs (file, class, field and service names, numbers) do not count against Korean.
_IDENT = re.compile(r"`[^`]*`|\b\w+(?:\.\w+)+\b|[\w.-]*/[\w./-]+|\b[a-z]+[A-Z]\w*\b|\b[A-Z][a-z]+[A-Z]\w*\b|\b[\w.]*\d[\w.%:]*\b"
                    r"|\b[a-z_]+==?\S+", re.A)


def display_safe(text: str | None) -> bool:
    """The dashboard shows this Korean text as it is (it is not hidden by the technical-text filter)."""
    return bool(text) and bool(HANGUL.search(text)) and not _TECHNICAL.search(text)


def summary_ok(text: str | None) -> bool:
    """A Korean summary the screens can show: displayable, at least four Hangul letters, and mostly Korean once the
    identifiers it keeps (paths, class and field names, numbers) are set aside."""
    if not text or not display_safe(text) or len(HANGUL.findall(text)) < 4:
        return False
    return hangul_ratio(_IDENT.sub(" ", text)) >= 0.5
