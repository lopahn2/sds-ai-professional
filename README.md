# 🛰️ LiveProbe 설치와 운영

2026-11-15 · LiveProbe 프로젝트 · Kubernetes / 자체 설치

---

LiveProbe는 Kubernetes 장애를 감지하고, AI가 근거를 수집·분석한 뒤 허용된 복구 조치를 수행하거나 운영자에게 이관하는 프로젝트입니다. 이 공개 저장소에는 플랫폼과 테스트 시스템의 전체 소스가 들어 있습니다.

> 💡 **먼저 알아둘 점** — 소스를 `clone`하거나 `pull`하는 것만으로 실행되지는 않습니다. 설치자는 **자신의 Kubernetes 설정, 컨테이너 이미지, 저장소, AI 로그인, 인증서와 실행 권한**을 준비해야 합니다. 개발자의 인증·DB·운영 이력·로컬 이미지는 제공하지 않습니다. 아래 절차는 현재 코드와 배포 선언에 맞춘 설치 안내이며, 임의의 새 클러스터에서 전체 구성을 검증했다는 뜻은 아닙니다.

## 목차

1. [제품 구성과 설치 범위](#1-제품-구성과-설치-범위)
2. [설치 전에 준비할 것](#2-설치-전에-준비할-것)
3. [소스와 클러스터 설정](#3-소스와-클러스터-설정)
4. [이미지 빌드와 배포 주소](#4-이미지-빌드와-배포-주소)
5. [AI 로그인과 비밀정보](#5-ai-로그인과-비밀정보)
6. [Kubernetes 관제 설치](#6-kubernetes-관제-설치)
7. [관측 연결과 자동 복구 권한](#7-관측-연결과-자동-복구-권한)
8. [dev/prd 전체 테스트 시스템](#8-devprd-전체-테스트-시스템)
9. [Application 관제와 실행기](#9-application-관제와-실행기)
10. [설치 완료 확인](#10-설치-완료-확인)
11. [자주 막히는 지점](#11-자주-막히는-지점)
12. [업데이트·백업·개발](#12-업데이트백업개발)

## 1. 제품 구성과 설치 범위

| 구성 | 하는 일 | 별도로 필요한 설정 |
|---|---|---|
| **LiveProbe K8s 관제 모듈** | Kubernetes 장애 감지, 서비스 지도, AI 분석·진행 스트리밍, 제한된 복구, 사건·감사 이력 | Kubernetes 읽기 권한, 관측 연결, DB, AI 인증, 복구용 별도 권한 |
| **LiveProbe 코드 조치 모듈 (Application)** | 애플리케이션 오류 분석, 소스 수정, 재현·회귀 시험, dev 배포, 사용자 시험과 prd 승인 | 독립 DB·인증·AI 저장소, 서비스 프로필, 호스트 실행기 |
| **테스트 시스템** | 주문·재고·상품·결제·배송·알림 및 트레이드·딜리버리·비즈니스 시스템 | 서비스 이미지, 업무 DB, Kafka, 내부 인증·TLS, 환경별 배포 |

현재 활성 환경은 **`dev`와 `prd`**입니다. `stg`는 과거 기록을 조회하기 위한 퇴역 환경입니다. 새 실험은 dev에서 시작하고 prd에는 설치 확인 목적으로 장애를 주입하지 않습니다.

설치 범위는 다음과 같이 구분합니다.

- **기본 구성:** `deploy/base`와 사용자 overlay로 관제 API·대시보드·DB·관측 도구 및 `liveprobe-target`의 주문/재고 참조 서비스를 설치합니다. 이것만으로 dev/prd 전체 시스템이나 Application 관제가 설치되지는 않습니다.
- **현재 전체 구성:** 기본 구성을 준비한 뒤 [8장](#8-devprd-전체-테스트-시스템)의 dev/prd 업무 환경과 [9장](#9-application-관제와-실행기)의 Application을 구성합니다. 기존 서비스 관제만 필요하면 업무 테스트 시스템 설치를 생략하고 자신의 서비스에 맞게 감시·권한·관측 설정을 변경합니다.

K8s의 과거 코드 변경 이력은 보존합니다. 신규 코드 수정·시험·승격은 Application이 담당합니다. Jira 연결은 필요하지 않습니다.

## 2. 설치 전에 준비할 것

| 항목 | 설치자가 확인할 내용 |
|---|---|
| Kubernetes | `admissionregistration.k8s.io/v1`의 `ValidatingAdmissionPolicy`를 지원하는 클러스터. 이 API는 Kubernetes 1.30부터 안정 기능입니다. 버전 숫자뿐 아니라 API 활성화와 실제 정책 집행을 확인합니다. |
| 설치 권한 | namespace, Deployment, StatefulSet, PVC, Secret, ConfigMap, RBAC, NetworkPolicy, admission 정책을 설치할 권한. 관제 실행 계정에 설치자의 관리자 자격을 전달하지 않습니다. |
| 네트워크 | NetworkPolicy를 집행하는 CNI, CoreDNS, Ingress controller. DNS·API 서버·노드 주소·관측 서비스·모델 서비스로의 통신 허용이 필요합니다. |
| 저장소 | 실제 사용 가능한 StorageClass와 영속 볼륨. DB 및 AI 인증 PVC의 용량·접근 권한·백업 정책을 정합니다. |
| 이미지 배포 | 노드 CPU 아키텍처와 맞는 이미지, 노드가 접근 가능한 레지스트리 또는 각 노드에 이미지를 반입하는 절차. 비공개 레지스트리는 `imagePullSecrets`도 설정합니다. |
| AI 계정 | 본인이 사용할 수 있는 ChatGPT/Codex 계정과 모델. 이 프로젝트의 기본 경로는 **ChatGPT 로그인 기반 `codex_cli`**입니다. |
| 호스트 도구 | Git, Bash, kubectl의 Kustomize 지원, Docker/Buildx, OpenSSL, Python 3.12, uv. 프런트 개발은 Node.js 22와 npm. 선택적 관측 설치에는 Helm 등 해당 도구가 필요합니다. |
| 자원 여유 | 배포 YAML의 requests/limits 합계 외에 이미지 빌드·Maven·AI 프로세스·관측 데이터의 여유 공간을 확보합니다. 이 저장소는 범용 최소 CPU/RAM이나 HA 구성을 보장하지 않습니다. |

기존 검증 환경은 로컬 Rancher Desktop/K3s이며 여러 도구가 이 환경을 전제로 합니다. 다른 클러스터에는 `portable-example`을 복사해 맞춥니다. **이 예제는 렌더링 검증용이지 범용 설치 완료 템플릿이 아닙니다.**

기본 K8s 대시보드에는 사용자 로그인 기능이 없습니다. Host·동일 출처 검사는 사용자 인증을 대신하지 않습니다. 공유 환경에 서비스하려면 HTTPS와 외부 인증·접근 통제를 먼저 구성합니다. 프로젝트 공개와 실행 서비스의 인터넷 공개는 별개입니다.

참고: [Kubernetes admission 정책](https://kubernetes.io/docs/reference/access-authn-authz/validating-admission-policy/), [컨테이너 이미지와 pull 정책](https://kubernetes.io/docs/concepts/containers/images/).

## 3. 소스와 클러스터 설정

### 3.1 소스 받기

처음에는 clone, 이후에는 기존 checkout에서 pull합니다. 아래 명령은 Bash에서 순서대로 실행하며 예시 값을 실제 값으로 바꾸고, 오류가 나면 다음 단계로 넘어가지 않습니다. 여러 터미널에서는 필요한 `LP_*` 변수를 각각 설정합니다.

```bash
set -euo pipefail
git clone https://github.com/lopahn2/sds-ai-professional.git
cd sds-ai-professional

# 이후 업데이트할 때: 로컬 변경을 먼저 보존하고 실행
# git pull --ff-only origin main

export LP_CONTEXT='your-kube-context'
export LP_OVERLAY='deploy/overlays/my-cluster'

kubectl --context "$LP_CONTEXT" cluster-info
kubectl --context "$LP_CONTEXT" get nodes -o wide
kubectl --context "$LP_CONTEXT" get storageclass,ingressclass
kubectl --context "$LP_CONTEXT" api-resources --api-group=admissionregistration.k8s.io

# Python 도구 의존성 준비; 클러스터를 설치하지 않는 단계
(cd liveprobe && uv sync --frozen --group dev)
# install.sh 내부의 python3도 같은 의존성을 사용하도록 설정
export PATH="$PWD/liveprobe/.venv/bin:$PATH"
```

### 3.2 자신의 overlay 만들기

```bash
# my-cluster가 없는 첫 준비 때만 실행
cp -R deploy/overlays/portable-example "$LP_OVERLAY"
```

`$LP_OVERLAY/platform.yaml`의 다음 값을 **실제 환경 값으로 모두 변경**합니다.

| 키 | 의미와 확인 방법 |
|---|---|
| `ingressClassName` | 사용할 IngressClass 이름 |
| `storageClassName` | 사용할 StorageClass 이름 |
| `liveprobeHost` | 관제 웹/API 도메인. 해당 이름이 Ingress 주소로 해석되도록 DNS 또는 hosts 설정 |
| `ingressNamespace` | Ingress controller Pod의 namespace |
| `ingressPodName` | controller Pod의 실제 `app.kubernetes.io/name` 라벨 값 |
| `apiserverCIDR` | Pod에서 접속하는 Kubernetes API endpoint 주소 범위. Service ClusterIP만 보고 정하지 않음 |
| `nodeCIDR` | Prometheus가 kubelet 10250 포트로 접근할 노드 주소 범위. 다중 노드면 관련 정책도 점검 |
| `imageRegistry` | 자체 이미지와 미러 이미지를 제공하는 레지스트리 |

`registry.example.test`, `192.0.2.*`, `standard-rwo`, `nginx`는 그대로 사용할 값이 아닙니다. `$LP_OVERLAY/kustomization.yaml`의 `images`도 따로 변경해야 합니다. `imageRegistry` 한 항목이 모든 이미지 주소를 자동 변경하지는 않습니다.

```bash
kubectl --context "$LP_CONTEXT" -n default get endpointslices \
  -l kubernetes.io/service-name=kubernetes -o wide
kubectl --context "$LP_CONTEXT" -n kube-system get pods --show-labels
```

추가로 API Deployment의 환경 변수 **`LIVEPROBE_ALLOWED_HOSTS`**에 자신의 도메인을 JSON 배열로 설정합니다. Ingress 도메인만 변경하면 API가 요청을 거부할 수 있습니다. 다음 파일을 overlay의 `api-settings.yaml`로 만들고 `kustomization.yaml`의 `patches`에 `- path: api-settings.yaml`을 추가합니다.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: liveprobe-api
  namespace: liveprobe-system
spec:
  template:
    spec:
      containers:
        - name: api
          env:
            - name: LIVEPROBE_ALLOWED_HOSTS
              value: '["liveprobe.your-domain.example","localhost","127.0.0.1"]'
```

`liveprobe.your-domain.example`도 자신의 이름으로 바꿉니다. Ingress TLS 설정은 플랫폼 자동 치환에 포함되지 않으므로 overlay에서 직접 추가합니다.

### 관측 위치와 태그

기본 위치는 클러스터의 Kubernetes API와 `LIVEPROBE_LOKI_URL`, `LIVEPROBE_PROMETHEUS_URL`입니다. 같은 어댑터에 추가 위치가 필요하면 API Deployment에서 `LIVEPROBE_OBSERVATION_LOCATIONS`를 설정합니다. 예를 들어 `{"loki":{"team":"https://logs.example"},"prometheus":{"team":"https://metrics.example"}}`를 등록한 뒤 **설정 → 관측 소스**에서 위치를 선택해 저장합니다. 해당 API의 네트워크 정책도 선택한 수집기와의 통신을 허용해야 합니다.

**수집 확인**은 Kubernetes 이벤트 조회와 워크로드 태그, Loki의 최근 5분 스트림, Prometheus의 현재 Pod 수집 대상을 확인합니다. 시스템은 Pod의 `app.kubernetes.io/part-of` 라벨(수집 시 `system`), 서비스는 `app`, 환경은 대상 `namespace`로 식별합니다. 태그 누락, 한 환경·서비스에 여러 시스템이 붙은 경우, 표본이 없는 경우를 구분해 표시합니다.

## 4. 이미지 빌드와 배포 주소

저장소에 기록된 `liveprobe/...@sha256:...`는 개발 환경에서 관측한 이미지입니다. GitHub에서 소스를 받았다고 그 이미지까지 내려받을 수 있는 것은 아닙니다. **직접 빌드·배포하고 확인한 manifest digest로 교체**합니다.

아래는 자신이 쓰기 권한을 가진 레지스트리에 이미지를 올리는 예입니다. 먼저 해당 레지스트리에 로그인하고, 아키텍처와 값을 변경하세요. `git archive`를 사용하므로 로컬 `.env`, 인증 파일, 가상환경은 빌드 context에 들어가지 않습니다. 대상은 현재 커밋에 저장된 소스입니다.

```bash
export LP_REGISTRY='registry.your-domain.example/liveprobe'
export LP_VERSION='2026-11-15-1'
export LP_PLATFORM='linux/amd64'  # 노드에 맞게 linux/arm64 등으로 변경
LP_BUILD_DIR=$(mktemp -d)
git archive HEAD liveprobe dashboard services | tar -x -C "$LP_BUILD_DIR"

for component in liveprobe dashboard; do
  docker buildx build --platform "$LP_PLATFORM" --push \
    -t "$LP_REGISTRY/$component:$LP_VERSION" "$LP_BUILD_DIR/$component"
done

docker buildx build --platform "$LP_PLATFORM" --push \
  -f "$LP_BUILD_DIR/liveprobe/Dockerfile.rollback" \
  -t "$LP_REGISTRY/rollback-controller:$LP_VERSION" "$LP_BUILD_DIR/liveprobe"

# 기본 참조 서비스까지 설치할 때
for component in order inventory; do
  docker buildx build --platform "$LP_PLATFORM" --push \
    -t "$LP_REGISTRY/$component:$LP_VERSION" "$LP_BUILD_DIR/services/$component"
done

# 각 이미지의 registry manifest/index digest 확인 예
docker buildx imagetools inspect "$LP_REGISTRY/liveprobe:$LP_VERSION"
```

`$LP_OVERLAY/kustomization.yaml`의 각 이미지 항목을 실제 주소와 digest로 바꿉니다. `newTag`와 `digest`를 혼용하지 말고 기존 `newTag`를 제거합니다.

```yaml
# images 아래에 들어갈 한 항목의 형식 예시. 실제 digest로 교체해야 함.
- name: liveprobe/liveprobe
  newName: registry.your-domain.example/liveprobe/liveprobe
  digest: sha256:<registry에서-확인한-64자리-manifest-digest>
```

API, 대시보드, rollback-controller, order, inventory를 모두 설정합니다. portable 예제는 pgvector·Prometheus·Alertmanager·Loki·Alloy·kube-state-metrics·busybox·curl 이미지까지 예제 레지스트리로 바꿉니다. 이 이미지들을 실제로 미러링하거나 사용 가능한 upstream 이미지 주소로 항목을 조정해야 합니다.

- Docker config ID인 `.Id`를 배포 manifest digest로 간주하지 않습니다.
- `scripts/build-images.sh`는 기본 네 이미지의 **로컬 태그**를 만들 뿐, registry push나 overlay의 digest 변경은 하지 않습니다. rollback-controller도 별도 빌드해야 합니다.
- `Dockerfile.samsung`, `Dockerfile.applayer`, `Dockerfile.application` 등 증분 Dockerfile에는 로컬 기반 이미지 digest가 남아 있을 수 있습니다. 새 환경에서는 기반 이미지를 먼저 빌드하고 `FROM`을 자신의 이미지로 바꿉니다.
- Docker/Kubernetes 런타임이 다른 경우 호스트에서 빌드한 이미지가 노드에 자동으로 생기지 않습니다. 모든 대상 노드가 접근할 수 있는 배포 경로를 확인합니다.

## 5. AI 로그인과 비밀정보

### 5.1 자신의 ChatGPT 계정으로 로그인

기본 런타임은 `codex_cli`입니다. **ChatGPT 구독 로그인과 OpenAI API 키는 서로 다른 인증 경로**이며 이 설치 절차는 API 키를 요구하지 않습니다. 코드에 다른 공급자 어댑터가 존재하더라도 공급자를 임의로 바꾸지 않습니다.

호스트에 Codex CLI를 설치한 뒤, 배포용으로 분리한 로컬 디렉터리에 로그인합니다. 실제 모델 사용 권한과 CLI 버전 호환성을 확인하세요. API Dockerfile에는 Codex CLI 버전이 고정되어 있습니다.

```bash
LP_AUTH_DIR="$HOME/.local/share/liveprobe/codex-bootstrap"
mkdir -p "$LP_AUTH_DIR"
chmod 700 "$LP_AUTH_DIR"

# 브라우저에서 본인 계정으로 직접 로그인
CODEX_HOME="$LP_AUTH_DIR" codex -c 'cli_auth_credentials_store="file"' login
CODEX_HOME="$LP_AUTH_DIR" codex login status

test -s "$LP_AUTH_DIR/auth.json"
chmod 600 "$LP_AUTH_DIR/auth.json"
export CODEX_AUTH_FILE="$LP_AUTH_DIR/auth.json"
```

브라우저 콜백을 사용할 수 없으면 계정에서 지원하는 `codex login --device-auth`를 사용합니다. 조직에서 인증 저장 방식을 제한했다면 그 정책에 맞는 전달 절차가 필요합니다. [공식 Codex 인증 안내](https://developers.openai.com/codex/auth)를 참고하세요.

`auth.json`은 실제 OAuth 인증정보입니다. Git·Docker 이미지·채팅·로그에 포함하지 않습니다. 파일 내용 대신 존재 여부와 `login status`만 확인합니다. Kubernetes Secret으로 전달하면 클러스터에서 해당 Secret과 PVC를 읽을 수 있는 주체도 인증정보에 접근할 수 있으므로 접근 권한을 제한합니다.

### 5.2 기본 Secret 생성

새 설치의 namespace를 준비한 뒤 다음을 실행합니다.

```bash
kubectl --context "$LP_CONTEXT" apply -f deploy/base/namespaces/namespaces.yaml
KUBE_CONTEXT="$LP_CONTEXT" CODEX_AUTH_FILE="$CODEX_AUTH_FILE" scripts/create-secrets.sh
```

이 스크립트는 DB 비밀번호, webhook 토큰, 참조 서비스 자격을 생성하고 `liveprobe-llm` Secret에 AI 인증 파일을 전달합니다. 인증 파일이 없으면 빈 Secret을 만들고 진행할 수 있으므로, 앞 단계의 `test -s`를 생략하지 않습니다.

기존 Secret은 일반적으로 재사용하지만 **webhook Secret 쌍 중 하나가 없으면 두 쪽 토큰을 다시 맞추는 동작**이 있습니다. 기존 설치 갱신에 무조건 재실행하지 마세요. `--force`는 인증 갱신을 의도한 때만 사용합니다.

API의 첫 실행에서는 Secret의 `auth.json`을 쓰기 가능한 `liveprobe-codex` PVC로 복사합니다. 이후 CLI가 PVC 안에서 인증을 갱신합니다. **Secret만 교체하거나 Pod만 재시작해도 기존 PVC의 인증이 자동으로 바뀌지는 않습니다.** 재로그인 시 실행을 정지하고 해당 인증 홈만 안전하게 갱신해야 하며 DB/PVC 전체 삭제로 해결하지 않습니다.

### 5.3 모델 설정

K8s의 `liveprobe-config` ConfigMap 안 `config.yaml`과 `/settings`의 그룹 모델 설정을 확인합니다. 기본 모델 ID는 `liveprobe/app/config.py`에 있지만 모든 계정에서 사용 가능하다는 보장은 아닙니다.

자신의 모델로 변경할 때는 `llm.roles` 또는 `llm.provider_roles.codex_cli`뿐 아니라 그룹 모델·승격 순서·가격/품질 설정도 함께 맞춰야 합니다. `provider_roles`를 명시하면 해당 provider의 설정이 `roles`보다 우선합니다. 모델명을 한 곳에서만 바꾸고 모든 역할이 바뀐 것으로 판단하지 않습니다. 운영 시작 전 실제 역할 호출을 확인합니다.

## 6. Kubernetes 관제 설치

### 6.1 롤백 구성부터 준비

> ⚠️ **설치 스크립트가 만들지 않는 필수 항목** — 기본 API는 `rollback-ca` ConfigMap과 `rollback-action-grant` Secret을 필수로 마운트합니다. rollback-controller도 TLS Secret과 승인 이미지 정책을 요구합니다. `create-secrets.sh`만 실행한 상태에서 `install.sh`를 실행하면 이 항목 때문에 시작하지 못할 수 있습니다.

| 대상 | 준비해야 하는 값 |
|---|---|
| Secret `rollback-admission-tls` | `tls.crt`, `tls.key`. 인증서 SAN에 `rollback-controller.liveprobe-system.svc` 포함 |
| ConfigMap `rollback-ca` | 위 인증서를 검증할 CA 인증서 `ca.crt` |
| Secret `rollback-action-grant` | 별도로 생성한 서명 키 `key` |
| ConfigMap `rollback-image-policy` | 운영자가 확인한 order/inventory의 원복 이미지 digest. `immutable: true`로 관리 |
| Webhook `rollback-controller-image-guard` | `clientConfig.caBundle`에 위 CA의 base64 값 |

신규 설치에서, 조직 절차로 발급받은 인증서가 보호된 로컬 경로에 있다고 가정한 예입니다. 아래 경로는 설치자가 준비해야 하며 기존 파일이나 Secret을 덮어쓰지 않습니다.

<details>
<summary>조직 CA가 없는 격리된 로컬 실험: 자체 서명 인증서 준비 예</summary>

클러스터 내부 롤백 서비스에만 사용하는 예입니다. 외부 Ingress 인증서나 조직 PKI를 대신하지 않습니다. 다른 터미널에서도 실패 시 중단되도록 설정하며 기존 파일이 있으면 멈춥니다.

```bash
set -euo pipefail
LP_TLS_DIR="$HOME/.local/share/liveprobe/rollback-tls"
mkdir -p "$LP_TLS_DIR"
chmod 700 "$LP_TLS_DIR"
test ! -e "$LP_TLS_DIR/tls.key"
test ! -e "$LP_TLS_DIR/tls.crt"
test ! -e "$LP_TLS_DIR/ca.crt"
(umask 077; openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days 90 \
  -keyout "$LP_TLS_DIR/tls.key" -out "$LP_TLS_DIR/tls.crt" \
  -subj '/CN=rollback-controller.liveprobe-system.svc' \
  -addext 'subjectAltName=DNS:rollback-controller.liveprobe-system.svc')
cp "$LP_TLS_DIR/tls.crt" "$LP_TLS_DIR/ca.crt"
```

유효기간과 갱신 담당자를 정하고 아래 CA 마운트와 Webhook caBundle을 같은 인증서로 구성합니다. 개인 키는 외부에 공유하지 않습니다.

</details>

```bash
LP_TLS_DIR="$HOME/.local/share/liveprobe/rollback-tls"
test -s "$LP_TLS_DIR/tls.crt"
test -s "$LP_TLS_DIR/tls.key"
test -s "$LP_TLS_DIR/ca.crt"
test ! -e "$LP_TLS_DIR/grant-key"
(umask 077; openssl rand 48 > "$LP_TLS_DIR/grant-key")

kubectl --context "$LP_CONTEXT" -n liveprobe-system create secret tls rollback-admission-tls \
  --cert="$LP_TLS_DIR/tls.crt" --key="$LP_TLS_DIR/tls.key"
kubectl --context "$LP_CONTEXT" -n liveprobe-system create configmap rollback-ca \
  --from-file=ca.crt="$LP_TLS_DIR/ca.crt"
kubectl --context "$LP_CONTEXT" -n liveprobe-system create secret generic rollback-action-grant \
  --from-file=key="$LP_TLS_DIR/grant-key"
```

overlay에 `rollback-image-policy.yaml`을 추가하고 `resources`에서 참조합니다. 새 환경에서는 개발자의 예전 이미지 정책 파일을 재사용하지 않습니다.

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: rollback-image-policy
  namespace: liveprobe-system
immutable: true
data:
  order: registry.your-domain.example/liveprobe/order@sha256:<검증한-원복-digest>
  inventory: registry.your-domain.example/liveprobe/inventory@sha256:<검증한-원복-digest>
```

Webhook CA도 overlay의 패치로 저장하여 매번 렌더링에 포함합니다. 아래 코드는 **공개 CA 인증서만** 읽어 패치 파일을 만들며 개인 키는 읽지 않습니다.

```bash
python3 - "$LP_TLS_DIR/ca.crt" "$LP_OVERLAY/rollback-ca-patch.yaml" <<'PY'
import base64, pathlib, sys, yaml
patch = {
    "apiVersion": "admissionregistration.k8s.io/v1",
    "kind": "ValidatingWebhookConfiguration",
    "metadata": {"name": "rollback-controller-image-guard"},
    "webhooks": [{"name": "rollback-controller.liveprobe.invalid",
                  "clientConfig": {"caBundle": base64.b64encode(pathlib.Path(sys.argv[1]).read_bytes()).decode()}}],
}
pathlib.Path(sys.argv[2]).write_text(yaml.safe_dump(patch, sort_keys=False))
PY
```

`kustomization.yaml`의 `patches`에 `- path: rollback-ca-patch.yaml`을 추가합니다. 인증서 체인·서비스 DNS·유효기간을 확인하고 CA가 비어 있는 Webhook을 설치하지 않습니다. 나중에 원복 이미지를 바꾸면 새 이름의 immutable 정책을 만들고 controller 참조도 함께 바꿉니다.

### 6.2 렌더링·사전 확인·설치

기본 구성은 `liveprobe-target`의 참조 서비스까지 포함합니다. 이미지·TLS·Secret·도메인·스토리지 설정을 완료한 **신규 설치**에서 실행합니다.

```bash
python3 scripts/preflight.py --context "$LP_CONTEXT" --overlay "$LP_OVERLAY"
kubectl kustomize "$LP_OVERLAY" > /tmp/liveprobe-install.yaml
kubectl --context "$LP_CONTEXT" apply --dry-run=server -f /tmp/liveprobe-install.yaml

# 위 결과를 검토한 다음 실제 설치. 이미지를 registry에 준비했으므로 로컬 재빌드 생략.
KUBE_CONTEXT="$LP_CONTEXT" OVERLAY="$LP_OVERLAY" SKIP_BUILD=1 scripts/install.sh
```

`install.sh`는 Secret 준비, 렌더링 적용, rollout 대기, 참조 inventory DB 준비까지 수행합니다. **Application·dev/prd 전체 앱·Hubble·safe-action-executor를 일괄 설치하는 도구는 아닙니다.** 기존 클러스터 업그레이드에는 변경 리소스를 비교하고 적용해야 합니다.

### 6.3 화면 열기

설정한 Ingress 도메인으로 접속합니다. Ingress가 `/`는 `dashboard:80`, `/api`는 `liveprobe-api:8000`으로 보내는 구조입니다. **기본 dashboard nginx에는 API 프록시가 없으므로 대시보드 Service만 포트 포워딩하면 화면 틀은 보여도 API가 연결되지 않습니다.**

API만 먼저 확인할 때는 다음과 같이 로컬 포트 포워딩을 사용할 수 있습니다.

```bash
kubectl --context "$LP_CONTEXT" -n liveprobe-system \
  port-forward --address 127.0.0.1 service/liveprobe-api 8000:8000
# 다른 터미널: curl --fail http://127.0.0.1:8000/api/readyz
```

로컬 프런트 개발이 필요하면 위 API 연결을 유지하고 `dashboard`에서 `npm ci` 후 `npm run dev -- --host 127.0.0.1`을 실행합니다. Vite 개발 서버는 `/api`를 로컬 8000 포트로 프록시합니다. 실제 배포의 HTTPS Ingress에서도 웹과 API를 같은 출처로 제공합니다.

## 7. 관측 연결과 자동 복구 권한

### 7.1 어떤 관측 도구가 필요한가

| 도구 | 용도 | 설정 위치 |
|---|---|---|
| Prometheus + kube-state-metrics | 시계열 지표·워크로드 상태·경고 규칙 | `deploy/base/observability/prometheus`, `kube-state-metrics`, `LIVEPROBE_PROMETHEUS_URL` |
| Alertmanager | 경고를 LiveProbe webhook으로 전달 | `deploy/base/observability/alertmanager`, 양쪽 webhook Secret |
| Loki + Alloy | 수집 로그를 근거로 조회 | `deploy/base/observability/loki`, `alloy`, `LIVEPROBE_LOKI_URL` |
| Hubble 호환 Relay / Retina | 실제 서비스 흐름과 네트워크 drop/error 관측 | `LIVEPROBE_HUBBLE_RELAY`, `deploy/optional/flow-observer/values.yaml` |
| Istio/Envoy | 현재 참조 환경의 mesh 및 지연 지표 | `deploy/optional/istio`, 대상 환경별 scrape·정책 |

Grafana 서버/UI는 기본 설치에서 제외되어 있습니다. 기존 Grafana 데이터를 보존하기 위한 PVC 선언은 남아 있습니다. 이미지 이름에 `grafana/loki`, `grafana/alloy`가 보이는 것은 Grafana UI 설치를 의미하지 않습니다.

기존 Prometheus/Loki를 사용할 때는 **새 수집기를 무조건 중복 설치하지 말고** base 자원과 중복 대상을 검토한 뒤 URL·접근 정책·수집 라벨·경고 규칙을 맞춥니다. API URL만 바꾸면 해당 지표/로그가 자동으로 수집되는 것은 아닙니다.

Hubble/Retina는 기본 overlay에서 설치되지 않습니다. 현재 CNI·커널·아키텍처에 맞는 관측기를 별도로 설치합니다. 저장된 Retina values는 개발 환경용 TLS 비활성 예제이므로 다른 환경의 보안 설정에 맞춰야 합니다. 연결하지 않은 네트워크 데이터는 **미관측**이며 정상으로 간주하지 않습니다. 기존 CNI를 교체할 필요가 있는지 확인 없이 교체하지 않습니다.

### 7.2 자동 조치를 사용할 때

관제 API의 `liveprobe` ServiceAccount는 읽기 계정입니다. 복구 기능은 별도 실행 계정·대상 범위·admission 정책을 사용합니다.

- 타입 조치: `deploy/optional/safe-action-executor/`의 RBAC·정책·토큰 Secret과 API 마운트를 확인합니다.
- 운영자가 확인한 명령: `deploy/base/liveprobe/operator-executor.yaml`의 별도 실행기와 토큰을 사용합니다.
- 롤백: [6.1절](#61-롤백-구성부터-준비)의 controller·TLS·승인 이미지 정책을 사용합니다.
- 대상 Deployment의 `liveprobe.io/managed: "true"` 라벨, namespace·소유 관계, replica/메모리 상한, 독립 검증을 함께 확인합니다. 쓰기 권한을 주기 위해 API 읽기 계정을 관리자 계정으로 바꾸지 않습니다.

**기본 실행기 선언에는 `liveprobe-target` 범위가 포함되어 있으므로 dev/prd 또는 자신의 namespace에 그대로 적용했다고 복구 권한이 생기는 것은 아닙니다.** Role/RoleBinding·admission의 namespace 조건·NetworkPolicy·허용 대상과 API 마운트를 같은 범위로 구성하고 허용/거부를 모두 시험합니다.

```bash
# API 자체는 대상 Deployment 쓰기가 no여야 함
kubectl --context "$LP_CONTEXT" auth can-i patch deployments \
  --as=system:serviceaccount:liveprobe-system:liveprobe -n dev
# 설치자가 허용한 실행기는 허용 대상에서만 yes여야 함
kubectl --context "$LP_CONTEXT" auth can-i patch deployment/order \
  --as=system:serviceaccount:liveprobe-system:safe-action-executor -n dev
```

이 명령은 점검만 합니다. 기본 `assessment` 정책의 HIGH_RISK는 이관합니다. 건강 재확인은 두 번의 최신 관측으로 검증하며, 실제 조치가 없으면 **“정상 상태 확인 · 원인 미확정 · AI 조치 없음”**으로 구분합니다. 권한·데이터 안전 경계나 정해진 자율 복구 소진을 무제한 재시도로 우회하지 않습니다.

## 8. dev/prd 전체 테스트 시스템

### 8.1 함께 맞출 설정 파일

| 파일 | 변경할 내용 |
|---|---|
| `deploy/samsung/system-catalog.json` | 환경·시스템·서비스 소속. 활성 `dev/prd`, 퇴역 `stg` 구분 |
| `deploy/samsung/images.json` | 직접 빌드한 서비스별 실제 manifest digest |
| `deploy/samsung/application-profiles.json` | 서비스별 소스 루트·언어·시험 경로·이미지 저장소·업무 확인 경로 |
| `deploy/samsung/infra-template.json` | Kafka/Postgres 이미지, 저장소 및 기반 설정 |
| `deploy/environments/liveprobe-config.yaml` | K8s 감시 환경·의존성·모델·재분석 정책 |
| `deploy/environments/actions.json`, `apps.json`, `observability.json` | dev/prd 대상 권한·네트워크·수집 설정 |
| `liveprobe/app/systems.py` | 현재 참조 시스템의 표시·연계 정의. 완전히 다른 업무 시스템이면 코드의 고정 참조도 검토 |

`deploy/base`의 `liveprobe-config`는 여전히 **참조용 `liveprobe-target`**을 선택합니다. dev/prd를 사용하려면 `deploy/environments/liveprobe-config.yaml` 내용을 ConfigMap **`liveprobe-config`의 `data.config.yaml`**에 넣도록 사용자 overlay를 구성합니다. 이 파일 자체는 ConfigMap 매니페스트가 아니므로 `kubectl apply -f`로 직접 설치하지 않습니다. 변경 후 API를 재시작하고 `/api/status`에서 실제 감시 대상을 확인합니다. ConfigMap 읽기 Role과 로그 수집·지표 정책도 새 대상에 맞춰야 합니다.

### 8.2 서비스와 기반 준비

1. `dev`, `prd` namespace의 기존 리소스와 데이터가 있는지 확인합니다. 타인의 환경을 초기화하거나 namespace를 삭제하지 않습니다.
2. order/inventory 외에 catalog, payment, delivery, notification, samsung-domain 이미지를 해당 `services/<이름>/Dockerfile`에서 빌드합니다. samsung-domain 이미지는 `trade-member`, `delivery-member`, `business-api`가 모드를 나누어 사용합니다. 별도 `services/member` 소스도 공개되어 있지만 현재 이 세 역할의 대체 이미지로 임의 사용하지 않습니다.
3. DB·Kafka·TLS·환경별 Secret을 준비합니다. 메모리 전용 시험 데이터와 영속 DB 데이터의 보존 방식은 구분합니다.
4. 서비스별 digest와 프로필의 `image_repository`를 동일하게 맞춘 뒤 앱 선언을 렌더링합니다.

```bash
# 아래 기반 생성기는 본문에 명시한 환경 계약 불일치를 먼저 수정·검증한 후 실행
# 두 명령 모두 비밀 생성·클러스터 변경 없이 신규 파일에 선언만 렌더링
mkdir -p .e2e-tmp/install-review
python3 scripts/samsung_infra.py --render --context "$LP_CONTEXT" \
  --output .e2e-tmp/install-review/infra-review.json
python3 scripts/render-samsung-environments.py \
  --images deploy/samsung/images.json \
  --output .e2e-tmp/install-review/apps-review.json
```

**현재 신규 전체 설치의 제한:** `scripts/samsung_infra.py`의 `ENVS`에는 아직 `dev/stg/prd`가 고정되어 있습니다. 기반 생성기는 로컬 Docker의 이미지 존재와 저장소 등에도 가정이 있습니다. 현재 카탈로그는 dev/prd이므로 **그대로 실행하면 `--render` 단계부터 환경 계약 불일치로 중단됩니다.** 이를 피하려고 퇴역한 stg를 활성 카탈로그에 다시 넣거나 `--execute`를 바로 실행하지 않습니다. 새 dev/prd 설치에는 이 생성기의 환경·이미지·스토리지 검증을 자신의 구성으로 조정하고 관련 시험을 통과시키거나, 같은 Secret/DB/Kafka 계약을 운영자가 별도 준비해야 합니다. 과거 DB에 보존된 stg 데이터를 지우라는 뜻은 아닙니다.

`infra-review.json`은 `manifest`, `secret_refs` 등을 담은 묶음입니다. 전체 파일을 Kubernetes List로 간주해 `kubectl apply -f`에 넣지 않습니다. `apps-review.json`은 앱 선언이며 여기에 나오는 Secret·DB·정책이 실제로 준비됐는지 먼저 확인합니다. 전체 시스템의 신규 설치·데이터 이관·원복은 기본 설치 스크립트가 자동 수행하지 않습니다.

자세한 계약: [업무 기반](docs/operations/samsung-infra.md), [업무 시스템](docs/operations/samsung-domain.md). 이 문서의 이전 개발 환경 경로·stg 설명보다 **현재 소스와 위 dev/prd 구분**을 우선합니다.

## 9. Application 관제와 실행기

### 9.0 하나의 사건으로 연결하는 통합 모드

[통합 사건 설계](docs/plans/liveprobe-unified-incident-plan-2026-11-16.md)에 따라 하나의 관제실에서 K8s 분석과 코드 수정·시험·dev 검증·prd 승인을 이어가는 **선택적 통합 모드**를 추가했습니다. 기존 사건 번호·Application DB·원문 이력을 유지합니다. 새 설치에서는 통합 모드를 명시적으로 켭니다. 로컬 운영 반영과 검증 범위는 [통합 관제 작업 기록](docs/plans/unified-operator-console-2026-11-20.md)에 있습니다.

설치자는 다음을 별도로 준비해야 합니다.

- core API에 `LIVEPROBE_UNIFIED_ENABLED=true`, 설치마다 고유하고 재시작 후에도 같은 `LIVEPROBE_INSTALLATION_ID`를 설정합니다. 같은 출처 `/api/application/v1`에 기존 pull API를 제공하며 호스트 실행기로 들어가는 통로는 만들지 않습니다.
- core API에는 기존 Application DB 연결과 프로필·catalog를 설정하고, 루프백 운영자 게이트웨이의 조회·쓰기 키와 활성화할 실행기의 역할별 인증 파일을 각각 연결합니다. 기존 Application 모델 로그인 PVC는 별도 모델 작업자에 마운트합니다. 이 구성에서 API는 `APPLICATION_EXTERNAL_MODEL_WORKER=true`, `APPLICATION_WORKER_ENABLED=false`를 사용합니다. 두 DB와 기존 사건 번호를 유지하며 코어 마이그레이션 `0011`에서 연결 표를 추가합니다. [모델 작업자의 시작과 상태 확인](deploy/application/README.md#통합-api와-모델-작업자의-계정-분리)
- **writer는 하나씩**: 통합 모드로 전환하기 전 기존 Application API의 분석 worker와 같은 일감을 받는 legacy runner를 drain하고 결과 불명 쓰기를 조정합니다. 두 독립 worker를 무조건 동시에 켜거나 기존 배포를 삭제해서 전환하지 않습니다.
- 기계 identity는 소스, 배포, 브라우저 QA에 서로 다른 subject와 자격을 발급합니다. 각 주체는 기본 `runner`와 전용 범위 하나(`runner:source`, `runner:cluster`, `runner:qa`)를 가집니다. QA의 Chromium 설치와 dev 업무 검증 설정은 [실행기 설치 안내](deploy/application/README.md#배포-결정과-보존-경계)를 따릅니다. 소스 실행기는 `--role source`, 배포 실행기는 `--role cluster`로 시작합니다. 전자는 Kubernetes 읽기 전용 proxy만 사용하며 저장소 쓰기 자격을 가질 수 있습니다. 후자는 등록 대상 배포 자격만 가지며 Git 쓰기 자격을 주지 않습니다. 기존 배포 선언은 이 계정 분리를 자동 설치하지 않습니다. 운영에서는 별도 OS 사용자 또는 자격이 격리된 실행 환경을 사용하고, 같은 사용자 홈·Git 자격·쓰기 가능한 실행기 설정을 공유하지 않습니다.
- 서로 다른 실행기는 검증된 snapshots/artifacts/promotions 영수증을 제한된 파일 공유로 전달해야 합니다. 클러스터 계정에 소스 계정의 자격 디렉터리·Git 설정·Codex 인증을 공유하지 않습니다. 현재 파일 계약은 원격 산출물 전송 서비스를 제공하지 않으므로 실행 호스트/경로·소유권 검증이 필요합니다.
- 전용 루프백 관제 주소에서는 한 운영자가 조회·접수·dev 시험·prd 승인을 수행합니다. 서버는 각 변경의 게이트웨이 서명과 CSRF를 확인하고, 시험과 승인을 별도의 결정으로 기록합니다. 게이트웨이 서명 키와 조회 키는 서로 다르며 사람 화면에 노출되지 않습니다. [로컬 운영자 관제 설치](deploy/application/README.md#루프백-운영자-관제)
- `APPLICATION_REPOSITORIES_FILE`은 설치자가 확인한 repositories.json 경로입니다. 현재 파일의 예시 조직·비공개 저장소 이름을 자신의 실제 등록 저장소로 바꿉니다. `APPLICATION_GITHUB_READ_TOKEN_FILE`은 private repo 병합 조회 전용 자격 파일입니다. 파일 내용은 소스·이미지·문서·URL에 넣지 않습니다. 소스 runner의 PR 작성 자격과 분리합니다.

코드 조치의 재현·회귀 시험 후 소스 계정은 `python -m liveprobe.application_runner.publish_pr --help`에 나온 명시 명령으로 **시험한 영수증의 PR만** 게시할 수 있습니다. 원래 main commit과 시험 소스 지문을 증명하지 못하면 게시하지 않으며 자동 병합하지 않습니다. 응답 유실 후에는 조회로만 조정하고 push/PR 생성을 무조건 다시 보내지 않습니다. 현재는 큐의 자동 PR 게시 작업이 아니므로 source runner 운영 절차에서 이 명령을 수행해야 합니다.

새 통합 사건은 dev 업무 화면의 브라우저 QA 영수증을 사용합니다. 기존 `dev-prd/v2` 사건은 당시 시험자가 남긴 시험 기록을 사용합니다. 관제실에서 등록된 PR의 사람 병합을 확인한 뒤 prd 승인자는 시험한 버전과 변경 계획을 확인하고 승인합니다. 서버는 등록된 GitHub 저장소의 병합·현재 main·실제 소스 파일 지문을 대조합니다. 새 통합 사건은 prd 배포 후 원래 실패한 업무의 복구를 관측해야 종료됩니다. 새 승격 환경은 dev·prd이며 기존 stg 기록은 조회할 수 있습니다. [QA·승인·복구의 판정 기준](docs/plans/unified-operator-console-2026-11-20.md#운영자가-보는-사건-흐름과-api-계약)

운영 전환은 설계 P5의 별도 인수 대상입니다. 비활성화해도 진행 중 조치를 미실행으로 바꾸지 않으며 연결·양쪽 DB·PVC를 남깁니다. 기존 코드 조치가 있으면 다른 K8s 변경을 차단하고 drain/reconcile 후 원복합니다. 아래 9.1–9.3은 기존 독립 배포의 설치 경로입니다.

### 9.1 독립 API·DB·인증

Application은 K8s API 안에서 자동 활성화되지 않습니다. 별도 API `liveprobe-application-api`, DB `liveprobe-application-db`, 인증 PVC `liveprobe-application-codex`를 구성합니다. 이전 독립 배포의 전용 DB와 실행기 자격을 유지합니다. 통합 운영자는 9.0절의 루프백 관제 주소를 사용합니다.

`liveprobe/Dockerfile.application`의 `FROM`을 [4장](#4-이미지-빌드와-배포-주소)에서 만든 **자신의 API runtime digest**로 바꾸어 Application 이미지를 빌드합니다. 비root 사용자 `65532:65532`, 읽기 전용 rootfs, 8080 포트 설정은 유지합니다. 빌드할 때는 인증 파일이 없는 별도 context를 사용합니다.

```bash
# 실제 레지스트리에서 확인한 Application 이미지 주소로 바꿔 입력
export LP_APP_IMAGE='registry.your-domain.example/liveprobe/application@sha256:<실제-digest>'
python3 scripts/render-application-deploy.py --image "$LP_APP_IMAGE" \
  --out .e2e-tmp/install-review/application
python3 scripts/render-application-deploy.py --image "$LP_APP_IMAGE" \
  --out .e2e-tmp/install-review/application --check
```

렌더러의 기본값은 `local-path`, `traefik`, `application.liveprobe.localhost`입니다. **기본 플랫폼 overlay의 값이 Application에 자동 전파되지 않습니다.** 자신의 StorageClass, Ingress/TLS, ingress controller namespace/라벨, registry, DNS와 egress에 맞게 별도 overlay/렌더러를 조정합니다. 생성 결과를 수동 편집했다면 원본 렌더러의 `--check`와 일치하지 않으므로 배포용 패치와 원본을 구분합니다.

필수 환경 변수는 다음과 같습니다. 생성된 `30-api.yaml`에서 확인할 수 있습니다.

| 변수 | 내용 |
|---|---|
| `APPLICATION_DATABASE_URL_FILE` | 전용 DB URL 파일 |
| `APPLICATION_DATABASE_ISOLATED` | `true` |
| `APPLICATION_MACHINE_IDENTITY_FILE` | 실행기 전용 인증 JSON |
| `APPLICATION_IDENTITY_FILE` | 기존 독립 배포의 사람 자격 호환 파일. 신규 설치는 빈 목록을 제공합니다. |
| `APPLICATION_PROFILES_FILE` / `APPLICATION_CATALOG_FILE` | API와 호스트 실행기가 동일하게 사용하는 프로필·카탈로그 |
| `APPLICATION_CODEX_HOME` | `/var/lib/liveprobe/application-codex` 전용 쓰기 가능한 인증 홈 |
| `APPLICATION_WORKER_ENABLED` | `true` |
| `APPLICATION_MODEL`, `APPLICATION_<역할>_MODEL`, `APPLICATION_<역할>_EFFORT` | 실제 사용 가능한 모델·추론 강도. 공통 모델을 바꾸면 역할별 override도 확인 |

신규 Secret 세 개는 다음 도구로 준비합니다. 기존 Secret이 있거나 조회 결과가 불명확하면 의도적으로 중단하는 **최초 생성 전용** 도구입니다.

```bash
python3 scripts/provision-application-secrets.py --context "$LP_CONTEXT" --dry-run
# 실제 신규 생성. 생성되는 토큰 경로만 출력되며 값은 출력하지 않음.
python3 scripts/provision-application-secrets.py --context "$LP_CONTEXT"
```

운영자는 로컬 관제 주소에서 시험·병합 확인·승인을 각각 기록합니다. 최초 설치 도구는 실행기용 `application-runner` 토큰 한 개를 `.e2e-tmp/application/application-runner.token`에 보관합니다. 게이트웨이 키는 아래 연결 절차에서 준비합니다.

환경에 맞춘 최종 매니페스트를 서버 dry-run 후 설치합니다. 아래 경로가 실제로 검토·수정한 배포 파일을 가리키는지 확인합니다.

```bash
LP_APP_MANIFEST_DIR='.e2e-tmp/install-review/application'
kubectl --context "$LP_CONTEXT" create --dry-run=server -f "$LP_APP_MANIFEST_DIR"
# 충돌·권한·설정 확인이 끝난 신규 설치에서만 실행
kubectl --context "$LP_CONTEXT" create -f "$LP_APP_MANIFEST_DIR"
kubectl --context "$LP_CONTEXT" -n liveprobe-system rollout status \
  statefulset/liveprobe-application-db --timeout=300s
kubectl --context "$LP_CONTEXT" -n liveprobe-system rollout status \
  deployment/liveprobe-application-api --timeout=300s
```

최초 설치에 사용하는 `kubectl create`가 부분 성공하면 이미 만든 자원·Secret·PVC를 조사하고 보존합니다. 실패했다고 Secret을 삭제하거나 무조건 `apply`로 덮어쓰지 않습니다. 구체적인 Secret 키·권한 목록은 [Application 배포 계약](deploy/application/README.md)에 있습니다.

인증 PVC는 UID 65532가 쓰고 갱신할 수 있도록 스토리지 권한을 미리 준비합니다. API Pod가 시작된 후 그 전용 경로에 본인 계정으로 로그인할 수 있습니다.

```bash
kubectl --context "$LP_CONTEXT" -n liveprobe-system exec -it \
  deployment/liveprobe-application-api -c api -- \
  env CODEX_HOME=/var/lib/liveprobe/application-codex \
  codex -c 'cli_auth_credentials_store="file"' login --device-auth
```

인증 홈 쓰기 실패를 컨테이너 전체 root 실행으로 우회하지 않습니다. `APPLICATION_CODEX_HOME`만 설정했다고 실제 로그인까지 완료된 것은 아닙니다.

### 9.2 호스트 실행기를 계속 실행해야 함

Application의 수집·시험·빌드·배포 실행기는 **호스트 프로세스**입니다. API Pod만 Ready여도 실행기가 없으면 `/api/application/v1/readyz`의 `runner`가 false입니다. 터미널 종료나 재부팅 후에도 동작하도록 자신이 쓰는 서비스 관리자에 실행기와 연결 프로세스를 등록합니다. 실행 상태 디렉터리는 임시 폴더가 아닌 영속 경로로 보존합니다.

다음 두 연결은 별도 터미널/서비스로 유지합니다.

```bash
# Application API를 호스트에 연결
kubectl --context "$LP_CONTEXT" -n liveprobe-system port-forward \
  --address 127.0.0.1 service/liveprobe-application-api 18081:8080
```

```bash
# 실행기 전용 SA의 제한된 신원으로만 Kubernetes 연결
# 설치자는 해당 SA impersonation 권한이 있어야 함. 관리자 신원 proxy로 대체하지 않음.
kubectl --context "$LP_CONTEXT" \
  --as=system:serviceaccount:liveprobe-system:liveprobe-application-runner \
  proxy --address=127.0.0.1 --port=18082
```

`LP_APP_STATE` 아래 `runtime.json`을 준비합니다. 다음은 **형식 예시**이며 digest·소켓 경로·`platform`을 실제 환경으로 바꿉니다. 현재 실행기는 platform을 생략하면 `linux/arm64`를 선택하므로 amd64 노드에서는 반드시 명시합니다.

```json
{
  "kube_api": "http://127.0.0.1:18082",
  "docker_host": "unix:///var/run/docker.sock",
  "platform": "linux/amd64",
  "test_images": {
    "java": "your-java-test-image@sha256:<실제-digest>",
    "python": "your-python-test-image@sha256:<실제-digest>",
    "node": "your-node-test-image@sha256:<실제-digest>",
    "domain": "your-domain-test-image@sha256:<실제-digest>"
  },
  "database_image": "your-postgres-test-image@sha256:<실제-digest>"
}
```

시험 이미지는 프로필이 요구하는 Maven·Node·Python 의존성을 미리 갖춰야 합니다. 실행기는 격리된 환경에서 시험하며 테스트 중 인터넷 다운로드에 기대지 않습니다. 원본 서비스의 배포 이미지 digest도 호스트에서 실제 관측 가능해야 합니다.

```bash
export LP_APP_STATE="$HOME/.local/state/liveprobe-application"
mkdir -p "$LP_APP_STATE"
chmod 700 "$LP_APP_STATE"
# 위 형식을 참고해 $LP_APP_STATE/runtime.json을 먼저 준비

python3 scripts/liveprobe-application-runner.py run \
  --state-dir "$LP_APP_STATE" --runtime-config "$LP_APP_STATE/runtime.json" \
  --api-url http://127.0.0.1:18081 \
  --machine-token-file .e2e-tmp/application/application-runner.token
```

다른 터미널/서비스에서 지속 감지도 실행합니다.

```bash
python3 scripts/liveprobe-application-runner.py watch \
  --state-dir "$LP_APP_STATE" --runtime-config "$LP_APP_STATE/runtime.json" \
  --api-url http://127.0.0.1:18081 \
  --machine-token-file .e2e-tmp/application/application-runner.token
```

카탈로그가 바뀌면 API ConfigMap과 호스트 파일을 같은 내용으로 갱신합니다. `image_repository`와 `images.json` 주소도 일치해야 합니다. source→수정 전 실패→수정 후 통과→회귀 시험→실제 이미지→dev 업무 검증을 확인한 뒤 사용자 시험과 prd 승인으로 진행합니다.

**다중 노드·원격 클러스터 제한:** 현재 Application 실행기는 로컬 Docker의 이미지·RepoDigest 관측과 공유 이미지 저장소 경로를 전제로 합니다. 새 빌드 결과를 임의의 레지스트리에 자동 게시하고 모든 원격 노드에 반입하는 범용 단계는 제공하지 않습니다. 이 배포 경로를 별도로 구현·검증하기 전에는 “외부 Kubernetes에서 자동 코드 수정·배포까지 설치 완료”로 판단하면 안 됩니다. 관제 API 설치와 이 실행기 이식 작업은 구분합니다.

## 10. 설치 완료 확인

도메인을 자신의 값으로 바꾸고, HTTPS를 구성했다면 `https://`로 확인합니다. `/healthz`만 성공하거나 화면만 뜨는 것은 완료 기준이 아닙니다.

```bash
kubectl --context "$LP_CONTEXT" -n liveprobe-system get pods,pvc
kubectl --context "$LP_CONTEXT" -n liveprobe-observability get pods,pvc

curl --fail http://liveprobe.your-domain.example/api/healthz
curl --fail http://liveprobe.your-domain.example/api/readyz
curl --fail http://liveprobe.your-domain.example/api/status
curl --fail http://liveprobe.your-domain.example/api/policy/profile
curl --fail http://liveprobe.your-domain.example/api/llm/accounts

# Application을 설치했다면
curl --fail http://application.your-domain.example/api/application/v1/healthz
curl --fail http://application.your-domain.example/api/application/v1/readyz
```

- [ ] API·DB·관측 Pod가 준비되고 PVC가 Bound다.
- [ ] K8s `/readyz`에서 DB와 백그라운드 처리기가 준비됐다. 실제 AI 역할 호출도 성공했다.
- [ ] 지도에 자신의 대상 서비스가 표시되고 지표·로그·경고·네트워크 흐름의 수집 여부가 구분된다.
- [ ] 활성 감시·실행 대상이 dev/prd 또는 의도한 namespace이며 stg에 신규 처리를 하지 않는다.
- [ ] 실행 중 진행 상황과 공개 답변이 화면에 도착하고, 원문은 사용자가 펼칠 때만 표시된다.
- [ ] 무해한 dev 시험 한 건에서 감지→분석→허용된 조치/정상 확인→검증→이력 저장을 확인했다.
- [ ] 실제 권한·데이터 안전 경계는 자동으로 넘지 않으며, API 읽기 계정은 쓰기가 거부된다.
- [ ] Application을 설치했다면 `database`, `human_identity`, `machine_identity`, `profiles`, `runner`, `model`이 모두 준비됐다. 실제 역할 호출과 dev 빌드·업무 검증도 성공했다.
- [ ] Application의 prd 승인은 사용자 시험과 별도 승인 없이 진행되지 않는다.
- [ ] 재시작 후 DB 이력·인증 갱신·실행기 상태가 보존된다.

이 저장소의 과거 시험 수치나 개발 클러스터의 정상 상태는 **자신의 설치 완료 증거를 대신하지 않습니다.**

## 11. 자주 막히는 지점

| 증상 | 먼저 확인할 것 |
|---|---|
| `ImagePullBackOff` | 개발자 로컬 digest를 그대로 썼는지, registry 권한·아키텍처·노드 접근 가능 여부 |
| PVC Pending / 인증 홈 쓰기 실패 | StorageClass, 용량, 접근 모드, Application UID 65532의 볼륨 권한 |
| API Pod 시작 실패 | DB Secret 외에 `rollback-ca`, `rollback-action-grant`가 준비됐는지 |
| 롤백이 거부됨 | TLS SAN·CA·만료, Webhook caBundle, immutable 이미지 정책, 승인 grant, 대상 범위 |
| Ingress는 열리는데 API 403 | `LIVEPROBE_ALLOWED_HOSTS`, 웹/API 동일 출처, 프록시 Host 헤더 |
| 로그인했는데 AI가 동작하지 않음 | 실제 사용하는 PVC 인증 홈, 모델 권한·CLI 버전, 인증 갱신, 계정 제한·egress. 인증 파일 존재만으로 성공 판정하지 않음 |
| 설치 후 모든 사건이 이관됨 | 모델 오류, 근거 수집 실패, 실행기 토큰·RBAC·대상 정책 누락, 실제 안전 경계 여부 |
| Application `/readyz` 503 | 응답의 `checks`를 확인. `runner:false`이면 호스트 실행기·API 포워딩·전용 proxy·카탈로그 일치 여부 점검 |
| 지표/로그가 비어 있음 | 수집기와 대상 namespace/라벨, endpoint URL, NetworkPolicy, scrape 규칙 |
| 네트워크 지도 미관측 | Hubble 호환 Relay 설치·주소·프로토콜·수집기 권한. 미관측을 장애 없음으로 해석하지 않음 |
| Git pull 후 화면이 그대로 | 소스 pull은 실행 이미지 교체가 아님. 빌드·digest 갱신·배포·rollout 확인 필요 |
| 과거 기록의 원문 파일 링크가 없음 | 운영 원본·스크린샷·개인 작업 기록은 공개본에서 제외됨. 소스 결함과 구분 |

진단을 공유할 때 Secret 본문, `auth.json`, 원본 Pod 환경 변수, DB URL을 붙이지 않습니다. 필요한 상태·오류 분류만 비밀값을 제거해 공유합니다.

## 12. 업데이트·백업·개발

### 업데이트와 데이터 보존

1. 자신의 overlay·이미지 digest·모델 설정·인증/실행기 설정을 보존합니다.
2. DB와 영속 볼륨을 백업하고 복구 가능성을 확인합니다. 기존 자료는 삭제하지 않습니다.
3. `git pull --ff-only origin main` 후 변경 범위와 마이그레이션을 확인합니다.
4. 새 이미지를 빌드·게시하고 실제 digest를 overlay에 반영합니다. 롤백 승인 이미지는 별도로 검토합니다.
5. 변경 리소스만 검토·적용하고 rollout, `/readyz`, UI, AI 호출, 업무 동작을 확인합니다.

K8s API는 시작 때 DB 마이그레이션을 실행하고 Application은 독립 저장소를 초기화합니다. 이미지 원복만으로 DB 스키마 원복까지 보장하지 않습니다. `scripts/uninstall.sh`나 namespace/PVC 삭제는 일반적인 업그레이드·복구 방법이 아닙니다.

`deploy/retired/`의 Grafana·stg 자료는 보존/복구 참고용입니다. 현재 설치 경로에 임의로 다시 포함하지 않습니다. Docker 런타임 패치 파일도 특정 개발 환경의 장애 대응 기록이며 다른 Docker 버전에 그대로 적용하는 설치 단계가 아닙니다.

### 로컬 개발과 검증

```bash
(cd dashboard && npm ci && npm test && npm run build)
(cd liveprobe && uv sync --frozen --group dev)

# 운영 DB가 아닌 CREATEDB 권한을 가진 전용 시험 DB를 먼저 준비
# LIVEPROBE_TEST_DB_ADMIN에는 그 시험 DB의 연결을 보호된 환경으로 설정
(cd liveprobe && uv run pytest)
```

운영 자료를 공개하지 않았으므로 `docs/evidence`, `.e2e-tmp` 원본에 의존하는 과거 결과 재생성·일부 회귀 시험은 clean checkout에서 추가 자료가 필요할 수 있습니다. 실패를 피하려고 운영 DB를 시험 DB로 연결하지 않습니다. `chaos/`, `scripts/eval-real.sh`, fixture 배포 명령은 실제 장애·비용을 발생시킬 수 있으므로 설치 확인 과정에서 자동 실행하지 않습니다.

### 참고 문서와 소스

- [공개 범위와 기존 저장소 연동](docs/operations/github-repositories.md) · [보안 경계](SECURITY.md)
- [플랫폼 파라미터 연결](deploy/components/platform/kustomization.yaml) · [이식 예제](deploy/overlays/portable-example/kustomization.yaml)
- [Application 배포 상세](deploy/application/README.md) · [업무 기반](docs/operations/samsung-infra.md)
- [시스템 구조도](docs/diagrams/liveprobe_agent.drawio) · [초기 과제계획서](과제계획서.md)
- [개발 환경 실제 운영 기록](docs/operations/liveprobe-real-operation-dev-prd-2026-11-13.md) · [조기 이관 개선](docs/operations/liveprobe-premature-handoff-2026-11-14.md)

초기 과제계획서는 초안입니다. 현재 동작은 소스·배포 설정·검증 기록을 함께 확인하세요. **이 README 변경은 설치 설명을 정리한 것이며 클러스터를 새로 배포하거나 기존 실행 상태를 바꾸지 않습니다.**
