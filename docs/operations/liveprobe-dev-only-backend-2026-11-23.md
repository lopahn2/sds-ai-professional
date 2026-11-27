# 장애 비교 기간의 dev 관제 범위

비교 기간에는 dev를 활성 환경으로 두고 prd의 저장 사건은 이력에서 조회한다. 기본 배포 설정의 dev·prd 활성 구성을 유지하고, 이번 실행 환경의 LiveProbe ConfigMap만 전환한다.

```yaml
target_namespaces: [dev]
historical_namespaces: [stg, prd]
```

실행 중인 API Deployment에서 `config.yaml`을 제공하는 ConfigMap 이름을 확인한다. 해당 키의 다른 내용을 보존하고 위 두 목록만 갱신한 뒤 API를 재기동한다. Application 모델 작업자도 동일한 ConfigMap을 `/etc/liveprobe/config.yaml`에 읽기 전용으로 마운트한 뒤 재기동한다. 별도 경로를 쓰는 경우 `LIVEPROBE_CONFIG_FILE`에 그 경로를 지정한다. API `/api/status`의 활성·이력 환경과 `/api/unified/catalog`의 새 Application 접수 환경이 일치하는지 확인한다. PRD Pod를 중지하기 전 기존 분석, 쓰기 임대와 배포 조치의 안전한 종료를 확인한다.

dev 및 공유 인프라의 경고는 계속 표시한다. 이력 전용 환경은 새 K8s 이벤트, 엔드포인트 감시, Loki 오류, Alertmanager, 신호 최종 저장과 라이브 지도에서 제외한다. 기존 PRD 사건을 시작할 때 다시 작업 큐에 넣지 않는다. 중단 가능성이 있는 과거 쓰기의 결과 확인은 기존 조정 경로가 처리한다. Application은 새 PRD 사건과 승인 요청을 거부하고 PRD 관측·분석·배포 대기 작업을 보류한다. 저장된 사건·감사·기존 승인과 Job 기록을 조회할 수 있다.

PRD를 정상 기동하고 준비 상태와 기존 데이터 연결을 확인한 뒤 PRD를 활성 목록으로 옮기고 이력 전용 목록에서 제거한다. 두 프로세스에 같은 변경을 반영한다. 기존 대기 작업은 원래 임대·기한·권한 조건으로 다시 판단한다.

격리 SQLite 회귀에서 PRD 신호 제외, dev 수신 유지, PRD 보존 이력과 재개 억제, 공유 인프라 경보, Application 접수·배포 보류 및 dev 수집을 검증한다. [Kubernetes 회귀](../../liveprobe/tests/test_paused_environment.py) · [Application 회귀](../../liveprobe/application_tests/test_application_paused_environment.py)

## dev 재고 이미지 장애 사전 확인

실행 중인 `dev/inventory`에서 원하는 복제본, Ready Pod, 가용 복제본, Ready EndpointSlice 항목이 각각 1개였다. Deployment는 `RollingUpdate`의 `maxSurge=1`, `maxUnavailable=0`, 이력 한도 10을 사용한다. HPA와 Application 쓰기 잠금이 없었다. 실제 설정은 보존하는 `liveprobe-data`에서 dev 데이터베이스를 사용한다.

활성 API는 `target_namespaces=[dev]`, 자동 조치 모드 `auto`, 안전 프로필 `assessment`이며 `rollout_undo`를 허용한다. 별도 롤백 컨트롤러의 권한 조회에서 `dev/inventory` 조회·패치 및 dev ReplicaSet 조회가 확인됐다. 승인된 현재 이미지와 준비 완료 ReplicaSet 리비전 31을 읽고, 이미지 하나와 다음 리비전 값만 바꾼 로컬 복사본을 롤백 계획 함수에 전달했다. 함수는 배포 버전과 실패 이미지를 검사한 뒤 해당 이미지 필드만 승인된 리비전 31 이미지로 되돌리는 패치를 반환했다. API Pod의 공개 CA로 롤백 서비스의 인증서 체인·서버 이름을 확인했다. 이 사전 확인에서 `/undo` 요청이나 시험 주입을 실행하지 않았다. [롤백 계획과 승인 경계](../../liveprobe/app/rollback_controller.py) · [자동 조치 정책](../../liveprobe/app/safety.py)

