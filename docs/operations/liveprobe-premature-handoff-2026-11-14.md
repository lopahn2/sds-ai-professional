# LiveProbe 조기 이관 수정 · 실제 반영 및 검증

대상 작업: t_1e84a05b
범위: 승인된 두 이관 경계를 유지하는 소스·회귀 수정, 관제 API·화면 배포, 기존 사건의 읽기 전용 재확인. Jira와 커밋·푸시는 사용하지 않았다. 기존 DB·이력·PVC를 보존했다.
정책 근거: [실제 운영 합의](liveprobe-real-operation-dev-prd-2026-11-13.md).
실행 계획: `.hermes/plans/liveprobe-premature-handoff-2026-11-14.md`.

## 완료 범위

- 건강/근거 부족의 router 결과는 `recheck_required`이며 더 이상 `handoff_only`가 아니다.
- 분류기 오류·저신뢰는 제한 재시도 후 규칙 수준을 유지하고 최소 NORMAL 분석으로 진행한다. 실제 규칙 HIGH_RISK는 낮추지 않는다. 엔진에서 이미 공급자/출력 검증 예산을 소진한 경우 중첩 재시도를 막는다.
- deployment/pod/service/endpoints의 핵심 실패는 한 번 새로 수집한다. 첫 실패는 `collection_attempts` 및 Evidence 원장에 보존하되 최종 Bundle의 판단 근거에는 이어붙이지 않는다.
- NORMAL/COMPLEX 처리 전 새 관측을 수행한다. 같은 Pod UID·대상 Service 선택 범위, 준비 상태·엔드포인트, 원래 지원 증상의 해소를 확인한 경우만 관측 회복으로 종료한다. 새 재시작/종료·준비 실패·경고·현재 예외·불명 신호·미관측 필수 지표는 회복으로 인정하지 않는다. 누적 restart 21회 및 시각이 확인된 과거 경고 자체는 진행 중 장애가 아니다.
- 관측 회복은 기존 `resolved` 상태와 `handoff.observed_recovery`를 사용한다. `cause_confirmed:false`, `ai_remediation:false`, 두 관측 요약을 저장하고 WorkflowRun에는 전체 관측 Bundle을 보존한다. 새 상태나 DB migration은 없다.
- Group 무조치 종료도 제한 건강 확인을 수행한다. 오래된 Group 입력은 건강 관측으로 재사용하지 않고 새 관측 쌍을 확보한다. 정상 미증명 진단 종료는 복구 소진으로 기록하되 실제 HIGH_RISK·허용 밖 제안·실행 거부·결과 불명 쓰기 경계는 유지한다.
- 재관측에서 새로 발견한 HIGH_RISK는 다시 기록한다. 새 핵심 수집 실패는 Group 진입 전에 종료하므로 모델 정책 누락 예외로 빠지지 않는다.
- 목록/상세/narrative의 `recovery_kind: observed`와 UI 결과·배지·안내는 “정상 상태 확인 · 원인 미확정 · AI 조치 없음”으로 구분한다. 표식 없는 기존 자동 복구 이력의 문구는 유지한다.

## 읽기 전용 재평가 계약

`POST /api/incidents/{iid}/reassess`

본문:
`{"confirm":true,"reason":"조기 이관 재확인 사유","read_only":true,"observed_recovery_only":true}`

- 확인·1~500자 사유가 필수다. 두 읽기 전용 옵션의 기본값은 true이고 false는 422다.
- 활성 환경의 handed_off 중, 원래 routing이 evidence_state(healthy/insufficient) 또는 명시된 분류기/핵심 수집 실패였던 조기 router 이관만 허용한다. 원래 규칙 HIGH_RISK 및 이후 확정된 HIGH_RISK는 거부한다. 원래 규칙을 검증할 수 없는 불완전 features도 거부한다.
- Incident 행 잠금 후 검사한다. Action 이력, OperatorCommand 이력(재시작 후 결과 불명인 failed 포함), 실행/재개 대기 WorkflowRun, 일시정지/중단 제어, 미반영 운영자 지시, 다른 운영자의 인수는 409다.
- 원래 상태 사유·handoff·routing ID·종료 시각을 `handoff.reassessments`와 `incident_reassessment_started` 감사 기록에 보존하고 새 WorkflowRun/Evidence/RoutingDecision을 추가한다. 기존 실행·근거 행은 삭제하지 않는다. 최대 두 재평가까지만 허용한다.
- 별도 `reassess_read_only` 경로는 Group·executor·코드 변경 생성 경로를 호출하지 않는다. 회복이 증명되면 observed_recovery, 아니면 handed_off를 유지하고 `handoff.reassessment.outcome=diagnosed`를 기록한다. 실제 쓰기는 하지 않는다.
- 동시 재평가 시험은 진행 중 요청과 겹치는 두 번째 요청을 409로 거부하고 새 실행/보존 항목을 하나만 추가함을 DB에서 확인했다.
- 이미 진단 역할이 실행된 #1390/#1383/#1382 같은 이력은 이 API의 조기 router 이관 대상이 아니다. 그런 이력에 임의 실행 권한을 재생하지 않는다. Group 무조치 개선은 신규 정상 처리 경로에 적용된다.

