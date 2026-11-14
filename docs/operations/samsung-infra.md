# 삼성 실험 기반 생성기

## 범위와 현재 상태

`scripts/samsung_infra.py`는 단일 arm64 Rancher Desktop 노드용 신규 Kafka·Postgres 기반을 선언하고, 명시적인 `--execute`에서만 신규 자격 생성과 Kubernetes `create`를 수행한다. 이번 구현 시험은 실클러스터를 변경하지 않았다. 기존 LiveProbe Postgres, 원본 inventory DB·Secret 및 사용자 데이터를 조회·회전·삭제하지 않는다. 애플리케이션 이미지 배포·원본 데이터 이관·전환은 부모 실행자의 별도 작업이다.

출력 JSON은 `manifest`(Kubernetes List), `topics`, `acls`, `secret_refs`, `inputs`를 가진 비밀 없는 묶음이다. 이 JSON 전체를 `kubectl -f` 입력으로 주지 않는다. CLI의 `--execute`가 `manifest`를 해석하고 신규 Secret을 별도 보호 경로에서 생성하여 순서대로 전송한다.

## 실행 방법

저장소에서 실행한다. 모든 Python 호출은 `-B`와 `PYTHONDONTWRITEBYTECODE=1`을 유지한다. 출력 경로의 상위 디렉터리는 미리 준비하고, 출력 파일과 비공개 상태 디렉터리는 **존재하지 않는 새 경로**를 사용한다.

```sh
cd /Users/hwany/sds-ai-professional
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/samsung_infra.py \
  --render --context rancher-desktop \
  --output /Users/hwany/.hermes/cache/scratch/samsung-infra-review.json
```

`--render`는 Docker·kubectl·OpenSSL을 호출하지 않으며 자격·CA·인증서를 만들지 않는다. `--private-dir`가 같이 있어도 해당 경로는 읽거나 만들지 않는다. 공개 선언 파일만 생성한다.

다음 명령은 **부모 실행자가 배포 검토 후 수행할 실제 변경 명령**이며 이번 작업에서는 실행하지 않았다.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/samsung_infra.py \
  --execute --context rancher-desktop \
  --output /Users/hwany/.hermes/cache/scratch/samsung-infra-execute.json \
  --private-dir /Users/hwany/.hermes/cache/scratch/samsung-infra-private-v1