## 단독 모델 비교 중 dev 워크로드 임대

단독 모델이 직접 조사·복구하는 동안 동일한 `dev/<workload>` 신호만 일시 보류한다. 이미 진행·승인·조정 중인 같은 대상 작업이나 다른 임대가 있으면 임대를 거부한다. 획득과 사건 입력은 같은 PostgreSQL 트랜잭션 잠금을 사용한다. 다른 dev 워크로드와 다른 namespace는 계속 수신한다. 옵션 없는 기존 전역 `lock` 및 `unlock` 명령은 기존 시험용으로 유지한다.

`python -m app.evalrun lock --namespace dev --workload inventory --owner F1-01-single-terra --ttl 600`을 관제 API Pod에서 실행한다. 결과 JSON의 토큰은 실행 중 메모리에 보관한다. 동일한 범위와 소유자로 `lock-status`와 `unlock`을 실행할 때 `--token-stdin`을 사용한다. 상태 및 해제 명령은 토큰을 출력하지 않는다. 원복과 독립 검증이 끝난 뒤 해제한다. 임대 최대 기간은 900초이며, 만료 토큰으로 다시 발급된 임대를 해제할 수 없다.

자동 감지와 별도로 이미 존재하는 인계 사건과 운영자 요청은 수정하지 않는다. 비교 실행기는 임대 소유권, 기준선, 작업 결과를 각각 확인한다. 동시 획득 2개 중 1개만 성공, 범위 외 신호 입력, 만료·재발급 뒤 오래된 토큰 거부, 옵션이 빠진 CLI 요청 거부를 새 SQLite에서 검증했다. 이 문서의 임대 시험은 배포나 운영 DB에 임대를 생성하지 않았다. [실행기 계약](../../liveprobe/app/evalrun.py) · [임대와 신호 입력 검사](../../liveprobe/app/evalguard.py) · [회귀](../../liveprobe/tests/test_evaluation_workload_lease.py)

## 2026-11-23 로컬 시험 환경 실제 전환

진행 중이던 복합 주문의 모델 비교와 네이티브 시험을 완료한 뒤 전환했다. 기존 ConfigMap의 설정을 보존하고 임시 복사본 `liveprobe-config-naria-dev-benchmark-20261123`을 만들었다. 기존 API 이미지로 같은 Deployment를 롤아웃했다. 4175에서 dev만 활성이고 PRD 사건 목록은 계속 HTTP 200으로 열렸다. 실제 이미지에서 직접 감시, Loki 수집과 웹훅의 dev 수신 유지 및 PRD 신규 경보 제외를 확인했다. [설정 전환 기록](../../.hermes/evidence/liveprobe-comparison-2026-11-23/dev-only-api-config-rollback.json) · [가동 조회](../../.hermes/evidence/liveprobe-comparison-2026-11-23/dev-only-runtime-status.json)