## 독립 검토 반영

읽기 전용 구현 검토에서 지적한 다섯 항목을 보완하고 회귀로 고정했다.

1. 현재 `code_refs.exceptions`가 남아 있으면 관측 회복 거부, ApplicationException은 성공한 로그/코드 근거도 요구.
2. 양수 엔드포인트만 보지 않고 Service가 해당 워크로드 Pod들을 선택하는지 검사.
3. 두 번째 수집의 핵심 실패는 분석 워크플로 전에 복구 소진으로 종료.
4. 재평가 두 번째 관측의 HIGH_RISK를 보존하고 후속 재평가도 거부.
5. 오래된 첫 관측은 회복 증명에 사용하지 않음. 장시간 Group은 두 번 새 수집.

## 실제 로컬 검증 결과

- 백엔드 집중 회귀: **337 passed, 실패 0 / 오류 0 / 건너뜀 0**, 212.50초. 15개 관련 시험 파일을 실행했다.
- 대시보드: **168 passed, 실패 0 / 건너뜀 0**. `npm run build`의 TypeScript 검사와 Vite production build도 통과했다.
- 수정한 추적 파일의 `git diff --check` 통과. 브랜치 main에서 커밋하지 않았다.
- PostgreSQL 시험은 상위 작업이 제공한 전용 pgvector 서버에서 UUID 이름의 일회용 DB를 사용했다. 인증값은 subprocess 환경으로만 전달하고 출력하지 않았다. 운영 DB를 시험 대상으로 쓰지 않았다.
- 초기 회귀가 요구하던 “분류기 실패 즉시 HIGH_RISK”, “healthy 즉시 인계” 기대값을 새 승인 정책으로 갱신했다. 직렬화 모의 시험은 기본 활성 재수집과 두 수집 이력을 반영했다. API 형상 시험은 기존 pagination/비용/출처 확장 필드와 신규 recovery_kind를 명시적으로 검증한다.

재실행 명령(저장소 루트):

    liveprobe/.venv/bin/python .hermes/plans/run-handoff-tests.py tests/test_premature_handoff.py tests/test_router_workflows.py tests/test_router_policy.py tests/test_kan57_offline.py tests/test_application_runtime_context_engine.py tests/test_application_runtime_integration.py tests/test_p17_close.py tests/test_agent_groups.py tests/test_p15_narrative.py tests/test_safety.py tests/test_typed_actions.py tests/test_api.py tests/test_evidence.py tests/test_p18_w6_narrative.py tests/test_p18_w6_handoff_summary.py --junitxml=../docs/evidence/liveprobe-recovery-2026-11-14/premature-handoff-focused.xml
    cd dashboard && npm run build && npm test

보존 증거:

- [pytest 원문](../evidence/liveprobe-recovery-2026-11-14/premature-handoff-focused.log)
- [JUnit 결과](../evidence/liveprobe-recovery-2026-11-14/premature-handoff-focused.xml)
- [소스·정적 빌드 SHA-256 및 결과 집계](../evidence/liveprobe-recovery-2026-11-14/premature-handoff-manifest.json)

## 구현 인계 기록 · 아래 실제 운영 검증으로 완료

