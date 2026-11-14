"""P18 W6 실배포 관측: 근거 수집 행 중 엔드포인트·지표·코드 참조가 '한글 요약이 없습니다'로 남았다(#1081).

세 출처는 구조화된 값이라 기록된 필드만으로 고정 문장을 만든다. 비어 있는 지표(미관측)는 0으로 쓰지 않는다.
"""
import json

from app.interpret import rule_ko


def _item(ref, kind, payload):
    return {"id": f"evidence:1:{ref}#0", "ref": ref, "kind": kind, "raw": json.dumps(payload, ensure_ascii=False)}


def test_endpoints_row_reads_ready_addresses():
    assert rule_ko(_item("E3_service", "status", {"ready_addresses": 1})) == "엔드포인트에 준비된 주소가 1개 등록되어 있습니다."
    assert rule_ko(_item("E3_service", "status", {"ready_addresses": 0})) == \
        "엔드포인트에 준비된 주소가 0개라 요청을 받을 Pod가 없습니다."


def test_metrics_row_states_only_observed_values_and_never_fills_unobserved_with_zero():
    raw = {"db_pool_acquire_timeouts_5m": {}, "db_pool_active": {}, "db_pool_max": {}, "db_pool_pending_threads": {},
           "http_5xx_rate_per_s": {"delivery": 0.0}, "http_rate_per_s": {"delivery": 1.5779},
           "mean_latency_ms_1m": {"delivery": 33.2}, "restarts": {"delivery-579989759-jggnq": 0}}
    ko = rule_ko(_item("E6_metrics", "metric", raw))
    assert ko == "delivery: 초당 요청 1.58건 · 5xx 초당 0건 · 평균 지연 33.2ms. 재시작 합계 0회. DB 연결 풀 지표는 관측되지 않았습니다."
    hot = dict(raw, http_5xx_rate_per_s={"delivery": 0.42}, db_pool_active={"order": 5}, db_pool_max={"order": 5},
               db_pool_pending_threads={"order": 3})
    ko = rule_ko(_item("E6_metrics", "metric", hot))
    assert "5xx 초당 0.42건" in ko and "order 사용 5/5 · 대기 3" in ko and "관측되지 않았습니다" not in ko
    assert rule_ko(_item("E6_metrics", "metric", {"http_rate_per_s": {}, "restarts": {}})) == "수집 시점에 관측된 지표 값이 없습니다."


def test_pool_counts_keep_integer_trailing_zeroes_and_explicit_zero():
    raw = {"db_pool_active": {"order": 10}, "db_pool_max": {"order": 100},
           "db_pool_pending_threads": {"order": 0}, "db_pool_acquire_timeouts_5m": {"order": 20}}
    assert rule_ko(_item("E6_metrics", "metric", raw)) == "DB 연결 풀 order 사용 10/100 · 대기 0 · 5분 획득 시간초과 20건."


def test_other_metric_items_stay_uninterpreted_by_rules():
    assert rule_ko(_item("E4_callers", "metric", {"order->inventory": {"5xx": 1}})) is None


def test_code_refs_row_names_exceptions_and_first_frame():
    raw = {"exceptions": [
        {"exception": "DependencyError", "message": "같은 환경의 업무 연동에 실패했습니다.",
         "frames": [{"file": "/app/dependency.js", "line": 21, "symbol": "callDependency"}]},
        {"exception": "Error", "message": "의존 호출 2초 제한 초과", "frames": []}], "frames": [], "reason": None}
    assert rule_ko({**_item("code_refs", "status", raw), "ref": "code_refs"}) == (
        "최근 예외 2종: DependencyError(같은 환경의 업무 연동에 실패했습니다., /app/dependency.js:21 callDependency), "
        "Error(의존 호출 2초 제한 초과).")
    none = {"frames": [], "exceptions": [], "reason": "최근 예외 로그 미관측"}
    assert rule_ko({**_item("code_refs", "status", none), "ref": "code_refs"}) == "최근 예외 로그가 관측되지 않았습니다(최근 예외 로그 미관측)."


def test_unobserved_rows_still_have_no_reading():
    assert rule_ko({**_item("E6_metrics", "metric", {"http_rate_per_s": {"a": 1}}), "observed": False}) is None
