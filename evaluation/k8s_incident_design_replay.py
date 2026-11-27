"""제안한 판정의 합성 사례. Python 3.10+ 표준 라이브러리만 사용한다.

각 입력 묶음은 같은 클러스터의 한 조사 구간이다. 시각은 구간 내 초다.
공급원 조회 실패와 성공적으로 조회한 빈 관측을 구분한다. 제품 감시기,
네트워크, DB를 호출하지 않는다. 저장소 루트에서
`python3 evaluation/k8s_incident_design_replay.py`로 실행한다.
"""

import re
from typing import NamedTuple


NO = "조건 미충족"
UNKNOWN = "미관측"
WARN = "사전 주의"
INCIDENT = "장애"


class Pod(NamedTuple):
    at: int
    uid: str
    scheduled: str | None = "False"  # PodScheduled condition, None은 condition 부재
    reason: str = "Unschedulable"
    node: str = ""
    transition: int = 0  # 해당 condition의 lastTransitionTime
    gated: bool = False
    deleting: bool = False


def schedule(*observations):
    # 현재 UID의 연속된 condition만 인정한다. 정상 관측 또는 조회 단절은 구간을 끊는다.
    first = None
    key = None
    previous_at = None
    for pod in observations:
        if (pod is None or pod.scheduled != "False" or pod.reason != "Unschedulable"
                or pod.node or pod.gated or pod.deleting):
            first = key = None
            continue
        if key != (pod.uid, pod.transition) or not 0 < pod.at - previous_at <= 120:
            key = (pod.uid, pod.transition)
            first = pod.at
        previous_at = pod.at
    if not observations or observations[-1] is None:
        return UNKNOWN
    if first is not None and observations[-1].at - first >= 120:
        return INCIDENT
    return NO


class Target(NamedTuple):
    gvk: str
    namespace: str
    name: str


class Rejection(NamedTuple):
    target: Target
    reason: str  # 적용 결과에서 확인한 GVK 미지원 또는 다른 오류


def api_version(target, discovery_status, served, rejection=None):
    # served는 대상 GVK에 대한 완전한 discovery 결과다. 빈/잘린 목록을 False로 넣지 않는다.
    if discovery_status != 200 or served is None:
        return UNKNOWN
    if rejection is not None:
        if rejection.target != target or rejection.reason != "GVKNotServed":
            return UNKNOWN
        return UNKNOWN if served else INCIDENT
    return NO if served else WARN


class Provider(NamedTuple):
    at: int = 60
    api: str = "v1beta1.metrics.k8s.io"
    available: str = "False"
    reason: str = "MissingEndpoints"


class HPA(NamedTuple):
    uid: str
    at: int = 60
    metric_type: str = "Resource"  # 실패한 metric spec의 type
    scaling_active: str = "False"
    reason: str = "FailedGetResourceMetric"
    event: str = ""  # 동반된 경고 이벤트. HPA condition 및 대상 UID와 별도다.


def metric_supply(provider, hpas):
    if provider is None:
        return UNKNOWN
    if (provider.api, provider.available, provider.reason) != (
        "v1beta1.metrics.k8s.io", "False", "MissingEndpoints"
    ):
        return NO
    if hpas is None:
        return "수집 장애 · HPA 미관측"
    impacted = {
        hpa.uid for hpa in hpas
        if abs(hpa.at - provider.at) <= 120
        and hpa.metric_type == "Resource"
        and hpa.scaling_active == "False" and hpa.reason == "FailedGetResourceMetric"
    }
    return f"수집 장애 1건 · 영향 HPA {len(impacted)}개"


class NodeFailure(NamedTuple):
    uid: str
    at: int
    ready: str
    diagnostic: str
    # ExplicitSkewRejection은 오류가 양쪽 버전 조합을 직접 지목했음을 뜻한다.
    # Kubernetes의 공통 Event reason을 가정하지 않는다.


def minor(version):
    match = re.fullmatch(r"v?1\.(\d+)\.\d+(?:\+[\w.-]+)?", version)
    return int(match[1]) if match else None