PRD의 `business-api`, `delivery-member`, `inventory`, `trade-member`, `order`, `catalog`, `delivery`, `notification`, `payment`의 복제본을 각각 1에서 0으로 조정했다. 앞의 네 서비스는 공용 DB에 의존한다. 프로세스 메모리를 쓰는 나머지 다섯 서비스에서는 조회 가능한 초기 업무 상태만 관측됐으며, 종료 전 두 번씩 조회해 응답 18개와 지문을 보존했다. 재기동하면 메모리의 시각·카운터 및 조회할 수 없는 일시 상태는 초기값으로 시작한다. 복제본과 Deployment UID를 기록했다. 기존 PRD 사건 125건이 남아 있고 전환 뒤 새 PRD 사건은 0건이었다. dev 준비 복제본은 14/14였고 공용 데이터·메시징·관제·관측 PVC 12개는 모두 `Bound`였다. [DB 서비스 복구 기준](../../.hermes/evidence/liveprobe-comparison-2026-11-23/prd-benchmark-db-backed-rollback.json) · [메모리 서비스 복구 기준](../../.hermes/evidence/liveprobe-comparison-2026-11-23/prd-benchmark-seed-services-rollback.json) · [조회 자료 지문](../../.hermes/evidence/liveprobe-comparison-2026-11-23/prd-seed-api-archive/manifest.json) · [전환 뒤 검증](../../.hermes/evidence/liveprobe-comparison-2026-11-23/prd-benchmark-post-scale.json)

PRD Pod가 모두 종료된 뒤 노드는 2,709m/4,000m CPU와 7,586MiB/11,945MiB 메모리를 기록해 기존 게이트를 통과했다. 다음 표본의 CPU는 2,993m였다. 주입 직전 같은 기준을 다시 확인한다. [종료 뒤 자원 계측](../../.hermes/evidence/liveprobe-comparison-2026-11-23/dev-benchmark-node-post-prd-shutdown.json)

자동 경보를 차단하는 임시 ConfigMap을 적용한 뒤, 로컬 기존 이미지를 바탕으로 검증한 API 코드만 덧씌운 새 이미지를 만들었다. 네트워크 없이 모듈·계약을 시험한 뒤 같은 dev 전용 설정으로 API를 롤아웃했다. 4175는 준비 상태이고 Application 접수 환경은 dev, 공통 이력은 dev·stg·prd다. 별도 Application 모델 작업자는 이번 이미지 전환에 포함되지 않았다. [로컬 레이어 지문](../../.hermes/evidence/liveprobe-comparison-2026-11-23/dev-only-scoped-layer-sources.json) · [이전 API 이미지와 전환 기록](../../.hermes/evidence/liveprobe-comparison-2026-11-23/dev-only-scoped-api-image-rollback.json) · [새 API 실조회](../../.hermes/evidence/liveprobe-comparison-2026-11-23/dev-only-scoped-api-live-status.json) · [전환과 시험 범위](liveprobe-dev-only-comparison-gate-2026-11-23.md)

