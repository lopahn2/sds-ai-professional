# dev 비교 시험 재개 조건

## 2026-11-23 관제 전환 확인

현재 배포 이미지의 로컬 함수를 합성 입력으로 실행했다. `target_namespaces: [dev]`, `historical_namespaces: [stg, prd]`에서 Pod·Event·Service 감시 시작 범위는 dev였다. Alertmanager 훅이 지원하는 경보 10종 모두 PRD의 Kubernetes 조회 전에 제외했다. 애플리케이션 예외 수집기는 같은 합성 예외를 dev에서 한 건, PRD에서 0건 발행했다. PRD의 이력 조회 범위는 남았다. 이 검증은 읽기 전용이며 관제 DB나 실행 중인 설정을 변경하지 않았다. [감시와 훅](../../liveprobe/app/main.py) · [네임스페이스 설정](../../liveprobe/app/config.py) · [로그 감시](../../liveprobe/app/app_errors.py)

실제 API의 `config` 볼륨을 dev 전용 설정을 담은 별도 ConfigMap으로 교체한 뒤 `/api/status`의 준비 상태와 `target_namespaces`를 조회한다. PRD 이력은 `/api/console/incidents?environment=prd&limit=1`로 확인한다. 설정을 되돌릴 때는 동일 볼륨을 원래 `liveprobe-config-naria-a70cb0f8f801`으로 연결한다. Alertmanager가 전달한 PRD 경보도 API 입구에서 걸러지므로 이 전환에는 Prometheus·Alertmanager 재시작이 필요하지 않다. dev가 PRD에 의존해 실제로 겪은 실패는 dev 신호로 계속 감시된다.

## 자원 확인

