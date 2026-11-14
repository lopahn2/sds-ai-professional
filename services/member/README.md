# 회원 시스템

## 업무 경로
| 방식 | 경로 | 요청·응답 |
|---|---|---|
| 조회 | `/api/members?search=이름` | 이름 부분 검색, 전체 회원 수 |
| 등록 | `/api/members` | `POST {"name":"새회원"}` → 201, 중복 409 |
| 로그인 시도 | `/api/login` | `POST {"name":"장애회원"}` → 가짜 로그인 결과 |

이름만 받는다. 비밀번호를 포함한 다른 필드는 400으로 거절하고 저장·해시·로그 기록을 하지 않는다. 데모 회원은 김하늘·이준호·장애회원이다.

| 프로필 | `/api/members/{id}/profile` | GET, 추천 연동 `CATALOG_URL` → `/api/products?q=` |

## 코드 결함 재현과 정상 수정
`code_bug` 활성 상태에서 `POST /api/login {"name":"장애회원"}` 요청이 실패한다. 이름에 ‘장애’가 포함된 가입 회원만 해당하며 화면의 ‘업무 요청 즉시 재현’ 또는 기본 자체 트래픽으로 재현한다.

고치는 방법: `business.js`의 `login`에서 단일 프로필 배열에 없는 인덱스 1을 선택하는 식을 유효한 인덱스 0 조회로 바꾸면 장애가 켜진 상태에서도 정상 로그인된다.

업무 고유 지표는 `member_signups_total`·`member_login_attempts_total`다.

## 실행·시험·이미지
외부 패키지가 없으므로 `npm install`을 실행하지 않는다. 업무 시드는 메모리, 장애 상태는 선택적으로 영속한다. 같은 환경 의존 주소가 설정되면 업무 연동을 호출한다.

서비스 저장소 루트에서:
```sh
node --test test
node server.js
docker build --pull=false --network=none -t liveprobe/member:p18-dev .
docker run --rm -p 18084:8080 --memory 128m -e APP_ENV=dev liveprobe/member:p18-dev
```

통합 저장소 루트에서는 `node --test services/member/test`, `docker build --pull=false --network=none -t liveprobe/member:p18-dev services/member`를 사용한다. 포트는 8080이며 이미지 실행 사용자는 비루트 `node`다. 도커파일은 작업 시작 시 존재했던 `node:22-alpine`의 다이제스트를 고정한다. 빌드 중 단위 시험도 실행한다. 정상 시 메모리 목표는 60메비바이트 이하다.

## 공통 경로
| 방식 | 경로 | 설명 |
|---|---|---|
| 조회 | `/` | 실제 업무 화면, 지역 스타일·스크립트 |
| 조회 | `/api/info` | 시스템·서비스·환경·버전 |
| 조회 | `/healthz/live` | 생존 확인 |
| 조회 | `/healthz/ready` | 준비 확인, 준비 실패 실험 중 503 |
| 조회 | `/metrics` | 프로메테우스 텍스트 |
| 조회 | `/api/chaos` | 각 장애의 켜짐·매개변수·메모리 증가분 |
| 변경 | `/api/chaos` | `POST {"fault":"latency","on":true,"params":{"delay_ms":2000}}` |
| 해제 | `/api/chaos` | `DELETE`로 runtime만 해제 |

개별 해제는 `POST {"fault":"latency","on":false}`다. 업무 장애는 제어 경로·화면·지표 조회에는 적용하지 않는다. 이벤트 루프 정지와 프로세스 종료는 프로세스 전체에 영향을 준다.