- 구현 실행기는 로컬 단계까지 수행했고, 상위 작업에서 아래 이미지 교체·pins·실제 재평가를 완료했다. 로컬 시험과 실제 운영 검증을 구분해 기록한다.
- 신규 설정 `recovery.observation_interval_s`는 코드 기본 **1.0초**, 허용 범위 0.1~5초다. 재확인은 기본 활성이라 별도 ConfigMap 활성화 패치가 필요 없다.
- 백엔드 포함 대상 9개: `liveprobe/app/{router.py,engine.py,config.py,k8s.py,main.py,observed_recovery.py,narrative.py,flowgraph.py,routing_ko.py}`. 신규 observed_recovery.py와 Pod UID를 수집하는 k8s.py를 함께 배포해야 한다.
- UI 포함 대상 8개: `dashboard/src/{narrative.ts,api.ts,labels.ts,ui.tsx,Incidents.tsx,IncidentRoom.tsx,history.ts,presentation.ts}`. API와 UI 번들을 함께 갱신해야 관측 회복 배지가 일치한다.
- 수정 시험 파일 7개와 각 파일의 지문은 manifest에 있다. 기존 작업 디렉터리의 다른 변경을 일괄 포함하지 않는다. `application/control.py`, `application/releases.py`는 수정하지 않았으며 이번 배포 범위에서 제외한다.
- 운영 인수 때는 먼저 정확한 이미지/제공 번들과 API 계약을 대조하고 #1402/#1403의 원래 이관 보존, 새 read-only 실행·두 근거, Action 0건, 정상 미증명 시 diagnosed 유지, UI의 AI 조치 없음 문구를 확인한다. 이후 동일 자격인 최근 사건에만 확장한다. prd 장애 주입과 임의 이력 재실행은 금지한다.

## 실제 반영 및 검증 완료

- 구현 실행기의 Claude API 429 대기를 중단하고 이 작업만 `openai-codex:gpt-6.1-sol`로 전환했다. ChatGPT 구독 인증을 확인했다. 전역 프로필은 변경하지 않았다.
- API와 대시보드 모두 롤아웃 완료. Rancher Desktop overlay 이미지 지문을 실제 배포와 일치시켰다. API의 변경 대상 9개 파일은 실행 컨테이너와 일치하고, 그 외 모듈은 기존 이미지와 동일하다.
- 최종 무조치 Group 후 재확인은 두 관측 모두 분석 종료 뒤 새로 수집하도록 보완했다. 앞선 337개 집중 회귀 통과 뒤, 이 보완을 포함한 최종 관련 63개 시험이 추가로 모두 통과했다. 대시보드 168개 시험과 TypeScript·Vite 빌드 통과.
- #1402(dev), #1403(prd)를 먼저 재확인하고 동일 자격의 18건으로 확장했다. **총 20건이 두 차례 최신 관측으로 정상 확인**되었다. 모든 응답은 `read_only:true`, `cluster_changed:false`, Action 0건이며 AI가 복구했다는 의미가 아니다.
- 실제 권한 제한 사례 #1336 재평가는 HTTP 409로 거부됐고 `handed_off`를 유지한다. #1390/#1383/#1382/#1380 등 실제 진단 역할을 실행한 과거 이력은 이 API 대상이 아니므로 임의 재생하거나 결과를 바꾸지 않았다.
- 기존 사건 **1,371건 전부**, PVC **12개**의 UID·연결 볼륨, 재확인한 20건의 기존 Evidence·RoutingDecision·AgentRun·감사 행과 원래 이관 사유를 보존했다.
- K8s API 준비 상태 200, 실행 Pod 42개 모두 준비 완료, dev/prd 업무 서비스 18개 모두 HTTP 200을 확인했다.
- 브라우저 #1402에서 “복잡 정상 상태 확인”, “원인 미확정 · AI 조치 없음”, 진행 중 장애 0건을 확인했다. 지난 분석과 원문은 접힌 상태다.

실제 배포 이미지:

- liveprobe-api: `liveprobe/liveprobe@sha256:19aa6b58a6f256400274f8f27b9ddf794c7c6295af91a6ba6790caefb75a4079`
- dashboard: `liveprobe/dashboard@sha256:fba308bc9358e7e799f93b33010641bb8c397fe4c55ee49b30f56009eb844ac6`

남은 별도 운영 상태: Application 관제 API의 업무 준비 상태는 `runner:false`로 503이다. 호스트 실행기 프로세스가 없음을 확인했으며, 기존 전용 실행 설정·상태 경로를 확인하지 못해 새 권한이나 임시 실행 상태로 대체하지 않았다. Application 배포·DB·인증은 이 수정에서 변경하지 않았다. 이는 K8s 조기 이관 수정과 별도로 복구가 필요한 연결 상태다.

실제 증거: [실제 재평가·보존·상태 확인](../evidence/liveprobe-recovery-2026-11-14/premature-handoff-live-verification.json), [최종 63개 시험](../evidence/liveprobe-recovery-2026-11-14/premature-handoff-final-deployed.xml), [대시보드 시험](../evidence/liveprobe-recovery-2026-11-14/premature-handoff-dashboard-tests.log).
