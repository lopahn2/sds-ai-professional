# 동일 조건 dev 장애 복구 비교

## 합의한 범위

기존 dev에서 Kubernetes 5종을 LiveProbe와 Astra 6.2, Sol 6.2, Terra 6.1에 각각 한 번 수행한다. 네 방식 모두 동일 노드, 리소스, 주입과 독립 검증에서 시작한다. Application은 같은 업무 요청과 Hikari 설정을 적용한 네 Spring Boot 소스 변형을 한 방식씩 배정한다. 핵심 비교는 24회이며 OOM 4회는 별도 자원 및 복구 기준을 통과할 때만 추가한다. 노드 등록 전 오류는 이 비교와 실시간 접수에서 제외하고 원기록을 유지한다.

LiveProbe는 장애 주입 후 제품의 자체 탐지와 처리를 관찰한다. 단독 모델은 원본 오류 한 줄과 `장애를 복구하라.`를 받고 새 `xhigh` 세션에서 할당된 시험 대상을 조사한다. LiveProbe API, DB, RAG, 수집기, 운영 도구, 실행기와 다른 실행의 맥락은 단독 모델에 주지 않는다. 복구는 시험기의 원복 전 독립 요청과 상태로 판정한다. 대응 시간은 주입 승인부터 측정하고 토큰과 환산 비용은 관측된 호출만 기록한다.

## 진행 근거

- 최초 dev API 사전 검사 시 6443 연결이 거부됐다. 2026-11-26 00:49 KST에 동일 노드에서 API를 복구했다. 00:51 검사에서 CPU와 메모리 사용률이 70% 상한을 넘어 이번 비교 본 실행과 장애 주입은 시작하지 않았다. 기존 데이터와 원장을 사용해 이번 비교 결과를 채우지 않았다. [최초 사전 확인](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/preflight.json) · [Rancher 복구 기록](../operations/rancher-desktop-api-recovery-2026-11-26.md)
- 비교 매트릭스는 검증 대상 다섯 Kubernetes 장애, 앱 유출 4변형, 선택 OOM을 선언한다. 단독 세션은 Codex App Server에서 새 홈, 임시 스레드와 허용된 브로커로 분리한다. 로컬에서 Sol 6.2, `xhigh`, 빈 지시 파일 목록, 호출 전 스레드 기록 0건을 확인했다. 인증된 추론과 도구 왕복은 미검증이다. [매트릭스](../../evaluation/matched_recovery_campaign.py) · [실행기](../../evaluation/isolated_recovery_agent.py) · [호스트 확인](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/standalone-handshake.json)
- Spring Boot 3.5.16 변형 4개를 각각 빌드했다. 임시 PostgreSQL 17.11에서 모두 업무 경로가 HTTP 500과 Hikari `SQLTransientConnectionException`을 반환하고, 부하 뒤에도 활성 연결이 4/4에 머무르는 것을 확인했다. 첫 500은 4~5번째 요청이었다. 소스 수정 전 재현 확인이며 모델 성능·수정 후 배포 증거는 없다. [독립 판정](../../evaluation/verify_jdbc_fixture.py) · [변형별 확인](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26)
- 기존 이벤트에서 KW-03을 감지하도록 시험 워크로드 범위를 명시했다. 이전 Ready 리비전, 현재 반복 마운트 실패와 두 PVC–PV 바인딩을 확인하는 Deployment 참조 복원을 추가했다. 시험 namespace에 한정한 선택 권한이며, 기본 선언과 기존 권한은 일치한다. 준비 PV 지정, 작업 허용과 실제 서버의 승인 검사는 남아 있다. [참조 검증](../../liveprobe/app/pvc_reference.py) · [선택 권한 렌더러](../../scripts/render-p18-actions.py)
- 임시 PostgreSQL을 포함한 감지·기록 보존·분리 실행 계층 검증 134개가 통과했다. 새 KW-03 제한과 선언을 추가로 확인했다. 기존 CEL 실행 검사 두 개는 작업 환경에 없는 외부 캐시의 `prepare.py`를 참조한다. [회귀 기록](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/pytest.log)

남은 범위는 실제 dev와 LiveProbe 수집 경로의 준비 확인, 각 사례의 안전한 주입·판정 연결, 애플리케이션 변형 배정 및 dev 배포, 인증된 단독 모델 실행, 본 비교 집계다.

## API 복구 이후 진행

- dev API가 `/readyz`에 정상 응답했다. 기존 노드와 PVC 바인딩을 대조했고, 준비 중단 상태였던 KW-03 Deployment를 원래 PVC 참조로 활성화했다. 두 요청이 200을 반환했고 마운트 자료의 지문을 기록했다. 과거의 메모리 시험은 활성 사건·임대가 없고 임시 볼륨이 비어 있는 것을 확인한 뒤 비교 중 일시 정지했다. [기준선](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/kw03-existing-fixture-baseline.json)
- 설치된 정책에는 별도 시험 ConfigMap 변경 규칙이 있었다. 그 규칙과 적용 범위를 유지한 채, KW-03의 한 방향 Deployment 참조 변경을 추가했다. PVC와 PV 권한은 해당 이름 조회에 한정했다. 실제 서버 검사 후 후보 API를 배포해 dev 감시기와 복원 조치가 사용 가능해졌다. [서버 검사](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/kw03-live-policy-preserving-dry-run.txt) · [배포 검증](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/kw03-api-release-ready.json)
- 공식 고정 CLI 패키지의 도구 호스트와 macOS DNS 소켓이 필요했다. 세 모델의 `xhigh` 추론과 사용량을 확인했고, Sol로 허용 도구 호출이 실제로 시험 조정자에 전달되는 것을 확인했다. 격리 프로세스에서 관제 4175와 Kubernetes 6443 연결은 차단했다. 보정 호출은 복구 시험 결과에 포함하지 않는다. [인증 연결 확인](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/independent-agent-provider-after-resolver.json)
- 앱 변형을 네 방식에 비공개 배정하고 시험 전 지문을 고정했다. 공통 검증 트래픽은 200ms 간격으로 발송하도록 보완했다. [배정 지문](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/jdbc-assignment-commitment.json) · [독립 요청 검증](../../evaluation/verify_jdbc_fixture.py)

## 첫 동일 조건 비교 결과

