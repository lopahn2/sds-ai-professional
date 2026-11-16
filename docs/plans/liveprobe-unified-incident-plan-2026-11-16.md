# 🛰️ LiveProbe 통합 사건 설계

2026-11-16 · Codex · 사건 통합 / 보존형 전환

---

> 💡 **합의 범위** — 제품 하나·실행 위치 둘·권한 분리는 소유자 확정 사항이다. 마지막 지시로 설계 후 소스 구현·격리 검증·작업 브랜치 푸시까지 진행한다. 클러스터 변경, 기존 DB 이전 실행, Secret 본문 조회, 실제 모델 호출, main 병합은 제외한다. Jira는 쓰지 않는다.

## 1. 현재 상태 대조

기준 main: `04a3e384b868be3dbff748978edb6e964b8c2b89`. `과제계획서.md`는 초기 의도이며 확정 명세가 아니다. 기존 제품 분리 판단(`docs/reviews/project-direction-2026-11-09.md`)은 이번 제품 통합 지시로 대체하지만 보존·권한 경계는 유지한다.

2026-11-16 읽기 전용 조회에서 rancher-desktop/liveprobe-system의 Application Deployment·Service·Ingress와 DB StatefulSet이 모두 존재했다. `deploy/application/README.md`의 11월 9일 미설치는 과거 기록이다. 존재가 인증·준비·실행기 연결·복구 성공을 의미하지 않는다. Secret·DB 본문은 조회하지 않았다.

| 개념 | K8s | Application | 구별 |
|---|---|---|---|
| 사건 | models.Incident: bigint ID, namespace/workload/system, status, severity, handoff, datetime | application.storage.Incident: string ID, state, epoch float, JSON document | 논리 루트와 코드 조치 실행을 구분; native ID 보존 |
| 실행 | WorkflowRun(status/phase/snapshot), AgentRun(role/model/usage/output) | analysis.roles/analysis lease, Job(kind/status/fence/receipt) | 조사 시도와 외부 일감은 같은 개념 아님 |
| 근거 | Evidence.payload, Citation→RagChunk/RagDocument | collection.evidence, source_snapshot 파일/해시, role citations | RAG 인용·관측 근거·소스 인용의 출처/해시 유지 |
| 조치 | Action(policy/executed/before/after), OperatorCommand(plan/dry-run/confirm) | patch/test_build/release와 deploy/verify/reconcile Job | 코드 수정은 조치 유형; 시험 통과와 배포 검증은 별개 |
| 승인 | Action 승인, 지시 계획 해시 확인, 인계 인수 | tester 시험, prd-approver 승인, release/artifact/plan/image 지문·만료 | 인계 인수나 K8s 승인은 prd 승인 아님 |
| 종료 | resolved/handed_off/failed/closed | completed/handoff/failed, reconciliation_required | dev 정상·prd 완료·쓰기 불명을 구분 |
| 인증 | auth.require_user: Host/동일 출처 검사, 고정 local-operator; 사용자 인증 아님 | human/machine 별도 파일, incident:read/create, tester, prd-approver, runner scope | local-operator에 사람 승격 권한 부여 금지 |
| UI | IncidentRoom/StepCards/ChangeRequestCard | application/static 별도 UI | 같은 관제실에서 코드 조치로 표시, 과거 URL/ID 보존 |

근거: `liveprobe/app/models.py`, `liveprobe/app/application/{contracts,storage,control,worker,releases,security}.py`, `liveprobe/app/{incidents,engine,router,agent_groups,flowgraph,narrative,changes,change_api,handoff_classes,safety,runtime_fencing}.py`, `dashboard/src/{IncidentRoom,StepCards,ChangeRequestCard,Shell}.tsx`, `dashboard/DESIGN.md`.

### 확인한 제약

- `repositories.json`은 auto_merge=false/human_merge_required_before_prd=true/each_promotion_requires_confirmation=true다. stg 정책 값은 과거 자료이며 새 승격은 `app/environments.py`·Application catalog의 dev/prd만 따른다.
- Application source/build는 로컬 빌드 영수증 기반이며 PR 생성·사람 병합 관측 경로가 없다(`application_runner/{source,build,executor}.py`). 구형 `changes.py`의 merge 검사는 현재 Application 성공 증거가 아니다.
- 현재 runner는 여섯 종류를 모두 자기신고 claim한다(`application_runner/worker.py`). 소스 쓰기와 배포 권한이 한 프로세스에 함께 있을 수 있다. 통합 모드는 job 종류·주체를 서버에서 제한해야 한다.
- Codex 로그인은 소스 저장소 커밋이 아니라 APPLICATION_CODEX_HOME/auth.json 등 운영 저장소/PVC에 둔다(README 5·9장, application/settings.py/worker.py). 실제 로그인 성공은 확인하지 않았다. 분석 CLI는 현재 클러스터 측이며 호스트 runner와 다르다.
- 현재 main CI의 backend 임포트 오류·화면 E2E 실패는 사용자 제공 출발점이다. 이번에 고치지 않으며 통합 전용 검사와 전체 기존 실패를 구분한다.

## 2. 통합 사건 모델

