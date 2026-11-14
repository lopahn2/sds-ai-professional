# 삼성 도메인 운영 안내

## 범위와 데이터 표시

`services/samsung-domain/`는 비즈니스 원장과 트레이드·딜리버리 자체 회원·HR 읽기 모델을 제공한다.
실제 사내 인증·전자서명·실제 임직원 반입 기능은 아니다. 데이터·한국어 화면·권한 시험은 합성 예제다.
운영 배포·기존 회원 보존/이관·실브로커 자격·CA/Secret·환경별 cutover는 부모 통합 작업에서 수행한다.

| DOMAIN_MODE | SYSTEM_NAME | SERVICE_NAME | 정확한 화면 이름 |
|---|---|---|---|
| business | samsung-business | business-api | 삼성 비즈니스 시스템 |
| trade | samsung-trade | trade-member | 삼성 트레이드 시스템 |
| delivery | samsung-delivery | delivery-member | 삼성 딜리버리 시스템 |

`APP_ENV=dev|stg|prd`, `APP_VERSION=<고유 빌드 버전>`을 지정한다.
서로 다른 시스템·환경이 같은 DB를 잘못 사용하면 DB identity 검사로 시작을 거절한다.

## 파일·포트 계약

파일 내용은 런타임 내부에서만 읽고 로그·응답에 노출하지 않는다. Secret은 이 앱이 생성·조회·회전하지 않는다.

| 환경변수 | 기본 경로 또는 값 |
|---|---|
| DATABASE_DSN_FILE | /var/run/samsung/db/dsn |
| KAFKA_CONFIG_FILE | /var/run/samsung/kafka/client.json |
| KAFKA_CA_FILE | /var/run/samsung/kafka/ca.crt |
| API_CREDENTIALS_FILE | /var/run/samsung/api/credentials.json |
| BUSINESS_TOKEN_FILE | /var/run/samsung/api/reader-token |
| BUSINESS_API_URL | https://business-api.<env>.svc:8443 |
| BUSINESS_CA_FILE | /var/run/samsung/tls/ca.crt |
| TLS_CERT_FILE | /var/run/samsung/tls/tls.crt |
| TLS_KEY_FILE | /var/run/samsung/tls/tls.key |
| READ_ONLY_MODE | 1 |
| HTTP_PORT / TLS_PORT | 8080 / 8443 |

- `credentials.json`은 `{principals:[{token,role,environment,target_system}]}` 구조다. 실제 파일은 JSON의 따옴표를 포함해야 한다.
- 역할은 `business-admin`(대상 null), `trade-reader`(samsung-trade), `delivery-reader`(samsung-delivery)이다.
- reader 파일에는 자기 환경·시스템 주체만 마운트한다. 원장 관리자 자격을 소비자에게 전달하지 않는다.
- 보호 API는 `Authorization: Bearer <서비스 자격>`을 사용한다. query/body의 환경·대상으로 권한을 늘리지 못한다.
- 인증서 SAN에는 Service DNS가 필요하다. 원장 클라이언트는 CA·서버 이름을 검증하고 HTTP/redirect/환경 proxy로 우회하지 않는다.
- 비즈니스 PostgreSQL role은 자체 DB·samsung_domain schema의 DDL/CRUD가 필요하다. 서비스 시작 시 확장 전용 `CREATE IF NOT EXISTS`를 적용하며 기존 테이블·볼륨을 drop하지 않는다.
- 런타임은 비root UID/GID 10001이다. 마운트 파일을 해당 사용자에게 읽을 수 있게 한다.

## 공개·보호 표면

공개 Ingress backend는 HTTP 8080이다. 임직원·계약·원문·복구 라우트를 등록하지 않는다.
`X-Forwarded-Proto: https`로 보호 경로를 열 수 없다. 내부 TLS 8443은 별도 앱이다.

### 공개 HTTP

- `GET /`: 한국어 소개·DB 준비·동기화·쓰기 모드·로컬 고객 화면.
- `GET /api/info`: `system,service,environment,display_name,version,synthetic`.
- `GET /api/status`: 비민감 준비·동기화 상태. 직원/계약 데이터·자격·원문은 제외한다.
- `GET /healthz/live`, `GET /healthz/ready`: DB 불가 시 readiness는 503. Kafka 지연은 별도 상태이며 고객 API를 일괄 중지하지 않는다.
- `GET /metrics`: DB 준비, 읽기 전용, fresh, 미복구 격리, 미발행 outbox 지표.
- trade/delivery: `GET /api/members?search=` → `{members:[{id,name,createdAt}],total}`. total은 기존 회원 서비스와 같이 전체 로컬 회원 수다.
- trade/delivery: `POST /api/members`에 `{name:"합성 고객"}`. 로컬 DB 저장·중복 이름 409·최대 1,000명.
- trade/delivery: `POST /api/login`, `GET /api/members/{id}/profile`은 합성 고객 확인이며 실제 인증·원장 권한을 부여하지 않는다.

