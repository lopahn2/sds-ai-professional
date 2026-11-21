# LiveProbe Application 실행 구성 — 제한된 별도 배포

이 디렉터리는 **선언과 오프라인 시험**이다. 클러스터 적용·이미지 빌드·모델/실행기 연결 성공을 뜻하지 않는다.
`rendered/*.yaml`의 현재 `000…000` digest는 **파싱 시험용 표식**이다. 실제 manifest digest로 다시 렌더하기 전에는 적용하지 않는다.

## 통합 모드와 현재 조사

2026-11-20 21:48 KST에 로컬 관제 4175와 기존 Application 주소를 통합 API로 전환했다. 11월 21일에는 역할별 실행기를 연결했고 `/api/application/v1/readyz`에서 기계 인증, 실행기, 모델 작업자의 준비 상태를 확인했다. Git 병합 조회는 이 점검에 포함되지 않는다. 아래 이전 경로 중계 방식은 전환 전 설치에서 사용한다. [전환 결과와 데이터 보존 검증](../../docs/plans/unified-operator-console-2026-11-20.md#로컬-적용-상태)

2026-11-16 읽기 전용 확인에서는 Application API Deployment·Service·Ingress와 DB StatefulSet이 존재했습니다. 아래 11월 9일 미설치 기록은 과거 상태입니다. 존재 확인은 readiness·인증·runner·업무 복구 검증을 뜻하지 않습니다.

### 루프백 운영자 관제

4175의 관제 페이지를 연 사람이 로컬 운영자다. 개인별 자격 등록은 필요하지 않다. 게이트웨이는 127.0.0.1에만 바인드하며 정확한 Host·동일 Origin·브라우저의 CSRF 응답을 검증한다. 통합 API는 경로·메서드·시각·호스트·브라우저 nonce에 묶인 서명을 확인한다. 시험과 prd 승인은 `local-operator`의 별도 결정으로 남는다. 기계 실행기와 Git의 자격은 따로 구성한다.

호스트의 0700 전용 디렉터리에 기존 조회 키와 별도의 32바이트 이상 운영자 키를 0600 파일로 준비한다. 기존 조회 키를 쓰는 설치는 그 파일을 계속 사용한다. 신규 설치 예시는 아래와 같다. 키 파일 또는 Kubernetes Secret이 이미 있으면 기존 파일·Secret을 보존한다.

```sh
umask 077
LP_GATEWAY_DIR="$HOME/.config/liveprobe/gateway"
mkdir -p "$LP_GATEWAY_DIR"
( set -C; openssl rand -hex 32 > "$LP_GATEWAY_DIR/read" )
( set -C; openssl rand -hex 32 > "$LP_GATEWAY_DIR/operator" )
kubectl -n liveprobe-system create secret generic liveprobe-operator-gateway \
  --from-file=read="$LP_GATEWAY_DIR/read" --from-file=operator="$LP_GATEWAY_DIR/operator"
```

통합 API에만 두 파일을 볼륨으로 연결한다. 아래 패치는 기본 Deployment의 `api` 컨테이너가 UID 0으로 동작하는 설치용이다. 두 파일은 0400 일반 파일로 마운트되며 API는 실제 파일 소유자·권한을 확인한다. 비root 설치는 API의 실행 UID가 소유하는 0400 일반 파일로 제공한다. 운영 중인 클러스터에서는 작업을 비운 교체 시점에 적용한다. 모델 작업자와 외부 실행기에는 운영자 키를 마운트하지 않는다.

```sh
kubectl -n liveprobe-system patch deployment liveprobe-api --type=strategic \
  --patch-file deploy/application/local-operator-gateway.patch.yaml
python3 scripts/liveprobe-local-gateway.py --port 4175 --upstream http://127.0.0.1:80 \
  --upstream-host liveprobe.localhost \
  --key-file "$LP_GATEWAY_DIR/read" --operator-key-file "$LP_GATEWAY_DIR/operator"
```

`http://127.0.0.1:4175/`에서 접속한다. 기존 조회 키를 쓰는 경우 위 Secret의 `read`와 호스트의 `--key-file`에 같은 기존 파일을 지정한다. 키를 교체한 뒤에는 API Pod와 게이트웨이를 갱신하고 페이지를 다시 연다. 조회 키만 있는 게이트웨이에서는 운영자 변경이 차단된다.

이 로컬 구성에서 `liveprobe.localhost`의 기존 브라우저 북마크도 사용하려면, 검증한 대시보드 이미지의 로컬 태그를 `DASHBOARD_IMAGE`에 지정하고 아래 프로필로 빌드한다. 브라우저 문서의 경로와 쿼리를 4175로 전달한다. API는 기존 ingress가 처리하며 정적 자산은 대시보드가 제공한다.

```sh
docker build --pull=false --network none --build-arg DASHBOARD_IMAGE="$DASHBOARD_IMAGE" \
  -f dashboard/Dockerfile.local-operator -t liveprobe/dashboard:local-4175 dashboard
sh scripts/verify-local-dashboard-image.sh liveprobe/dashboard:local-4175
```

새 통합 모드는 루트 사건/관제실을 K8s 쪽에 두고 이 DB와 실행 계약을 코드 조치 저장소로 재사용합니다. 이 디렉터리의 선언을 자동 변경하거나 적용하지 않았습니다. [설치 경계와 필요한 설정](../../README.md#90-하나의-사건으로-연결하는-통합-모드), [설계와 단계 계획](../../docs/plans/liveprobe-unified-incident-plan-2026-11-16.md)을 따릅니다. 특히 legacy writer/runner drain, 기계 신원 source/cluster/qa 분리, 별도 읽기 전용 GitHub 병합 조회, 파일 영수증 전달이 운영 전환 전에 필요합니다. 기존 PVC·DB·Secret은 유지합니다.

### 로컬 통합 화면으로 이전하는 동안의 조회

기존 두 DB에 통합 연결 행이 아직 없는 설치는 대시보드와 기존 Application 기록을 한 주소에서 먼저 열 수 있다. 기존 로컬 조회 키 파일 경로를 `LP_READ_KEY_FILE`로 지정하고 다음 명령을 실행한다.

```sh
python3 scripts/liveprobe-local-gateway.py --port 4175 --upstream http://127.0.0.1:80 \
  --upstream-host liveprobe.localhost --legacy-application-host application.liveprobe.localhost \
  --key-file "$LP_READ_KEY_FILE"
```

이전 사건 번호로 조회한 요약은 기존 Application API의 공개 투영이다. 원문은 개인 읽기 권한으로 열고 이전 dev 시험 대기 건은 기존 시험자 권한으로 확인한다. 신규 접수와 prd 승격에는 통합 실행 설정이 필요하다. 통합 writer를 활성화할 때에는 같은 명령에서 `--legacy-application-host` 옵션을 제거하고 core API에 등록한 로컬 조회 키를 지정한다. 게이트웨이는 core의 통합 상태를 확인할 수 없거나 writer가 활성화되어 있으면 이전 경로 중계를 중지한다.

구버전 API는 모르는 `work` 필터를 무시하고 전체 사건을 반환할 수 있다. 이전 경로를 중계할 때 게이트웨이는 한 건 조회로 필터별 합계를 지원하는지 확인한다. 지원이 확인되지 않으면 해당 필터 요청에 503을 반환하고, QA 보완·복구 관측 화면은 ‘일부 미관측’으로 표시한다. 기존 시험·승인 대기는 원본 상태로 계속 조회한다.

### 통합 API와 모델 작업자의 계정 분리

기존 Application 모델의 로그인 PVC를 계속 사용하는 설치에서는 모델 작업자를 별도 Pod 하나에 둔다. 통합 API에는 `APPLICATION_EXTERNAL_MODEL_WORKER=true`와 `APPLICATION_WORKER_ENABLED=false`를 설정한다. 과거 Application 주소를 통합 로컬 화면으로 연결할 때는 `LIVEPROBE_LEGACY_APPLICATION_HOST`에 해당하는 로컬 호스트 이름을 지정한다.

모델 작업자의 명령은 `python -m app.application.model_worker`이다. 기존 Application DB·프로필·모델 설정과 전용 Codex 홈, `APPLICATION_DATABASE_ISOLATED=true`, `APPLICATION_WORKER_ENABLED=true`를 사용한다. 원래 로그인 파일 소유자인 UID 65532로 실행하고 서비스 계정 토큰 자동 마운트를 끈다. 모델 작업자에는 기계·사람 인증 파일과 Git 자격이 필요하지 않다. 이 Pod는 HTTP를 열지 않고 기존 분석 큐에 등록된 일만 가져간다.

전환할 때 기존 API의 진행 중인 분석과 실행 임대가 끝났는지 확인한 후 기존 모델 소비자를 정지한다. 새 소비자의 `python -m app.application.model_worker --ready`와 통합 API의 `/api/application/v1/readyz`에서 모델과 외부 실행기의 관측을 각각 확인한다. 원복도 기존 소비자를 다시 시작하기 전에 새 소비자의 작업을 종료하고, 연결된 사건과 미확정 작업을 먼저 점검한다.

### 외부 실행기의 Kubernetes 권한

외부 실행기를 연결할 때 역할별로 다른 Kubernetes 신원과 해당 신원으로 실행한 루프백 프록시를 지정한다. 기존 `liveprobe-application-runner` 서비스 계정과 프록시는 배포 권한이 있으므로 source와 QA에 재사용하지 않는다. 일반 사건 조회·접수는 이 설정 전에 이용할 수 있다.

| 실행기 역할 | 네임스페이스 | 필요한 Kubernetes 권한 |
| --- | --- | --- |
| `runner:source` | 담당 사건의 원래 환경(dev 또는 prd) | 등록된 대상 이름에 한해 `get deployments.apps`. 소스 분석 시 대상 UID와 이미지 확인에 사용한다. 빌드 시험에 추가 Kubernetes 권한은 필요 없다. |
| `runner:qa` | dev | 대상 이름에 한해 `get deployments.apps`, 배포한 버전의 Pod 확인용 `list replicasets.apps`와 `list pods`. 주문의 재고 불변 검사를 실행한다면 `get services/proxy`를 `http:inventory:8080`에 한해 추가한다. |
| `runner:cluster` | 등록된 dev·prd 대상 | 대상 이름에 한해 `get`·`patch deployments.apps`. 수집용 `list pods`·`list replicasets.apps`·`list events`, `get pods/log`, 업무 검증용 `get services/proxy`를 등록한 서비스 이름과 포트에 한정한다. |

소스·QA 역할의 서비스 계정과 RoleBinding은 [runner-read-rbac.yaml](runner-read-rbac.yaml)에 있다. 4175 통합 설치에서는 각 역할의 `runtime.json`에 `kube_unix_socket`을 지정한다. 해당 사용자가 소유한 0700 디렉터리에서 역할별 서비스 계정으로 `kubectl proxy --unix-socket=<경로>`를 실행한다. 소스·QA 프록시는 GET 이외의 변경 메서드를 차단한다. 세 실행기 프로세스는 각자의 `--role`, `--state-dir`, `--machine-token-file`로 통합 API에서 일감을 가져간다.

소스 실행기의 `provenance_dir`에는 배포 이미지 지문과 대조한 빌드 당시 소스가 있어야 한다. QA는 같은 빌드 증빙을 읽는다. 소스 실행기와 클러스터 실행기의 `artifact_receipts_dir`은 검증된 빌드 결과를 전달하는 경로다. 클러스터 실행기는 이미지·소스·패치·시험한 트리·빌드 영수증 지문이 요청과 일치할 때 해당 결과를 사용한다. 이전 설치의 로컬 소스 증빙을 복구할 때는 저장 사건에 빌드 전 소스·패치·시험 결과·dev 검증 결과가 모두 남아 있고, Docker에 같은 manifest가 있으면 `scripts/restore-application-build-proof.py --incident <사건 ID> --image <저장소@sha256:지문> --destination <전용 provenance_dir>`로 복원할 수 있다.

아래 명령은 등록 대상이 `order`일 때의 예다. 설치에서 준비한 세 컨텍스트 이름을 환경 변수에 지정하고, 다른 등록 대상에도 같은 검사를 반복한다.

```sh
kubectl --context "${SOURCE_CONTEXT:?}" auth can-i get deployments.apps/order -n dev  # yes
kubectl --context "${QA_CONTEXT:?}" auth can-i list replicasets.apps -n dev           # yes
kubectl --context "$QA_CONTEXT" auth can-i get services/http:inventory:8080 --subresource=proxy -n dev  # 주문 재고 검사 시 yes
kubectl --context "$QA_CONTEXT" auth can-i get deployments.apps/order -n prd          # no
kubectl --context "${CLUSTER_CONTEXT:?}" auth can-i patch deployments.apps/order -n dev  # yes
for ctx in "$SOURCE_CONTEXT" "$QA_CONTEXT"; do
  for ns in dev prd; do
    kubectl --context "$ctx" auth can-i patch deployments.apps/order -n "$ns"  # no
    kubectl --context "$ctx" auth can-i get secrets -n "$ns"                   # no
    kubectl --context "$ctx" auth can-i create pods --subresource=exec -n "$ns" # no
  done
done
```

## 배포 결정과 보존 경계

브라우저 QA 실행 호스트에는 소스 쓰기 및 배포 자격과 분리한 `runner:qa` 신원, 검증된 빌드 자료의 읽기 전용 사본, 등록된 dev 업무 화면 접근이 필요하다. 저장소 루트에서 다음 명령으로 해당 실행기의 브라우저 의존성을 설치한다.

```sh
npm ci --prefix liveprobe/application_runner
npm --prefix liveprobe/application_runner run install:chromium
```

실행기는 개발자 컨트롤러 테스트의 지문을 시험한 소스와 대조하고 브라우저 조작 후 최종 업무 결과를 확인한다. 현재 실제 dev 주문·카탈로그·결제·알림·배송 화면에서 읽기 흐름을 검증했다. 지원되는 업무 화면이나 이를 뒷받침하는 테스트가 없으면 해당 서비스의 QA 상태에 이유를 기록한다.

- 이번 작업 지시가 계약 보완 §8의 신규 namespace 결정을 대체한다. 기존 `liveprobe-system` 안에 API·전용 DB를 둔다. namespace, CNI, Istio, Ingress controller, 기존 default-deny/DNS/mesh 정책은 만들거나 변경하지 않는다.
- API: `liveprobe-application-api` SA/Deployment/Service/Ingress, 8080, `app=liveprobe-application-api`. 모든 새 객체는 `app.kubernetes.io/part-of=liveprobe-application` 라벨을 가진다.
- 독립 DB: `liveprobe-application-db`, PostgreSQL DB/역할 `liveprobe_application`, `pgvector/pgvector:0.8.6-pg17-bookworm`/IfNotPresent, 새 `data-liveprobe-application-db-0` PVC(local-path, 1Gi). 기존 `postgres`/업무 DB/PVC를 참조하지 않는다. PVC 삭제·축소·데이터 마이그레이션을 자동 수행하지 않는다.
- API SA 및 Pod는 서비스 계정 토큰을 자동 마운트하지 않으며 API SA에 RBAC 바인딩이 없다. API는 비root UID/GID 65532, 읽기 전용 rootfs, /tmp emptyDir, capabilities 없음이다. 기존 Dockerfile에는 `USER`가 없어 root 기본값이다. 새 Dockerfile은 그 값을 상속하지 않고 명시적으로 비root로 전환한다. 이미지의 실제 Config.User·실행 가능 여부는 부모가 확인해야 한다.
- 구독 인증용 전용 PVC는 `liveprobe-application-codex`이고 Application의 비root 사용자(UID 65532)는 `/var/lib/liveprobe/application-codex`를 사용한다. 운영자는 전용 저장소에 인증을 수립하고 쓰기·갱신 가능 여부와 실제 역할 호출을 확인한다. 기존 K8s 제품의 인증 저장소는 접근하지 않는다.
- 2026-11-09에는 로컬에 Application이 설치되기 전이었다. 2026-11-20에는 기존 Application API와 DB의 저장 사건을 통합 로컬 화면에서 조회했다. 운영 반영 범위와 검증 시점은 [통합 관제 작업 기록](../../docs/plans/unified-operator-console-2026-11-20.md)에 있다.

## 설정·비밀값

`render-application-deploy.py`는 stdlib만 사용한다. JSON 형식은 YAML 1.2 부분집합이므로 각 `.yaml`은 YAML로도 파싱된다. 원본 `deploy/samsung/application-profiles.json`, `system-catalog.json` **bytes를 그대로** ConfigMap 값에 넣는다. API/runner의 catalog SHA256 일치를 위해 JSON을 재직렬화하지 않는다. 설정 지문 변경은 Pod template annotation에 반영된다.

2026-11-13 dev/prd 전환: catalog의 `environments`가 `["dev","prd"]`, `retired_environments`가 `["stg"]`로 바뀌어 `10-configmap.yaml`과 `30-api.yaml`의 `liveprobe.io/application-config-sha256`이 함께 바뀌었다. catalog SHA가 달라지므로 API(ConfigMap·Deployment)와 호스트 runner(같은 catalog 파일)를 **동시에** 갱신해야 한다. 한쪽만 바꾸면 catalog 지문 불일치로 runner claim이 거부될 수 있다.

API 환경 변수는 현재 `settings.py` 이름을 따른다:

| 이름 | 값/경로 |
|---|---|
| APPLICATION_DATABASE_URL_FILE | /run/application/database/url |
| APPLICATION_IDENTITY_FILE | /run/application/identity/human.json |
| APPLICATION_MACHINE_IDENTITY_FILE | /run/application/identity/machine.json |
| APPLICATION_PROFILES_FILE | /etc/application/application-profiles.json |
| APPLICATION_CATALOG_FILE | /etc/application/system-catalog.json |
| APPLICATION_CODEX_HOME | /var/lib/liveprobe/application-codex |
| APPLICATION_DATABASE_ISOLATED / APPLICATION_WORKER_ENABLED | true / true |
| APPLICATION_MODEL | gpt-6.2-sol |
| APPLICATION_CLASSIFIER_MODEL / APPLICATION_CRITIC_MODEL | gpt-6-luna / gpt-6.2-astra |

역할별 override를 명시한다. 현재 코어는 공통 `APPLICATION_MODEL`이 있으면 역할 기본값도 덮으므로 공통 값만 지정하면 분류/반론 모델이 바뀐다. 다른 역할은 sol/medium 기본값을 쓴다. readiness/liveness는 `/api/application/v1/healthz`다. `/readyz`는 DB·identity·profiles·model·최근 runner claim의 종합 점검이며 bootstrap에서 503일 수 있으므로 Pod readiness로 쓰지 않는다. healthz 성공은 업무 준비 완료가 아니다.

프로비저닝은 **신규 Secret 세 개만 create-only**로 생성한다:

| Secret | 키 |
|---|---|
| liveprobe-application-db | password |
| liveprobe-application-db-url | url |
| liveprobe-application-identity | human.json, machine.json |

신규 설치의 `human.json`은 기존 마운트와 호환되는 빈 목록 `{"principals":[]}`이다. 사람의 시험·승인 결정은 위 루프백 게이트웨이가 증명한다. `machine.json`에는 `application-runner`의 기존 `runner`, `incident:create`, `incident:read` 범위를 기록한다.

`provision-application-secrets.py`는 세 Secret에 대해 `kubectl --context … -n liveprobe-system get secret <name> -o name`만 실행한다. 모두 확실한 NotFound일 때만 새 값을 생성한다. 존재/Forbidden/연결 오류/불명확한 응답은 중단한다. Secret 본문 조회·회전·덮어쓰기·삭제는 없다. 값은 `kubectl create -f - -o name`의 stdin으로만 전달하며 argv에는 없다. **stdout/stderr를 모두 캡처하고 원문을 출력하지 않는다.** 응답 유실/AlreadyExists/부분 실패 시 재시도/자동 정리를 하지 않는다.

실행기 토큰 한 개는 저장소의 gitignore 경로 `.e2e-tmp/application/application-runner.token`에 0600으로 저장하고 경로만 출력한다. 디렉터리는 0700이며 symlink/기존 토큰은 읽지 않고 거부한다. 비밀번호/DB URL/identity JSON은 디스크에 쓰지 않는다. 부분 실패 시 이미 만든 토큰 파일은 보존한다. 임의 재실행·기존 Secret 삭제로 복구하지 말고 부모가 name-only 상태를 확인하여 별도 복구를 결정해야 한다. kubectl debug/HTTP tracing, shell `set -x`, 토큰 `cat`, Secret YAML 출력, 토큰의 채팅/증거 파일 복사를 금지한다.

## 네트워크·실행기 권한과 한계

- API ingress: kube-system의 `app.kubernetes.io/name=traefik` Pod → 8080만. egress: cluster DNS TCP/UDP 53, 앱 DB 5432, 외부 IPv4 443만. 공개 443에서 RFC1918/loopback/link-local/CGNAT를 제외하므로 현재 K3s Pod/Service/노드 CIDR도 제외된다. IPv6·FQDN 제한은 제공하지 않는다.
- DB ingress: 앱 API selector → 5432만, 이 선언의 DB egress는 빈 목록이다.
- NetworkPolicy는 **가산 허용**이다. 기존 namespace 전체 DNS/istiod/mesh telemetry 정책도 적용된다. 따라서 전체 패킷이 이 두 정책만으로 제한된다고 주장하지 않는다. 기존 정책을 삭제/변경하지 않는다. CNI enforcement, Traefik label, Istio probe rewrite/mTLS/주입 initContainer의 admission 호환성은 부모의 실제 readback/허용·거부 접속 시험이 필요하다.
- runner는 호스트 프로세스이며 새 `liveprobe-application-runner` SA가 Kubernetes 신원이다. API bearer와 Kubernetes 신원은 별개다. **활성 환경 dev/prd 각각**에만 namespaced Role/RoleBinding을 생성한다(`50/51-runner-dev-*`, `54/55-runner-prd-*`; 퇴역한 stg 번호 52/53은 더 이상 렌더하지 않는다). pods/events get/list, pods/log get, replicasets get/list, deployments get/list/watch. deployments patch는 카탈로그의 **9개 이름에만** 허용한다. 업무 확인용 services/proxy는 **GET만**, `http:<서비스>:8080` 9개 이름에 한정한다. Secret/ConfigMap/pods/exec/delete/create/cluster 권한은 없다.
- **stg 퇴역:** 렌더에서 stg를 뺀다고 클러스터에 이미 있는 `stg/liveprobe-application-runner` Role/RoleBinding이 지워지지는 않는다. 이 선언은 `create`만 쓰므로 자동 정리도 없다. 부모가 해당 두 객체의 이름·UID·resourceVersion·`app.kubernetes.io/part-of=liveprobe-application` 라벨을 확인한 뒤 **개별로** 퇴역한다(namespace 삭제·라벨 광역 삭제 금지). stg namespace의 기존 Secret·DB·PVC·이력은 보존한다. 퇴역 전 마지막 렌더 사본(52/53)은 sha256 `0ad8d458…9b5e`(52-runner-stg-role.yaml)·`e26c8520…7fba`(53-runner-stg-binding.yaml)로 기록했고 작업 scratch(`t_c9309d70/removed-rendered/`, 24시간 유휴 시 정리됨)에 임시 보관했다. 영구 보존이 필요하면 부모가 증거 위치로 옮긴다.
- Pod/Event list와 로그 권한은 namespace 전체 읽기를 허용한다. RBAC는 JSON Patch 필드/CAS/승인 정책을 강제하지 않으므로 테스트된 runner allowlist/fencing·호스트 자격 보호가 추가로 필요하다. prd RBAC가 있다는 사실이 prd 승인이나 배포 증거는 아니다.
- **업무 확인 RBAC 보완:** `application_runner/cli.py`의 서비스 프록시 GET과 일치하도록 렌더러를 보완했다. 이름제한·GET 전용 음성 회귀가 통과했으며 클러스터 권한 인수는 아직 실행하지 않았다. RBAC는 프록시 뒤의 HTTP 경로까지 제한하지 않으므로 실행기의 등록 GET 경로 제한과 전용 신원 보호가 함께 필요하다.
- **독립 DB bootstrap 보완:** lifespan과 `python -B -m app.application.bootstrap`이 Application 전용 연결·metadata만 초기화한다. 부모가 폐기 PostgreSQL에서 최초 초기화·재시작 후 사건 보존·반복 초기화·경합 claim의 단일 승자를 확인했다. 기존 관제 DB·Base·alembic에는 접속하지 않았다. 실제 클러스터 DB 인수는 남아 있다.
- 호스트 runner의 loopback Kubernetes proxy가 관리자 신원으로 동작하면 위 RBAC가 무의미해진다. 전용 SA의 인증/impersonation을 강제하고 실제 `auth can-i`를 확인해야 한다. SA 토큰 Secret이나 관리자 kubeconfig를 새 앱에 전달하지 않는다.

## 부모가 직렬로 실행할 정확한 순서

아래는 **실행 지침**이지 실행 기록이 아니다. 사전 조건(전용 PVC의 비root 접근·별도 로그인, runner 업무 HTTP 경로)을 먼저 해결해야 한다. 기존 DB/Secret/PVC, 다른 작업의 미커밋 파일을 건드리지 않는다. 전체 overlay를 적용하지 않는다.

```sh
cd /Users/hwany/sds-ai-professional
export PYTHONDONTWRITEBYTECODE=1 TMPDIR=/Users/hwany/.hermes/cache/scratch
PY=liveprobe/.venv/bin/python
KUBE_CONTEXT=rancher-desktop
# 아래 블록은 실패 즉시 중단되는 별도 신뢰된 shell에서 실행한다.
set -eu

# 1. 이미지 빌드: 기존 .env/.venv/인증파일을 Docker context로 보내지 않는 최소 context.
BUILD_CONTEXT=$(mktemp -d "$TMPDIR/application-build-XXXXXXXX")
cp liveprobe/Dockerfile.application "$BUILD_CONTEXT/Dockerfile"
cp -R liveprobe/app "$BUILD_CONTEXT/app"
BUILD_TAG="liveprobe/application:local-$(date -u +%Y%m%dT%H%M%SZ)"
docker build --platform linux/arm64 --pull=false -f "$BUILD_CONTEXT/Dockerfile" -t "$BUILD_TAG" "$BUILD_CONTEXT"
docker image inspect "$BUILD_TAG" --format '{{.Config.User}}'
# 반드시 65532:65532. 기존 base가 로컬에 없으면 임의 외부 pull 없이 중단한다.

# 2. manifest digest 확인: .Id/config digest나 빌드 로그의 예상값을 대용하지 않는다.
docker image inspect "$BUILD_TAG" --format '{{json .RepoDigests}}' > "$BUILD_CONTEXT/repodigests.json"
IMAGE=$("$PY" -B -c 'import json,re,sys; refs=json.load(open(sys.argv[1])); images=[r for r in refs if re.fullmatch(r"(?:docker.io/)?liveprobe/application@sha256:[0-9a-f]{64}",r)]; assert len(images)==1,"observable repository manifest digest required"; print(images[0])' "$BUILD_CONTEXT/repodigests.json")
printf '%s\n' "$IMAGE"
# RepoDigests가 비어 있으면 여기서 중단한다. 부모가 로컬 containerd의 실제 manifest/index
# descriptor를 확인/가져오는 별도 경로가 필요하며 .Id를 붙여 통과시키지 않는다.

# 3. 공개 선언 렌더 및 결정성 검사
"$PY" -B scripts/render-application-deploy.py --image "$IMAGE"
"$PY" -B scripts/render-application-deploy.py --image "$IMAGE" --check
"$PY" -B -m unittest scripts/test_application_deploy_render.py -v

# 4. 기존 namespace/StorageClass/IngressClass 및 새 DB PVC 충돌 확인 (비밀 본문 조회 없음)
# 활성 환경 dev/prd만 확인한다. 퇴역한 stg에는 runner 권한을 새로 만들지 않는다.
kubectl --context "$KUBE_CONTEXT" get namespace liveprobe-system dev prd -o name
kubectl --context "$KUBE_CONTEXT" get storageclass local-path -o name
kubectl --context "$KUBE_CONTEXT" get ingressclass traefik -o name
PVC_COLLISION=$(kubectl --context "$KUBE_CONTEXT" -n liveprobe-system get pvc data-liveprobe-application-db-0 --ignore-not-found -o name)
test -z "$PVC_COLLISION"
# 첫 설치 전에 전용 인증 PVC 이름 충돌도 확인한다.
AUTH_PVC=$(kubectl --context "$KUBE_CONTEXT" -n liveprobe-system get pvc liveprobe-application-codex --ignore-not-found -o name)
test -z "$AUTH_PVC"

# 5. server dry-run: create를 사용하여 같은 이름의 기존 객체를 덮어쓰지 않는다.
kubectl --context "$KUBE_CONTEXT" create --dry-run=server -f deploy/application/rendered
# dry-run 성공 후 신규 Secret만 생성. 기존 것이 하나라도 있으면 중단, apply/rotate 금지.
"$PY" -B scripts/provision-application-secrets.py --context "$KUBE_CONTEXT" --dry-run
"$PY" -B scripts/provision-application-secrets.py --context "$KUBE_CONTEXT"

# 6. 이번 산출물만 적용. create 실패/부분 성공이면 멈추고 name-only 확인. 무조건 apply로 재시도 금지.
kubectl --context "$KUBE_CONTEXT" create -f deploy/application/rendered

# 7. readback (Secret body, Pod env 원문, 로그 원문을 증거로 덤프하지 않는다)
kubectl --context "$KUBE_CONTEXT" -n liveprobe-system rollout status statefulset/liveprobe-application-db --timeout=180s
kubectl --context "$KUBE_CONTEXT" -n liveprobe-system rollout status deployment/liveprobe-application-api --timeout=180s
OBSERVED_IMAGE=$(kubectl --context "$KUBE_CONTEXT" -n liveprobe-system get deployment liveprobe-application-api -o jsonpath='{.spec.template.spec.containers[0].image}')
test "$OBSERVED_IMAGE" = "$IMAGE"
kubectl --context "$KUBE_CONTEXT" -n liveprobe-system get pods -l app=liveprobe-application-api -o custom-columns='NAME:.metadata.name,SA:.spec.serviceAccountName,IMAGE:.spec.containers[0].image,IMAGE_ID:.status.containerStatuses[0].imageID'
kubectl --context "$KUBE_CONTEXT" -n liveprobe-system get ingress liveprobe-application-api -o wide
kubectl --context "$KUBE_CONTEXT" -n liveprobe-system get pvc data-liveprobe-application-db-0 -o custom-columns=NAME:.metadata.name,PHASE:.status.phase
curl --noproxy '*' --fail --resolve application.liveprobe.localhost:80:192.168.64.3 http://application.liveprobe.localhost/api/application/v1/healthz
# /readyz는 아직 503일 수 있다. 코드를 포함해 checks만 확인하며 200이라고 추정하지 않는다.
curl --noproxy '*' --resolve application.liveprobe.localhost:80:192.168.64.3 -w '\nhttp_status=%{http_code}\n' http://application.liveprobe.localhost/api/application/v1/readyz
```

외부 네트워크 설치/새 registry push가 필요한 단계는 포함하지 않았다. 새로운 이미지에 실제 정합 manifest digest가 없으면 부모가 먼저 로컬 image-store 경로를 확정해야 한다. 이번 lane은 ARM64만 대상으로 하며 다중 아키텍처·VDI 반입을 검증하지 않았다.

추가 인수: `kubectl auth can-i --as=system:serviceaccount:liveprobe-system:liveprobe-application-runner -n dev patch deployment/catalog`는 yes, `get secrets`, `get configmaps`, `create pods/exec`, `delete deployments`, `patch deployment/unregistered`는 no이어야 한다(prd도 반복). 부모가 stg Role/RoleBinding을 개별 퇴역한 뒤에는 `-n stg`의 `patch deployment/catalog`·`get pods`가 모두 no이어야 한다(퇴역 전에는 기존 바인딩 때문에 yes일 수 있으므로 이 선언의 증거로 쓰지 않는다). API SA는 deployment patch가 no여야 한다. 실제 네트워크 허용/거부, API static 화면, 모델 호출, runner claim/업무 검증, 무승인 prd 차단을 부모가 별도로 관측한다.

접속: `http://application.liveprobe.localhost/` (DNS/localhost 포워딩이 없으면 위 `--resolve` 또는 hosts 설정 사용). 현재 Ingress는 로컬 PoC용 HTTP이며 기업 SSO/TLS 경계가 아니다. bearer를 LAN 평문으로 전송하지 않는다. 호스트 runner는 현재 `safe_url`에 맞게 별도 터미널의 `kubectl --context rancher-desktop -n liveprobe-system port-forward --address 127.0.0.1 service/liveprobe-application-api 18080:8080`와 `--api-url http://127.0.0.1:18080`, `--machine-token-file /Users/hwany/sds-ai-professional/.e2e-tmp/application/application-runner.token`을 사용한다. runtime-config/전용 Kubernetes proxy와 services/proxy 충돌 해결은 부모 통합 후에만 실행한다.

## 안전한 원복

첫 설치 전 이 이름들이 없었다는 확인과 이번 생성 영수증을 보존한다. **이번 생성 성공으로 확인된 객체만** 아래 명령 대상으로 삼는다. 기존 제품/namespace/Secret/PVC를 지우지 않는다. 부분 실패라면 생성되지 않은 객체/기존 충돌 객체는 목록에서 뺀다.

```sh
KUBE_CONTEXT=rancher-desktop
# 먼저 호스트 runner를 정상 종료한다. 기존 관제는 종료/재시작하지 않는다.
kubectl --context "$KUBE_CONTEXT" -n liveprobe-system delete ingress/liveprobe-application-api service/liveprobe-application-api deployment/liveprobe-application-api
# 이번 렌더가 만드는 runner 권한은 활성 환경 dev/prd뿐이다. stg Role/RoleBinding은 이 원복 대상이 아니며
# 부모가 이름·UID 확인 후 별도로 개별 퇴역한다.
for ns in dev prd; do
  kubectl --context "$KUBE_CONTEXT" -n "$ns" delete rolebinding/liveprobe-application-runner role/liveprobe-application-runner
done
kubectl --context "$KUBE_CONTEXT" -n liveprobe-system delete statefulset/liveprobe-application-db service/liveprobe-application-db
kubectl --context "$KUBE_CONTEXT" -n liveprobe-system delete configmap/liveprobe-application-config networkpolicy/liveprobe-application-api networkpolicy/liveprobe-application-db serviceaccount/liveprobe-application-api serviceaccount/liveprobe-application-runner
```

StatefulSet PVC retention은 Retain이다. 위 원복은 신규 DB PVC와 세 신규 Secret, 로컬 토큰 파일을 **의도적으로 보존**한다. 기존 `liveprobe-codex-spare`, 기존 `liveprobe-api`, 기존 DB, 기존 모든 Secret은 삭제/덮어쓰기/회전하지 않는다. 신규 DB 데이터와 비밀값 폐기는 별도 명시 승인 없이는 하지 않는다. `kubectl delete namespace`, `delete -l` 광역 삭제, 전체 overlay apply, `rollout undo`로 임의 과거 버전 선택을 하지 않는다. 새 API만 업그레이드했다면 이번 작업 전 관측한 이전 digest/설정의 정확한 영수증을 통해 부모가 별도 CAS 원복을 설계해야 하며 최초 설치 삭제와 혼동하지 않는다.

## 검증 기록과 확인 필요 이름

오프라인 증거: `docs/evidence/application/lanes/deploy/{red.txt,red-provision.txt,red-role-models.txt,red-rbac-targets.txt,green.txt,yaml-validation.txt}`. 시험은 스크래치 내 합성 파일과 가짜 kubectl transport만 쓴다. 실제 kubectl/docker/helm, Secret/PVC 읽기, 외부 통신은 실행하지 않았다.

병렬 작성자 통합 시 재확인할 이름: 위 APPLICATION_* env, `human.json`/`machine.json`(파일별 종류 구분), scope `incident:read/incident:create/tester/prd-approver/runner`, runner `--machine-token-file`/`--runtime-config` 및 `kube_api`, catalog 원본 bytes SHA256, 전용 인증 경로/UID 권한, runner의 services/proxy 의존, healthz/readyz 및 lifespan DB 초기화/worker 시작. 공유 파일을 이 lane에서 변경하지 않았다.
