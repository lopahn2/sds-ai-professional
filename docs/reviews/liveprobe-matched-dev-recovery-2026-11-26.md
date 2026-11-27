# LiveProbe 동일 조건 dev 장애 복구 평가

2026년 11월 26일 KST · 24회 중 23회 판정

## 주요 결론

동일한 상태에서 시작한 **Kubernetes 4종**에서 LiveProbe는 모두 스스로 사건을 접수했으며, 자동 복구는 **0/4**였다. Astra 6.2, Sol 6.2, Terra 6.1 단독 실행은 각각 **4/4** 복구했다. LiveProbe가 인계한 시점에도 업무 장애가 남아 있었다. 최초 접수까지는 45.024~144.289초가 걸렸고, 근거 누락, 그룹 호출 한도, 독립 검증 뒤 조치 미생성 등이 서로 다른 사례에서 확인됐다. [원장 집계](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/matched-summary-native-0812.json)

**Spring Boot JDBC 연결 유출**은 공통 업무 요청을 사용하는 별도 코드 변형으로 시험했다. Astra와 Sol은 같은 회귀 시험에서 수정 전 실패와 수정 후 통과를 확인하고, dev 업무 요청까지 복구했다. Terra의 경우 모델 실행 종료 후에도 독립 요청 두 건이 HTTP 500이었다. LiveProbe도 로그를 자동 접수하고 수정·회귀 시험을 통과했지만, 수정 이미지의 `/app.jar`를 실행 계정이 읽지 못해 dev 배포에서 복구에 실패했다. [Application 제품 시험 판정](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-liveprobe-native-2-adjudicated.json) · [JAR 권한 대조](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-native-candidate-jar-permissions.json)

## 시험 범위와 판정

같은 Rancher Desktop 단일 노드의 `dev`에서 Kubernetes 5종을 기준으로 네 방식을 시험했다. 각 Kubernetes 사례는 노드·Deployment UID, 시작 설정, 주입, 정규화한 최초 오류, 독립 검증의 지문이 일치하는 시행만 대조했다. Application은 `lp-eval-reservation` 한 곳에 비공개 배정한 Spring Boot 변형 네 개를 사용했다. 업무 요청, 기준 배포와 Hikari 설정을 공유했으며 코드 결함은 변형별로 달랐다. OOM은 24회 계획과 이번 집계에서 제외했다. [합의 범위](../plans/liveprobe-matched-dev-recovery-2026-11-26.md) · [시작 지문](../../evaluation/matched_recovery_campaign.py)

LiveProbe에는 장애만 주입했다. 단독 모델에는 실제 첫 오류 한 줄과 `장애를 복구하라.`를 제공했다. 각 모델은 새 격리 세션에서 `xhigh`와 시험 대상에 한정한 도구를 사용했다. 모델 세션에 LiveProbe API·DB·RAG·실행기나 다른 실행의 대화가 전달되지 않았다. 자동 접수·수집과 정책 검증을 포함한 제품 응답과, 사람이 오류를 전달한 단독 세션을 비교하는 시험이다. [단독 실행 경계](../../evaluation/isolated_recovery_agent.py)

복구 시간은 주입 승인부터 독립 확인까지의 경과다. 실패 판정 시에도 원복 직전 실제 업무 장애나 지속 적체를 확인했다. 시험기가 수행한 원복은 복구 성과에서 제외했다. 토큰은 관측된 입·출력 토큰의 합이며, 비용은 단가 환산액이다. LiveProbe의 화면용 해석 호출과 구독 요금은 비교 비용에서 제외했다. [판정·비용 집계 코드](../../evaluation/matched_recovery_summary.py)

## Kubernetes 대조 결과

네 방식 모두 판정된 사례 4종의 집계다. 복구 시간 중앙값은 실제 복구한 시행에 대해 계산했다. 토큰과 비용은 4종의 복구 여부와 관계없이 관측값을 합했다. [시행별 시간·사용량](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/matched-summary-native-0812.json)

| 방식 | 실제 복구 | 복구 시간 중앙값 | 분석 토큰 합 | 추정 비용 합 |
| --- | ---: | ---: | ---: | ---: |
| LiveProbe | 0/4 | 미관측 | 655,545 | $3.052013 |
| Astra 6.2 `xhigh` | 4/4 | 35.119초 | 333,367 | $0.616120 |
| Sol 6.2 `xhigh` | 4/4 | 29.481초 | 378,553 | $0.279408 |
| Terra 6.1 `xhigh` | 4/4 | 34.366초 | 317,815 | $0.076979 |

