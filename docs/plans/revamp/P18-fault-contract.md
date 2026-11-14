# P18 애플리케이션 장애 공통 계약 (장애 캠페인용)

목적: 6개 시스템(order·payment·member·catalog·delivery·notification) × 장애 30건(단순·보통·복잡 각 10건)을 같은 방식으로 주입·원복·기록한다. 세 언어(Python·Node·Java) 구현이 아래 이름·매개변수·동작을 똑같이 따른다.

## 1. 제어 API

| 언어 | 경로 | 보호 |
|---|---|---|
| Python(payment·catalog·notification)·Node(member·delivery) | `GET/POST/DELETE /api/chaos` (기존 경로 유지) | 없음(사용자가 화면 버튼으로 직접 누르는 요구) |
| Java(order·inventory) | `GET/POST/DELETE /internal/chaos/v1/app-faults` (신규) | 기존 v1과 같은 `chaos` 프로필·루프백·운영자 토큰 |

- `POST {"fault": "<이름>", "on": true|false, "params": {...}, "ttl_seconds": 1..3600}` → 켜기·끄기. 여러 장애를 **동시에** 켤 수 있다.
- `DELETE` → 전부 해제하고 영속 상태 파일도 지운다.
- `GET` → `{"active":[...], "faults":[{"name","label","signal","on","params","expires_at"}]}`.
- 기존 이름(`duplicate_charge`, `stale_price`, `queue_stuck`, `event_loop_block`)과 기존 Java v1 모드(`crash, oom, latency, pool_exhaust, endpoint_off`)는 그대로 유지한다.
- 켜고 끌 때 JSON 로그 `event=chaos_changed`(fault, on, params)를 남긴다.

## 2. 영속(재시작 후에도 유지)

- 환경변수 `FAULT_STATE_DIR`(쿠버네티스에서는 파드 emptyDir)가 있으면 켜진 장애를 `faults.json`에 `{name:{params, expires_at}}`로 저장하고, 시작할 때 만료되지 않은 장애를 다시 읽는다.
- 컨테이너가 재시작돼도 장애가 유지되므로 crash·startup_config_error·liveness_fail·memory_leak이 실제 반복 재시작(CrashLoopBackOff)이나 반복 OOMKilled로 이어진다.
- 파드를 지우면 emptyDir가 비워져 장애가 사라진다. 따라서 "파드 재시작"은 이 장애들을 실제로 해소한다.
- 기본 TTL은 900초, 최대 3600초다. 만료되면 자동으로 꺼진다.

## 3. 공통 장애 프리미티브

| 이름 | 매개변수(기본값) | 실제 동작 | 기대 신호 |
|---|---|---|---|
| `code_bug` | – | 업무 함수 A의 고의 결함(기존) | 500 + 사용자 코드 스택 |
| `code_bug_validation` | – | 업무 함수 B: 입력·조회 처리 결함(예: 없는 키, None 접근) | 500 + 다른 위치 스택 |
| `code_bug_serialization` | – | 목록·조회 응답 직렬화 결함(예: 직렬화 불가 값) | 500 + 또 다른 위치 스택 |
| `intermittent_exception` | `ratio` 0.15 | 업무 요청 중 비율만큼 처리되지 않은 예외 | 간헐 500 + 스택 |
| `error_rate` | `ratio` 0.5 | 업무 요청 비율만큼 503(스택 없음, 기존) | 5xx 비율 |
| `latency` | `seconds` 2 | 업무 응답 고정 지연(기존) | 평균 지연 |
| `latency_spike` | `ratio` 0.3, `seconds` 3 | 일부 요청만 긴 지연 | 꼬리 지연 |
| `hang` | `seconds` 40 | 업무 요청이 오래 멈춤(호출자 시간 초과) | 시간 초과·지연 |
| `crash` | – | 시작 3초 뒤 종료 코드 1(영속) | 반복 재시작·CrashLoopBackOff |
| `startup_config_error` | – | 시작 시 설정 검증 함수에서 예외 스택 출력 후 종료 코드 1(영속) | CrashLoopBackOff + 스택 |
| `memory_leak` | `mib_per_second` 8 | 상한 없는 메모리 증가(영속) | OOMKilled 반복 |
| `not_ready` | – | 준비 확인 503(기존) | 준비된 엔드포인트 0 |
| `liveness_fail` | – | 생존 확인 503(영속) | 활성 프로브 실패 재시작 |
| `cpu_spin` | `threads` 2 | 바쁜 반복으로 CPU 점유 | 지연 증가·CPU 포화 |
| `worker_exhaustion` | – | 업무 처리 슬롯(동시 8개)을 모두 점유. 대기 10초 뒤 503과 `WorkerPoolExhausted` 예외 스택 | 503 + 스택 + 지연 |
| `background_job_failure` | `interval_seconds` 3 | 주기 작업(정산·색인 등)이 예외 스택을 남기며 실패. HTTP 영향 없음 | 앱 예외 로그만 |
| `business_invariant` | – | 시스템별 업무 불변식 위반을 감지해 예외 스택을 로그로 남김(응답은 정상) | 앱 예외 로그 |
| `dependency_failure` | – | 외부 연동(결제 게이트웨이·메일 게이트웨이 등) 호출이 실제 연결 거부로 실패 | 502 + 연결 예외 스택 |