```

필수 실행 도구는 Python 표준 라이브러리, OpenSSL, Docker CLI, kubectl이다. Docker 엔진과 Kubernetes 노드는 같은 Rancher Desktop 로컬 이미지 저장소를 사용해야 한다. `local-path` StorageClass, CNI의 NetworkPolicy 집행, 기존 `dev/stg/prd` namespace가 필요하다. 이미지의 실제 로컬 RepoDigest를 선언과 대조한다. 선언은 `deploy/samsung/infra-template.json`의 검증된 RepoDigest를 사용한다. 이미지 config ID를 manifest digest로 대신하지 않는다.

### 명령·변경 순서

1. 비공개 디렉터리가 이미 존재하면 **내용을 읽기 전에** 거절한다.
2. 로컬 이미지 RepoDigest를 검사한다. namespace·생성 대상 Secret/NetworkPolicy의 **이름만** 조회한다(`-o name`). 기존 `liveprobe-messaging` 또는 `liveprobe-data` namespace가 있으면 기존 기반을 채택하지 않고 거절한다. 기존 앱 namespace의 생성 대상 이름이 충돌해도 거절한다.
3. 새 비공개 상태를 0700으로 생성하고 자격·키·인증서·상태 파일을 0600으로 저장한다. OpenSSL 자식 프로세스에도 0077 umask를 적용한다. CA·서버 SAN을 검증하고 PKCS12 keystore를 생성한다. 비밀번호는 argv가 아니라 보호 파일로 전달한다.
4. 신규 기반 namespace → 신규 immutable Secret → NetworkPolicy → Service/ConfigMap → Postgres 3개와 Kafka 순서로 생성한다. 각 StatefulSet은 `rollout status --timeout=600s`를 직렬로 기다린다. `apply`, `delete`, `patch`, `kubectl exec`는 사용하지 않는다.
5. 각 생성 직후 정확한 대상 이름을 다시 조회한다. 완료 영수증은 비공개 `completed.json`이며 이름·종류·입력 SHA·상태만 기록한다. stdout은 상태·개수·공개 출력 경로만 출력한다.

Kubernetes payload는 `create -f -`의 stdin으로 전달한다. Secret 값이 든 argv·stdout·stderr는 내보내지 않는다. 명령 실패는 도구 원문 대신 종료코드만 보고한다. Kafka 공식 시작 스크립트와 관리자 초기화의 출력은 값 노출 방지를 위해 폐기한다. 따라서 일반 `kubectl logs`에 Kafka 원본 진단이 없다는 운영상 제약이 있다.

### 반복·부분 실패

반복 실행은 자격을 회전하거나 기존 Secret을 읽지 않고 거절한다. 새 경로를 골라도 이미 생성한 namespace/Secret 이름과 충돌하므로 기존 기반을 덮지 않는다. 부분 생성 후에는 자동 삭제·자동 재시도·PVC 초기화를 하지 않는다. 비공개 상태·`attempt.json`과 생성된 리소스를 보존하고 부모 실행자가 이름·상태만 조사한다. 자동 재개 기능은 없다. 비공개 파일을 삭제해서 새 자격으로 덮어쓰는 복구를 하지 않는다.

비공개 경로는 저장소 밖의 보호된 디렉터리를 권장한다. `secrets.json`에는 새로 만든 Secret 값 전체가 있으므로 `cat`, 에이전트 파일 읽기, 로그 첨부, Git 추가를 하지 않는다. CA private key도 그 안에 남으므로 접근 제한과 별도 보관이 필요하다. 365일 인증서의 갱신·회전은 별도 승인된 운영 절차이며 이 CLI는 기존 자격을 갱신하지 않는다.

## Kafka 선언

- namespace `liveprobe-messaging`; Service `kafka`, `kafka-headless`; StatefulSet `kafka`, replica 1; 4Gi `local-path` PVC.
- `apache/kafka:4.4.0`의 실제 RepoDigest 고정. KRaft broker/controller 결합 노드다. ZooKeeper·operator·Connect·Schema Registry·관리 UI는 없다.
- broker 9092는 `SASL_SSL`/`PLAIN`, controller 9093도 `SASL_SSL`/`PLAIN`이며 **127.0.0.1에만 bind**한다. 클라이언트 CA·호스트 이름 검증을 사용한다. 인증서에 broker 서비스 FQDN과 자기 quorum의 127.0.0.1 SAN이 있다.
- `StandardAuthorizer`, `allow.everyone.if.no.acl.found=false`, 유일한 super user는 `bootstrap-admin`이다. 관리자 설정·JAAS·PKCS12는 브로커에만 mount한다. 앱에는 환경별 비관리 주체만 준다.
- 자동 토픽 생성은 꺼져 있다. 환경별 두 토픽 `<env>.business.trade.master.v1`, `<env>.business.delivery.master.v1`: 파티션 1, RF1, `compact,delete`, 64MiB/72시간, 최대 메시지 64KiB, segment 16MiB. 내부 토픽 RF1·소수 파티션, cleaner 1개와 작은 buffer를 명시한다.
- 프로듀서 `business-<env>`는 자기 두 토픽 Write/Describe만 가진다. 컨슈머 `trade-<env>`, `delivery-<env>`는 자기 토픽 Read/Describe와 정확한 그룹 `<env>.samsung-<target>.master.v1` Read만 가진다. wildcard·prefix·다른 환경·클러스터 관리자 ACL은 없다. Kafka 4.4의 idempotent producer는 자기 topic Write ACL로 동작함을 실제 시험했다.
- heap 128/256MiB, 요청 100m/256Mi, 제한 500m/512Mi. 768Mi 조정이 필요하면 비밀 없는 템플릿을 명시적으로 바꾸고 다시 검증한다. 512Mi 합성 기동·소량 발행 시험은 장시간 부하 안정성의 증명이 아니다.
- hostPort·외부 ingress·외부 Service 유형은 없다. ingress는 dev/stg/prd의 정확한 `business-api`, `trade-member`, `delivery-member` app selector와 브로커 자기 자신만 9092에 허용한다. DNS와 자기 브로커 egress만 허용하며 controller 외부 포트는 없다.

## Postgres 선언과 경계

namespace `liveprobe-data`의 StatefulSet/Service는 `trade-db`, `delivery-db`, `business-db`다. 각각 2Gi `local-path` PVC, UID/GID/fsGroup 70, `PGDATA` 하위 디렉터리를 사용한다. `postgres:17-alpine`의 실제 RepoDigest를 고정한다. `shared_buffers=16MB`, `max_connections=40`, 요청 128Mi·제한 256Mi이며 상향 허용값은 384Mi다.

각 물리 서버에 `trade_dev/stg/prd`, `delivery_dev/stg/prd`, `business_dev/stg/prd`의 별도 role·DB를 만든다. trade 서버에는 `inventory_dev/stg/prd`도 별도 role·DB로 만든다. PUBLIC의 DB CONNECT와 public schema 권한을 회수하고 해당 role에 자기 DB만 허용한다. role은 `NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS`, 연결 상한은 domain 6개·inventory 4개다. TCP 인증은 SCRAM이다. 이 버전은 DB 전송 TLS를 선언하지 않는다. Kafka와 보호 API TLS 요구와 별개인 제한이다.

관리자는 신규 `samsung-<system>-bootstrap/admin-password`를 `POSTGRES_PASSWORD_FILE`로 읽는다. 초기 SQL은 신규 Secret의 `bootstrap.sql`이며 최초 빈 PGDATA에서만 실행된다. 기존 role/password의 ALTER·회전·DROP이나 데이터 이관은 하지 않는다.

초기화 완료는 TCP 수락만으로 판정하지 않는다. 같은 신규 Secret의 `bootstrap.contract`는 초기화 계약 버전과 전체 SQL의 SHA256을 담는다. `10-init.sh`는 `ON_ERROR_STOP=1` SQL 전체가 성공한 뒤에만 임시 파일을 원자적으로 이동해 PGDATA의 `.samsung-bootstrap-complete`를 기록한다. startup/readiness는 이 영속 표식과 현재 계약이 일치하고 127.0.0.1 TCP까지 준비되어야 성공한다. 초기 SQL 도중 실패한 PGDATA가 재기동해 TCP를 열어도 표식이 없으므로 실패 폐쇄한다. 완료 표식이 없거나 계약이 다르면 데이터를 지우거나 초기화를 재실행하지 않고 운영자 검증 대상으로 남긴다. 정상 완료한 같은 PVC 재기동은 표식을 보존해 통과한다. 생존 probe는 서버 TCP만 검사하며, 초기 준비 probe가 먼저 완료되어야 생존 검사를 시작한다.

DB ingress는 각 환경의 해당 domain 앱에만 5432를 허용한다. trade DB는 같은 환경별 `inventory`도 허용한다. 앱 egress의 DNS·자기 DB·Kafka·해당 환경 business HTTPS 허용 규칙을 별도 선언한다. inventory egress 정책은 `app=inventory` **및** `liveprobe.io/database=samsung-trade`가 모두 있는 Pod에만 적용한다. 부모는 데이터 선보존·이관·검증 후 새 DB로 전환한 inventory Pod에만 이 label을 붙인다. 기반 초기화만으로 기존 inventory의 이전 DB 연결을 차단하지 않기 위한 전환 경계다. **Kubernetes NetworkPolicy는 가산형**이므로 renderer의 기존 broad allow 제거와 CNI 실접속 허용/거절 검증이 필요하다. 이 생성기만으로 다른 정책의 광범위 허용을 취소하지 않는다.

## 신규 앱 Secret 참조

각 `dev/stg/prd`에서 아래 `<service>`는 `business-api`, `trade-member`, `delivery-member`다.

| Secret | 키 | 앱 경로 |
| --- | --- | --- |
| `<service>-db` | `dsn` | `/var/run/samsung/db/dsn` |
| `<service>-kafka` | `client.json`, `ca.crt` | `/var/run/samsung/kafka/` |
| `<service>-api` | `credentials.json`; 소비자만 `reader-token` | `/var/run/samsung/api/` |
| `<service>-tls` | `ca.crt`, `tls.crt`, `tls.key` | `/var/run/samsung/tls/` |
| `samsung-inventory-db` | `password` | 기존 inventory의 신규 참조 배선에 사용 |

`client.json`은 런타임 규약의 Confluent 설정 객체이며 암호는 JSON 파일 안에만 있다. API JSON은 `principals` 배열의 token/role/environment/target_system 계약을 따른다. business에는 자기 환경 admin과 두 reader를 주고 각 directory에는 자기 reader 한 명만 준다. reader-token 값은 business에 등록한 동일 reader와 일치한다. business HTTPS는 `https://business-api.<env>.svc:8443`이며 서버 인증서에 해당 SAN을 포함한다. 기존 `inventory-db` Secret은 대상이 아니다.

