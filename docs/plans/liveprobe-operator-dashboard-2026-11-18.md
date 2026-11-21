# 🛰️ 운영 대시보드·단계별 에이전트 관제·역할별 승인

2026-11-18 · Hermes(칸반 t_7d34fbd9) · 운영 UX / 실시간 관제 / 모델 ID

---

> 💡 **범위** — `.hermes/plans/liveprobe-operator-dashboard-2026-11-18.md`(구현 전 검토)의 6개 변경을 소스·격리 검증까지 진행한다. 클러스터·실제 DB·실제 모델·Git 원격 변경과 운영 인증 자료 조회는 하지 않는다. 로컬 배포와 실서버 확인은 이 작업을 연 세션이 맡는다. Jira는 쓰지 않는다.

## 1. 출발점 (2026-11-18 관측)

- 브랜치 `codex/unified-incident-product` @ `92133f8`. 미추적 `docs/operations/local-server-recovery-2026-11-18.md`는 보존한다.
- 대시보드 단위 189 통과, `tsc --noEmit`+`vite build` 통과(작업 전 기준선, 산출물은 scratch 경로).
- 로컬 Codex 모델 목록(`~/.codex/models_cache.json`, 2026-11-18T14:15Z)에 `gpt-6.2-sol`, `gpt-6.2-astra` 존재. `gpt-6-luna` 유지.
- 공식 단가(developers.openai.com/api/docs/pricing, 2026-11-18 조회, Standard·짧은 문맥, 1M 토큰당): gpt-6.2-sol 입력 $2.00·캐시 $0.10·출력 $10.00 / gpt-6.2-astra 입력 $8.00·캐시 $0.40·출력 $40.00. 출시 공지(openai.com/index/introducing-gpt-6-2-astra-sol)와 일치.
- Naria 모션 토큰(`@naria/ui` 1.0.1 tokens.json·styles.css): press 120ms, hover 160ms, select 220ms, expand 240ms, enter 280ms, `--nds-ease` cubic-bezier(.22,.8,.22,1).
- 변경 전 화면 캡처 `/private/tmp/liveprobe-ui-before-tcYoLA/{home,application}.jpg`: 홈은 `미인수 이관 · 누적 1,192`가 현재 수치(진행 중 0·승인 대기 0) 옆에 같은 위계로 놓이고 지도와 경고 목록이 폭을 나눠 쓴다. Application 첫 화면은 bearer 입력이 사건 목록을 가린다.

## 2. 결정 기록 (소유자 승인 반영)

| # | 결정 | 이유·대안 |
|---|---|---|
| D1 | **소유자 선택 — 토큰 입력 없이 조회·역할별 승인까지 적용**. 사건 조회는 최소 범위, 생성·dev 시험·병합 확인·prd 승인은 서버가 기존 개인 권한을 확인한다. 화면에 bearer를 입력하지 않는다. 독립 Application 호스트와 통합 화면에 개인 자격으로 단기 브라우저 세션을 여는 실제 경로를 제공한다. | 기존 로컬 ingress는 모든 인터페이스에서 수신한다. Host·클라이언트 제공 역할만으로 권한을 발급하면 안 된다. 실행기 자격은 사람 세션을 만들 수 없다. |
| D2 | 모델 계층: 낮음 `gpt-6-luna`(유지), 중간 `gpt-6.1-sol`→`gpt-6.2-sol`, 높음 `gpt-6-astra`→`gpt-6.2-astra`. 저장된 설정·환경변수의 이전 ID는 **새 실행에서만** 새 ID로 대응한다(저장 행을 조회 때 고쳐 쓰지 않음). 실행별 스냅샷·AgentRun 모델명·과거 비용은 그대로 두고 이전 단가는 과거 비용 추정용으로 남긴다. 평가 하네스의 과거 실험 모델은 바꾸지 않는다. | 저장 설정의 Literal 검증이 이전 ID를 거부하면 새 실행이 모델 호출 전에 실패한다. 단가 버전 `2026-11-18`, 출처 URL을 카탈로그에 함께 내보내 이전 단가를 새 공식 단가처럼 표시하지 않는다. |
| D3 | 실시간 계약 `dispatch/v1`: `agent_started.detail.dispatch`에 실제 호출 직전 최종 프롬프트에서 계산한 공개 항목만 기록한다 — 역할 업무(`task_ko`, 역할 사전의 공개 설명), 근거 참조 ID(`evidence_refs`, `rag_refs`), 이 호출에 포함된 운영자 메시지 ID(`operator_message_ids`), 모델·추론 강도·`call_id`, 프롬프트 지문(`prompt_sha256`)·길이. 프롬프트 본문·비밀·내부 추론은 저장하지 않는다. 진행(`agent_progress` phase)·공개 답변(text)·확정 결과(`agent_finished`)는 계속 별도 이벤트다. | 구버전 사건에는 `dispatch`가 없고 화면은 "업무 지시 미기록"으로 표시한다(역산 금지). |
| D4 | 홈: 상단 `지금 대응할 일`을 **현재 기준**(승인·인수 필요 / 진행 중 분석 / 영향 서비스)과 **누적·기간 표기**(미인수 이관 전체 기간 등)로 분리한다. 지도는 전폭, 경고·네임스페이스·클러스터 진단·자원 상세는 보조 패널(열고 닫는 서랍)로 둔다. 이력 검색 링크 유지. | 미관측은 0이 아니라 `미관측`으로 둔다. |
| D5 | 관제실: 흐름도 옆 기본 패널을 **단계 패널**로 바꾼다. 기본은 현재 호출을 따라가고, 병렬 실행 역할은 전환 버튼, 노드 선택은 고정(새 이벤트에도 유지)·`현재 작업으로` 복귀를 제공한다. 패널은 목적·근거·판단·다음 분기·실행 내용(대기 안내 1곳)을 보인다. 대화·지시는 같은 열의 두 번째 보기로 유지한다. 승인·인계는 머리말의 즉시 조치로 연결한다. | 기존 "노드 클릭→대화 카드 스크롤"은 위치 찾기 부담이 있었다. |
| D6 | 모션은 Naria 토큰만 사용: 버튼 press, 노드 선택 select, 상세 교체 enter/expand. 실제 실행 중인 분기의 연결선만 움직인다. `prefers-reduced-motion`에서 추적 이동·반복 모션을 끈다. | 상태는 글자·아이콘으로도 읽힌다. |
| D7 | 독립 Application 화면은 외부 자원 없이 Naria 토큰 값을 정적 CSS에 옮기고 대시보드와 같은 메뉴 이름·단계 상태 문구를 쓴다. 첫 화면은 사건 목록과 사람 시험·승인 대기다. | 정적 파일 시험(외부 URL 금지)을 유지한다. |

