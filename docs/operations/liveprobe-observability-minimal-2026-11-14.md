🧹
# Grafana 퇴역과 장애 관제 필수 구성 보존

2026-11-14 · Claude (Hermes Kanban t_5d649638, 소스 구현) · LP-OBS-20261114

태그: `observability` `grafana-retire` `data-preservation` `incident-detection` `kustomize` `network-policy`

---

> ✅ **운영 적용·자료 보존 확인 완료** — Grafana 실행 자원과 접속·통신 권한을 제거했고, 일반 배포에서 다시 설치되지 않게 정리했다. 기존 PVC·DB·사건 이력은 보존했다. 최종 3회 연속 점검에서 42개 실행 파드/자원 지표와 실측 흐름이 정상이며, 아래에 중간 자원 경합과 복구 내역도 기록했다. 관련 소스 검증 31건은 통과했고, 기존 전체 검사 불일치 12건은 별도로 남아 있다.

## 합의 범위

- 사용자 승인: option1. Hubble Relay + Retina 실측 흐름과 서비스 지도는 유지하고 Grafana는 제거한다. 장애 관제에 필요한 구성만 남긴다. 승인 절차는 완료됐다. Jira는 사용하지 않는다(AGENTS.md).
- 소스 구현은 Hermes 작업 t_5d649638로, 운영 적용·자료 보존·런타임 검증은 이 대화에서 수행했다. 합의한 범위의 실제 적용까지 완료했다.
- 보존 대상: `grafana-data` PVC(SQLite DB·사용자·데이터 소스 정의·이력), `grafana-datasources`/`grafana-dashboards` ConfigMap, `grafana-admin` Secret, 모든 사건·DB 이력. 소스는 이들을 삭제하거나 다시 만들지 않는다.
- 금지: Grafana 문자열 전역 삭제. `grafana/loki`, `grafana/alloy` 이미지와 과거 증거 문서는 그대로 둔다. 기존 runtime/probe 복구(PVC 이전, readiness 5s, Recreate)도 되돌리지 않는다.

## 완료 기준

| # | 기준 | 소스 결과 |
|---|---|---|
| 1 | 지원 렌더(base, base/observability, rancher-desktop, rancher-desktop-observability, portable-example, rancher-desktop-chaos)에 Grafana Deployment/Service/Ingress/NetworkPolicy/ConfigMap/이미지/`grafana-admin` 참조가 없다 | ✅ |
| 2 | base·observability 렌더에 `grafana-data` PVC가 있고 spec이 기존과 같다. overlay가 storageClass를 주입한다 | ✅ |
| 3 | platform 검사가 LiveProbe Ingress 2개(`liveprobe-api`, `liveprobe-dashboard`)를 기대하고 통과한다 | ✅ |
| 4 | 보관본이 어느 활성 kustomization에도 없고, replicas 0이며, PVC가 중복되지 않고, 단독으로 렌더된다 | ✅ |
| 5 | 회귀 테스트가 재설치 방지·자료 보존·필수 탐지 구성 유지를 확인한다. Loki/Alloy 테스트는 유지한다 | ✅ |
| 6 | 라이브 최종 검증 | ✅ 자료 보존·Grafana 부재·필수 수집 경로 확인. 중간 자원 경합은 아래 참조 |

## 변경 파일