def cgroup_start(version, hierarchy, fail_on_v1, startup_reason=None):
    # 같은 Linux 노드·부팅의 자료. "default"는 별도 설정이 없음을 확인한 경우다.
    # None은 설정을 읽지 못한 경우이며, 관측 대상인 1.34~1.36에서 기본값을 비교한다.
    release = minor(version)
    if release not in (34, 35, 36) or hierarchy not in ("v1", "v2"):
        return UNKNOWN
    if hierarchy == "v2":
        return NO
    if fail_on_v1 not in (True, False, "default"):
        return UNKNOWN
    rejects_v1 = release >= 35 if fail_on_v1 == "default" else fail_on_v1
    if not rejects_v1:
        return NO
    return INCIDENT if startup_reason == "CgroupV1Rejected" else WARN


def skew(servers, kubelet, failure=None, *, uid="node-a", at=60, inventory_complete=True):
    if not inventory_complete or not servers or kubelet is None:
        return UNKNOWN
    apis = [minor(server) for server in servers]
    node = minor(kubelet)
    if node is None or node < 25 or None in apis or min(apis) < 25:
        return UNKNOWN
    oldest, newest = min(apis), max(apis)
    node_outside = node > oldest or node < newest - 3
    if node_outside and failure and failure.uid == uid and abs(failure.at - at) <= 120:
        if failure.ready == "False" and failure.diagnostic == "ExplicitSkewRejection":
            return "버전 원인 유력"
    return WARN if node_outside or newest - oldest > 1 else NO