시스템별 `business_invariant` 의미: payment 중복 청구, catalog 오래된 가격, notification 발송 대기열 정체, member 세션 불일치, delivery 배송 상태 역행, order(inventory) 재고 초과 판매. 기존 `duplicate_charge`·`stale_price`·`queue_stuck`도 같은 예외 로그를 남기도록 맞춘다.

## 4. 공통 실행 조건

- 자체 트래픽: `SELF_TRAFFIC_INTERVAL_SECONDS`(기본 4, 배포 시 1). 업무 경로를 돌아가며 호출해 각 코드 결함 경로가 실제로 실행되게 한다. Java order에도 같은 자체 트래픽을 추가한다(`/api/orders/check`, 재고 연동 포함). loadgen은 쓰지 않는다.
- 예외는 언어 표준 스택 형식으로 표준 출력에 남긴다(Python `File "...", line N`, Node `at ... (file.js:N:C)`, Java `at ...(File.java:N)`). JSON 접근 로그와 섞지 않는다.
- 지표 `http_server_requests_seconds_*`와 `app_errors_total{exception}`을 유지한다.
- 외부 패키지 추가 금지(Python 표준 라이브러리, Node 내장 모듈, Java는 기존 의존성).

## 5. 배포 결함(release) 장애 — 재시작으로 해소되지 않는 결함

실제 운영에서 코드 결함·잘못된 설정은 파드를 재시작해도 남는다. 이를 재현하기 위해 두 번째 장애 출처를 둔다.

- 환경변수 `RELEASE_FAULTS`를 고유 장애 이미지의 ENV에 고정한다. 정상 이미지에는 이 값이 없다. 같은 장애 이미지로 파드를 재시작해도 결함이 남고, 이전 정상 이미지로 rollback하면 사라진다. ConfigMap 파일을 읽는 초기 초안은 사용하지 않는다.
- 형식: `{"faults": {"<이름>": {"params": {...}}}}`. 이름·매개변수 검증은 런타임 장애와 같다.
- 배포 결함은 API로 끌 수 없다(`DELETE /api/chaos`는 런타임 장애만 해제). 각 항목에 `source: "runtime" | "release" | null`, 최상위에 `release_active: [...]`를 제공한다. 기존 Node faults map 호환을 유지한다.
- 배포 결함 활성 목록을 시작 로그로 남긴다. 장애를 꺼서 복구한 것과 실제 소스 수정 PR로 근본 해결한 것을 혼동하지 않는다.
- `startup_config_error`가 배포 결함으로 켜지면 업무 모듈의 설정 검증 함수에서 예외 스택을 남기고 종료 코드 1로 끝난다(재시작해도 같은 ENV이므로 반복 종료).
- 장애 판정 함수 `enabled(name)`은 런타임과 배포 결함 둘 중 하나라도 켜져 있으면 참이다.

## 6. 시스템별 업무 경로·결함 위치·의존 관계

같은 환경 안에서만 호출한다. 의존 대상 주소는 환경변수로 받는다(비어 있으면 연동을 건너뛴다). 호출 제한 시간 2초. 연결 실패·시간 초과·5xx 응답은 업무 예외 `DependencyError`(원인 예외 포함)로 바꿔 스택을 남기고 502로 응답한다.