| 파일 | 변경 |
|---|---|
| `deploy/base/observability/grafana/grafana.yaml` | 삭제. 내용은 보관본으로 이동 |
| `deploy/base/observability/grafana/grafana-data-pvc.yaml` | 신규. PVC `grafana-data`만 둔다(1Gi RWO, storageClass는 overlay) |
| `deploy/retired/grafana/{grafana.yaml,kustomization.yaml}` | 신규. CM 2개(기존과 동일)와 Deployment(replicas 0, Recreate, readiness timeout 5s, PVC 마운트, automount false, `grafana-admin` 이름 참조). PVC·Service·Ingress·정책은 없다 |
| `deploy/base/kustomization.yaml`, `deploy/base/observability/kustomization.yaml` | data PVC 파일만 등록. automount 패치 regex에서 grafana 제거 |
| `deploy/components/platform/kustomization.yaml` | `grafanaHost` → Ingress grafana 치환 제거, 주석을 Ingress 2개로 수정 |
| `deploy/overlays/{rancher-desktop,portable-example}/platform.yaml` | `grafanaHost` 제거 |
| `deploy/overlays/portable-example/kustomization.yaml` | `grafana/grafana` 이미지 미러 제거(loki/alloy는 유지) |
| `deploy/overlays/rancher-desktop-observability/kustomization.yaml` | Grafana Ingress 패치·automount regex 제거. PVC storageClass 패치는 유지 |
| `deploy/base/network/network-policies.yaml` | observability `allow-ingress-controller`(Grafana 전용)와 `grafana` egress 정책 삭제, prometheus·loki ingress의 grafana peer 삭제, 흐름표 갱신 |
| `scripts/check-overlay.py` | Ingress 집합 = {liveprobe-api, liveprobe-dashboard}. Grafana 활성 객체·정책 참조·이미지·`grafana-admin`·`grafanaHost` 금지, `grafana-data` PVC와 탐지 워크로드 5종 필수 |
| `scripts/check-chaos-overlay.py` | `EXPECTED_INGRESSES`에서 grafana 제거 |
| `scripts/netpol-check.sh` | api→grafana 거부 항목, grafana 출발 검사 블록, `grafana.localhost` 200 기대 제거 |
| `scripts/create-secrets.sh` | `grafana-admin` 신규 생성 제거. 기존 Secret은 건드리지 않는다는 주석 추가 |
| `deploy/base/observability/test_config.py`, `test_logs_grafana.py` | Grafana 프로비저닝 테스트 대상을 보관본으로 변경. 컴포넌트 렌더는 grafana 이름 객체를 `grafana-data` PVC 하나만 허용 |
| `deploy/base/observability/test_observability_minimal.py` | 신규 회귀 테스트(아래) |
| `README.md` | `grafana.localhost` 접속 행 제거, 퇴역 안내 추가 |

## 장애 관제 보존 매트릭스

LiveProbe 호출부(`liveprobe/app/app_errors.py`, `evidence.py`, `main.py`, `network_signals.py`, `overview.py`)에서 Grafana 참조는 0건이다(`grep -i grafana` 결과 없음).

| 구성 | 유지 | 장애 관제 용도 | 호출·연결 근거 |
|---|---|---|---|
| Prometheus | ✅ | 지표 근거, 경보 규칙, 개요 지표 | `evidence.py:132`, `overview.py:48`, `main.py:393` (`settings.prometheus_url`) |
| Alertmanager | ✅ | 경보 → LiveProbe webhook 탐지 | `main.py:879` `/api/hooks/alertmanager`, `alertmanager.yaml` webhook URL, 정책 `alertmanager` egress |
| kube-state-metrics | ✅ | 워크로드 상태 지표(재시작, OOM 등) | Prometheus `kube-state-metrics` job |
| metrics-server | ✅ (kube-system) | HPA·자원 사용량 | 클러스터 기본 구성 |
| Loki + Alloy | ✅ | 예외 로그 탐지, AI 근거 로그 | `app_errors.py:88`, `evidence.py:155` (`settings.loki_url`), Alloy → Loki push |
| Hubble Relay + Retina | ✅ (kube-system) | 실측 흐름, 네트워크 장애(NetworkDrop) 탐지, 서비스 지도 | `main.py:120-123` HubbleCollector, `network_signals.py`, 정책 `liveprobe-api` egress hubble-relay:4245 |
| Istio/Envoy | ✅ | 활성 트래픽, HTTP 지표 | `overview.py:37-39` `istio_requests_total`, Prometheus `envoy-stats` job |
| CoreDNS, Traefik, local-path, dev/prd 대상·DB·Kafka | ✅ | 인프라와 업무 대상 | 이번 변경 범위 밖, 수정하지 않음 |
| Grafana | ❌ 퇴역 | 장애 탐지 경로에 없음 | 자료(PVC/CM/Secret)만 보존, 복구본 `deploy/retired/grafana` |
| Kiali / Jaeger / Tempo / Hubble UI | 없음 | — | 읽기 전용 조회(20:50 KST)에서 미설치 확인 |