노드 `lima-rancher-desktop`의 `kubectl top`은 Metrics API를 조회하지 못했고, Kubelet `/stats/summary`는 시간 초과됐다. 같은 노드의 API 서버 프록시를 통한 Kubelet `/metrics/resource`에서 채집 시각이 14:38:26.407과 14:38:47.322 UTC인 샘플을 읽었다. 조회 시 각각 약 17초와 15초가 지났고 수집 오류 지표는 두 번 모두 0이었다. CPU 누적 초 861,590.735→861,673.145에서 평균 3.94코어(할당 가능 4코어)를 계산했다. 두 작업 집합의 큰 값은 9,500,315,648바이트(할당 가능 12,232,176Ki의 약 75.8%)였다. 두 값 모두 PRD 축소 전 70% 상한을 넘었다. [Kubelet의 노드 지표 경로](https://kubernetes.io/docs/reference/instrumentation/node-metrics/) · [시험 실행기의 자원 조건](../../chaos/run-case.py)

실행기는 앞선 두 API가 유효한 계측을 제공하지 않을 때만 `/metrics/resource`를 사용한다. 두 시점의 CPU·메모리 타임스탬프가 서로 맞고 모두 60초 이내이며, 같은 노드에서 CPU 카운터가 초기화 없이 증가할 때 평균 코어와 메모리 작업 집합을 계산한다. 기존 CPU 및 메모리 70% 상한, 가용 메모리 2GiB 이상, 노드 Ready 및 압박 조건을 그대로 적용한다. 샘플이 없거나 같은 시각이면 쓰기 전에 중단한다. 실행기 회귀 60개와 하위 시험 27개가 통과했다. 14:40:55 UTC의 읽기 전용 실환경 사전 확인은 자원 기준을 넘겨 주입을 거부했다. [계측과 경계 검증](../../chaos/tests/test_runner.py)

## 먼저 실행할 시험

- 기존 27건의 원본 실행기는 `liveprobe-target`의 주문·재고 Deployment, Service, 독립 요청을 보내는 `loadgen`, 기준선 DB 계측을 요구한다. `liveprobe-target`에는 Deployment와 Service가 없었고 dev에도 `loadgen`은 없었다. dev 업무 주문·재고의 이름만 바꿔 끼우면 복구·원복 및 사용자 데이터에 대한 기존 기준선이 맞지 않는다. [원래 실행기](../../chaos/run-case.py) · [독립 응답 검증](../../evaluation/harness/real.py)
- 자원 조건이 통과하면 dev의 별도 `lp-eval-*` 시험 Deployment를 한 번에 하나씩 사용한다. 기존 dev 검증용 이미지의 고정 지문을 재사용할 수 있다. 현재 배포 감지가 다루는 이미지 당겨오기 실패부터 `정상 기준선 → 실제 신호 → 자동 수집 → 독립 복구 확인 → 원복`을 검증한다. 별도 워크로드에 대한 직접 비교 기록에는 같은 첫 오류 로그, 동일한 조치 범위, 각 단독 모델의 시간·토큰을 함께 남긴다. [기존 이미지 감시 범위](../../liveprobe/app/watcher.py)
- 배치·설정·복제본·롤아웃 검증은 dev 전용 워크로드에 한정한다. 노드·DNS·CNI·CSI·제어면 호환성 장애는 별도 클러스터에서 같은 독립 기준으로 검증한다. Application 네이티브 시험은 임시 저장소·후보 이미지로 실행하고 PRD 업무 자원에 의존하지 않는다. [신규 Kubernetes 카탈로그](../../evaluation/catalogs/comparative-2026-11-23.json) · [Application 격리 검증](../../evaluation/application_release_trial.py)

### 재고 이미지 가져오기 시험

F1-01 이미지 실패의 제품 감지와 원복을 볼 때는 dev 재고 Deployment에서 이미지 필드만 시험한다. 독립 사전 조회에서 현재 이미지 지문, 원복 정책의 동일 지문, 보존된 동일 리비전의 Pod 템플릿, maxSurge 1·maxUnavailable 0, 준비된 Service endpoint 하나와 재고 GET·readiness를 모두 확인한 경우에만 실행한다. 최신 노드 자원 조건도 모든 주입에서 다시 조회한다. 실제 읽기 전용 사전 확인은 2706m·7671Mi로 통과했다. 별도 실행의 CPU 계측이 기준을 넘으면 원래 배포를 변경하지 않는다. [시험 실행기](../../evaluation/k8s_dev_image_trial.py)

시험은 자동 감시를 그대로 사용하고 생성된 오류 Pod와 같은 사건을 연결한다. LiveProbe의 실제 이미지 원복 조치가 감사 기록에서 확인되고 정리 전에 원래 지문·현재 배포 세대·업무 요청 두 건이 통과한 경우에만 이 제품 관측에서 자동 복구로 기록한다. 정리 과정에서 다른 변경이 발견되면 이미지를 덮어쓰지 않으며 남은 작업 범위를 보존한다. 원복 증거와 동시 변경 검증에 대한 오프라인 시험 7개가 통과했다. [증명 조건 회귀](../../evaluation/tests/test_k8s_dev_image_trial.py)

별도 워크로드 시험에는 기존 전역 평가 잠금 대신 dev 재고 한 곳의 평가 임대를 사용한다. 임대가 있는 동안 해당 워크로드의 자동 접수만 보류한다. 단독 실행기는 최초 감지한 오류를 제품 시험 영수증과 같은 방식으로 정제한 다음 일치할 때만 모델에 전송한다. 각 조치 전 임대 소유권을 확인하고 정확한 기존 이미지 외의 변경을 허용하지 않는다. 임대가 만료되어도 시험이 소유한 이미지에 한해 운영자 원복을 시도한다. 임대 유실·다른 관제 사건 개입·원본과 다른 오류는 비교 성공으로 기록하지 않는다. 전후 업무 검증, 최초 오류부터 확인까지의 시간, 호출별 사용량과 비용은 실행별 영수증에 보존한다. [비교 실행기](../../evaluation/k8s_dev_image_trial.py)

임대 기능을 포함한 API가 가동되면 다음과 같이 모델별로 순차 실행할 수 있다. 기준으로 사용하는 LiveProbe 사건은 #1649이며 이전 영수증은 계속 보존한다. 이번 소스 검증에서는 관제 정책, 모델 호출, 운영 대상에 쓰기 작업을 실행하지 않았다. [모델별 시험 판정](../../evaluation/tests/test_k8s_dev_image_trial.py)

```sh
PYTHONPATH=liveprobe:. liveprobe/.venv/bin/python -B -m evaluation.k8s_dev_image_trial \
  --execute --arm single_terra \
  --reference .hermes/evidence/liveprobe-comparison-2026-11-23/dev-image-f1-01-first.json \
  --output .hermes/evidence/liveprobe-comparison-2026-11-23/dev-image-f1-01-single-terra.json
```

첫 실제 실행은 쓰기 직전 CPU 조건에서 중단됐다. 조건을 다시 확인한 뒤 2,716m/4,000m 및 7,724MiB/11,945MiB에서 F1-01을 실제 주입했다. 새 Pod의 첫 이미지 오류는 6.03초에 관측됐으며 주입 전부터 가동하던 Service 엔드포인트는 계속 준비 상태였다. LiveProbe가 [사건 #1649](http://127.0.0.1:4175/incidents/1649)를 만들고 `rollout_undo`를 한 번 실행해 102.25초에 정상 템플릿·원래 이미지와 독립 업무 조회 두 건을 확인했다. 시험기가 정리하면서 별도 복구 쓰기를 할 필요가 없었다. Luna 두 호출과 Sol 6.2 한 호출에 54,993토큰과 환산 $0.060343을 기록했다. [독립 복구 영수증](../../.hermes/evidence/liveprobe-comparison-2026-11-23/dev-image-f1-01-first.json)