## 장애 버튼과 기대 신호
| 이름 | 화면 버튼명 | 기대 신호·기본값 | 복구 |
|---|---|---|---|
| `code_bug` | 코드 결함 | 특정 업무 요청 500, 실제 `TypeError` 스택, `app_errors_total` 증가 | 버튼 끄기 또는 모두 해제 |
| `latency` | 응답 지연 | 업무 응답 2초, 요청 히스토그램·합계 증가 | 끄기 |
| `error_rate` | 일부 요청 실패 | 기본 절반의 업무 요청 503, `rate` 0~1 | 끄기 |
| `crash` | 프로세스 종료 | 3초 뒤 실제 종료 코드 1, FAULT_STATE_DIR 재사용 시 장애 유지 | 컨테이너 재시작 |
| `memory_leak` | 메모리 누수 | 초당 8메비바이트 실제 할당·보유, 누적 상한 없음, 128메비바이트 제한에서 메모리 부족 종료 가능 | 종료 전 끄기 또는 재시작 |
| `not_ready` | 준비 상태 실패 | 준비 확인 503·생존 확인 200 | 끄기 |
`latency.params.delay_ms`는 1~30000, `memory_leak.params.mib_per_second`는 1~64다. 한 번의 할당 크기만 제한하며 누적 메모리 증가량은 제한하지 않는다. 해제는 버퍼 참조를 비우므로 실제 메모리 회수는 수거 시점에 따라 늦어질 수 있다. 중단 위험이 있는 버튼은 확인을 받는다.

## 환경 변수
| 변수 | 기본값 | 의미 |
|---|---|---|
| `SYSTEM_NAME` | `member` | 시스템 식별자 |
| `SERVICE_NAME` | `member` | 서비스 식별자 |
| `APP_ENV` | `dev` | `dev`·`stg`·`prd`, 화면에 개발·검증·운영 환경 배지 |
| `APP_VERSION` | `0.1.0` | 화면 하단·식별 지표 |
| `PORT` | `8080` | 로컬 확인용 포트 |
| `FAULT_CODE_BUG` | `0` | `1`이면 실제 코드 결함 분기 활성 |
| `SELF_TRAFFIC` | `1` | 4초마다 내부 루프백 업무 요청, `0`이면 중단 |

코드 결함 버튼을 켜면 자체 트래픽을 즉시 한 번 실행한다(기존 요청 처리 중이면 다음 주기). 내부 자체 트래픽은 자기 업무 HTTP를 호출하며 해당 업무가 설정된 같은 환경 의존 서비스를 호출할 수 있다. 데이터가 끝없이 쌓이지 않도록 같은 점검 데이터를 사용한다. 데모 회원·송장은 각각 최대 1000개다.

## 지표와 로그
공통 `http_server_requests_seconds_count/sum/bucket{method,uri,status}`를 노출한다. 구간 `le`는 `0.05,0.1,0.25,0.5,1,2.5,5,+Inf`다. 가변 식별자는 `{id}`, 알 수 없는 경로는 `NOT_FOUND`로 모아 시계열 폭증을 막는다. `app_info{system,service,language="node",version}`는 1, 예외는 `app_errors_total{exception}`로 센다. 추가로 실제 프로세스 메모리와 실험으로 보유한 메모리 바이트를 제공한다.

접근 로그는 표준출력에 한 줄 JSON으로 남긴다. 본문·검색 문자열·비밀번호·인증 헤더는 로그에 넣지 않는다. 예외는 `level=ERROR system=member service=member` 식별 줄 다음에 실제 노드 표준 스택 원문을 여러 줄로 출력한다. `/app/business.js`는 통합 저장소의 `services/member/business.js` 또는 분리 저장소 루트의 `business.js`와 줄 번호까지 대응한다. 일반 예외 로그 형식·정상 경로·지표·장애 켜기와 끄기를 시험하되, 세 코드 결함의 실제 HTTP·서로 다른 업무 스택과 복구를 자동 시험한다.

## 운영 경계와 초기 업로드
이 앱은 장애 대응 실험용이며 실제 인증·배송 처리·개인정보 저장 서비스가 아니다. 장애 API는 인증이 없으므로 외부 공개하지 말고 로컬 또는 격리된 실험 환경에서만 실행한다. 운영 시스템의 인증 경계를 대신하는 앱이 아니다.



