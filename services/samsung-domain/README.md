# 삼성 원장·회원 런타임

Python 3.12 한 이미지가 `DOMAIN_MODE=business|trade|delivery`로 실행된다.
실제 PostgreSQL에 저장하며 임직원·계약과 고객 회원은 별도 테이블·API다.
모든 화면·시험 자료는 **합성 예제**이며 실제 삼성 임직원·계약 자료가 아니다.

## 실행

```sh
python3.12 -B -m venv "$HOME/.hermes/cache/scratch/samsung-domain-run"
PYTHONDONTWRITEBYTECODE=1 "$HOME/.hermes/cache/scratch/samsung-domain-run/bin/pip" install -r services/samsung-domain/requirements.lock
PYTHONDONTWRITEBYTECODE=1 "$HOME/.hermes/cache/scratch/samsung-domain-run/bin/pip" install --no-deps ./services/samsung-domain
# 운영자가 규약에 맞는 보호된 파일을 마운트한 환경에서 실행
PYTHONDONTWRITEBYTECODE=1 "$HOME/.hermes/cache/scratch/samsung-domain-run/bin/python" -B -m samsung_domain.main
```

환경변수·마운트 파일의 상세 규약과 API 입력은 `docs/operations/samsung-domain.md`를 따른다.
`READ_ONLY_MODE`는 기본 `1`이다. 초기 검증 중 외부 원장·고객 쓰기는 503으로 거절한다.
내부 snapshot·inbox·outbox ACK·읽기 모델 복제는 이 모드에서도 허용한다.

- HTTP 8080: 한국어 소개·상태, `/healthz/live`, `/healthz/ready`, `/api/info`, `/api/status`, `/metrics`.
- trade/delivery HTTP: `GET/POST /api/members`, 합성 로그인 `/api/login`, 고객 프로필 조회.
- TLS 8443: 임직원·계약·원문·export·동기화 상태. 공개 앱에는 이 라우트를 등록하지 않는다.
- 프록시 헤더를 신뢰하지 않는다. 보호된 앱도 실제 ASGI scheme이 https가 아니면 403이다.

## 실제 시험·패키징

프로젝트 루트에서 다음을 실행한다. 저장소의 다른 pytest/conftest는 불러오지 않는다.

```sh
PYTHONDONTWRITEBYTECODE=1 "$HOME/.hermes/cache/scratch/samsung-domain-run/bin/python" -B services/samsung-domain/tests/run.py
PYTHONDONTWRITEBYTECODE=1 "$HOME/.hermes/cache/scratch/samsung-domain-run/bin/python" -B services/samsung-domain/tests/package.py
```

`tests/run.py`는 캐시된 `postgres:16` 이미지로 **새 고유 컨테이너만** 만들고
256MiB 메모리·자체 tmpfs·임의 로컬 포트를 사용한다. 각 시험은 새 고유 DB를 만든다.
기존 55432, public 스키마, 운영 DB·PVC는 사용하지 않는다.
HTTP/TLS 진입점 시험은 시험 안에서만 생성·정리하는 합성 인증서와 고정된 합성 주체를 쓴다.
그 자격은 운영용으로 재사용하지 않는다. 종료 후 자체 컨테이너·DB·fixture를 정리한다.

영수증은 `docs/evidence/samsung/domain/<고유실행>/` 아래에 종료코드·로그·정리 확인·소스 SHA256을 기록한다.
`tests/package.py`는 linux/arm64 build 후 이미지 내부 import·SQL/HTML 포함·pip check까지 검사한다.
RepoDigest를 실제 읽어 기록하며 image config ID를 manifest digest로 대신 표기하지 않는다.
push, deploy, 기존 서비스 변경은 수행하지 않는다.

## 검증 경계

실제 PostgreSQL TX·rollback·동시 expected_version·대상별 ACK·불변 snapshot·세대 전환·inbox·격리·실제 HTTP/TLS 소켓을 시험한다.
1,000개 합성 원장 변경과 두 독립 DB의 snapshot 1,000행을 검사한다.
발행 ACK 실패·Kafka commit 유실·복구 루프의 transport는 명시적 fixture다.
실제 SASL_SSL Kafka의 topic/group ACL·두 소비 그룹·재시작·보존 초과·10분 자원 측정과 dev/stg/prd 배포는 부모의 통합 인수 범위다.
로컬 시험 통과를 실제 브로커 ACL 또는 실클러스터 부하 통과로 해석하지 않는다.