**추천: K8s 사건을 논리 루트로 두고 Application DB는 코드 조치 실행 저장소로 유지한다.** 루트 ID는 기존 숫자 ID다. 추가 연결 표는 incident_id, action_id, application_id, 요청 해시, 연결 상태/시각을 가진다. 설치 ID를 포함한 결정적 child ID로 두 DB 사이 재전송·응답 유실을 복구한다. 과거 사건은 자동 backfill/재실행하지 않는다.

```text
신호 → 근거 수집(RAG/관측) → 수준 판단 → 그룹 분석/독립 검증
  ├ K8s 허용 조치 → 검증 → 해결
  ├ 검증된 코드 원인 → 코드 조치 → 실행기 대기 → 소스 출처 확인
  │ → 분석/수정 → 재현·회귀 시험 → dev 배포/검증 → 사용자 시험
  │ → 사람 병합 확인 → prd 승인 → prd 배포/검증 → 해결
  └ 권한·데이터 안전 경계 / 제한된 복구 소진 → 인계
```

기존 K8s status는 역사 사실로 남기고 통합 응답의 phase/state/next_ko/observation을 운영 상태로 사용한다. K8s 분석 종료와 전체 사건 종료를 혼동하지 않는다. 조회는 연결 생성·모델·만료·조치를 실행하지 않는다.

코드 연결 조건: 활성 dev/prd·등록 서비스/프로필, 실제 수집 코드 위치와 일치하는 code_fix/both, 독립 검증 통과, HIGH_RISK 아님, 결과 불명·데이터/인증 변경 아님, 진행 중 Action/OperatorCommand/구형 변경 없음. analyst 자유 문장이나 마지막 remediation만으로 연결하지 않는다. 자동 연결은 **새 분석 완료 경계에서만** 실행한다. 기존 사건은 명시 요청 후 같은 검사로 연결한다. Application은 소스 출처/인용/위험/패치 검사를 새로 수행한다.

등록/출처/권한 불명, Secret·파괴적 데이터 변경, HIGH_RISK, 쓰기 결과 불명은 인계/조정 유지. 실행기 미접속은 ‘실행기 연결 대기’이며 자동 인계하지 않는다. 코드 조치 중 K8s 변경을 병행하지 않는다.

## 3. 실행기 계약

기존 pull JobResponse(id/kind/incident_id/payload/lease_token/fencing_token/idempotency_key/lease_expires_at)를 유지한다. 통합 ID·요청 해시는 출처 메타데이터이며 자유 명령·저장소 URL·호스트 callback은 받지 않는다. 호스트가 클러스터 API에서 claim/heartbeat/result를 호출한다. 클러스터에서 호스트로 들어가는 통로는 없다.

소스 실행기는 source/test_build/PR, 클러스터 실행기는 collect/deploy/verify/reconcile만 담당한다. 서버는 인증 주체의 허용 종류와 capabilities 교집합을 배정하고 heartbeat/result도 동일 주체인지 검사한다. 소스 계정은 클러스터 변경 권한이 없고 배포 계정은 repo 쓰기 권한이 없다. 레거시 모드는 별도 유지하며 통합 모드의 제한을 명시 설정한다.

연결 중간 실패는 결정적 ID/요청 지문으로 같은 child를 조회·재사용한다. 다른 payload는 409다. 보고는 result hash로 멱등이다. 읽기/빌드 lease 만료는 fence를 바꿔 재배정한다. deploy 만료·쓰기 불명은 reconciliation_required와 대상 잠금을 보존하며 재배포하지 않는다. runner_recent는 최근 claim 관측일 뿐 작업 성공이 아니다. 마지막 관측/연결 실패/대기/실행을 구분한다.

PR 정책 증명 없이는 통합 코드 조치의 prd를 막는다. 시험·병합·승인은 별도 gate다. PR 공급 설정 전에는 ‘병합 확인 대기’로 보이며 병합을 추정하지 않는다. 실제 repo PR/인증 설정·실행은 운영 단계에서 검증한다.

## 4. 권한

| 주체 | 허용 | 금지 |
|---|---|---|
| 운영자 | 조회/조사/계획 확인/인계 인수 | 인수로 시험·prd 권한 대체 |
| tester | 정확한 release/artifact/plan/image dev 시험 | 기계 신원·다른 산출물 시험 재사용 |
| prd-approver | 유효 시험·병합 증거가 있는 버전 승인 | 만료·지문 변경·기계 승인 |
| 관찰 | K8s/RAG/관측 수집 | 클러스터 변경·repo 쓰기 |
| K8s 조치 | 기존 allowlist/RBAC/admission 내 변경 | repo 쓰기·Application 소유 대상 침범 |
| 코드 실행기 | 등록 소스·격리 시험·빌드·PR 생성 | 클러스터 변경·main 병합 |
| 배포 실행기 | 등록 dev/prd CAS·검증·조정 | repo 쓰기·승인 생성 |
| 조정 API | 루트 연결·읽기 투영 | local-operator 권한 승격·repo 쓰기 |