| 장애 | LiveProbe의 자동 접수·종료 | Astra 복구 | Sol 복구 | Terra 복구 |
| --- | --- | ---: | ---: | ---: |
| 주문 이미지와 재고 응답 규격 불일치 | 72.075초 접수 · 325.590초 인계 | 47.748초 | 39.943초 | 44.059초 |
| 상류는 회복한 뒤에도 호출자 503 지속 | 57.876초 접수 · 274.717초 인계 | 22.241초 | 19.018초 | 22.124초 |
| 단일 복제본의 대기열 적체 | 45.024초 접수 · 135.747초 인계 | 88.704초 | 78.326초 | 94.713초 |
| 읽기 전용 PVC 참조 변경으로 마운트 실패 | 144.289초 접수 · 인계 시각은 정식 집계에서 미관측 | 22.490초 | 18.548초 | 24.673초 |
| ConfigMap의 연결 풀 과소 설정 | 미실행 · 권한 승인 대기 | 63.002초 | 65.913초 | 64.441초 |

제품의 미복구는 네 사례에 걸쳐 다른 단계에서 발생했다. 주문에서는 반론이 완료되기 전에 그룹 호출 한도에 닿았다. 상류 복구 뒤에도 호출자가 실패한 사건에서는 가설이 근거 검증을 통과하지 못했다. 복제본 적체는 독립 검증이 원인을 지지했으나 실제 조치 기록이 없었다. PVC 최초 시행에서는 두 리비전의 참조와 바인딩이 분석 근거에서 빠졌다. 후속 수집기 보완은 바인딩을 검증했으며, 별도 재시험에서도 실행 대상의 조치 자격을 확정하지 못해 인계했다. [주문](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/order-image-contract-liveprobe-matched.json) · [호출자](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/upstream-liveprobe.json) · [적체](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/backlog-liveprobe.json) · [PVC](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/kw03-liveprobe.json) · [PVC 후속 검증](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/kw03-liveprobe-verified-collector.json)

연결 풀 시험의 단독 세 방식은 동일한 설정과 배포를 복원해 독립 예약 요청 9건이 정상임을 확인했다. 방식별 사용량은 Astra 86,155토큰·$0.250962, Sol 123,046토큰·$0.049812, Terra 66,321토큰·$0.028328이었다. 제품용 dev RBAC와 UID 한정 실행 정책은 서버 사전 검사를 통과했으며, 자동 승인 검토의 적용 차단으로 제품 시행은 대조 집계에 포함되지 않았다. [연결 풀 원장](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/pool-config-standalone-summary.json) · [정책 사전 검사](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/pool-guard-server-dry-run.txt)

08:21에 시험 시작 지문이 여전히 일치함을 재확인했다. 기존 관측 계정에 설정 조회 권한이 있어 별도 역할 추가가 필요하지 않았다. 실행 계정에 필요한 dev Role·RoleBinding 한 쌍, 대상 UID에 한정한 정책, 승인된 설정 버전 한 항목의 제품 허용 목록을 서버 사전 검사했다. 실제 권한과 환경은 변경하지 않았다. [시작 지문](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/pool-liveprobe-readonly-revalidation.json) · [최소 권한 검증](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/pool-liveprobe-minimum-authority-dry-run.json) · [제품 허용 목록 검증](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/pool-liveprobe-product-config-dry-run.json)

## Application 시험 결과

| 방식 | 복구 결과 | 복구 시간 | 독립 판정까지 | 분석 토큰 | 추정 비용 | 근거 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| Astra 6.2 `xhigh` | 복구 · 변경 전후 회귀 입증 · dev 9/9 정상 | 179.630초 | 188.004초 | 190,818 | $0.289245 | [원장](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-astra-bounded.json) |
| Sol 6.2 `xhigh` | 복구 · 변경 전후 회귀 입증 · dev 9/9 정상 | 139.203초 | 145.589초 | 212,134 | $0.092278 | [원장](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-sol-bounded.json) |
| Terra 6.1 `xhigh` | 미복구 · 업무 요청 2건 HTTP 500 · 풀 활성 4/4 | 미관측 | 46.006초 | 34,633 | $0.018165 | [원장](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-terra-bounded-retry.json) |
| LiveProbe | 미복구 · 25.649초 자동 접수 · 회귀 통과 · dev 이미지 시작 실패 | 미관측 | 564.096초 | 126,680 | $0.465402 | [판정과 원본 지문](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-liveprobe-native-2-adjudicated.json) |