KW-03 네 실행의 시작 지문은 일치했다. 시작 시간은 시험 Deployment의 PVC 참조 변경이 Kubernetes API에서 승인된 시점이다. 각 실행의 종료 뒤 같은 PVC·PV 바인딩과 마운트 자료 지문을 확인했다. 가격은 고정 토큰 단가 환산이며 실제 청구액과 구분한다. [결과 집계](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/kw03-initial-matched-summary.json)

| 방식 | 판정 | 복구 확인 | 입·출력 토큰 합 | 비용 환산 |
| --- | --- | ---: | ---: | ---: |
| LiveProbe | 미복구, 운영자 인계 | 미관측 | 122,086 | $0.429015 |
| Astra 6.2 xhigh | 복구 | 22.490초 | 60,280 | $0.132115 |
| Sol 6.2 xhigh | 복구 | 18.548초 | 61,428 | $0.060648 |
| Terra 6.1 xhigh | 복구 | 24.673초 | 42,968 | $0.015203 |

LiveProbe 사건 #1683은 주입 뒤 144.289초에 자동 접수됐고 304.920초에 운영자 인계로 끝났다. 현재 리비전의 마운트 실패와 이전 리비전의 Ready Pod는 확인했다. 이전·현재 리비전의 PVC 참조와 실제 바인딩이 분석 근거에 빠져 조치 제안은 생성되지 않았다. 시험 조정자가 원래 참조를 복원한 뒤 정상 요청을 확인했다. 나머지 세 방식은 자신의 격리된 세션과 허용된 대상 도구로만 참조를 복구했다. [LiveProbe](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/kw03-liveprobe.json) · [Astra](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/kw03-astra.json) · [Sol](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/kw03-sol.json) · [Terra](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/kw03-terra.json)

표의 LiveProbe 비용은 인계까지 관측한 분석 호출 6개의 합이다. 인계 뒤 화면 해석 호출 2개가 29,756토큰과 $0.004259를 추가로 사용했다. 분석과 화면 해석을 포함한 해당 사건의 관측 합은 151,842토큰과 $0.433274였다.

누락된 참조와 바인딩은 현재 소유권이 확인된 kubelet `FailedMount`에 대해 연관 Deployment 근거로 추가했다. 원래 네 방식 비교를 보존하고 같은 시작 지문에서 제품 재검증을 따로 실행한다. [근거 수집](../../liveprobe/app/evidence.py) · [개선 후보 배포](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/kw03-api-evidence-release-ready.json)

첫 보완 이미지에는 두 배포 호환성 문제가 있었다. 근거 수집 블록이 이벤트 수집의 예외 분기에 들어갔고, 이미 배포된 감시기의 반환 항목 수가 작업본과 달랐다. #1684의 인계 원기록을 보존했다. 수정 이미지 내부에서 이벤트 소유권 검증과 근거 저장 경로를 각각 실행해 확인했다. #1685에서는 실제 읽기 계정의 두 바인딩 조회와 Deployment 증거 저장을 확인했다. [첫 보완 시행](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/kw03-liveprobe-after-evidence.json) · [호환성 확인 후 배포](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/kw03-api-verified-collector-ready.json)

최종 수집기 재시험 #1685도 같은 시작 지문에서 142.646초에 접수됐고 인계로 끝났다. 공개 계획은 이전의 읽기 전용 참조를 복원하면 된다고 판단했으나, 활성 허용 목록과 실행 대상 자격 확인을 운영자에게 넘겼다. 원인을 해석하는 단계에서 두 바인딩을 확인했고, 조치 제안은 0건이었다. 현재 설정의 허용 목록을 조사·복구 계획에 전달하도록 보완해 dev API에 반영했다. 격리 PostgreSQL을 사용한 관련 회귀 45개가 통과했고 제품 API와 감시기의 준비 상태를 확인했다. 이 버전의 실제 자동 복구는 미검증이다. KW-03과 일시 정지한 이전 메모리 시험의 원래 복제본 설정을 되돌렸다. [최종 수집기 재시험](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/kw03-liveprobe-verified-collector.json) · [허용 목록 반영](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/matched-api-with-planner-allowlist-ready.json) · [원복 검증](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/kw03-fixtures-cleanup.json)

다음 이미지 계약 장애 후보는 실제 dev 주문 이미지의 JAR에서 재고 응답을 읽는 클래스 하나만 바꿨다. JAR의 나머지 221개 항목과 압축 방법은 동일하다. dev 재고 API에서 받은 응답으로 로컬 JVM을 실행하니 두 버전 모두 준비 요청 200을 반환했고, 주문 업무 요청 두 건은 정상본에서 200, 후보에서 502였다. 원본의 컨테이너 설정을 유지한 로컬 후보 이미지를 준비해 이미지 한 필드의 변경에 대한 서버 사전 검사를 통과했다. 실제 자동 경보·복구 검증은 남아 있다. Application의 별도 dev 시험 배포 선언은 서버 사전 검사를 통과했다. 시험 DB는 해당 Pod 내부의 루프백에만 열리며 기존 DB와 연결하지 않는다. [이미지 변경 대조](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/order-contract-native-image-ready.json) · [로컬 교차 서비스 확인](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/order-contract-native-jar-calibration.json) · [이미지 서버 사전 검사](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/order-contract-dry-run.json) · [Application 시험 선언](../../evaluation/jdbc_dev_manifest.py)

이 시점에는 정식 비교 24회 중 KW-03의 4회가 종단 판정에 도달했다. 제품 보완 2회는 별도로 기록했다.

## API 접근과 주문 이미지 계약 시험

