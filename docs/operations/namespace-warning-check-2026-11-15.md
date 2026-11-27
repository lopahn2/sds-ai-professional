# 🔎 네임스페이스 확인 실패 점검

2026-11-15 · Codex · 현재 상태 및 경고 이력

---

> ✅ **결과** — 실행 Pod 42/42가 준비됐고, 모두 CPU·메모리 지표를 제공한다. 약 9분 관찰 중 경고 횟수 증가와 컨테이너 재시작이 없었다. 과거 경고가 현재 장애처럼 보이는 화면을 수정해 4175에 반영했다.

## 무엇이 실패했는가

| 대상 | 기록된 실패 | 현재 확인 |
|---|---|---|
| dev·prd 서비스 | 준비·생존 점검 응답 시간 초과 | 각 환경 9/9 준비 |
| Loki·Kafka·Alertmanager·Alloy·Prometheus·kube-state-metrics | 대부분 1초 준비 점검 시간 초과 | 모두 준비됨 |
| metrics-server | 수집된 지표가 없어 준비 점검 500 | metrics API Available=True, 42개 실행 Pod 지표 수집 |
| istiod HPA | CPU 자료 조회 실패로 자동 확장 계산 실패 | ScalingActive=True / ValidMetricFound, CPU 사용률 6% |
| Retina operator | 19:39:44 리더 임대 갱신 실패로 종료 | 19:40:26 재시작 후 준비 상태 유지 |

19:35~19:45에 4코어 노드 CPU가 최대 3.86코어(약 96.6%)까지 사용됐고, 19:40에는 cAdvisor 수집도 실패했다. 여러 네임스페이스의 점검이 동시에 지연됐다는 점과 함께 자원 포화가 공통 요인이었음을 뒷받침한다. 다만 특정 프로세스 하나가 유일한 원인이었다고 단정하지 않는다. 앞서 실행했던 검수용 DB와 시험 작업은 이미 종료됐다. 마지막 Warning은 19:45:36 Loki 기록이다.

지난 복구에서 수정한 Docker 지표 복원·Envoy 연결 설정은 유지돼 있었다. 이번 점검에서 이들을 다시 변경하거나 서비스·DB·VM을 재시작할 필요는 없었다. 인증·권한과 probe 기준도 유지했다.

## 적용한 수정

기존 화면은 최근 경고의 종류만 ‘상태 확인 실패’로 표시했다. 현재 준비 상태와 경고가 발생한 시각을 구별하지 않아 복구된 대상도 계속 실패 중인 것으로 읽혔다.

- 각 경고에 **현재 준비됨 / 현재 준비 확인 필요 / 현재 실행 중지 / 현재 상태 미확인**을 표시한다.
- 현재 준비 수를 경고 시각·누적 횟수와 구분하고, 실제 미준비 대상부터 정렬한다.
- 네임스페이스와 자원 종류를 맞춰 Deployment·StatefulSet·DaemonSet·독립 Pod를 연결한다. 과거 Pod 자체가 복구됐다는 의미로 표시하지 않는다.
- HPA는 Deployment 준비 상태로 정상 판정하지 않는다. 현재 API에 HPA 상태 필드가 없어 화면은 ‘현재 상태 미확인’으로 남으며, 이번 점검에서는 Kubernetes에서 직접 정상 계산을 확인했다.
- 조회 실패, 45초가 지난 자료, 빠진 준비 수, 0개 실행을 정상으로 표시하지 않는다.
- 제목·요약·현재 상태는 기본 표시하고, 원문만 접는다. 기존 경고와 사건 이력은 지우지 않는다.

## 검증

| 검증 | 결과 |
|---|---|
| 대시보드 단위 시험 | 184 통과, 이번 경고 상태 회귀 시험 7건 포함 |
| 타입 검사·빌드 | 통과; 관측 시각 null 처리도 확인 |
| 화면 시험 | 375·768·1440px에서 12 통과; 기존 요약 노출·원문 접기 포함 |
| 실제 4175 화면 | Loki·Kafka 등 현재 준비됨, 과거 시각·횟수 별도 표시 확인 |
| 실행 Pod·지표 | 준비 42/42, 지표 42/42, Pending/Failed 0 |
| 19:55:29~20:04:22 관찰 | 경고 추가·횟수 증가 0, 주/초기화/사이드카 컨테이너 상태·식별자 76개 동일 |
| 저장 볼륨 | PVC 12개 UID·볼륨 연결·상태 동일, 삭제 0 |

이 작업은 운영 DB 변경 요청을 보내지 않았다. 기존 사건을 닫거나 상태를 고쳐 정상으로 보이게 하지 않았다. 관제 화면의 집계 대상과 과거 인계 사건은 별도이며 이번 네임스페이스 상태 점검 범위를 바꾸지 않았다.

## 소스와 반영 위치

현재 4175가 사용하는 `/private/tmp/liveprobe-publish-20261115/source`의 `codex/visible-incident-summaries`를 수정했다. 기준 커밋은 `3becacb`이며 다른 세션의 main 변경을 섞지 않았다. 변경은 `dashboard/src/Overview.tsx`, `warningModel.ts`, `incidentOverview.css`와 회귀 시험 2개다. 기존 Application 관제 연결 `/application/`도 유지했다.

`VITE_APPLICATION_URL=/application/ npm run build -- --outDir /private/tmp/liveprobe-namespace-dashboard-dist`로 별도 빌드 후 확인했다. 검증된 자산을 기존 dist에 추가하고 index를 교체해 서버 프로세스를 유지했다. 이전 파일은 `/private/tmp/liveprobe-namespace-dashboard-before`에 보존돼 있다.

계획과 완료 기준은 [작업 계획](../../.hermes/plans/namespace-warning-current-state-2026-11-15.md), 검증 요약과 소스 패치는 [근거 디렉터리](../evidence/namespace-warning-check-2026-11-15/)에 보존한다.