## 2.1 실행 계약 (구현 레인 공통 기준)

### C1 · 개인 역할 세션 (`human-session/v1`, D1 구현)

- 브라우저는 bearer를 입력·보관하지 않는다. 역할 세션은 **브라우저 시작 → 로컬 CLI가 개인 자격 파일로 승인 → 브라우저 활성화**의 기기 승인 방식이다. 역할 버튼 클릭·Host 문자열·클라이언트 헤더만으로는 어떤 권한도 생기지 않는다.
- 엔드포인트(독립 Application: `/api/application/v1/human-sessions…`, 통합 화면: `/api/human-sessions…`, 같은 구현·같은 프로세스 저장소):
  - `POST …/human-sessions` `{scope}` (`incident:read|incident:create|tester|prd-approver`) → 대기 세션 생성, `lp_human_session` 쿠키(HttpOnly, SameSite=Strict, Path=/api, 무작위 32바이트, 서버는 SHA-256만 보관) 설정, 응답 `{state:'pending', scope, user_code:'XXXX-XXXX', expires_at}`. 대기 5분. 같은 출처·`X-LiveProbe-Client: dashboard` 필수.
  - `GET …/human-sessions/current` → `{state:'none'|'pending'|'active'|'expired', scope?, scopes?, subject?, user_code?(pending), expires_at?, csrf_token?(active)}`.
  - `DELETE …/human-sessions/current` → 브라우저 로그아웃(활성 세션은 CSRF 필요), 쿠키 삭제.
  - `POST …/human-sessions/approve` (CLI 전용, `Authorization: Bearer <개인 human 자격>`, `{user_code, scope}`) → 사람 자격 파일의 주체만 허용(실행기 자격 403 `identity_kind_forbidden`), 주체가 해당 scope를 가져야 함(403 `scope_forbidden`), Origin·Sec-Fetch-* 헤더가 있는 요청 거부. 활성 scopes = {요청 scope} ∪ ({incident:read} ∩ 주체 scopes). 만료 15분, `prd-approver`는 10분이며 승인 1회 성공 시 소멸.
  - `POST …/human-sessions/revoke` (CLI, bearer) → 해당 주체의 모든 세션 폐기.
- 동작 엔드포인트의 인증: 기존 bearer 경로는 그대로 유지(기계·CLI 호환). bearer가 없으면 세션 쿠키 → 활성·미만료·scope 포함 확인, 변경 요청은 `X-CSRF-Token`(세션 csrf_token) + 같은 출처(Origin/Sec-Fetch-Site) + `X-LiveProbe-Client: dashboard`. 실행기(runner) 경로는 세션을 받지 않는다. 저장소는 API 프로세스 메모리(1 replica, 재시작 시 재로그인).
- CLI: `liveprobe/application_runner/human_login.py` — `approve --token-file <0600 개인 파일> --code XXXX-XXXX --scope tester --url <API 기준 주소>`, `revoke …`. 토큰 값은 출력·로그하지 않고 symlink·느슨한 권한 파일을 거부한다.