def main():
    ingress = Target("networking.k8s.io/v1beta1/Ingress", "dev", "shop")
    other = ingress._replace(name="other")
    api_ha = ["v1.35.2", "v1.36.2+k3s1"]
    rejection = Rejection(ingress, "GVKNotServed")
    hpa = HPA("shop-hpa")
    failure = NodeFailure("node-a", 60, "False", "ExplicitSkewRejection")
    cases = [
        ("Pod · 2분 경계", INCIDENT, schedule(Pod(0, "a"), Pod(120, "a"))),
        ("Pod · 119초", NO, schedule(Pod(0, "a"), Pod(119, "a"))),
        ("Pod · Pending만 관측", NO, schedule(Pod(180, "a", scheduled=None, reason=""))),
        ("Pod · 이미 노드 배정", NO, schedule(Pod(0, "a"), Pod(180, "a", node="worker"))),
        ("Pod · 중간 회복", NO, schedule(Pod(0, "a"), Pod(80, "a", scheduled="True"), Pod(150, "a", transition=100))),
        ("Pod · 다른 UID", NO, schedule(Pod(0, "a"), Pod(180, "b"))),
        ("Pod · 미관측", UNKNOWN, schedule(Pod(0, "a"), None)),
        ("Pod · 관측 단절 뒤 재개", NO, schedule(Pod(0, "a"), None, Pod(150, "a"))),
        ("Pod · 관측 시각이 10분 비었음", NO, schedule(Pod(0, "a"), Pod(600, "a"))),
        ("Pod · scheduling gate 대기", NO, schedule(Pod(0, "a", gated=True), Pod(120, "a", gated=True))),
        ("Pod · 삭제 중", NO, schedule(Pod(0, "a"), Pod(120, "a", deleting=True))),
        ("GVK · 제거된 API 사전 발견", WARN, api_version(ingress, 200, False)),
        ("GVK · 같은 대상 적용 거부", INCIDENT, api_version(ingress, 200, False, rejection)),
        ("GVK · discovery 403", UNKNOWN, api_version(ingress, 403, None, rejection)),
        ("GVK · discovery 503", UNKNOWN, api_version(ingress, 503, None, rejection)),
        ("GVK · 다른 객체의 거부", UNKNOWN, api_version(ingress, 200, False, Rejection(other, "GVKNotServed"))),
        ("GVK · 일반 객체의 404", UNKNOWN, api_version(ingress, 200, True, Rejection(ingress, "ObjectNotFound"))),
        ("GVK · 제공 API", NO, api_version(ingress, 200, True)),
        ("지표 · 동시 HPA 2개", "수집 장애 1건 · 영향 HPA 2개", metric_supply(Provider(), [hpa, HPA("cart-hpa")])),
        ("지표 · 계산 실패 경고 중복", "수집 장애 1건 · 영향 HPA 1개", metric_supply(Provider(), [hpa, hpa._replace(event="FailedComputeMetricsReplicas")])),
        ("지표 · 다른 지표·오래된 관측", "수집 장애 1건 · 영향 HPA 0개", metric_supply(Provider(), [hpa._replace(metric_type="External"), hpa._replace(at=240)])),
        ("지표 · 계산 실패 경고만", "수집 장애 1건 · 영향 HPA 0개", metric_supply(Provider(), [hpa._replace(reason="", event="FailedComputeMetricsReplicas")])),
        ("지표 · 공급원 정상", NO, metric_supply(Provider(available="True", reason="Passed"), [hpa])),
        ("지표 · 공급원 조회 실패", UNKNOWN, metric_supply(None, [hpa])),
        ("지표 · HPA 조회 실패", "수집 장애 · HPA 미관측", metric_supply(Provider(), None)),
        ("버전 · HA 중 가장 낮은 API보다 높은 kubelet", WARN, skew(api_ha, "v1.36.2")),
        ("버전 · 높은 API보다 세 minor 낮음", NO, skew(api_ha, "v1.33.2")),
        ("버전 · 높은 API보다 네 minor 낮음", WARN, skew(api_ha, "v1.32.2")),
        ("버전 · 서버 간 두 minor 차이", WARN, skew(["v1.34.2", "v1.36.2"], "v1.34.2")),
        ("버전 · 같은 노드 호환 오류", "버전 원인 유력", skew(api_ha, "v1.32.2", failure)),
        ("버전 · NotReady만 관측", WARN, skew(api_ha, "v1.32.2", failure._replace(diagnostic=""))),
        ("버전 · 다른 노드의 오류", WARN, skew(api_ha, "v1.32.2", failure._replace(uid="node-b"))),
        ("버전 · 과거 장애", WARN, skew(api_ha, "v1.32.2", failure._replace(at=-300))),
        ("버전 · HA 목록 미완성", UNKNOWN, skew(api_ha, "v1.35.2", inventory_complete=False)),
        ("버전 · 버전 미관측", UNKNOWN, skew(api_ha, None)),
        ("cgroup · 1.34의 기본값", NO, cgroup_start("v1.34.2", "v1", "default")),
        ("cgroup · 1.35에서 기본값 변경", WARN, cgroup_start("v1.35.0", "v1", "default")),
        ("cgroup · 같은 부팅의 기동 거부", INCIDENT, cgroup_start("v1.35.0", "v1", True, "CgroupV1Rejected")),
        ("cgroup · 다른 이유로 기동 실패", WARN, cgroup_start("v1.35.0", "v1", True, "SocketUnavailable")),
        ("cgroup · 관리자가 설정 재정의", NO, cgroup_start("v1.35.0", "v1", False)),
        ("cgroup · 로컬과 같은 v2 노드", NO, cgroup_start("v1.36.2+k3s1", "v2", True)),
        ("cgroup · cgroupfs는 계층 버전 미상", UNKNOWN, cgroup_start("v1.36.2+k3s1", "cgroupfs", True)),
        ("cgroup · v1 노드의 유효 설정 미관측", UNKNOWN, cgroup_start("v1.36.2", "v1", None)),
    ]
    passed = 0
    for label, expected, actual in cases:
        ok = expected == actual
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {label} | 기대 {expected} | 실제 {actual}")
    print(f"{passed}/{len(cases)} 통과")
    return int(passed != len(cases))


if __name__ == "__main__":
    raise SystemExit(main())
