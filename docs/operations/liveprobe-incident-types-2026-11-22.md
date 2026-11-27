# 🩺 LiveProbe 장애 종류와 감지 조건

2026-11-22 · LiveProbe 운영 관제 · dev / prd

---

> **장애 관제 목록에는 Kubernetes, 애플리케이션, 복합 사건이 표시된다.** 운영 환경에서 사건을 자동으로 열거나 기존 사건에 붙이는 증상은 9종이다. Prometheus 경보가 7종이고, 예외 로그와 네트워크 흐름에서 각각 한 종류를 더 감지한다. 여러 감시기가 같은 증상을 보고해도 아래에서는 한 종류로 센다.

## 1. 자동으로 사건에 들어오는 증상

| 영역 | 장애 종류 | 사건에 들어오는 조건 | 저장 이력에서 확인한 예 |
|---|---|---|---|
| Kubernetes | 이미지 가져오기 실패 또는 잘못된 이미지 이름 | Deployment 소속 Pod가 `ErrImagePull`, `ImagePullBackOff`, `InvalidImageName` 대기 상태에 진입한다. 앞의 두 상태는 Prometheus도 별도 지속 시간 없이 경보한다. 새 Pod만 실패하고 이전 Pod가 요청을 처리할 때도 감지한다. | [#1556 notification](http://127.0.0.1:4175/incidents/1556) |
| Kubernetes | 컨테이너 반복 재시작 | Pod 감시가 `CrashLoopBackOff`를 관측하거나 Prometheus에서 같은 상태가 15초 지속된다. | [#1418](http://127.0.0.1:4175/incidents/1418) |
| Kubernetes | 컨테이너 메모리 부족 강제 종료 | Kubernetes가 기록한 컨테이너의 마지막 종료 사유가 `OOMKilled`다. 메모리 사용량과 종료 코드 137은 원인 조사에 따로 쓴다. | [#1418](http://127.0.0.1:4175/incidents/1418)에 재시작과 함께 기록 |
| Kubernetes | Service의 준비된 연결 대상 소실 | 준비된 엔드포인트가 0개인 상태를 10분 연속 관측한다. 현재 Service와 배포 상태를 다시 읽어 확인한다. selector 불일치와 Pod 자체의 준비 실패 모두 가능하다. 수동 연결 Service, ExternalName, 의도적인 0개 배포는 구분한다. [11월 22일 기준 변경](liveprobe-endpoint-loss-10m-2026-11-22.md) | [#1555 catalog](http://127.0.0.1:4175/incidents/1555) |
| Kubernetes · 네트워크 | 서비스 간 통신 드롭·오류 | 최근 5분간 같은 방향의 경로에서 서로 다른 `DROPPED`·`ERROR` 흐름이 합계 5건 이상이고 한쪽 이상을 대상 Deployment에 연결할 수 있어야 한다. 오래된 흐름은 건너뛴다. | [#1576](http://127.0.0.1:4175/incidents/1576) · 점검 시 수집 상태 아래 참조 |
| 애플리케이션 | 업무 요청 HTTP 5xx 증가 | `/api/.*` 요청의 최근 1분간 **초당 5xx 발생 건수**가 0.04를 초과한 상태가 15초 지속된다. 단위는 건/초다. 예외 스택이 없는 500·502·503도 요청 지표에 잡힐 수 있다. | [#1555 catalog](http://127.0.0.1:4175/incidents/1555) |
| 애플리케이션 | 응답 지연 | 최근 1분의 평균 응답 시간이 500ms를 초과하면 경보한다. `/actuator.*` 경로는 제외한다. 일부 요청만 느린 경우에는 평균이 문턱 아래에 머물 수 있다. | [#1401](http://127.0.0.1:4175/incidents/1401) |
| 애플리케이션 | DB 연결 풀 포화 | HikariCP 지표의 사용 중 연결 수 / 최대 연결 수가 95% 이상이다. 실제 요청 영향은 대기 스레드·연결 획득 실패와 함께 판단한다. | dev/prd에 규칙 활성. 점검 시 저장 이력에서 이 경보 이름 0건 |
| 애플리케이션 | 반복되는 코드·런타임 예외 | Loki에서 같은 환경·서비스·예외 클래스·대표 코드 위치 조합이 기본 120초 안에 3회 나타나면 `ApplicationException`을 발행한다. Python·Java·Node 형식을 읽으며 사용자 코드 위치가 추출되면 파일·행·심볼을 함께 묶는다. | [#1555 catalog](http://127.0.0.1:4175/incidents/1555) |

Prometheus의 dev/prd 규칙명은 `TargetWorkloadImagePullFailing`, `TargetWorkloadCrashLooping`, `TargetWorkloadOOMKilled`, `TargetServiceNoEndpoints`, `TargetHigh5xxRate`, `TargetHighLatency`, `TargetConnectionPoolExhausted`다. Pod 감시와 엔드포인트 감시가 발행하는 이름은 같은 증상의 별도 관측으로 남는다. 예전 단일 환경의 `OrderHigh5xxRate`, `InventoryHigh5xxRate`와 웹훅 호환명 `High5xx`는 HTTP 5xx에 포함한다. [운영 환경 경보식](../../scripts/render-p18-observability.py) · [Pod·이벤트·엔드포인트 감시](../../liveprobe/app/watcher.py) · [웹훅 라우팅](../../liveprobe/app/main.py) · [Service 상태 판별](../../liveprobe/app/service_health.py)

**서비스별 감지 범위가 다르다.** 업무 5xx·지연 규칙은 `app` 라벨이 `inventory|order|catalog|member|payment|notification|delivery`인 시계열을 사용한다. 현재 Deployment의 `trade-member`, `delivery-member`, `business-api`라는 라벨은 이 식에 포함되지 않는다. DB 풀 경보는 HikariCP 지표를 내보내는 대상에 적용된다. 로그 감시와 운영자 접수에는 각자의 서비스 식별 조건이 있다. [지표 규칙](../../scripts/render-p18-observability.py) · [로그 감시](../../liveprobe/app/app_errors.py) · [흐름 관측 범위](../../liveprobe/app/flows.py) · [네트워크 감지](../../liveprobe/app/network_signals.py)

## 2. 업무 장애와 코드 결함은 어떻게 확인하는가

자동 관제는 위의 증상으로 시작한다. 같은 HTTP 500이라도 주문 금액 계산, 상품 조회, 응답 직렬화는 서로 다른 원인일 수 있다. 다음은 시험 서비스에 구현된 **18종의 공통 장애 시나리오**와 운영자가 원인을 확인할 때 읽을 근거다.

| 장애 종류 | 구현된 시나리오 | 확인할 근거 |
|---|---|---|
| 코드·데이터 처리 4종 | 업무 함수 결함, 입력 검증·조회 결함, 응답 직렬화 결함, 간헐적 예외 | 실패한 업무 경로, 예외 위치, 같은 입력의 재현 결과 |
| 요청·성능 4종 | HTTP 오류율 증가, 지속 지연, 간헐적 꼬리 지연, 요청 멈춤·타임아웃 | 호출자/피호출자별 상태 코드, 응답 시간, 특정 요청에만 문제가 생기는지 |
| 시작·건강 확인 4종 | 프로세스 종료, 시작 설정 오류, readiness 실패, liveness 실패 | 종료 사유, 재시작 전후 로그, probe 이벤트, 준비된 엔드포인트 변화 |
| 자원·동시 처리 3종 | 메모리 누수, CPU 과점유, 업무 처리 슬롯 고갈 | 메모리 추세·OOM, CPU 사용·스로틀링, 작업 대기·503·워커 예외 |
| 배치·업무·연동 3종 | 백그라운드 작업 실패, 업무 정합성 위반, 외부 의존 서비스 호출 실패 | 배치/정합성 예외, 기대값과 실제 업무 결과, 의존 호출의 실패·지연 |

결제 **중복 청구**, 상품 **오래된 가격**, 회원 **세션 불일치**, 배송 **상태 역행**, 알림 **대기열 정체**, 재고 **초과 판매**처럼 HTTP 200으로 끝나는 문제도 있다. 반복 예외가 남으면 로그 감시로 포착할 수 있다. 정상 응답처럼 보이고 오류 지표나 예외도 없으면 운영자가 기대 결과와 실제 결과를 적어 접수하고, 조사에서 해당 업무를 재현해야 한다. 서비스별 예전 시나리오에는 이벤트 루프 차단, DB 연결 점유, 엔드포인트 비활성화도 있다. 시험 카탈로그는 단일·결합 시나리오 30개를 여섯 시스템에 배치한 180개 설계 사례다. [업무별 사례와 전체 시나리오](../plans/revamp/P18-fault-contract.md) · [30개 조합 원본](../../scripts/p18-campaign-catalog.py) · [시험 서비스 구현](../../services/catalog/runtime.py)

**단발성 장애 접수 방법**은 두 가지다. 외부 실행기의 Detector를 해당 서비스에 기동하면 Pod 로그의 예외 표본 한 건으로 접수하고 같은 서명의 중복을 거른다. 운영자는 장애 관제에서 dev/prd, 등록 서비스, 증상 설명을 입력해 직접 접수할 수 있다. 등록 서비스는 `order`, `inventory`, `catalog`, `payment`, `trade-member`, `delivery`, `notification`, `delivery-member`, `business-api`다. [#1557 inventory](http://127.0.0.1:4175/incidents/1557)는 단발성 직렬화 예외를 실행기가 실제 Pod 로그로 접수한 사례다. [Detector](../../liveprobe/application_runner/detect.py) · [접수 계약](../../liveprobe/app/application/contracts.py) · [서비스 목록](../../liveprobe/app/application/catalog.py) · [실제 시험 기록](liveprobe-real-operation-dev-prd-2026-11-13.md)

## 3. 경고 이벤트와 수집 진단에서 보이는 이상

| 화면에서 확인하는 항목 | 운영자가 구분할 내용 |
|---|---|
| Pod 건강·수명 | readiness/liveness/startup probe 실패(`Unhealthy`), 컨테이너 종료(`Killing`), 재시작 대기(`BackOff`), OOM 관련 이벤트, 자원 부족 퇴출(`Evicted`). readiness 경고 한 건과 Service의 연결 대상이 실제로 없어진 시점은 따로 확인한다. |
| 배치·생성·스토리지 | 노드 배치 실패(`FailedScheduling`), 생성 실패(`FailedCreate`), 동기화 실패(`FailedSync`), 볼륨 연결·마운트 실패(`FailedAttachVolume`, `FailedMount`), 일반 실행 실패(`Failed`). |
| 노드·확장 | 노드 재부팅·준비 실패(`Rebooted`, `NodeNotReady`), HPA 자원 지표 조회·복제 수 계산 실패(`FailedGetResourceMetric`, `FailedComputeMetricsReplicas`). 사건 근거에 노드 Ready·메모리·디스크·PID 압박 상태가 수집되면 함께 확인할 수 있다. |
| 자원·설정·수집 상태 | CPU 사용·스로틀링, 메모리 사용, 배포 리비전·이전 이미지, ConfigMap 변경 근거, 호출 경로 등을 조사한다. 수집원 오류·지연·미관측, 실행기 대기, 모델 호출 제한은 관제 또는 처리 경로의 상태로 표시된다. |

위 항목들은 운영 관제의 서비스 지도·경고 이벤트·수집 진단 또는 사건의 근거에서 확인한다. 예를 들어 볼륨 마운트 실패는 경고에 바로 나타날 수 있고, 준비된 Pod까지 사라지면 별도의 엔드포인트 신호로 장애 사건이 열린다. Kubernetes가 추가하는 다른 Warning reason도 원문을 열 수 있다. [경고 화면](../../dashboard/src/Overview.tsx) · [경고 표시명](../../dashboard/src/labels.ts) · [사건 근거 수집](../../liveprobe/app/evidence.py) · [수집원 상태](../../liveprobe/app/overview.py)

## 4. 복합 사건과 조사 난도

**복합**은 같은 사건에 Kubernetes와 애플리케이션 신호가 함께 기록됐거나 Kubernetes 사건에서 코드 작업이 연결된 경우다. **복잡**은 분석 난도다. Kubernetes 조사에는 단순·보통·복잡·고위험이 있고 Application 조사에는 국소 원인, 복수 증상·구성요소 간 조사, 권한·데이터 경계에 해당하는 1~3단계가 있다. 사건 유형과 난도는 각각 표시되며 실제 변경 여부는 근거 검증과 조치 정책으로 결정된다. [통합 목록 판별](../../liveprobe/app/console_history.py) · [Kubernetes 분류](../../liveprobe/app/router.py) · [Application 분류](../../liveprobe/app/application/worker.py)

예를 들어 [#1555 catalog](http://127.0.0.1:4175/incidents/1555)는 상품 검색 500과 readiness 503이 겹쳐 애플리케이션 예외, 높은 5xx, 준비 엔드포인트 소실이 한 사건에 모였다. 유형은 복합, 난도는 복잡이었다. 독립 검증 뒤에도 두 원인 후보가 남아 코드 변경은 실행되지 않았고, 운영자가 확인할 수 있도록 이력이 보존됐다. [실제 시험과 재분류 검증](liveprobe-real-operation-dev-prd-2026-11-13.md)

## 5. 11월 22일 운영 조회 결과

| 읽은 대상 | 결과 |
|---|---|
| 장애 관제 전체 이력, 19:46 KST의 동일 기준 시각 | Kubernetes **1,178건**, 애플리케이션 **329건**, 복합 **35건**, 합계 **1,542건**. 보존된 과거 환경과 시험 사건을 포함한 누적 건수다. [장애 관제](http://127.0.0.1:4175/incidents) |
| dev/prd Prometheus | 운영 경보 7개 모두 평가 상태 `ok`. 조회 시점에 엔드포인트 소실 규칙에 대기 중(`pending`)인 경보가 있었고 발화 중(`firing`)인 경보는 0건이었다. [운영 관측 설정](../../deploy/environments/observability.json) |
| 수집 상태 | dev·prd의 Pod·이벤트·엔드포인트 감시기 6개는 실행 중. 네트워크 감시기는 관측 시간 초과로 `degraded`였다. dev/prd에는 과거 `NetworkDrop` 사건 23건이 저장돼 있다. 이 시간대의 새 네트워크 장애 탐지는 제한적으로 해석해야 한다. [감시기 상태 API](http://127.0.0.1:4175/api/status) |
| 접속 확인, 19:48 KST | 4175의 Kubernetes와 Application 준비 API는 모두 HTTP 200. 같은 주소의 홈 HTML 조회는 HTTP 503을 반환했다. 위 수치와 감지 범위는 운영 API·Prometheus·저장 이력과 소스를 읽어 확인했다. [Kubernetes 준비 상태](http://127.0.0.1:4175/api/readyz) · [Application 준비 상태](http://127.0.0.1:4175/api/application/v1/readyz) |
