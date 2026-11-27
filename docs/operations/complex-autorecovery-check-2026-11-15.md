# 🧪 복잡 장애 자동 복구 실증

2026-11-15 · Codex · dev · 실제 검증

---

## 합의 범위와 완료 기준

사용자는 운영자 인계용이 아닌 복잡 사건 한 건을 실제로 발생시켜 자동 처리를 확인하도록 요청했다. 기존 승인에 따라 dev만 주입하고 prd는 읽기 전용으로 확인한다. DB·저장 이력은 보존한다. 별도 사건 생성 API, 난이도 강제 설정, 수동 복구를 자동 성공으로 처리하지 않는다.

대상은 dev inventory다. 별도 PostgreSQL 재고와 Pod 임시 시험 상태가 분리되어 있다. 정상 응답·기존 장애 없음·DB 행 지문·배포와 Pod UID를 보존한 후, runtime intermittent_exception(1.0)과 error_rate(0.5)를 같은 장애 창에 주입한다. 실제 GET 재고 요청만 보내며 재고를 변경하지 않는다. TTL은 1,800초, 20분 내 자동 처리가 확인되지 않으면 상태와 실패 근거를 보존하고 주입한 두 항목만 해제한다.

통과 조건:

- 실제 업무 HTTP 500·503 발생 및 자동 경보 수신.
- 실제 라우터가 COMPLEX로 판정하고 분석·독립 검증 실행.
- 운영자 개입 없이 허용된 AI 조치가 실행되고 영수증 저장.
- 새 Pod의 정상 응답 지속, DB 지문 동일, prd 배포·응답 정상.
- TTL 종료·시험 도구 해제·관찰상 회복은 AI 복구 성공으로 세지 않음.

## 진행

- [x] 계획서, 기존 운영 승인, 감지·분류·조치·시험 API 코드 확인.
- [x] 관제 ready, 자동 조치 모드, dev 9개 Deployment 정상 확인.
- [x] 기준 자료 저장 및 실제 장애 주입.
- [x] 자동 분석·조치·업무 복구 검증 완료 — **자동 복구 실패**.
- [x] 시험 종료 후 데이터 및 prd 정상 검증.

운영용 원자료는 `/private/tmp/liveprobe-complex-check-20261115`에 저장하며 인증값은 출력·저장하지 않는다. 최종 근거는 저장소에 비밀값 없이 기록한다.

## 실제 결과

> ⚠️ **실패 판정** — 자동 감지와 COMPLEX 분석은 작동했지만, Kubernetes 조치 0건으로 운영자 인계되었다. 시험 도구가 장애를 해제했으며 이를 AI 복구로 세지 않는다.

| 확인 항목 | 관측 결과 |
|---|---|
| 장애 주입 | 2026-11-15 16:31:19 KST, dev inventory의 두 runtime fault만 활성화 |
| 실제 업무 오류 | 해제 전 230회 모두 실패: HTTP 500 124회, HTTP 503 106회 |
| 자동 감지 | app_log의 ApplicationException, 사건 **#1404**, 16:31:42 KST |
| 난이도 | 규칙과 실제 모델 모두 COMPLEX, 신뢰도 0.94; 강제 설정 없음 |
| 분석 | router, k8s_log_analyst, cause_reasoner, challenger, recovery_planner, group_verifier 모두 성공. 표시용 interpreter 1회 별도 |
| 최종 상태 | 16:34:27 KST handed_off. Action 0건, 재시작·롤백·수정 실행 없음 |
| 원인 판단 | AppFaultFilter.java:28의 IntermittentException 경로는 지지됨. runtime 상태의 해제 가능성과 재시작 효과는 모델 근거에서 확인되지 않음 |
| 원복 | 정확한 Pod UID와 각 fault의 params·expires_at을 대조한 뒤 주입한 두 항목만 off. 전체 clear·Pod 삭제·배포 변경 없음 |
| 정상 응답 | 해제 후 연속 GET 22회 HTTP 200을 최종 검증 시점에 확인. 별도 dev/prd 각각 5회 200, 본문 기준값 일치 |
| 데이터 | inventory.stock 5행, 전후 SHA-256 `298ba55e1d04ee44fe395a4de0334ff6841cbb5c334754a9e5d5fdecd7395f70` 동일 |
| 영향 범위 | dev·prd Deployment UID·generation·image·replica·ready 상태 모두 기준값과 동일. DB·PVC·Secret·사건 이력 보존 |
| 시험 자원 | 생성한 읽기 전용 트래픽 프로세스와 포트 연결 종료. 관제 서버 유지 |

### 확인된 미해결 사항

1. 현재 수집 근거는 예외 스택과 집계 오류율을 제공하지만, 발생 조건·요청 단위 연계·재시작으로 해소되는 상태인지 추가 확인하지 못했다. 이번 시험은 자동 복구를 기대한 runtime 장애였으나, 실제 시스템은 애플리케이션 수정 필요로 판단했다. 안전 검증을 건너뛰거나 임의 조치를 강제하지 않았다.
2. 조치 0건, 그룹 호출 5/10회, 약 123/900초에서 `bounded_recovery_exhausted` / `group_diagnostic_exhausted` / `recovery_attempted=true`로 분류되었다. 이 값은 현재 구현이 진단 완료를 복구 소진으로 매핑한 결과이며, 실제 복구를 시도했다는 증거가 아니다. 인계 분류와 추가 근거 확보 경로의 보완이 필요하다.
3. 필터에서 일찍 반환한 500·503의 Prometheus URI가 `UNKNOWN`이어서 `uri=~"/api/.*"` 경보에 잡히지 않았다. 이번에는 로그 감지가 사건을 만들었다. 로그 예외가 없는 필터 503만 발생하는 경우에는 별도 감지 공백이 생길 수 있다.

이번 요청은 실제 검증이므로 소스·자동화 정책·보호 경계를 변경하지 않았다. 사건의 인계 상태를 성공으로 덮지 않았으며, 화면에서도 #1404의 복잡·운영자 인계 결과를 확인했다.

[검증 결과](../evidence/complex-autorecovery-2026-11-15/result.json) · [사건과 모델 결과](../evidence/complex-autorecovery-2026-11-15/incident-1404.json) · [시험 응답 기록](../evidence/complex-autorecovery-2026-11-15/traffic.jsonl)