### 보호 TLS

- `GET /api/v1/employees`, `GET /api/v1/contracts`: business-admin은 자기 환경 원장, reader는 자기 조직 투영만 조회한다. 소비자는 자기 로컬 읽기 모델만 조회한다.
- `GET /api/v1/employees/{id}`, `GET /api/v1/contracts/{id}`: 허용 범위 내 단건.
- business-admin: `POST /api/v1/employees`, `POST /api/v1/contracts`.
  - 직원 입력: `{id?,name,title?,department?,target_systems:["samsung-trade"],active?:true}`.
  - 계약 입력: `{id?,title,employee_id?,status?:"active",target_systems:[...],content?}`.
  - ID 생략 시 UUID 생성. 같은 ID 생성은 409. 허용하지 않는 필드·민감 개인정보는 422.
- business-admin: `PATCH /api/v1/employees/{id}`, `PATCH /api/v1/contracts/{id}`.
  - `{expected_version:1,...변경할 허용 필드}`. 버전 누락/형식 오류 422, 경합 패자 409.
  - 계약 상태: active/draft/approved/terminated/cancelled/revoked. 종료·취소·철회 또는 active=false는 revoke 이벤트다.
  - 소속 변경은 이전 대상 revoke·새 대상 upsert를 같은 TX에 생성한다.
- business-admin: `DELETE /api/v1/employees/{id}?expected_version=N`, 계약도 동일. 실제 행 삭제 대신 비활성화·철회 버전을 남긴다.
- reader: `POST /api/v1/sync/exports`에 `{}` 또는 자기 환경/대상만 지정. 응답에 첫 페이지·metadata·next_page가 있다.
- reader: `GET /api/v1/sync/exports/{export_id}?page=0`(0 시작). 소유권·만료 검사, 최대 500행/페이지. 다른 reader의 export ID는 404.
- `GET /api/v1/contracts/{id}/content`: 비즈니스 현행 원장의 활성·조직 범위 검사 후 원문.
- `GET /api/v1/employees/{id}/verify`, 계약 `/verify`: 현행 원장 확정과 버전·확정 시각. 이것은 장기 권한 grant가 아니다.
- directory의 원문/verify는 fresh이면서 비즈니스 원장 요청까지 성공해야 한다. 읽기 모델이 fresh여도 원장에서 이미 revoke되면 403이다. 원장 불가 시 503.
- `GET /api/v1/sync/status`: 인증된 동기화 세대·checkpoint·watermark·격리 상태.
- 오류: 미인증 401, 범위/행위 403, 없음 404, 충돌 409, 입력 422, 의존·복구·안전 모드 503. DSN·토큰·상세 내부 예외를 돌려주지 않는다.

`READ_ONLY_MODE=1`에서는 고객 POST와 원장 변경을 거절한다. 복구 export 생성·inbox·복제·ACK·상태 조회는 안전 복구를 위해 계속 허용한다.

## Kafka 배선과 순서

`client.json`은 Confluent 객체이고 bootstrap.servers, security.protocol=SASL_SSL, sasl.mechanism=PLAIN, sasl.username, sasl.password가 필요하다.
CA 위치는 `KAFKA_CA_FILE`로 고정하며 인증서·호스트 검증을 강제한다.
프로듀서는 acks=all·enable.idempotence=true·in-flight 1을 설정한다. 컨슈머는 auto commit/store·자동 토픽 생성 비활성, offset reset=error다.

- business → `<env>.business.trade.master.v1`, `<env>.business.delivery.master.v1`.
- trade 고유 group: `<env>.samsung-trade.master.v1`.
- delivery 고유 group: `<env>.samsung-delivery.master.v1`.
- 이 **정확한 group ID**를 Kafka ACL bootstrap에 허용해야 한다. client.json group.id는 위 고유 값으로 덮어쓴다.
- 단일 파티션 0만 인수한다. 원장 key=`employee:<id>`/`contract:<id>`, watermark key=`control:watermark`.
- 토픽·주체·ACL은 부모가 만들고 앱에는 Kafka 관리자 자격을 주지 않는다.

원장 행 잠금 전에 대상 상태를 ID 정렬 순으로 잠근다. 원장 버전·대상 커밋 순번·전체 최소 투영·대상별 outbox를 한 TX에서 변경한다.
발행기는 환경·대상 advisory lock 한 소유자만 동작하며 순번순 한 행씩 브로커 ACK를 받은 뒤 해당 행만 ACK한다.
ACK 실패·종료 불명 상태에서는 DB 미발행 행이 남는다. 한 대상 실패는 다른 대상 ACK를 지우지 않는다.
watermark는 30초 간격, 해당 barrier 이하 미발행이 없을 때만 발행한다. ACK 불명 watermark도 같은 ID·바이트로 재시도한다.
RF1에서 all ACK는 단일 브로커 ACK이며 복제 내구성·고가용성을 뜻하지 않는다.