## P18 장애 캠페인 계약
- `GET /api/chaos`는 기존 `faults` map을 유지하며 `active`, `release_active`, 항목별 `name/label/signal/source/expires_at`을 추가한다. 화면은 서버 metadata에서 버튼을 자동으로 만들고 배포 결함 버튼은 비활성화한다.
- 공통 18종: `code_bug`, `code_bug_validation`, `code_bug_serialization`, `intermittent_exception`, `error_rate`, `latency`, `latency_spike`, `hang`, `crash`, `startup_config_error`, `memory_leak`, `not_ready`, `liveness_fail`, `cpu_spin`, `worker_exhaustion`, `background_job_failure`, `business_invariant`, `dependency_failure`. delivery는 기존 `event_loop_block`도 유지한다.
- runtime TTL 기본 900초·최대 3600초. `FAULT_STATE_DIR/faults.json`에 원자 저장한다. 서버 정상 종료는 파일을 지우지 않는다. emptyDir 재사용 컨테이너 재시작은 유지, 파드 교체로 emptyDir 제거 시 해제된다. `DELETE`는 runtime만 지운다.
- 배포 결함은 최신 합의인 이미지 ENV `RELEASE_FAULTS={"faults":{"code_bug":{"params":{}}}}`를 시작할 때 읽는다. `RELEASE_FAULTS_FILE`은 사용하지 않는다. 재시작이나 API clear로 해제되지 않으며 정상 이미지 rollback으로 제거한다. Docker build의 `--build-arg RELEASE_FAULTS='{"faults":{"code_bug":{"params":{}}}}'`로 고정 가능하다.
- `latency.delay_ms`, `error_rate.rate`, delivery의 `event_loop_block.block_ms`를 유지하며 `latency.seconds`, `error_rate.ratio` 별칭도 받는다. hang 40초, latency_spike 비율 0.3·3초, 간헐 예외 비율 0.15가 기본이다.
- crash는 3초 뒤 실제 exit 1, startup_config_error는 업무 설정 검증 함수 스택과 exit 1이다. 메모리는 누적 상한 없이 실제 Buffer 할당, CPU는 종료 가능한 실제 worker_threads, 슬롯은 실제 동시 8개 제한 및 10초 대기 후 WorkerPoolExhausted 503이다. 타이머는 unref하고 서버 종료 때 정리한다.
- `SELF_TRAFFIC_INTERVAL_SECONDS` 기본 4초(배포 1초 권장), 0이면 중단. 자체 HTTP는 세 결함·목록·조회 경로를 순환하며 기존 시드를 재사용한다. 업무 코드 결함 세 곳에 정상 수정 주석이 있다.
- 외부 의존 URL이 없으면 정상 연동을 생략한다. 설정된 주소만 호출하고 redirect는 따르지 않는다. 전체 제한 2초, 연결 오류·timeout·5xx는 원인 스택이 포함된 DependencyError 502다. 명시적 dependency_failure는 루프백 닫힌 포트에 실제 접속한다.
- 주기 작업 실패는 실제 업무 함수에서 예외를 발생시켜 app_errors_total을 올리고 HTTP는 정상이다. 업무 정합성 장애는 실제 세션/배송 상태 불일치를 검증하고 복원하며 정상 응답을 유지한다.
- 인증 없는 API는 기존 실험용 경계다. 인터넷에 공개하지 않는다. 요청 오류는 기존 `{error: 메시지}`와 400/404/409, 장애는 500/502/503을 유지한다. 외부 소비자는 저장소 밖에서 열거 불가하므로 sunset 없이 기존 호환 필드를 보존한다.

분리 저장소에는 이 폴더의 소스·시험·정적 파일·문서·도커 설정만 넣는다. 자격 정보·환경 파일·로그·프로세스 번호 파일·화면 캡처·빌드 산출물은 포함하지 않는다. 화면 캡처와 실행 증거는 통합 저장소의 `docs/evidence/P18/systems/`에 따로 보관한다.