| 시스템(언어) | 업무 경로 | `code_bug`(기존) | `code_bug_validation` | `code_bug_serialization` | 의존(환경변수) | `background_job_failure` 작업 | `business_invariant` |
|---|---|---|---|---|---|---|---|
| payment(Python) | `POST/GET /api/payments`, `POST /api/payments/{id}/cancel`, `GET /api/payments/{id}/receipt`(신규) | 수수료 계산 0 나눗셈 | 취소 시 환불 금액을 없는 키로 조회(KeyError) | 영수증 금액 서식 오류(ValueError) | member `MEMBER_URL` → 결제 생성 시 `GET /api/members?search=<구매자>` | 정산 작업 `SettlementError` | 중복 청구 `DuplicateChargeError` |
| catalog(Python) | `GET /api/products?q=`, `GET /api/products/{id}`, `POST /api/products/{id}/price`(신규) | 검색어 키 오류(기존) | 상품 상세의 할인 정보 None 접근(TypeError/AttributeError) | 가격 변경 응답 서식 오류 | inventory `INVENTORY_URL` → 상품 상세에서 `GET /api/stock/{item}` | 검색 색인 재생성 `IndexRebuildError` | 오래된 가격 `PriceConsistencyError` |
| notification(Python) | `POST/GET /api/notifications`, `POST /api/notifications/{id}/retry`(신규) | 채널 정규화(기존) | 재발송 대상 조회 None 접근 | 목록 응답의 템플릿 서식 오류 | member `MEMBER_URL` → 발송 등록 시 수신자 조회 | 발송 작업 `DispatchJobError` | 발송 대기열 정체 `QueueStalledError` |
| member(Node) | `GET/POST /api/members`, `POST /api/login`, `GET /api/members/{id}/profile`(신규) | 로그인 프로필 인덱스(기존) | 회원 등록 이름 정규화 결함(TypeError) | 프로필 응답 날짜 서식 오류(RangeError) | catalog `CATALOG_URL` → 프로필의 추천 상품 `GET /api/products?q=` | 세션 정리 작업 `SessionCleanupError` | 세션 불일치 `SessionIntegrityError` |
| delivery(Node) | `GET/POST /api/deliveries`, `GET/PATCH /api/deliveries/{id}[/status]`, `GET /api/deliveries/{id}/eta`(신규) | 배송 중 전환 이벤트 인덱스(기존) | 등록 시 도착 예정일 파싱 결함 | 도착 예정 응답 서식 오류 | notification `NOTIFICATION_URL` → 상태 변경 시 `POST /api/notifications` | 지연 감시 작업 `DelayScanError` | 배송 상태 역행 `StatusRegressionError` |
| order(Java order) | `GET /api/orders/check?item=`, `POST /api/orders`(신규), `GET /api/orders/{id}`(신규) | 주문 금액 계산 `ArithmeticException` | 상품 코드 정규화 `NullPointerException` | 주문 조회 응답 변환 `NumberFormatException` | inventory `INVENTORY_URL`(기존) | 주문 만료 정리 `OrderExpiryJobException` | – |
| order(Java inventory) | `GET /api/stock/{item}`, `POST /api/stock/{item}/reserve`(신규) | 예약 수량 계산 `ArithmeticException` | 품목 조회 `NullPointerException` | 재고 응답 변환 `NumberFormatException` | PostgreSQL(기존); `dependency_failure`는 DB 대신 연결 거부 예외 | 재고 대사 작업 `StockReconcileException` | 재고 초과 판매 `OversellException` |

- 의존 방향(순환 없음): payment→member, notification→member, member→catalog, catalog→inventory, delivery→notification, order→inventory.
- 코드 결함 3종은 서로 다른 함수·다른 업무 경로에 둔다. 결함 위치 주석은 "정상 수정 방법"을 한 줄로 적는다(코드 수정 PR 흐름에서 쓰임).
- 결함은 장애가 꺼져 있으면 절대 실행되지 않아야 한다. 기본 동작은 정상이다.
- 자체 트래픽은 위 업무 경로를 모두 돌아가며 호출한다(존재하는 시드 id 사용). 따라서 어느 결함이든 켜지면 수 초 안에 실제 예외가 난다.
- 화면(`/`)의 장애 버튼 목록은 `GET /api/chaos`의 항목에서 자동으로 그린다. 배포 결함은 "배포 결함(설정)"으로 표시하고 버튼을 비활성화한다.

## 7. Java 제어 API 상세

- `GET/POST/DELETE /internal/chaos/v1/app-faults` — 기존 `/internal/chaos/v1/faults`와 같은 보호(프로필 `chaos`, 루프백, `Authorization: Bearer <CHAOS_OPERATOR_TOKEN>`)와 같은 오류 응답을 쓴다.
- 영속 파일은 `CHAOS_STATE_DIR/app-faults.json`. 배포 결함은 이미지 ENV `RELEASE_FAULTS`의 JSON이며 파일 기반 `RELEASE_FAULTS_FILE`은 사용하지 않는다.
- 자체 트래픽: `SELF_TRAFFIC_INTERVAL_SECONDS`가 0보다 크면 `@Scheduled`로 자기 자신(`http://127.0.0.1:8080`)의 업무 경로를 돌아가며 호출한다(order는 check·create·조회, inventory는 조회·예약). 기본 0(꺼짐) — 배포에서만 켠다.
- `memory_leak`은 힙에 계속 할당해 `OutOfMemoryError`(이미지의 `-XX:+ExitOnOutOfMemoryError`로 종료)를 일으킨다. 기존 v1 `oom`(네이티브 할당·커널 OOMKilled)과 다른 장애다.