## 초기·보존 초과·격리 복구

1. 소비 DB 소유자 advisory lock을 잡고 현재 Kafka 시작 offset과 next-write S를 캡처한다.
2. TLS 원장의 REPEATABLE READ export에서 대상 순번 W와 전체 현재 상태를 불변 JSON으로 고정한다.
3. 새 generation에 모든 페이지의 소속·스키마·개수·전체 해시·만료를 검증하여 적재한다. snapshot에서 빠진 행은 새 세대에 없다.
4. 적재 후 end H를 캡처하고 S부터 재생한다. target_seq≤W는 적용하지 않는다. W 초과는 전체 상태·버전으로 적용한다.
5. inbox ID+내용 SHA256, entity 버전·revoke, 적용 순번, DB checkpoint를 같은 TX에 저장한 뒤 Kafka commit한다.
6. H까지 재생한 뒤 활성 포인터·세대 checkpoint·경계를 DB 한 TX로 전환한다. Kafka commit 유실은 DB checkpoint에서 다시 시작한다.
7. 최근 90초 watermark, barrier까지 연속 적용, 재구축 없음·미복구 격리 0·의존 오류 없음이어야 fresh다. lag 0만으로 fresh가 되지 않는다.
8. 잘못된 JSON·스키마·소속·동일 ID 다른 바이트는 내용 해시/위치/고정 진단을 DB에 격리한 뒤 offset을 넘긴다. 원문·자격은 저장 로그에 덤프하지 않는다.
9. 현재 generation 격리는 새 export로 재구축한다. 이전 세대 격리는 새 세대의 W 이상 watermark/연속 순번 확정 후에만 해제한다. watermark가 전환 뒤 도착해도 이 절차를 적용한다.
10. 복구 S 보존 초과·만료·해시 불일치·재생 실패는 성공 처리하지 않는다. 활성 세대는 유지하고 새로운 복구를 시도한다.

`fresh`는 기준 시점까지의 수렴이다. 현재 순간의 즉시 최신성이나 장기 권한 승인을 뜻하지 않는다.

## 저장·자원·원복 경계

- DB pool 최대 4, statement timeout 60초, lock timeout 3초.
- 미발행 outbox 10,000행이면 새 원장 변경 503. ACK된 outbox는 72시간 이후 배치 정리한다.
- export 최대 60초·50MiB·15분·500행/페이지. 대상당 유효 export 8개를 넘으면 503이며 기존 불변 export를 조용히 지우지 않는다.
- 활성 generation inbox 10,000건 또는 격리 1,000건이면 offset 전진을 중단하고 snapshot 재구축한다.
- 이전 비활성 generation은 24시간 유예, 미복구 격리가 없고 활성/복구 포인터가 가리키지 않을 때만 자신의 replica/inbox/격리 자료를 정리한다.
- 원장·고객·현재 투영·최신 revoke 표식·미발행 outbox는 보존한다. 운영 DB/PVC와 다른 시스템 데이터는 삭제하지 않는다.
- broker PVC 85% 경고·95% 쓰기 중단은 부모의 저장 모니터/운영 게이트와 결합해야 한다. 이 앱은 클러스터/PVC 관리 권한을 갖지 않는다.
- 새 쓰기 수락 후에는 구 DB/메모리 앱으로 단순 재연결하지 않는다. READ_ONLY_MODE=1로 새 DB를 보존하며 호환 release 또는 검증된 역이관 절차를 사용한다.

## 재현과 남은 통합 인수

설치·시험·image build는 `services/samsung-domain/README.md`의 명령을 따른다.
모든 `tests/run.py` 실행은 고유 폐기 PostgreSQL(256MiB 이하·자체 tmpfs)을 만들고 정리 영수증을 남긴다.
실제 PG 동시 쓰기/rollback·snapshot·inbox·HTTP/TLS 시험과 1,000개 합성 원장·두 DB snapshot을 제공한다.
발행 ACK 실패·소비 commit 유실은 transport fixture이며 **실제 Kafka 장애·ACL 통과 결과가 아니다**.

부모 인수 필요 항목: 실제 SASL_SSL Kafka topic/group ACL, CA·SAN 실패, 두 독립 소비 그룹의 완전 수신,
브로커/PVC 재시작·retention 초과, 환경별 고객 데이터 보존/단일 writer cutover, 실제 이미지 digest/UID 배포,
10분 연속 메모리/CPU·1,000건 이벤트 전송 부하, dev→stg→prd rollout·NetworkPolicy·외부 host 확인.
독립 코드 리뷰·브라우저 시각 검수는 부모가 별도로 수행한다. 로컬 시험을 이 항목의 통과로 대체하지 않는다.