넓어지는 지점은 API의 별도 Application DB 접근과 같은 출처 API다. 기본 비활성·명시 설정으로 시작하고 사람 scope 검사를 유지한다. 두 DB 계정·인증 저장소를 합치지 않는다. tester/승인자 서로 다른 사람 강제 여부는 별도 결정이며 현재 scope 분리만 유지한다.

## 5. 이전 선택지

| 선택 | 절차 | 되돌리기 | 보존 |
|---|---|---|---|
| A 즉시 단일 DB | 모든 document/job/event 복제, ID/해시 대조, writer 전환 | 중단·신규 쓰기 역복제 필요 | 복잡한 스키마·권한 변경. 이번 실행 제외 |
| **B 루트 연결+DB 유지(추천)** | 추가 연결 표, 신규부터 결정적 연결, 과거는 명시 import | 신규 연결 off, 진행 job drain/불명 reconcile, 구 API 조회 | native ID/원문·승인·시각 그대로 |
| C 새 이벤트 저장소 | append-only backfill/watermark 대조 후 writer 전환 | old writer 복귀 전 역동기화 | 장기 확장성, 운영 복잡도 큼 |

B는 물리 이전을 강제하지 않는 추가형 구현이다. DB 삭제/기존 기록 변경/인증 이전은 하지 않는다. stg는 읽기 전용. 되돌릴 때 새 표를 drop하지 않고 연결/실행 기록을 남긴다.

## 6. 관제실

제목 → 문제·원인·상태·다음 행동 요약 → 요청할 때 펼치는 원문. 코드 조치 카드에 단계/대기 이유/관측된 시험/환경별 검증/시험·승인 gate를 표시한다. null은 미관측이다. stack/diff/JSON/로그는 원문 보기 안에 둔다. RAG/모델/추론/진행 스트리밍은 유지한다.

같은 관제실에서 Application 사람 인증을 별도로 검증한다. 브라우저에 기계 자격을 전달하지 않는다. 인증은 DOM 비밀번호 입력 후 메모리에만 보유하며 URL/로그/React state/localStorage/sessionStorage에 저장하지 않고 화면 이탈/해제 시 지운다. scope 오류는 빈 성공으로 표시하지 않는다. 승인은 정확한 지문과 명시 확인을 요구한다.

Application 메뉴는 내부 진입점으로 바꾼다. 과거 K8s 변경과 Application 이력은 출처·native ID를 표시하며 자동 매칭하지 않는다. 새 Application 사건도 루트 사건을 만들고 같은 관제실로 이동한다.

## 7. 구현 단계·완료 기준

| 단계 | 완료 기준 | 검증/원복 |
|---|---|---|
| P1 계약·연결 | 추가 schema, 결정적 child, 요청 멱등/충돌, 읽기 무부수효과 | 합성 DB 경합/응답 유실/원문 보존. feature off, 기록 보존 |
| P2 단일 사건 | 새 검증 경계 자동 연결, root API, App-origin 루트 | 검증 없음/고위험/진행 중/불명 쓰기 차단. 기존 이력 재실행 없음 |
| P3 권한·승격 | 서버 job 종류·주체 제한, 통합 prd 병합 gate | 위조 capabilities/타주체/만료/fence/시험 지문/병합 증거 거부 |
| P4 관제실 | 같은 사건 카드·상태·원문 folding·사람 시험/승인·내부 메뉴 | 단위/빌드·합성 UI; 배포/복구 성공과 구분 |
| P5 운영 전환(제외) | DB/인증/PR/실행기 설정 후 dev 실제 인수·점진 활성화 | 실제 readback/PR·시험·사람 병합·무승인 prd 차단. off/drain/reconcile |

이번은 P1–P4 소스 구현이다. P5 전 실제 운영 통합 성공은 주장하지 않는다. main 전체 CI의 기존 실패는 별도 기록한다. pytest 격리 DB/transport만 사용하며 실제 모델·운영 DB·클러스터 write는 금지한다.

## 8. 열린 결정

| 결정 | 선택지 | 추천·현재 기준 |
|---|---|---|
| 모델 | 구독 CLI / API key 무인 | 소유자 결정 전 CLI 유지. 클러스터 분석 인증과 호스트 실행기 인증을 구분 |
| 과거 목록 | 출처별 / 통합 목록·native ID 병기 / 수동 사건 연결 | 공통 입구·출처 병기, 자동 병합 없음; backfill 별도 |
| 저장 | A/B/C | B 추가 연결 구현; 물리 이전 안 함 |
| 사람 | scope만 / 서로 다른 subject 강제 | 현재 scope 구분 유지 |
| PR 공급 | GitHub App / 사용자 gh / 검증 서비스 | 등록 repo·PR만; 병합 자격 없음. 연결 전 prd 차단 |
| 자동 연결 | 신규만 / 과거 재분석 포함 | 신규 검증 사건만. 과거는 명시 요청 |

## 진행·검증

- [x] main/의도/지정 소스·화면/현재 설치 존재 조사
- [x] 합의 범위·설계·열린 결정·완료 기준 기록
- [ ] P1–P4 구현·전용 회귀
- [ ] 비밀 검사·커밋·브랜치 푸시
- [ ] P5 운영 전환: 범위 제외