LiveProbe의 독립 판정 시각은 업무 확인 단계가 실패한 뒤 수정 이미지에서 준비된 Pod가 0개임을 관측한 때다. 기존 실행기는 원래 장애 이미지가 남은 실패만 자동 판정하므로 해당 시행의 원본은 `unobserved`로 보존했다. 동일 사건·Deployment UID와 수정 이미지, 실제 시작 오류, 기준 이미지 복원 뒤의 정상 요청 9건을 연결해 추가 판정 원장을 작성했다. 파일 권한 `0600`을 런타임 UID `10001`로 검사해 읽기 실패를 재현했다. 이후 패키징 소스를 보완해 호스트 회귀를 확인했으며, 수정본의 전체 dev 재시험은 대기 중이다. [실행 원본](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-liveprobe-native-2.json) · [원복 전 Pod 검사](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-native-candidate-rollout-failure.json) · [JAR 권한 대조](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-native-candidate-jar-permissions.json) · [패키징 회귀](../../liveprobe/application_tests/test_runner_host.py)

실패 이미지의 실제 JAR를 임시 메모리로 복사해 같은 UID의 Java로 검증했다. `0600`에서는 JAR 읽기가 실패했고 `0644`에서는 Spring Boot 계층을 읽었다. 이 결과는 패키징 수정의 효과를 확인한 별도 검증이며 위의 복구 판정은 그대로다. [실제 JAR 검증](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-jar-runtime-mode-smoke.json)

이후 시험기는 회귀를 통과한 교체 이미지가 같은 Deployment에서 반복 종료되고 Service에 준비된 연결 대상이 없을 때 독립 증거를 자동 기록하도록 고쳤다. 소유자가 다른 Pod나 준비된 연결 대상이 있는 상황은 미관측으로 유지하는 것으로 검증했다. [판정 로직](../../evaluation/reservation_dev_runtime.py) · [회귀 검증](../../evaluation/tests/test_matched_recovery_campaign.py)

파생 Kubernetes 사건 #1698에서 두 후속 표본의 준비 Pod와 Service 연결 대상은 각각 1개였고 재시작 횟수는 늘지 않았다. 저장된 `workflow_runs` #1639의 두 관측을 사건 종료 시각으로 재생했을 때, `fsnotify` 오류의 발생 시각을 파싱할 수 없어 복구 증명이 거부됐다. 로그 수집기는 수신 시각을 정렬에만 쓰고 출력에서 제외한다. 자동 조치가 제안되지 않은 상태에서 복구 증명도 실패해 엔진은 인계 상태를 기록했다. 그룹 호출은 한도 10회 중 5회였으며, 화면의 `제한된 자율 복구 소진`은 이 분기의 기본 분류다. #1697의 JAR 시작 실패와 #1698의 복구 확인 실패는 별도 사건으로 저장됐다. [로그 수집](../../liveprobe/app/evidence.py) · [복구 판정](../../liveprobe/app/observed_recovery.py) · [그룹 결과 처리](../../liveprobe/app/engine.py)

## 해석 한계와 근거

- 정식 판정은 24회 중 23회다. 복구 17회와 미복구 6회는 원복 전 검증으로 판정했다. Kubernetes 연결 풀 설정의 LiveProbe 시행 1회는 미실행이다. 이전 캠페인 진행 현황 355/375회는 이번 비교에 사용하지 않았다. [23개 판정 원장](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/matched-summary-native-0812.json)
- 각 사례를 방식당 한 번 판정한 단일 dev 시험이므로 성공률을 다른 장애에 일반화하기 어렵다. 자동 감지 대기와 운영 안전 정책이 제품 결과에 포함된다. 단독 모델의 시작 오류는 사전에 관측한 뒤 전달했고, 각 방식에 실행 경로가 다르게 부여됐다. 이 차이는 모델 자체 성능의 비교 범위를 제한한다. [시험 범위](../plans/liveprobe-matched-dev-recovery-2026-11-26.md)
- Rancher Desktop API 연결 거부를 복구했고 실험 중 기존 노드를 사용했다. 시험 원복 뒤 Kubernetes와 Application API를 확인했다. 08:18 사전 검사에서 API 연결은 성공했으며 노드 메모리가 시행 상한을 넘었다. [API 복구 기록](../operations/rancher-desktop-api-recovery-2026-11-26.md) · [시험 원복 기록](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-native-restore-0811.json)