## 검증

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover \
  -s scripts -p test_samsung_infra.py -v

PYTHONDONTWRITEBYTECODE=1 SAMSUNG_INFRA_DOCKER_TEST=1 \
  python3 -B -m unittest discover -s scripts -p test_samsung_infra.py -v

# 새 폐기 볼륨에서 실제 SQL 중간 실패와 정상 완료의 재시작 경계를 각각 검사한다.
PYTHONDONTWRITEBYTECODE=1 SAMSUNG_INFRA_DOCKER_TEST=1 \
  python3 -B -m unittest discover -s scripts -p test_samsung_postgres_restart.py -v
```

첫 명령은 독립 unittest만 실행한다. 기존 conftest·환경 파일·운영 DB를 읽지 않는다. 두 번째 명령은 UUID 이름·소유 label의 **자체 폐기용** 컨테이너만 사용한다. Kafka 512Mi·Postgres 256Mi를 넘지 않고 순서대로 실행한다. 외부 포트·공유 네트워크·부모 DB는 사용하지 않는다. 파일은 private tar stdin으로 넣고 값은 출력하지 않는다. 컨테이너와 자체 익명 볼륨이 정리됐는지 다시 확인한다.

Kafka console consumer/producer는 권한 실패에도 종료코드 0을 반환할 수 있어 거절 시험은 예외 종류와 메시지 미수신을 함께 검사한다. 자체 Docker 시험에서는 진단 로그를 컨테이너 내부 파일에만 보관하고 오류 클래스만 공개한다. 정상 배포 선언은 원본 Kafka 로그를 폐기한다.

### 부모 통합 인수에서 남는 것

- 실제 Kubernetes server dry-run·create·Secret mount/fsGroup·StorageClass·CNI·Service DNS·rollout·PVC 재생성/재연결 시험.
- 환경별 전체 앱 통합, 운영용 데이터 선보존·이관·검증, 원본 writer 동결·전환·원복, reader-token API 권한 시험.
- 새 Kafka/PG와 기존 앱이 함께 동작하는 노드의 10분 연속 측정, 합성 변경 1000건·두 대상 수렴·격리/복구·재시작 인수. node memory 80% 초과 60초 지속·OOM·준비 실패·권한 혼입이면 다음 환경 승격을 중단한다.
- RF1·단일 local-path 노드·공유 CA/브로커는 고가용성 운영 구성이 아니다. 이름이 `prd`여도 고가용성을 뜻하지 않는다.
