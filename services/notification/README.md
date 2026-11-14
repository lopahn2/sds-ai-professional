# 알림 발송 시스템

P18 장애 대응 실험용 독립 서비스다. 파이썬 표준 라이브러리만 사용하며 외부 패키지·자체 데이터베이스가 없다. 의존 주소가 설정되면 같은 환경의 회원·재고 서비스에 실제 HTTP로 연동한다. 실제 외부 결제나 발송은 하지 않는다.

## 실행과 시험

전체 작업 저장소에서:
```sh
python -m unittest discover -s services/notification/tests
docker build --pull=false --network=none -t liveprobe/notification:p18-dev services/notification
docker run --rm -d --name p18-notification -p 127.0.0.1:18083:8080 --memory 128m -e APP_ENV=dev liveprobe/notification:p18-dev
curl -sS http://127.0.0.1:18083/healthz/ready
curl -sS -H 'Content-Type: application/json' -d '{"recipient":"운영팀","channel":"email","message":"운영 공지"}' http://127.0.0.1:18083/api/notifications
docker stop p18-notification
```

이 디렉터리를 별도 시스템 저장소의 루트로 업로드한 경우:
```sh
python -m unittest discover -s tests
docker build --pull=false --network=none -t liveprobe/notification:p18-dev .
SELF_TRAFFIC=0 python app.py
```

기본 포트는 8080이고 컨테이너 사용자는 `10001:10001`이다. 기반 이미지는 시작 시 로컬에 있던 `python:3.12-slim-bookworm`의 다이제스트 `sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e`로 고정했다. 태그만 사용하면 빌드 도구가 `--pull=false`여도 레지스트리의 최신 메타데이터를 조회할 수 있어 고정했다. 이 기반 이미지가 없는 다른 호스트는 사전에 승인된 방식으로 같은 로컬 이미지를 반입해야 한다. 빌드 중 설치·다운로드 단계는 없다.

## 화면과 주소

`/`에서 업무·환경 배지·버전·장애 실험을 확인한다. `/chaos`도 같은 화면이다. 모든 화면 자산은 `static/`에 있으며 외부 글꼴·외부 자산 주소가 없다. 375px에서 1열, 760px 초과에서 업무 입력과 이력을 2열로 배치한다.

| 방식 | 주소 | 용도 |
|---|---|---|
| GET | `/api/notifications` | 대기·발송 이력·대기열 길이 |
| POST | `/api/notifications` | 수신자·채널·내용을 대기열에 등록; 202 |
| GET | `/api/config` | 환경·시스템·서비스·버전·자체 트래픽 설정 |
| GET | `/healthz/live` | 프로세스 생존; 업무 장애와 분리 |
| GET | `/healthz/ready` | 준비 상태; not_ready일 때 503 |
| GET | `/metrics` | 프로메테우스 텍스트 형식 |
| GET | `/api/chaos` | 활성 장애 목록과 표시명·현재 파라미터 |
| POST | `/api/chaos` | 장애 활성화·해제·파라미터 변경 |
| DELETE | `/api/chaos` | 런타임 장애·영속 파일 해제; 배포 결함과 손상된 업무 데이터는 유지 |

## 장애 버튼과 기대 신호

| 이름 | 화면 버튼명 | 기대 신호 |
|---|---|---|
| `code_bug` | 업무 코드 결함 켜기/끄기 | 아래 재현 요청에서 500, app_errors_total 증가, 표준 traceback |
| `latency` | 응답 지연 켜기/끄기 | 업무 응답 지연·히스토그램 변화; 기본 2초 |
| `error_rate` | 오류율 증가 켜기/끄기 | 업무 503; 기본 비율 0.5 |
| `crash` | 프로세스 종료 켜기/끄기 | 활성화 약 3초 후 종료 코드 1; 영속 상태가 있으면 재시작 후 반복 |
| `memory_leak` | 메모리 누수 켜기/끄기 | 기본 초당 8MiB 증가; 상한 없이 누적, 128MiB 제한에서 메모리 부족 종료 |
| `not_ready` | 준비 상태 실패 켜기/끄기 | ready 503, 쿠버네티스에서 준비된 엔드포인트 0; live 200 유지 |
| `queue_stuck` | 대기열 정체 켜기/끄기 | 발송 중단, notification_queue_depth 증가, WARN 로그 |