읽기 전용 조회(`kubectl get deploy,ds -A`, 2026-11-14 20:50 KST) 결과, Ready 상태는 다음과 같다.

- liveprobe-observability: prometheus·alertmanager·kube-state-metrics·loki·alloy 각 1/1
- kube-system: hubble-relay·retina-operator·metrics-server·coredns·traefik·local-path-provisioner 각 1/1, retina-agent DS 1
- istio-system: istiod 1/1

grafana Deployment/Service/Ingress는 없었다. `grafana-data` PVC는 Bound 상태이고 spec(1Gi RWO, local-path)이 소스와 같다. ConfigMap `grafana-datasources`·`grafana-dashboards`와 Secret `grafana-admin`도 존재한다(값은 조회하지 않음).

## 복구 절차(운영자 승인 시에만)

`deploy/retired/grafana/grafana.yaml` 머리말에 같은 내용이 있다.

1. `grafana-data` PVC와 `grafana-admin` Secret이 있는지 확인한다. 둘 다 변경하지 않는다.
2. `kubectl apply -k deploy/retired/grafana`를 실행한다. replicas가 0이라 Pod가 뜨지 않고 PVC도 만들지 않는다.
3. `scale --replicas=1` 후 `port-forward deploy/grafana 3000:3000`으로 저장 자료를 확인하고, 다시 0으로 줄인다.
4. 복구한 Grafana는 기본 거부 정책 때문에 Prometheus/Loki를 질의하지 못하고 저장 DB만 읽는다. 실시간 질의가 필요하면 임시 정책을 별도로 승인받는다.

## 검증 결과(소스, 2026-11-14)

환경: `liveprobe/.venv/bin/python`(PyYAML 6.0.3, pytest 9.1.1), `kubectl` v1.36.2(Kustomize v5.8.1). 지시에 따라 Docker는 실행하지 않았다.

| 검증 | 명령 | 결과 |
|---|---|---|
| 신규 회귀 테스트 | `pytest deploy/base/observability/test_observability_minimal.py` | **13 passed, 60 subtests passed** |
| Grafana 관련 기존·갱신 테스트 + overlay 토큰 + Ingress | `pytest test_observability_minimal.py, test_config.py::{retired_grafana_provisioning, scoped_overlay, oom_alert}, test_logs_grafana.py::{GrafanaProvisioningTest, ComponentBuildTest, Loki retention/pvc}, scripts/test_check_overlay_tokens.py, test_check_chaos_overlay.py::{extra_ingress, non_chaos[observability]}` | **31 passed, 63 subtests passed** |
| base 렌더·이식성 | `kubectl kustomize deploy/base` 후 class/host/traefik grep | 96 objects, 이식성 OK |
| platform 검사 | `scripts/check-overlay.py` rancher-desktop / portable-example | 둘 다 **OK**, `ingresses=2 pvcs=5 netpols=22` |
| 나머지 렌더 | rancher-desktop-observability / rancher-desktop-chaos / retired/grafana | 26 / 100 / 3 objects |
| 셸 구문 | `bash -n scripts/netpol-check.sh scripts/create-secrets.sh` | OK |
| 전체 정적 묶음 | observability 테스트 4개 + scripts 테스트 2개(Docker 테스트 8개 제외) | 47 passed, **12 failed — 모두 이번 변경 이전부터 실패**(아래) |

### 이번 변경과 무관한 기존 실패 12건