### C2 · 로컬 읽기 경계 (`local-read/v1`, D1 일반 조회)

- 운영 플랫폼의 ingress는 `*:80`에서 수신하므로 Host·peer IP로 로컬을 인정하지 않는다. 서버 설정 `LIVEPROBE_LOCAL_READ_KEY_FILE`(K8s 코어)·`APPLICATION_LOCAL_READ_KEY_FILE`(Application)이 있을 때만 `X-LiveProbe-Local-Read` 헤더의 키(상수 시간 비교)를 **루프백 게이트웨이가 주입한 로컬 요청**으로 인정한다.
- 루프백 게이트웨이 `scripts/liveprobe-local-gateway.py`: 127.0.0.1에만 바인드, 클라이언트가 보낸 같은 헤더·`X-Forwarded-*` 제거 후 키 주입, 자기 Host:port 이외 요청 거부(DNS rebinding 방지), SSE 스트리밍 전달. 키 파일은 0600·symlink 거부.
- 키가 구성되면 K8s 코어 `require_user`는 모든 사용자 API에 키를 요구한다(웹훅·실행기 bearer 경로 제외). 구성되지 않으면 기존 동작과 같다.
- 무토큰 조회는 **요약 투영만** 준다: Application `GET /api/application/v1/summaries`, `GET …/incidents/{id}/summary`(상태·환경·서비스·시스템·사유·다음 행동·대기 사람 작업·시각; 로그·근거 표본·diff·소스·역할 원문·내부 이벤트 제외). 원문 조회는 `incident:read` 세션 또는 bearer.

### C3 · 호출 공개 기록 (`dispatch/v1`, D3)

`agent_started.detail.dispatch = {version:'dispatch/v1', role_ko, task_ko, evidence_refs:[…], rag_refs:[…], operator_message_ids:[…], prompt_sha256, prompt_chars}` — 실제 호출 직전 최종 프롬프트(운영자 지시 반영 후)에서 계산. 본문·비밀·내부 추론 미저장. 기존 `agent_progress`(call_id·attempt_id·validation_round·phase·output_mode·text)와 `agent_finished`(call_id·status)는 그대로다. `operator_applied.detail`에 `call_id`를 추가해 발신(`operator_message`)·해석(`steering_interpreted`)·전달(`operator_applied`+`dispatch.operator_message_ids`)을 구분한다. 서사 응답의 에이전트 흐름 노드에도 같은 객체를 `node.dispatch`로 붙인다(해당 call_id의 `agent_started`에 있을 때만). 화면은 `node.dispatch ?? 같은 call_id의 agent_started.detail.dispatch`를 쓰고 둘 다 없으면 "업무 지시 미기록"이다.

## 3. 진행

- [x] 의도·현재 소스·화면 캡처·Naria 토큰·모델 목록·공식 단가 확인
- [x] 격리 시험 기반: scratch venv(uv sync --frozen), tmpfs pgvector 임시 컨테이너, E2E 포트·출력 경로 환경변수화(`playwright*.config.mjs`)
- [x] 실행 레인 구현·검증 (아래 4절)
- [x] 통합 회귀·빌드·E2E
- [x] 문서·DESIGN 계약 갱신

## 4. 검증 기록

- [화면 계약](../../dashboard/DESIGN.md#현재-운영-화면-계약-2026-11-19-위-세대별-배치-설명에-우선), [구현 검증](../operations/operator-dashboard-implementation-evidence.md), [운영 배포 확인](../operations/operator-dashboard-rollout-2026-11-18.md).
- 프로덕션 빌드, 대시보드 단위 및 단계 선택 시험 192건, 375/768/1440px Chromium 집중 시험 12건, 임시 pgvector의 dispatch 계약 8건 통과.
- 감독자 최종 확인: 운영 Deployment 3개 각각 1/1, 두 UI의 배포 파일 검증 12/12 일치, 실호스트 1440/820/390px에서 홈·단계 선택/복귀·대화·Application 이동 통과. 별도 합성 사건에서 두 사람 역할로 dev 시험과 prd 승인을 검증했으며 운영 사건에 승인을 제출하지 않았다.
- 감독자의 현재 브랜치 임시 자료 전체 Application 회귀 JUnit 결과: 350건, 실패·오류·건너뜀 0건. 하위 시험을 별도로 센 구현 검증 기록과 집계 단위가 다르다.