종료·메모리 누수는 화면 확인을 거친다. 쿠버네티스의 엔드포인트 0·재시작은 배포 후 관측할 신호이며 이 서비스가 쿠버네티스 객체를 수정하지는 않는다. `crash`는 활성화 약 3초 뒤 종료되며 그 전에 런타임 장애를 해제하면 취소할 수 있다. 메모리 주입을 끄면 할당 목록을 비우지만 운영체제에 메모리가 반환되는 시점은 파이썬 할당기에 달려 있다.

```sh
curl -sS -H 'Content-Type: application/json' -d '{"fault":"code_bug","on":true}' http://127.0.0.1:18083/api/chaos
curl -sS -H 'Content-Type: application/json' -d '{"recipient":"운영팀","channel":"email","message":"운영 공지"}' http://127.0.0.1:18083/api/notifications
curl -sS -H 'Content-Type: application/json' -d '{"fault":"code_bug","on":false}' http://127.0.0.1:18083/api/chaos
curl -sS -H 'Content-Type: application/json' -d '{"fault":"latency","params":{"seconds":2}}' http://127.0.0.1:18083/api/chaos
curl -sS -H 'Content-Type: application/json' -d '{"fault":"error_rate","on":true,"params":{"ratio":0.5}}' http://127.0.0.1:18083/api/chaos
curl -sS -H 'Content-Type: application/json' -d '{"fault":"memory_leak","on":true,"params":{"mib_per_second":8}}' http://127.0.0.1:18083/api/chaos
curl -sS -X DELETE http://127.0.0.1:18083/api/chaos
```

`on` 미지정 시 활성화하며 `off:true`도 해제를 뜻한다. 지연은 0~30초, 오류 비율은 0~1, 메모리 증가는 초당 1~64MiB다. 잘못된 파라미터는 400이며 업무 JSON 본문은 64KiB 이하만 받는다.

## 실제 코드 결함

app.py의 normalize_channel에서 email 채널의 문자열에 존재하지 않는 lowercase()를 호출해 AttributeError가 난다.

재현 요청: `POST /api/notifications`. 버튼 또는 `FAULT_CODE_BUG=1`로 활성화한 뒤 위 업무 요청을 보내면 된다. 자체 트래픽도 같은 조건을 사용해 화면 없이 4초 안팎마다 5xx를 만든다. 결함을 해제하면 정상 경로로 복구된다.

고치는 방법: 결함 분기의 `return channel.lowercase()`를 `return channel.lower()`로 고친다.

수정 시 `code_bug` 토글을 없애는 대신 해당 분기를 정상 계산·접근·메서드로 바꾼다. 캠페인 시험은 코드 결함 3종의 실제 업무 HTTP 500·표준 스택·해제 후 정상 응답을 직접 검증한다. 코드 수정 실습에서 결함 자체를 고칠 때는 해당 캠페인 기대값도 정상 응답 기준으로 전환해야 한다.

## 지표와 로그

- 공통 지표: `http_server_requests_seconds_count`, `_sum`, `_bucket`에 method·uri·status 레이블을 쓴다. 버킷은 0.05·0.1·0.25·0.5·1·2.5·5·+Inf다. 경로 변수는 `{id}`, 미등록 경로는 `/unknown`으로 축약한다.
- 신원 지표: `app_info{system,service,language="python",version} 1`.
- 예외 지표: `app_errors_total{exception}`. 예외가 없으면 `exception="none"` 값 0이다.
- 업무 지표: notification_queue_depth(대기열 길이), notification_sent_total(누적 발송 수).
- 접근 로그는 표준출력 한 줄 JSON이다. 업무 본문·헤더·자격정보를 기록하지 않는다.
- 예외는 `level=ERROR system=notification service=notification` 접두 줄 다음에 파이썬의 표준 다중 줄 traceback을 표준출력으로 출력한다. 컨테이너 `/app/app.py`, `/app/runtime.py`는 저장소 루트의 동명 파일과 1:1 대응한다.

## 환경과 메모리