기존 HEAD 비교에서 동일 실패와 렌더 오류를 확인했고, 원인 설정은 이번 Grafana 변경에서 수정하지 않은 기존 파일에 있다. HEAD 내 일부 렌더 입력이 누락되어 12건 모두를 동일 단언으로 재현한 것은 아니다. 현재 전체 정적 묶음의 12건 실패를 숨기지 않으며, 이번 Grafana 관련 검증과 구분한다.

- `test_config.py`의 `alloy_source_is_target_only`, `prometheus_limits_and_target_scope`, `kubelet_cadvisor_scope`와 `test_logs_grafana.py`의 `explicit_namespace_and_label_selectors`: P15에서 Alloy가 전체 namespace 수집으로 바뀌었고 sample_limit은 20000이 됐다. 테스트는 이전 기대값을 유지하고 있다.
- `test_prometheus_ksm.py` 3건: envoy-stats job 추가, KSM 인자, cadvisor labeldrop 변경이 테스트에 반영되지 않았다.
- `scripts/test_check_chaos_overlay.py` 5건: `deploy/base/liveprobe/operator-executor.yaml`(커밋 efcd0a3)이 ServiceAccount token Secret 2개를 렌더하므로 "renders a Secret object"로 실패한다. Ingress 관련 실패는 0건이다.

### 실행하지 않은 검증(제한)

- kubeconform(`test_overlay_render_schema`, `validate-manifests.sh` 2단계), promtool, `alloy fmt/validate`, `loki -verify-config`는 Docker 컨테이너가 필요하다. 호스트 자원 압박 때문에 실행하지 말라는 지시가 있어 실행하지 않았다.
- `scripts/netpol-check.sh` 실제 연결 매트릭스는 라이브 실행이 필요하므로 parent가 담당한다.

## 비판적 검토

- PVC를 렌더에 남긴 이유: 렌더에서 빠지면 prune 계열 적용에서 고아가 되거나 삭제될 위험이 있다. spec은 라이브 last-applied와 같아서(1Gi RWO) 적용해도 변화가 없다.
- 보관본을 실수로 적용해도 replicas 0이고 PVC가 없어서 새 볼륨이나 Pod가 생기지 않는다. CM은 라이브와 같은 내용이다.
- `allow-ingress-controller`라는 이름의 정책은 liveprobe-system에만 남는다. platform component의 이름 기반 치환이 이 정책 하나에만 적용되는 것을 `check-overlay.py`로 확인했다.
- `check-overlay.py`의 api_clients 허용 목록은 바꾸지 않았다(`test_check_overlay_tokens` 고정값 유지).
- 라이브 클러스터에 남은 이전 Grafana 객체(PVC 제외)는 parent가 이미 제거했다. 소스와 라이브가 일치하는지는 parent의 최종 검증 대상이다.

## 라이브 최종 검증

2026-11-14 21:10 KST 기준. 증거: `docs/evidence/observability-minimal-2026-11-14/`.

| 검증 | 확인 결과 | 증거 |
|---|---|---|
| Grafana 실행·접속·정책 | Deployment / Service / Ingress / 전용 NetworkPolicy 2개 없음. Prometheus·Loki의 Grafana 접근만 제거 | `grafana-absence.json`, `network-cleanup.json` |
| 저장 볼륨 | 기존 12개 PVC의 UID·볼륨 연결 동일, 모두 보존. Grafana PV Retain 유지 | `final-preservation.json` |
| Grafana 저장 자료 | ConfigMap 2개와 Secret의 UID·내용 해시 동일. DB quick_check=ok, 데이터 소스 2개·사용자 1개 보존 | `grafana-backup.json`, `final-preservation.json` |
| 사건·과거 환경 이력 | 기존 K8s 사건 ID 1,346개 모두 유지(21:01 총 1,369개), Application 9개 유지, 과거 stg 44개 유지 | `before.json`, `final.json`, `final-preservation.json` |
| 실행·자원 지표 | 최종 3회 연속 42개 실행 파드 준비 완료, 42개 지표 조회, 누락 0. dev/prd 각각 9/9 | `final-observation.json` |
| 실측 통신 | Hubble connected, 최종 흐름 지연 0.1초, 연결 123개 관측 | `final-observation.json` |
| Prometheus | 수집 대상 38/38 정상, 경보 규칙 22개, Alertmanager 1개 연결 | `sources-current-logs.json` |
| 로그 수집 | Loki ready 200, 최근 LiveProbe 시스템 로그 5개 실제 조회, 최신 타임스탬프 확인 | `sources-current-logs.json` |
| 경보 전달 연결 | Alertmanager ready, LiveProbe webhook 구성, 양측 인증 값 일치, Alertmanager에서 API health 접근 성공 | `alertmanager-after.json` |
| 화면·접속 | 두 관제 화면 HTTP 200, K8s API health 200, Grafana 기존 경로 404. 서비스 지도와 dev/prd 화면을 브라우저로도 확인 | 이 대화의 실행·브라우저 검증 |