범위 임대를 사용한 단독 모델 비교가 끝난 뒤 이미지 원인 지침을 보완한 API를 배포했다. 같은 dev 설정으로 다시 준비됐고, 동일 장애 [#1651](http://127.0.0.1:4175/incidents/1651)이 자동 복구됐다. 보완 이미지의 되돌릴 지문은 별도 기록에 있다. [API 두 번째 전환 기록](../../.hermes/evidence/liveprobe-comparison-2026-11-23/dev-image-grounding-api-rollback.json) · [보완 API 실조회](../../.hermes/evidence/liveprobe-comparison-2026-11-23/dev-image-grounding-api-live-status.json)

주문·재고에 대한 다섯 방식 비교가 종료되고 분석과 쓰기가 없는 시점에, `dev/lp-eval-kw04`만 추가 감지하는 API 후보를 적용했다. 활성 범위는 dev, 이력은 stg·prd다. 이전 이미지와 ConfigMap을 원복 기준으로 보존했다. 해당 시험이 종료된 뒤 PRD 신규 사건 0건, PRD Pod 0개, 기존 PRD 사건 125건, PVC 12/12 바인딩과 시험 임대 0건을 확인했다. [API 전환 기록](../../.hermes/evidence/liveprobe-comparison-2026-11-23/dev-workload-api-rollout-result.json) · [종료 검증](../../.hermes/evidence/liveprobe-comparison-2026-11-23/dev-after-replica-comparisons.json)

## 2026-11-24 dev 전용 범위 재검증

PRD Deployment 9개는 모두 복제본 0이었다. Pod, Job, CronJob, HPA도 0개였고 Service 9개와 Ingress 8개는 복원에 활용할 수 있도록 유지했다. 직접 PRD 신호는 기존 게이트에서 걸러졌다. 병렬 소스 검토에서 Hubble에 이력 환경이 통신 상대인 흐름으로 dev `NetworkDrop`이 열릴 수 있는 경로를 찾았다. Hubble 전용 경보 판단에 지도와 같은 이력 환경 경계를 적용했다. 활성 환경과 일반 외부 통신의 흐름, dev HTTP·애플리케이션 오류는 감시한다. PRD 제거로 실제 오탐이 생겼다는 관측 기록은 확인되지 않았다. [PRD 자원 조회](../../.hermes/evidence/liveprobe-comparison-2026-11-23/prd-runtime-inventory-20261124T061226153295Z.json) · [Hubble 필터](../../liveprobe/app/network_signals.py)

별도 Application 모델 작업자가 API의 환경 설정을 마운트하지 않아 기본 dev·prd를 사용하고 있었다. 두 dev 실장애 비교와 임대 해제가 끝난 뒤, 이미 적용된 dev 범위 ConfigMap의 `config.yaml`을 작업자에도 읽기 전용으로 마운트했다. 이전 이미지에 이력 환경의 분석 접수와 추가 수집 만료를 건너뛰는 세 지점만 반영했다. 기존 API에는 Hubble 변경 한 파일을 반영했다. 두 후보를 네트워크 없이 기동해 PRD 제외, dev 분석 접수, 대기 PRD 기록 유지와 외부 통신 관측을 확인했다. 환경 회귀는 58개 통과, DB를 요구하는 6개는 건너뛰었다.

실제 두 Deployment가 각 1/1 준비 상태를 회복했고 API·Application 작업자 모두 dev만 활성, stg·prd를 이력으로 읽었다. Application 작업자의 분석·배포·수집 임대는 0건이었다. 전환 전에 사용한 이미지와 볼륨 참조를 보존했다. [전환 직전 기록](../../.hermes/evidence/liveprobe-comparison-2026-11-23/dev-prd-scope-rollout-before-20261124T062217254474Z.json) · [적용 결과](../../.hermes/evidence/liveprobe-comparison-2026-11-23/dev-prd-scope-rollout-result-20261124T062217254474Z.json)

### Application 한글 해석 작업의 환경 경계

로컬 회귀에서 dev만 활성인 상태에서도 별도 한글 해석 작업자가 대기 중인 prd 문장과 임대가 만료된 prd 해석을 가져가는 경로를 재현했다. 작업본의 해석 조회에 같은 환경 경계를 적용했다. dev 요청만 해석되고, 기존 prd 해석 결과와 대기 작업의 상태·시도 횟수는 보존됐다. prd를 복원하면 그 대기 작업을 다시 가져온다. 해석과 Application 환경 회귀 10개가 통과했다. 현재 시험의 실행 이미지와 운영 DB는 변경하지 않았다. [해석 작업 선택](../../liveprobe/app/application/display_readings.py) · [회귀](../../liveprobe/application_tests/test_display_readings.py)

### Kubernetes 승인 웹훅 실패의 감사 관측

운영 활성화에는 kube-apiserver의 메타데이터 감사 정책과 LiveProbe 프로세스가 읽을 수 있는 전용 JSON Lines 파일이 필요하다. 감사 정책은 활성 네임스페이스의 필요한 리소스와 변경 동작을 선택하고 `ResponseComplete`만 기록한다. Secret과 요청·응답 본문은 수집 범위에서 제외한다. 제어면이 분리돼 있으면 기존 승인된 수집 경로가 파일을 전달할 수 있다. LiveProbe에는 읽기 전용 경로와 파일 읽기 권한만 제공한다. [Kubernetes 감사 정책 및 로그 형식](https://kubernetes.io/docs/tasks/debug/debug-cluster/audit/) · [K3s 감사 로그 설정](https://docs.k3s.io/security/hardening-guide)

관제 API의 `LIVEPROBE_ADMISSION_AUDIT_LOG_FILE`에 해당 파일 경로를 설정하면 새 기록부터 읽는다. 파일은 일반 파일이어야 하며, 로그 회전 시 기존 파일을 읽은 뒤 새 파일을 연다. `/api/status`의 `watchers.admission_audit`에서 수집 상태를 확인한다. 활성 네임스페이스에서 실제 변경 요청이 웹훅 호출 실패로 5xx 응답을 받았을 때만 관제 사건을 생성한다. 사건 근거는 감사 ID, 웹훅·리소스 식별자, 동작, 응답 코드, 관측 시각을 보존한다. 웹훅의 호출 실패는 확인하고, 클러스터 범위의 승인 설정에 대한 조치는 권한 경계로 이관한다. 운영 4175에는 이 소스를 연결하지 않았다. [수집기](../../liveprobe/app/admission_audit.py) · [권한 경계와 근거 저장](../../liveprobe/app/engine.py)

### 지원이 종료된 Ingress API 요청 관측

같은 감사 파일로 `networking.k8s.io/v1beta1` 또는 `extensions/v1beta1` Ingress 변경 요청의 HTTP 404도 수집할 수 있다. 아래 규칙을 기존 감사 정책의 포괄 규칙보다 앞에 둔다. 활성 네임스페이스가 바뀌면 `namespaces`도 맞춘다. 두 버전 모두 Kubernetes 1.22부터 제공이 종료됐다. [감사 정책](https://kubernetes.io/docs/tasks/debug/debug-cluster/audit/) · [Ingress 이전 안내](https://kubernetes.io/docs/reference/using-api/deprecation-guide/#ingress-v122)

```yaml
- level: Metadata
  omitStages: [RequestReceived, ResponseStarted]
  verbs: [create, update, patch]
  namespaces: [dev]
  resources:
    - group: networking.k8s.io
      resources: [ingresses]
    - group: extensions
      resources: [ingresses]
```

LiveProbe의 읽기 계정으로 `/apis/`를 조회할 수 있어야 한다. 감사 요청 경로와 `objectRef`의 대상이 일치하고, 조회 결과에 구버전이 없으며 `networking.k8s.io/v1`이 있을 때 사건을 접수한다. 서버가 변경 요청을 받기 전에 `kubectl`이 버전 확인 단계에서 끝난 경우에는 이 감사 기록이 생기지 않는다. API 버전 조회에 실패하면 해당 묶음의 Ingress 신호를 접수하지 않고 `watchers.admission_audit`에 `api_discovery` 수집 저하를 표시한다. 같은 파일의 웹훅 이벤트 수집은 계속된다. [수집기](../../liveprobe/app/admission_audit.py)

사건에서 404 응답과 제공되는 버전을 확인할 수 있다. 요청 메타데이터가 가리키는 저장소와 배포 선언을 확인할 수 있어야 수정과 실제 라우팅 검증을 시작할 수 있으므로 운영자에게 이관한다. 작업본에서만 검증했으며 운영 감사 정책과 운영 배포는 변경하지 않았다. [사건 처리](../../liveprobe/app/engine.py)

같은 재확인에서 4175의 활성 환경은 dev, 이력은 stg·prd였다. API와 Application 모델 작업자는 같은 환경 ConfigMap을 참조했고 둘 다 준비 상태였다. PRD Deployment 9개는 복제본 0이며 StatefulSet·DaemonSet·Job·CronJob·HPA·Pod도 0개였다. PRD 사건 125건과 `Bound` 상태의 PVC 14개를 읽었다. 기존 임대의 heartbeat와 결과 보고는 별도 정합성 검토 대상으로 남긴다.