| 변수 | 기본값 | 설명 |
|---|---|---|
| `SYSTEM_NAME` | `notification` | 지표·로그의 시스템명 |
| `SERVICE_NAME` | `notification` | 지표·로그의 서비스명 |
| `APP_ENV` | `dev` | dev·stg·prd만 허용, 화면에는 개발·검증·운영 환경 표시 |
| `APP_VERSION` | `p18-dev` | 하단 버전과 app_info |
| `SELF_TRAFFIC` | `1` | 4초마다 내부 루프백으로 업무 요청; 0이면 중단 |
| `FAULT_CODE_BUG` | 미지정 | 1이면 실제 코드 결함 활성화 |

다른 장애도 `FAULT_<대문자 이름>=1`로 시작할 수 있다. 업무 데이터는 메모리이며 프로세스 재시작 시 초기화한다. 런타임 장애는 `FAULT_STATE_DIR/faults.json`에 저장해 만료 전까지 복원하며, 배포 결함은 이미지의 `RELEASE_FAULTS` 환경변수에 고정한다. 정상 이력은 최근 100건만 보관한다. 알림 대기열은 최대 1만 건으로 제한하며, 의도적 메모리 누수만 상한 없이 증가한다.

## 캠페인 확장 계약

- `POST /api/chaos`: `ttl_seconds`는 정수 1~3600, 기본 900초. 장애는 동시에 켤 수 있고 만료 시 작업자·메모리 참조를 해제한다.
- 공통 확장: `code_bug_validation`, `code_bug_serialization`, `intermittent_exception`, `latency_spike`, `hang`, `startup_config_error`, `liveness_fail`, `cpu_spin`, `worker_exhaustion`, `background_job_failure`, `business_invariant`, `dependency_failure`.
- `RELEASE_FAULTS='{"faults":{"code_bug":{"params":{}}}}'`를 **이미지 ENV**로 고정하면 재시작·API DELETE로 해소되지 않는다. 정상 이미지로 원복해야 한다. 실행 중 파일 감시나 `RELEASE_FAULTS_FILE`은 사용하지 않는다.
- 제어 API의 `source`, `release_active`로 배포 결함을 구분하며 화면의 해당 버튼은 비활성화된다. 같은 이름의 runtime·release가 겹치면 release 매개변수가 우선한다.
- `SELF_TRAFFIC_INTERVAL_SECONDS` 기본 4초(배포 시 1초 권장). 자체 트래픽은 코드 결함 3종을 포함한 업무 경로를 순환한다. 한글 조회는 URL 인코딩한다.
- `worker_exhaustion`은 실제 업무 슬롯 8개를 점유하며 기본 10초 후 `WorkerPoolExhausted` 스택과 503을 반환한다. 제어·상태·지표 API는 영향을 받지 않는다.
- 의존 호출은 2초 제한이며 연결 거부·시간 초과·5xx는 원인 스택을 포함한 `DependencyError`와 502가 된다. 주소 미설정 시 정상 연동은 생략한다.
- `business_invariant`는 실제 업무 상태를 손상시킨다. 장애 해제만으로 기존 손상이 복구되지 않으므로 아래 reconcile이나 프로세스 초기화가 필요하다.

추가 업무: `POST /api/notifications/{id}/retry`, `POST /api/notifications/reconcile`. 재발송은 validation, 목록은 serialization 결함 경로다. `MEMBER_URL`로 수신자를 조회한다. 발송 배치는 `DispatchJobError`, 대기열 검사는 `QueueStalledError`를 기록한다. 기존 `queue_stuck`은 해제 후 배출되지만 `business_invariant`가 손상시킨 대기 항목은 reconcile 후 배출된다.

## 별도 저장소 업로드 경계

업로드할 소스는 `app.py`, `runtime.py`, `static/`, `tests/`, `Dockerfile`, `.dockerignore`, `.gitignore`, `README.md`, `DESIGN.md`뿐이다. 비공개 시스템 저장소를 기준 원본으로 사용한다. 개발 자격정보·환경 파일·실행 로그·프로세스 번호·화면 캡처·바이트코드 캐시는 포함하지 않는다. 실행 증거는 플랫폼 저장소의 `docs/evidence/P18/systems/`에만 둔다.

장애 제어에는 인증이 없다. 외부 공개하지 말고 격리된 실험 환경·접근 제한 네트워크 안에서만 실행한다. 이 코드는 실제 결제·운영 알림에 사용할 수 없다.