01:42에 승인된 호스트에서 기존 노드의 API, 노드 자원 기준과 주문·재고 업무 요청을 확인했다. 시험 코드에서 기본 샌드박스의 소켓 접근 차단을 API 내려감과 구분한다. [API 호출 검증](../operations/rancher-desktop-api-recovery-2026-11-26.md#시험-실행-경로-확인)

주문 이미지 교체 첫 시도에서는 Kubernetes가 같은 이미지 지문을 저장소 이름과 함께 반환해 판정기가 중단됐다. 원래 이미지를 복원하고 업무 요청 200을 두 번 확인했다. 판정기는 런타임의 저장소 부분을 분리해 같은 지문을 확인하도록 수정했다. 재실행에서 준비 요청 200, 주문의 재고 확인 요청 502, 재고 단독 요청 200과 주문 경로의 실제 502 시계열을 확인했다. 배포된 규칙은 경로를 붙이지 않고 서비스 단위로 경보를 보냈고, Alertmanager를 거쳐 LiveProbe 사건 #1686이 생성됐다. [첫 시도와 원복](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/order-image-contract-liveprobe.json) · [경보 대조](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/order-image-contract-alert-1686.json)

#1686은 자동 조치 없이 운영자에게 인계됐다. 시험기가 원래 이미지를 되돌렸고 주문·재고 업무 요청에서 정상 응답을 두 번 확인했다. 해당 실행의 판정기는 경로 라벨이 있는 경보만 채택하므로 비교용 원장은 `unobserved`로 보존했다. 이후 판정기는 서비스 단위 경보에 대해 반복된 실제 502와 정확히 한 개의 실패 경로를 함께 관측할 때 감지를 인정한다. 이전 장애의 5xx가 감지 구간에 남아 있으면 다음 시험의 주입을 막는다. API 접근과 이미지·경보 판정에 관련된 시험 36개를 통과했다. [#1686 실행 원본](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/order-image-contract-liveprobe-runtime-id.json) · [비교 실행기](../../evaluation/matched_order_image.py)

정식 LiveProbe 시행 #1687의 첫 실제 502는 변경 승인 후 14.420초에 확인됐고, 자동 접수는 72.075초에 관측했다. 복잡 그룹이 추가 반론 단계 전에 그룹 호출 10회 한도에 도달해 325.590초에 운영자에게 인계했다. 원복 직전 주문 경로는 여전히 502였고, 재고·준비 요청은 200이었다. 복구 조치는 0건이었으며 시험기가 원래 이미지로 전환한 뒤 정상 상태를 확인했다. 인계까지 관측한 화면 해석 외 호출은 분류 포함 11회, 233,373토큰, 추정 $1.283057이다. [#1687 원장](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/order-image-contract-liveprobe-matched.json)

### 주문 이미지 계약 동일 조건 비교 결과

네 시행의 노드, Deployment, 초기 오류, 주입과 검증 지문이 일치했다. 시간은 이미지 변경 승인부터 독립 검증을 통과할 때까지의 경과다. [대응 시간·토큰·비용 원장](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/order-image-contract-matched-summary.json)

| 방식 | 실제 복구 | 복구 시간 | 분석 토큰 | 추정 비용 |
| --- | --- | ---: | ---: | ---: |
| LiveProbe | 미복구, 운영자 인계 | 미관측 | 233,373 | $1.283057 |
| Astra 6.2 xhigh | 복구 | 47.748초 | 64,763 | $0.1515504 |
| Sol 6.2 xhigh | 복구 | 39.943초 | 84,488 | $0.0704944 |
| Terra 6.1 xhigh | 복구 | 44.059초 | 71,232 | $0.0195856 |

LiveProbe 화면 해석은 별도로 35,001토큰과 $0.004130을 사용했다. Astra 복구 확정 및 시험 격리 해제 뒤 남은 경보가 #1688을 열었다. 이 사건은 Astra 검증 이후에 발생했고 비교 집계에서 제외했다. 첫 Sol 실행은 오류 지문을 확보한 뒤 관측용 `kubectl exec` 실패로 미관측 처리했으며 시험기가 원래 이미지를 복원했다. 이후 관측용 요청만 재시도했고, 결과 측정 이후까지 격리를 유지해 Prometheus와 Alertmanager의 잔류 신호를 확인한 뒤 해제했다. Sol 재실행과 Terra 실행 뒤 추가 관제 사건은 없었다. [첫 Sol 실행 원본](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/order-image-contract-sol.json) · [격리 해제 기준](../../evaluation/matched_order_drained.py)

정식 종단 판정은 24회 중 8회다. 다음 풀 설정 장애의 소스는 격리된 Spring Boot가 같은 SQL 여정에서 부하를 처리하도록 준비했다. 주입된 ConfigMap의 연결 풀 한도 아래에서 같은 부하를 재검증하는 실제 dev 시행은 남아 있다. 나머지 Kubernetes 3종과 Application 4변형의 실측을 최종 집계에 별도로 반영한다. [풀 설정 시험 소스 준비](../../evaluation/prepare_pool_config_fixture.py)

## 연결 풀 설정 장애의 dev 준비

시험 배포 `lp-eval-pool`을 기존 dev 노드에 띄웠다. Spring Boot와 같은 Pod의 루프백 PostgreSQL을 사용하며 시험 데이터는 고정 SQL에서 초기화된다. Kubernetes API의 Service 프록시를 사용한 실제 예약 요청 9건이 풀 4개에서 모두 업무 계약을 만족했다. 부하 중 사용량은 3/4, 부하 종료 후 사용량과 대기는 각각 0이었다. 기존 dev NetworkPolicy가 신규 Pod의 Prometheus 요청을 거부해 해당 Pod의 8080을 Prometheus에만 열었고 실제 스크랩을 확인했다. [리소스 선언](../../evaluation/jdbc_dev_manifest.py) · [업무 판정기](../../evaluation/pool_config_observe.py)

첫 보정에서는 새 Pod의 준비 확인이 DB를 초기화하지 않아 Hikari 지표 대기 시간이 초과됐다. 풀 설정을 4로 복원하고 같은 조회 계약과 지표를 확인했다. 이후 판정기는 Pod 교체마다 동일한 읽기 전용 요청으로 풀을 초기화한다. [첫 보정 원본](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/pool-config-calibration.json)

기존 LiveProbe 실행 계정의 정책은 이 시험 ConfigMap과 Deployment를 허용하지 않는다. 새 선언은 해당 ConfigMap UID에 대해 값 `1 → 4` 복원, 해당 Deployment UID에 대해 재시작만 열며 나머지 검증 규칙을 보존한다. 클러스터 범위 검증 정책과 dev RBAC, LiveProbe 설정의 사전 검사는 통과했다. 자동 승인 검토는 정확한 보안 설정 변경의 사용자 확인을 요구해 적용을 차단했다. 단독 모델의 격리 시험과 보정은 기존 권한으로 구분해 진행한다. [권한 선언](../../evaluation/pool_config_policy.py) · [서버 사전 검사](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/pool-guard-server-dry-run.txt)

두 번째 보정에서 같은 예약 요청 9건 중 5건이 HTTP 500이었으며 부하 중 직접 지표는 사용 1/1, 대기 1을 기록했다. 간헐적인 부하에서 감지 여부를 해당 실행 내에서 확정하지 못했다. 원복과 임대 종료 뒤 늦게 도착한 로그와 경보가 #1690으로 연결됐다. 이 사건은 `ApplicationException`과 `TargetConnectionPoolExhausted`를 수신했고 서비스 복구 후 소명 확인 대기 상태로 갔다. 두 번째 보정 원장을 보존하고, Hikari 초기화와 고정 200ms 지속 부하를 사용한다. 원복 이후 과거 Pod 로그 수집 창과 잔류 경보가 끝날 때 임대를 해제한다. [두 번째 보정](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/pool-config-calibration-warmed.json)

지속 부하 보정에서 최초 JDBC 시간 초과를 22.865초에 확인했다. 327건을 200ms 간격으로 보냈고 누락·전송 오류는 0건이었다. Prometheus에서 실제 풀 사용률 1.0을 관측했고 설정 복원 뒤 같은 예약 요청 9건과 유휴 풀 4개를 확인했다. 첫 단독 비교 시도에 이 시작 지문을 사용했다. 보정은 정식 비교 분모에서 제외한다. [지속 부하 원장](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/pool-config-calibration-continuous.json)

첫 Astra 시행은 Pod 전환 중 Hikari 지표 조회 실패로 미관측 처리했다. 도구가 이 상태를 일시 미관측으로 반환하고, 독립 판정기는 새 Pod에서 업무 요청을 다시 확인하도록 보완했다. 이 버전에서 다시 보정해 새 동일 조건 지문을 확보했다. Astra와 Terra의 각 첫 준비 단계에서 자원 기준에 걸린 시도는 주입 없이 기록했고, 기존 결과를 별도 보존했다. [첫 Astra 시행](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/pool-config-astra.json) · [최종 보정](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/pool-config-calibration-v2.json) · [실행기](../../evaluation/matched_pool_config.py)

| 방식 | 판정 | 복구 시간 | 토큰 | 추정 비용 |
| --- | --- | ---: | ---: | ---: |
| LiveProbe | 권한 적용 승인 대기 | 미실행 | 미실행 | 미실행 |
| Astra 6.2 xhigh | 복구 | 63.002초 | 86,155 | $0.2509616 |
| Sol 6.2 xhigh | 복구 | 65.913초 | 123,046 | $0.049812 |
| Terra 6.1 xhigh | 복구 | 64.441초 | 66,321 | $0.0283282 |

세 모델이 각각 같은 ConfigMap을 승인 버전으로 되돌렸고 원래 Service에서 새 Pod가 예약 요청 9건을 처리했다. 풀 크기 4, 사용 0, 대기 0을 확인했다. 동일 조건 원장에는 표의 세 복구만 추가했다. 전체 정식 종단 판정은 24회 중 11회다. [연결 풀 비교 원장](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/pool-config-standalone-summary.json)

08:21 재검사에서 기존 노드 UID, 시험 Deployment UID, 정상 설정·런타임 이미지 지문, 판정기 지문이 기존 보정 원장과 모두 일치했다. 관측 계정은 현재 설정과 불변 버전을 이미 읽을 수 있었다. 안전 조치 실행 계정에는 이 두 설정과 대상 Deployment의 조회 및 해당 설정·Deployment의 패치 권한이 없었다. 실제로 필요한 dev Role·RoleBinding 한 쌍과 두 UID에 한정한 admission 변경으로 서버 사전 검사를 다시 통과했다. 제품의 실행 허용 목록은 불변 버전의 데이터 해시 한 항목을 추가하고 다른 설정을 보존하는 후보로 확인했다. 실제 적용과 장애 주입은 정책 변경 승인 대기 중이다. [기준 지문 재검사](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/pool-liveprobe-readonly-revalidation.json) · [실효 권한](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/pool-liveprobe-role-delta.json) · [정책 사전 검사](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/pool-liveprobe-minimum-authority-dry-run.json) · [제품 설정 사전 검사](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/pool-liveprobe-product-config-dry-run.json)

## 나머지 유형의 시험 준비

- Application 격리 검증 도구에서 연결 반환 회귀 테스트 하나가 원본에서 실패하고, 같은 테스트가 수정 코드에서 통과했다. Maven은 네트워크와 다른 작업 디렉터리 접근을 차단한 컨테이너에서 실행했다. 원본 JAR은 로컬 Java 기반으로 패키징한 실제 이미지 digest로 dev 선언의 서버 검사를 통과했다. 이 보정은 단독 모델 결과 및 LiveProbe 결과에서 제외한다. [격리 검증 근거](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/jdbc-isolated-test-tool-calibration.json) · [이미지 증명](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/jdbc-runtime-calibration-image.json)
- 호출 프로세스는 상류 HTTP 오류를 경험한 뒤 상류가 200으로 돌아와도 503을 계속 반환했다. 같은 상류 데이터에서 새 프로세스만 정상 업무 응답을 반환했다. 루프백 보정 결과이며 Kubernetes 배포·실제 경보는 추가 검증한다. [연결 회로 보정](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/upstream-local-circuit-calibration.json)
- 복제본 적체 시험의 Spring Boot 소스와 JAR을 준비했다. DB 실행 시간과 실제 동시 요청의 진입 대기를 별도 지표로 계측한다. 같은 도착률에서 복제본 2개가 정상 업무 결과를 유지하는지는 dev 실측에서 판정한다. [적체 시험 소스](../../evaluation/fixtures/k8s-capacity/ReservationQuery.java)

로컬 PostgreSQL을 연결한 실제 Java 프로세스에 350ms 간격으로 같은 예약 요청을 보냈다. 프로세스 한 개는 후반 요청의 p95가 4,985ms였고 최대 대기자는 17이었다. 두 개에서는 후반 요청이 모두 계약을 만족했고 p95 571ms와 대기 0을 관측했다. 로컬 분산은 순환 배정으로 보정했다. 실제 Kubernetes Service의 분산과 두 Pod의 상태는 dev 비교에서 별도로 확인한다. [적체 보정 원장](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/backlog-local-rate-calibration.json)

호출자 재시작과 복제본 적체 이미지를 고정한 뒤, 기존 `lp-process`와 `lp-cpu`의 동일 UID를 확인하고 배포 패치·전용 Service·NetworkPolicy의 서버 사전 검사를 통과했다. Prometheus 새 규칙 두 건은 별도 엄격 검사에 통과했고, 전체 규칙은 문법 검사를 통과했다. 기존 `TargetHighLatency` 중복 경고는 남아 있다. 해당 경보의 실제 활성화와 dev 시행은 남아 있다. [서버 사전 검사](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/upstream-backlog-server-dry-run.json) · [경보 선언](../../evaluation/matched_k8s_observability.py)

dev의 호출자 보정에서는 기존 `lp-process`의 원본 배포를 보존하고 전용 재고 Service의 selector를 잠시 끊었다. 실제 호출자에 `URLError` 로그와 503이 발생했다. selector를 되돌린 뒤에도 같은 caller Pod는 503이었고, 별도 재고 요청은 원래 데이터로 200을 반환했다. 시험기의 재시작 후 새 caller Pod에서 두 요청이 정상화되기까지 주입 승인 기준 6.199초가 걸렸다. 이 값은 보정 시간이다. [dev 호출자 보정](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/upstream-dev-calibration.json) · [원래 배포 템플릿](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/upstream-dev-original-template.json)

Prometheus의 활성 규칙 파일은 ConfigMap `subPath` 마운트다. 두 dev 규칙을 반영하려면 새 설정을 마운트한 Pod가 필요하다. 기존 `prometheus-data` PVC를 쓰는 배포 선언의 서버 사전 검사를 마쳤다. [설정 전환 사전 검사](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/matched-prometheus-server-dry-run.json)

첫 dev 적체 보정에서도 복제본 두 개의 후반 응답이 모두 정상으로 돌아왔다. 단일 복제본에서 후반 p95에 필요한 정상 응답이 없어 p95를 계산할 수 없었고, 두 Pod의 대기 개수도 Service 경유로만 읽어 보정 상태를 미관측으로 남겼다. 판정기에 시간 초과 분류와 각 Pod에 대한 직접 지표 조회를 추가한 뒤 같은 트래픽으로 다시 검증했다. 복제본 하나에서 후반 43건이 시간 초과했고 최대 대기 19건에 도달했다. 두 개에서 후반 시간 초과 0건, p95 778ms, 두 Pod의 대기 0건을 확인했다. 실행 뒤 복제본을 한 개로 복원했다. [첫 dev 보정](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/backlog-dev-calibration.json) · [Pod별 최종 dev 보정](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/backlog-dev-calibration-pod-metrics.json)

03:13에 실행 중인 사건과 시험을 확인하고 dev의 `lp-process` 및 `lp-cpu`를 위한 두 규칙을 Prometheus에 반영했다. 새 Pod에서 두 규칙이 정상 상태이며 `prometheus-data` PVC의 UID와 바인딩이 전환 전과 일치한다. 다음 두 유형의 정식 비교는 LiveProbe의 자발 사건 접수와 네 방식의 복구 검증까지 각각 확인해야 한다. [활성 규칙 및 볼륨 검증](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/matched-prometheus-active.json)

## 상류 복구 후 호출자 재시작 시험

LiveProbe가 재고 경로 단절에서 시작된 실제 5xx 경보를 57.876초에 접수했다. 사건 #1691은 원인 가설의 근거가 검증 기준을 충족하지 못해 274.717초에 운영자에게 인계됐다. 당시 재고는 기존 수량 42로 200을 반환했고, 같은 호출자 Pod는 503을 반환했다. 조치는 기록되지 않았다. 시험기 원복 후 새 호출자에서 정상 업무 응답 두 건을 확인했다. 화면 해석을 제외한 모델 호출 11회에서 225,416토큰, 추정 $1.230719를 사용했다. 화면 해석은 28,040토큰, 추정 $0.003768이다. [LiveProbe 원장](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/upstream-liveprobe.json)

Terra 6.1 xhigh는 동일한 시작 지문에서 호출자를 재시작했다. 새 Pod의 연속 정상 업무 응답으로 검증한 복구 시간은 22.124초였고, 77,512토큰과 추정 $0.0186392를 사용했다. Astra와 Sol의 첫 주입에서는 복구 응답을 관측했지만 각각 허용된 도구 범위를 벗어난 호출이 감지돼 미관측으로 기록했다. 해당 두 시행은 시간·비용·복구 비교 분모에서 제외한다. 전체 정식 종단 판정은 현재 24회 중 13회다. [Terra 원장](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/upstream-terra-2.json) · [Astra 첫 주입](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/upstream-astra-1.json) · [Sol 첫 주입](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/upstream-sol-4.json)

Application 네 변형의 JAR을 오프라인으로 생성했다. 단독 모델의 시험 빌드에 사용할 공개 Maven 패키지 캐시를 별도로 구성했고, 해당 캐시로만 Maven 회귀 테스트를 실행할 수 있음을 확인했다. dev 기본 배포에 사용할 정상본은 동일한 회귀 테스트가 수정 전 실패하고 수정 후 통과했다. 이 빌드는 환경 보정이며 정식 Application 복구 결과는 dev 업무 요청으로 검증한다. [변형 JAR 지문](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-canonical-jars.json) · [공개 패키지 캐시](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-public-maven-cache.json) · [정상본 시험 결과](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-canonical-healthy-release.json)

## 복제본 적체 LiveProbe 시행

고정 350ms 간격으로 예약 요청을 계속 보냈다. 최초 애플리케이션 경고는 4.430초, LiveProbe 사건 #1692의 실제 `TargetHighLatency` 경보 접수는 45.024초에 관측했다. 독립 검증기가 원인 가설을 지지했으나, 135.747초에 운영자 인계 상태였고 조치 기록은 없었다. 최대 대기는 35건이며 인계 뒤에도 단일 복제본의 대기가 관측됐다. 시험 부하를 끈 뒤 동일 Deployment의 복제본 하나와 정상 예약 응답을 확인했다. 이는 시험 종료 확인이다. 화면 해석을 제외한 모델 호출 4회는 74,670토큰, 추정 $0.109222를 사용했다. 이 판정을 포함해 정식 종단 판정은 24회 중 14회다. [LiveProbe 원장](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/backlog-liveprobe.json)

## 재시험, 비교 집계, Application dev 기준선

03:37 Astra 호출자 재시험의 시작 지문은 #1691과 일치했다. 약 26.6초에 새 Pod의 정상 업무 요청을 관측했으나, 허용 도구 이외 호출을 감지해 미관측으로 기록했다. 이전 시도와 재시험을 모두 보존했다. 다음부터 격리 판정 기록에는 호출 원문 없이 차단된 도구 유형을 붙인다. 적체의 Terra 첫 시작은 3,084m/4,000m CPU 기준 검사에서 멈췄다. 장애 주입 원장은 생성되지 않았다. [Astra 재시험 원본](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/upstream-astra-2.json) · [격리 판정](../../evaluation/isolated_recovery_agent.py)

원장 집계기는 지정된 원본만 읽고 Kubernetes의 시작 지문, 단독 모델·`xhigh`·임시 스레드 증명, 원복 전 복구 또는 실패 확인과 최종 원복을 대조한다. Application 원장은 공통 비공개 배정 지문, 기준 이미지 JAR, 같은 dev Deployment를 확인하고 변형 지문을 중복 집계하지 않는다. 03:39 집계에는 복구 10회, 미복구 4회, 미관측 2회, 미실행 8회가 있다. 네 방식이 모두 판정된 Kubernetes 사례는 PVC 마운트와 주문 이미지 계약 두 종이다. 관련 회귀 17개가 통과했다. [원본 기반 집계](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/matched-summary-0339.json) · [집계기](../../evaluation/matched_recovery_summary.py)

03:38에 같은 노드의 dev에 `lp-eval-reservation` 정상본을 배포했다. 고정 SQL로 격리된 시험 DB를 초기화하고 예약 요청 9건 모두 업무 계약을 만족하는 것을 검증했다. 종료 표본의 Hikari 풀 크기는 4, 활성과 대기는 0이었다. Application의 실제 카탈로그와 source·cluster·QA 실행 계정은 이 별도 시험 서비스가 미등록임을 확인했다. 제품 방식의 본 시험에는 해당 dev 대상만 등록하고 역할별 기존 범위에 맞게 조회·배포를 열어야 한다. 기존 Kubernetes 연결과 단독 모델 원장은 이 등록과 독립적이다. [dev 정상 기준선](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-dev-seed.json) · [제품 등록 사전 확인](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-native-registration-preflight.json)

dev의 연결 유출 보정에서 처음 Hikari 오류를 변경 승인 후 27.087초에 확인했다. 예약 9건 중 6건은 500을 반환했고 부하 종료 후에도 활성 연결은 4/4였다. 시험기가 정상본을 적용한 뒤 같은 요청 9건이 모두 계약을 만족했고 풀은 다시 활성과 대기 0을 반환했다. 별도 보정 원본을 정식 비교에 넣지 않았다. 보정에 쓴 실제 이미지의 JAR과 소스 7개를 고정 빌드 영수증과 대조했고, 제품 실행기가 사용하는 `snapshot()` 형식 검증을 통과했다. 제품의 dev 대상 등록과 실제 작업자 실행은 남아 있다. [보정 증거](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-dev-calibration.json) · [제품 소스 검증](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-native-provenance-calibration.json)

Astra의 첫 Application 시험도 동일 예약 요청에서 6건의 500과 Hikari 활성 4/4를 재현했다. 첫 오류는 26.723초였다. 단독 세션에서 범위 밖 MCP 도구 호출이 감지돼 상태는 미관측이다. 동일 정상본을 복원하고 예약 9건 및 풀 정상 상태를 확인했다. [Astra Application 원장](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-astra-1.json)

기본 배포와 별도로 `lp-eval-reservation` 하나를 추가하는 dev 카탈로그를 생성했다. API와 실행기가 같은 지문과 정확히 10개 프로필을 확인했다. dev 등록, prd 접수·실행 거부, dev 검증 후 prd 일감 없이 끝내는 상태를 시험했다. 관련 회귀는 80개 통과, 3개 건너뜀이다. 활성 서버 반영은 아직 없다. 제품 방식의 실주입 전에는 dev에 한정한 세 역할 권한, 소스와 빌드 환경, 제품의 자체 로그 감지와 dev 검증을 연결해야 한다. [카탈로그 검증](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-native-catalog-staged.json) · [카탈로그 규칙](../../liveprobe/app/application/catalog.py) · [실행기 검증](../../liveprobe/application_runner/profiles.py)

Terra 6.1 xhigh는 #1692와 같은 시작 지문에서 복제본을 두 개로 늘렸다. 원래 요청량에서 독립 표본 43건이 모두 업무 계약을 만족했고, 두 Pod의 최종 대기 표본 세 번은 모두 0이었다. 복구 판정까지 94.713초, 126,103토큰, 추정 $0.023551이었다. 검증 구간의 p95는 3.089초로, 보정에서 얻은 778ms보다 느렸다. 시험 뒤 `lp-cpu`와 `lp-eval-reservation`은 각각 1/1 준비 상태였고 활성 시험 표식은 해제됐다. 03:53 이후 집계는 24회 중 15회 판정, 복구 11회, 미복구 4회, 미관측 3회, 미실행 6회다. [Terra 적체 원장](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/backlog-terra-1.json) · [최신 집계](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/matched-summary-0353.json)

## 격리 이벤트 확인과 동일 조건 재시험

합성 Astra 시험에서 앱 서버의 허용 조회가 완료된 뒤 `codex` 내장 `list_mcp_resources` 및 `list_mcp_resource_templates`가 발생하는 것을 확인했다. 외부 서버 개수는 0이었고 두 내장 결과는 빈 배열이었다. 격리기는 시작·종료의 서버 목록이 모두 비어 있고 같은 스레드·시도에서 인자 없는 해당 내장 호출이 실제로 빈 결과를 돌려준 경우만 기록한다. 실제 데이터가 있는 결과와 다른 내장 도구는 미관측으로 차단한다. 이 변경 후 합성 시험이 정상 종료됐고, 이전 미관측 원본은 보존했다. [이벤트 재현](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-server-empty-inventory-v2-astra.json) · [경계 검증](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-server-empty-inventory-guard-astra.json)

| Kubernetes 사례 | 방식 | 복구 판정까지 | 토큰 | 추정 비용 |
| --- | --- | ---: | ---: | ---: |
| 상류 복구 후 호출자 재시작 | Sol 6.2 xhigh | 19.018초 | 93,130 | $0.0690984 |
| 상류 복구 후 호출자 재시작 | Astra 6.2 xhigh | 22.241초 | 83,843 | $0.1457584 |
| 복제본 적체 | Astra 6.2 xhigh | 88.704초 | 124,481 | $0.186696 |

세 시행은 각각 해당 LiveProbe 원본과 시작 지문이 일치한다. 호출자 시행은 새 caller에서 업무 응답 두 건이 정상이며 원래 재고 자료가 유지됐다. 적체 시행은 같은 요청량에서 독립 표본 43건을 통과했고, 대기는 0·1·0건, p95는 1.897초였다. 적체 Sol은 주입 직전 CPU 2,990m/4,000m에서 멈췄고 미실행으로 집계했다. 새 정식 판정은 24회 중 18회다. [호출자 Sol](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/upstream-sol-bounded.json) · [호출자 Astra](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/upstream-astra-bounded.json) · [적체 Astra](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/backlog-astra-bounded.json) · [최신 정식 집계](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/matched-summary-0412.json)

제품의 Application 빌더에서 검증된 소스·diff를 사용해 JDBC 회귀 하나가 변경 전 실패한 뒤 성공했다. Maven은 제품 실행기의 격리 컨테이너에서 공개 종속성 캐시만 읽었고, 패키지와 런타임 이미지 지문을 확인했다. 초기 시도에서는 macOS 임시 경로의 별칭 때문에 캐시 검사에서 멈췄다. 경로 정규화 후 보정이 통과했다. API와 모델 작업자의 실제 두 이미지에서 동일 네 파일을 읽고, dev 예약 프로필 관련 소스만 별도로 준비했다. 이름으로 한정한 세 Role·바인딩은 서버 사전 검사를 통과했다. 운영 API 설정 두 카탈로그 키만 바꾼 별도 후보와 제품 자체 접수·dev 확인을 읽는 시험기를 준비했다. 실배포와 제품 자체 복구 증거는 아직 없다. [제품 빌더 보정](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-native-builder-calibration-2.json) · [권한 사전 검사](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-native-dev-runner-rbac-server-dry-run.json) · [설정 후보](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-native-config-staged.json) · [제품 시험기](../../evaluation/matched_jdbc_product.py)

API·모델 작업자 후보 이미지를 현행 로컬 이미지의 레이어 위에 구성했다. 각 이미지의 기존 레이어, 환경 정의, 실행 진입점과 비교했다. 네트워크를 차단한 검증 컨테이너에서 App 모듈을 로딩했고 두 후보와 외부 실행기의 dev 카탈로그가 같은 지문을 반환했다. 새 ConfigMap과 두 Deployment 변경도 서버 사전 검사에 통과했다. [후보 이미지 증명](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-native-images-built.json) · [배포 사전 검사](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-native-release-server-dry-run.json)

04:21 Astra Application 재시험에서 할당된 변형의 최초 Hikari 오류를 26.397초에 확인했다. 수정 전 동일 테스트 한 개에서 오류가 재현됐고 수정 후 통과했다. 수정 이미지를 dev에 올린 뒤 독립 예약 요청 9건 모두 정상, Hikari 활성·대기 0과 풀 크기 4를 확인했다. 복구 판정까지 179.630초, 190,818토큰, 추정 $0.2892448였다. App Server의 연결 MCP 서버는 0개였다. 이후 기준선 이미지를 복원하고 다음 시험의 격리 기준을 확인했다. 04:21 집계에서 정식 판정은 24회 중 19회, 실제 복구 15회, 미복구 4회다. [Astra 원장](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-astra-bounded.json) · [04:21 집계](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/matched-summary-0421.json)

04:24 적체 Sol 재시험은 같은 시작 지문에서 복제본을 두 개로 늘려 78.326초에 복구 조건을 충족했다. 같은 요청량에서 43건이 모두 정상이고 Pod 대기 표본 세 번이 0이었다. p95는 3.345초, 사용량은 139,507토큰, 추정 $0.0791676이었다. 기존 단일 복제본과 정상 요청을 확인한 뒤 시험 표식을 해제했다. 이 결과로 정식 판정은 24회 중 20회다. 네 방식이 모두 판정된 Kubernetes 4종에서는 단독 세 방식이 각각 4/4 복구, LiveProbe는 0/4였다. 연결 풀 설정의 LiveProbe와 Application의 Sol·Terra·LiveProbe가 남아 있다. [Sol 적체 원장](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/backlog-sol-bounded-retry.json) · [04:24 집계](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/matched-summary-0424.json)

## Application 단독 검증과 제품 런타임 준비

Sol의 비공개 변형은 23.605초에 첫 Hikari 오류가 확인됐다. 같은 테스트는 수정 전 오류를 재현했고 수정 후 통과했다. dev의 독립 예약 요청 9건 성공과 Hikari 활성·대기 0, 풀 크기 4를 확인했다. 복구 판정 139.203초, 212,134토큰, 추정 $0.0922776이다. 원복과 MCP 서버 0개가 확인됐다. 04:33 집계는 정식 판정 21회, 복구 17회, 미복구 4회다. [Sol 원장](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-sol-bounded.json) · [04:33 집계](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/matched-summary-0433.json)

Terra의 첫 시행은 실제 연결 유출을 재현했으나 소스 파일 요청이 경로 검사를 통과하지 못해 미관측으로 종료됐다. 기준선 이미지와 업무 응답은 복원됐다. 파일 목록 도구가 상대 경로를 반환하는 반면 읽기 도구 설명과 구현은 파일명만 받았다. 목록에 나오는 정확한 상대 경로와 기존 파일명을 모두 받도록 맞추고, 범위 밖 요청에는 거부 결과를 돌려주도록 수정했다. 경로 이탈과 시험·소스 영역 혼용은 거부된다. [Terra 첫 원장](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-terra-bounded.json) · [파일 도구 검사](../../evaluation/tests/test_matched_recovery_campaign.py)

Source·QA·Cluster 실행기 시작 설정과 dev 예약만 읽는 관측기 설정을 준비했다. 기존 실행기 샌드박스 정책의 코드 경로를 후보 번들로 바꾼 사본에서 dev 카탈로그를 읽었고 신설 서비스의 prd 접근은 거부됐다. 후보 번들과 설정은 새 호스트 경로에 설치해 지문을 확인했다. Kubernetes에서는 Source·QA 조회, Cluster 조회·패치와 Service 프록시 GET의 새 권한 여섯 객체를 dev에 생성했다. 새 대상의 prd 패치는 세 역할 모두 거부됐다. Cluster 계정에는 기존 prd Deployment 메타데이터 조회가 있어 별도로 기록했다. [호스트 검증](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-native-host-services-sandbox-proof.json) · [후보 설치](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-native-host-services-installed.json) · [실제 권한 대조](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-native-dev-runner-rbac-active.json)

Terra 재시험의 첫 오류는 21.711초에 관측됐다. 할당 변형에서 단독 세션이 정상 종료됐고, 46.006초의 판정 시점에 독립 요청 두 건이 HTTP 500으로 남았다. 풀은 활성 4/4였다. 수정본 배포 증거가 없어 미복구로 판정했다. 34,633토큰과 추정 $0.018165를 기록했고 원복 뒤 예약 서비스가 정상임을 확인했다. 04:40 집계는 정식 판정 22회, 복구 17회, 미복구 5회다. [Terra 재시험 원장](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-terra-bounded-retry.json) · [04:40 집계](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/matched-summary-0440.json)

Application 실제 전환의 첫 시도에서 `launchctl bootout` 반환 직후 서비스가 여전히 등록된 상태를 오류로 판정했다. API·모델 작업자 Deployment는 원래 이미지와 준비 상태를 유지했다. 중지된 Source·QA는 원래 LaunchAgent 정의로 다시 시작했고 `/readyz`의 `ready=true`를 확인했다. 재시도부터는 종료와 시작 상태가 각각 끝날 때까지 짧게 조회한다.

Application 전환 뒤 첫 시도에서 관측기는 Hikari가 남긴 Pod 로그를 읽었으나 사건 접수 API가 403을 반환했다. 시험기는 사건이 생성되지 않은 시행을 미관측으로 보존하고 기준 이미지를 복원했다. 관측기의 기존 인증에 더해 dev 시험 서비스의 Pod 로그 사건 접수만 허용하도록 호출 경계를 좁혀 수정했다. 일반 사용자 인증과 prd 및 다른 서비스에 대한 실행기 범위는 그대로 적용한다. [첫 시행 원장](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-liveprobe-native-1.json) · [접수 권한 검사](../../liveprobe/app/application/browser_access.py) · [반영 이미지](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-native-intake-image.json)

08:01에 같은 변형의 실제 Pod 로그로 LiveProbe가 Application 사건을 자체 접수했다. 08:07의 사건 기록에서 소스 수정, 동일 회귀의 수정 전 실패와 수정 후 통과, 전체 6개 시험 성공, dev 배포 후 확인 단계가 관측됐다. 독립 업무 확인과 기준 이미지 원복이 끝나면 정식 원장에 반영한다. [08:07 사건 조회](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-native-progress-0807.json)

08:10에 제품의 dev 검증 실패와 수정 이미지의 준비 Pod 0개를 연결했다. 해당 Pod에는 `CrashLoopBackOff`와 `/app.jar`를 읽지 못한 JVM 오류가 있었고, Service에 준비된 연결 대상이 없었다. 고정 이미지를 네트워크 없는 읽기 전용 검증 컨테이너에서 확인했다. JAR는 root 소유, 권한 `0600`였으며 이미지의 실행 UID `10001`은 읽지 못했다. 기준 이미지로 돌아간 뒤 독립 예약 요청 9건과 Hikari 기준 상태를 확인했다. 시험 원본을 보존하고 후속 독립 검증을 대조한 판정을 추가했다. 정식 집계는 24회 중 23회, 복구 17회, 미복구 6회다. [원복 전 실패 관측](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-native-candidate-rollout-failure.json) · [JAR 권한 확인](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-native-candidate-jar-permissions.json) · [재집계](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/matched-summary-native-0812.json)

빌더는 dev 예약 시험용 JAR의 권한을 Docker 빌드 컨텍스트에 옮길 때 `0644`로 맞춘다. 호스트 `umask 077` 아래서 실제 패키징 경로를 따라가 파일 크기와 권한을 확인하는 회귀를 추가했다. 수정본을 새 이미지로 배포하는 검증은 호스트 저장 공간 여유가 약 165MiB인 시점에 보류했다. [패키징 수정](../../liveprobe/application_runner/host.py) · [회귀 검증](../../liveprobe/application_tests/test_runner_host.py) · [비교 보고서](../reviews/liveprobe-matched-dev-recovery-2026-11-26.md)

파생된 Kubernetes 사건 #1698이 인계 상태로 전환된 뒤 시험용 API·모델 작업자와 Source·QA·Cluster 실행기를 기준 설정으로 복원했다. 시험 관측기를 중지하고, 기준 이미지에서 정상 여부를 확인한 dev Deployment의 시험 배포 소유권 표시와 이번 시험에서 추가한 dev Role·RoleBinding 6개를 확인 후 제거했다. 사건·DB·원본 증거를 보존했다. API·패키징·집계 회귀 33개가 통과했다. [실행 환경 원복](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-native-restore-0811.json) · [배포 표시 원복](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-native-restored-fence.json) · [dev 권한 원복](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-native-dev-runner-rbac-restored.json)

08:24에 실패한 고정 이미지의 실제 JAR를 네트워크 없는 읽기 전용 컨테이너의 임시 메모리 파일로 복사했다. 원래 권한 `0600`에서 UID `10001`의 Java 실행이 거부됐다. 패키징 수정에서 사용하는 `0644`로 바꾸자 같은 JVM이 Spring Boot 계층을 읽었다. 원본 이미지와 Kubernetes 배포는 그대로였으며, 이 확인은 전체 dev 복구 시험과 별도로 기록했다. [실제 JAR 권한 검증](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-jar-runtime-mode-smoke.json)

기존 시험기가 교체 이미지의 시작 실패를 자동 기록하도록 판정 경로를 보완했다. 제품이 실패로 종료되고 보류 중인 쓰기가 없을 때, 회귀를 통과한 이미지와 동일 Deployment의 현재 리비전, 반복 비정상 종료, Service의 준비 연결 대상 0개를 원복 전에 함께 확인한다. 소유자가 다른 Pod가 실패했거나 준비 연결 대상이 남으면 미관측으로 유지한다. 이 세 가지 회귀를 포함해 API·패키징·집계 관련 36개 검사가 통과했다. 이전 실측 원본과 집계는 그대로 보존했다. [실행기](../../evaluation/matched_jdbc_product.py) · [롤아웃 확인](../../evaluation/reservation_dev_runtime.py) · [판정 회귀](../../evaluation/tests/test_matched_recovery_campaign.py)