### 보존 백업

Grafana 중지 후 PVC의 SQLite 자료를 별도로 백업했다. VM 내부 `/var/lib/liveprobe-runtime-repair/grafana-retired-20261114.tar`(0600), SHA-256 `d08b5e3a01325f86875bd53b3d4f8150fe0ca8901e29dc04497afd54f9fa3ddc`. 사본과 DB 검사는 비공개 임시 경로에서 수행했다. 인증·DB 원문은 저장소에 넣지 않았고, 이 증거 폴더에는 해시와 건수만 기록했다. 기존 이전 백업도 삭제하지 않았다.

### 중간 자원 경합과 복구

Grafana 제거 직후에는 다른 실행 프로세스가 모두 유지됐다(`preservation.json`). 이후 같은 Mac의 병렬 S3 백업, 동일 VM의 별도 백업 DB 복원 검증 등과 자원 경합이 관측됐다. 클러스터 전반의 응답 지연·probe 실패·자동 재시작이 발생했으므로 **이 작업 시간 전체에 걸쳐 모든 파드가 무재시작이었다고 주장하지 않는다**. 저장 볼륨과 기존 사건 ID 보존은 별도로 다시 확인했다.

사용자 승인에 따라 백업을 중단하지 않고 우선순위를 낮췄다. S3 프로세스와 그 자식만 대상으로 한 임시 우선순위 관리기는 S3 프로세스 종료와 함께 종료됐다. 같은 백업의 DB 검증 컨테이너에는 CPU shares 128을 적용했다. 이는 현재 백업 검증 컨테이너 수명 동안만 유지되며, 백업 파일·데이터·메모리 한도·다른 컨테이너는 변경하지 않았다. 0으로 되돌리는 Docker 요청은 무시되어 실제 값은 128이며, 복원했다고 기록하지 않는다(`backup-container-priority.json`).

쌓인 관리 작업을 복구하기 위해 K3s 관리 프로세스에 정상 종료 신호를 보내 기존 감독 프로세스가 재기동하게 했다. Docker 데몬·DB·PVC는 재생성하지 않았다. K3s가 기본 DNS·metrics-server 매니페스트를 재적용하면서 앞선 probe 5초 설정이 1초로 돌아가, 기존 5초 값으로 복원했다(`control-restart.json`). 이 두 구성요소는 K3s 갱신·재시작 후 다시 확인해야 한다.

최종 관찰 7회 중 중간 2회에 Loki 준비 상태 지연이 있었고, 마지막 3회는 모든 확인 항목이 연속 정상이다. 이후 Loki ready 3회(200)와 실제 새 로그 조회도 통과했다. 이 기록은 해당 시점의 검증이며, 호스트에서 다른 무거운 작업을 병행할 때의 용량 한계까지 없어진 것은 아니다.

실제 신규 장애 주입이나 전체 `netpol-check.sh`는 이번 제거 검증에서 실행하지 않았다. 기존 데이터 조회·실제 수집·허용된 연결과 변경된 정책 범위를 확인했다.
