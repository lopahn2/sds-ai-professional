# P18 W6 실행 계약 — 2영역 장애 관제실, 판단 과정, 단계별 대화형 지시

상태: 구현 계약. 화면·API 완료를 뜻하지 않는다. 원 요구는 `P18-incident-conversation-brief.md`, 이 문서는 그 위에 사용자 추가 지시(2026-10-25)를 반영한 구현 기준이다.

## 사용자 추가 지시(원문 요지)

1. 운영자 인계 카드의 "권장 조치(원문)"이 아니라 **요약문과 결정사항을 명확하고 간결하게** 보여 준다.
2. 기존 "02 근거 · 한글 해석"과 "03 운영자 대화"를 **하나로 합친다**.
3. "사건 수준 판단"은 근거만이 아니라 **판단을 내린 과정**을 보여 준다. **Group 선택** 단계에도 그 내용이 들어 있어야 한다.
4. Group이 작업하는 단계(분석·검증 Agent, Group)를 클릭하면 **그 내용이 보이고 그 자리에 채팅창이 생겨**, 운영자가 말을 걸면 **steering으로 바로 상호작용**한다.

## 1. API 계약 (백엔드, `GET /api/incidents/{id}/narrative`)

기존 필드는 모두 유지한다(하위 호환). 추가 필드만 정의한다. 모든 한글 문장은 규칙 문장 또는 기존 한글 해석기(Interpretation 캐시, 표시 전용)에서 온다. 기록되지 않은 판단 과정을 지어내지 않는다. 값이 없으면 `null`과 미기록 사유를 둔다.

### 1.1 FlowNode 추가 필드

- `at`: 단계 발생 시각 ISO 문자열 또는 `null` (signal=incident.created_at, evidence=첫 수집 시각, router/group=RoutingDecision.created_at, agent=AgentRun.created_at, action=Action.created_at, outcome=incident.updated_at, steer=메시지 created_at, command=created_at).
- `card`: 단계 종류별 구조화 내용. `card.kind`로 구분한다.

#### signal
`{"kind":"signal","what_ko":str,"place_ko":str,"source":str|null,"reason":str|null,"signal_count":int,"first_at":iso|null}`
- source/reason은 감사 기록 `incident_created`(source, reason)에서. 없으면 null.

#### evidence
`{"kind":"evidence","total":int,"ok":int,"failed":int,"items":[{"id":int,"source":str,"source_ko":str,"target":str,"ok":bool,"error":str|null,"summary_ko":str|null,"ref":str|null,"collected_at":iso|null}]}`
- `total` = 그 사건의 Evidence 행 수 = 흐름 노드 라벨의 "근거 수집 N건"의 N = `items` 길이. 같은 집합.
- `summary_ko`는 해당 ref의 규칙/AI 한글 해석 첫 문장(없으면 null). ref 대응: deployment→E1_deployment, pod_status→E2_pods, service→E3_service, endpoints→E3_service, k8s_event→E5_recent_events, prometheus→E6_metrics, loki→E7_error_logs, node→E8_node, configmap→E9_config, code_refs→null.

#### router (사건 수준 판단) — 판단 과정
```
{"kind":"routing","routing_id":int,"level":str,"level_ko":str,"decided_by":str,"decided_by_ko":str,
 "confidence":float|null,
 "steps":[{"key":"features|rule|llm|final|model","title_ko":str,"detail_ko":str,"items_ko":[str],"source":"rules|llm|policy"}],
 "final_reason_ko":str|null, "reason_raw":str,
 "llm":{"level":str,"confidence":float,"rationale_raw":str,"rationale_ko":str|null}|null,
 "model_policy":{"selected_model":str|null,"reason_ko":str,"candidates":[{"model":str,"eligible":bool,"score":float|null,"reason_ko":str}]}|null,
 "interpreting":bool}
```
`steps` 순서와 내용:
1. `features` 관측 특징 확인 — `RoutingDecision.features`를 한국어 사실 목록으로(규칙 문장). 예: "준비된 Pod 1개", "반복 재시작 없음", "메모리 초과 종료 없음", "최근 배포 없음(현재 버전 2, 296.8분 전)", "Service 연결 대상 있음", "호출 서비스 실패 없음", "감지 신호: TargetHigh5xxRate", "수집 출처 10종, 수집 오류 없음". 값이 없는 키는 생략하고 '없음'과 '미관측'을 구분한다.
2. `rule` 규칙 판정 — `router.rules_decide`의 고정 문장을 한국어로 대응(접두 일치). 예: "no deterministic SIMPLE/NORMAL/HIGH_RISK pattern matched." → "단순·보통·고위험 규칙에 해당하지 않아 기본값 복잡으로 판정". OOM/지연 → 보통, 이미지/반복재시작+최근배포 → 단순, NetworkDrop/보호자원/라우팅 → 고위험, 수집 실패·근거 없음·분류기 실패 → 고위험 인계. `RoutingDecision.rationale` 앞부분(규칙 문장)에서 판정한다.
3. `llm` AI 분류 의견 — router AgentRun.output(level, confidence, rationale). rationale 한글은 해석기 항목 `routing:<routing_id>:llm`. 해석 전이면 `rationale_ko=null`, `interpreting=true`.
4. `final` 최종 결정 — rationale의 "LLM agrees" / "LLM raised level A->B" / "LLM suggested lower level ...; conservative rule level kept" / "rules win for HIGH_RISK" / "rules keep COMPLEX" / "confidence below threshold" 를 한국어 결정 문장으로. 운영자 재지정(decided_by=operator)은 "운영자가 Group을 바꿔 다시 분석".
5. `model` 분석 모델 선택 — policy.selected_model, candidate_scores(점수·적격·'below quality floor'→"품질 기준 미달", 'unknown price or throughput'→"가격·처리량 정보 없음"), 가중치. reason_ko 예: "적격 후보 중 가중 점수(품질 0.6·비용 0.25·처리량 0.15)가 가장 높은 gpt-6.1-sol 선택".

#### group (Group 선택)
```
{"kind":"group","routing_id":int,"level":str,"group_ko":str,
 "decision":{"level_ko":str,"decided_by_ko":str,"confidence":float|null,"final_reason_ko":str|null,"steps":<router.steps와 같은 배열>},
 "roles":[{"role":str,"role_ko":str,"job_ko":str,"model":str|null,"effort":str|null,"status":str}],
 "budget":{"max_agent_calls":int|null,"max_cost_usd":float|null,"deadline_s":int|null}|null,
 "progress_ko":[str],"status_ko":str}
```
- roles는 감사 `wf_group_started.roles`(모델·추론강도) + 흐름 STAGES의 역할 순서. status는 해당 역할 노드 상태.
- progress_ko는 같은 사건의 감사 `wf_early_stop`(stage, reason), `wf_escalated`, `wf_budget_exceeded`, `wf_group_finished`(status, candidates, budget.agent_calls, estimated_cost_usd)를 한국어로. 예: "반론 검토가 결론을 지지해 재추론을 생략", "독립 검증이 결론을 지지해 추가 회차 없음", "Group 종료: 검증 통과 · Agent 호출 5회 · 추정 0.4427 USD".
- 재판단으로 Group이 여러 개면 각 Group 노드가 자기 RoutingDecision을 가진다.

#### agent / verify
```
{"kind":"agent","agent_run_id":int|null,"summary_raw":str|null,"summary_ko":str|null,
 "candidates":[{"cause_raw":str,"confidence":float|null,"verifier":str|null}],
 "verdict":str|null,"verdict_ko":str|null,"next_action":str|null,"next_action_ko":str|null,
 "counter_evidence_count":int,"error":str|null,"accepts_steering":bool,"steer_target_role":str}
```
- summary_ko는 기존 `conclusion_ko`(agent 항목 해석). verdict_ko: supported→"근거로 지지", contradicted→"반박됨", insufficient→"근거 부족", not_applicable→null. next_action_ko: handoff→"운영자 인계", investigate_more→"추가 조사 필요", none→"조치 없음", act→"Kubernetes 조치 제안".
- `accepts_steering` = 사건이 종료 상태가 아니고 이 단계가 planned/running이거나 같은 Group의 이후 단계가 남아 있음. 계획(planned) 노드도 card를 갖는다(agent_run_id=null, 요약 null, accepts_steering=true).

#### action
`{"kind":"action","action_id":int,"type":str,"type_ko":str,"target":str,"status":str,"status_ko":str,"changes":[str]}`

#### outcome
`{"kind":"outcome","status":str,"status_ko":str,"handoff":<handoff_view>|null}`

### 1.2 `narrative.handoff_view` (인계·종료 요약, 없으면 null)
```
{"summary_ko":str|null,"summary_raw":str|null,
 "decisions":[{"key":"outcome|cause|auto_action|operator","label_ko":str,"value_ko":str}],
 "next_steps_ko":[str],"next_steps_raw":[str],"interpreting":bool}
```
- decisions 예:
  - outcome: "결과" → "운영자 인계 · 자동 복구 아님" / "운영자 종료 · 서비스 복구 판정 아님" / "자동 복구 완료".
  - cause: "원인 판단" → 첫 원인 후보 한글(해석기 항목 `handoff:cause#0`) + "(신뢰도 0.65, 독립 검증: 근거로 지지)". 해석 전이면 "한글 해석 준비 중".
  - auto_action: "자동 조치" → remediation.kind none → "실행하지 않음 — " + why 한글(`handoff:why`); 조치 실행이면 조치 이름·결과.
  - operator: "운영자 결정" → 상태별 규칙 문장(인계: "원인을 확인한 뒤 대화로 조치를 지시하거나 장애를 종료하세요." 등).
- next_steps_ko: recommended_steps 최대 3개 한글(`handoff:step#i`), 각 1문장으로 간결하게. 원문은 next_steps_raw로만.

### 1.3 해석기 항목 추가 (`interpret.incident_items`)
- kind=`decision`: `routing:<id>:llm`(router 출력 rationale).
- kind=`handoff`: `handoff:summary`, `handoff:cause#0`, `handoff:why`, `handoff:step#0..2`.
- 이 두 종류는 규칙 해석(`rule_ko`)을 적용하지 않는다(agent와 동일). 근거 목록 표시(`narrative.raw`)에는 넣지 않거나, 넣더라도 `kind`로 화면에서 제외 가능해야 한다. 표시 전용이며 분석 판단에 쓰지 않는다.
- 해석 프롬프트는 "1~2문장, 한국어만, 원문에 없는 원인·조언 추가 금지" 기존 규칙을 따른다. 인계 항목은 "운영자가 바로 이해할 결정 문장으로 간결하게"를 덧붙여도 된다.

### 1.4 단계 대상 지시 (steering 대상 지정)
- `POST /api/incidents/{id}/messages` 본문에 선택 필드 `target_role`(등록된 역할 이름만, 그 외 422), `target_node`(문자열 ≤80, 표시용).
- 저장: `OperatorMessage.intent_detail`에 `target_role`, `target_node`(기존 `target_level` 유지). 이벤트 `operator_message`와 `steering_interpreted`의 detail에 `message_id`, `target_role`, `target_node` 포함.
- 응답: 기존 `{message_id,intent,target_level,ack_ko,applies}` + `target_role`, `target_node`, `hold`(bool).
- `steering.take(iid, role, level)` 규칙:
  - 대상 없는 메시지: 기존과 동일(다음 Agent 경계에서 반영).
  - `instruction`/`escalate`에 target_role이 있고 `role != target_role`이며, target_role이 현재 level의 계획 역할에 있고 이 사건에서 아직 `agent_started` 기록이 없으면 **보류**(소비하지 않음).
  - 그 외(대상 역할이 이미 시작했거나 현재 Group 계획에 없음)는 이번 경계에서 반영하고, `operator_applied` detail에 `applied_to_role`, `targeted_role`, `fallback: true`를 남긴다.
  - reroute/stop/cluster_change 의미와 안전 경계는 바꾸지 않는다. 사건 종료 후 메시지와 cluster_change는 기존대로 명령(계획→부작용→확인) 경로.
- ack_ko(대상 지정 instruction): 보류면 "<역할 한글> 시작 전에 이 지시를 전달합니다.", 이미 시작했으면 "<역할 한글>은 이미 실행되어 다음 단계 Agent에게 전달합니다."
- 흐름 steer 노드는 target_role이 있으면 그 역할 노드에 연결한다.

## 2. 화면 계약 (대시보드 `IncidentRoom`)

### 2.1 2영역
- 데스크톱: `01 AI 분석 흐름`(왼쪽) | `02 분석 · 운영자 대화`(오른쪽). 기존 별도 "근거 · 한글 해석" 열과 "운영자 대화" 열을 없앤다. 탭(모바일)은 `흐름` / `대화` 두 개.
- 02는 하나의 시간순 대화: 단계 카드(흐름 노드 순서·`at`), 운영자 메시지·AI 지시 해석·반영 기록, 조치 승인·인계·지시 실행·변경 PR·종료 카드. 하단에 분석 제어와 입력창 고정.
- 다른 실행 기록 이벤트(agent_started 등)는 단계 카드와 중복이므로 카드 안의 시각/상태로 흡수하고 대화에 따로 늘어놓지 않는다.

### 2.2 노드 클릭 → 카드 이동·펼침·채팅
- 흐름 노드 클릭/Enter: 02의 같은 노드 카드(`id="step-<node.id>"`)로 부드럽게 스크롤(움직임 감소 시 즉시), 일시 강조, 카드 펼침. 모바일은 대화 탭으로 전환 후 이동.
- 펼친 router/group/agent/verify 카드에는 내용과 **그 단계 전용 채팅창**이 나타난다(사건이 closed가 아니면). 보내면 `target_role`(agent/verify: 그 역할, group: 그 Group의 다음 미실행 역할 또는 생략, router: 생략)과 `target_node`를 담아 `/messages`로 보낸다.
- 그 카드 안에 그 노드를 대상으로 한 운영자 메시지·AI 지시 해석(ack)·반영 상태("○○ 시작 전에 반영됨" / "반영 예정" / "대상 단계가 실행되지 않아 반영되지 않음")가 스레드로 바로 보인다. 전송 응답의 ack_ko를 즉시 표시하고 이후 이벤트 폴링으로 갱신한다.
- 분석이 끝난(resolved/handed_off/failed) 사건의 카드 채팅은 "분석이 끝나 실행 계획 확인 절차로 접수됩니다" 안내 후 기존 명령 경로를 사용한다.
- `상세보기`: 오른쪽 사이드 패널(기존 03 폭 수준, 1440에서 약 340px)에 원문·메타(모델·추론 강도·토큰·비용·소요·입력 근거 원문)를 연다. 닫기/Esc로 닫고 포커스 복귀, 작성 초안·스크롤 보존. 노드 클릭만으로 패널을 열지 않는다.

### 2.3 단계 카드 기본 내용
- 감지: what_ko, 위치, 출처·사유, 시각.
- 근거 수집: "수집 N건 · 성공 a · 실패 b"와 N개 전체 행(출처 한글·대상·상태·짧은 한글 요약). 각 행에서 원문 보기(상세 패널 또는 접기).
- 사건 수준 판단: 결정 수준·판단 주체·신뢰도, **판단 과정 5단계**(관측 특징 → 규칙 판정 → AI 분류 의견 → 최종 결정 → 분석 모델 선택)를 단계 목록으로. 영어 원문은 상세에서만.
- Group 선택: Group 이름, 선택 이유(위 판단 과정 요약: 최종 결정·규칙 판정·AI 의견), 역할 구성(역할·담당 업무·모델·추론강도·상태), 예산, 진행 기록(progress_ko).
- 분석/검증: 한글 결론(없으면 "해석 준비 중" 또는 실행 전 상태), 원인 후보 수·신뢰도, 검증 판정(verdict_ko), 다음 행동.
- 조치: 조치 이름·대상·변경 값·상태.
- 결과/인계: handoff_view의 요약·결정사항 표·다음 단계(최대 3개, 한글). "권장 조치(원문)" 목록은 기본 표시하지 않고 `원문 보기` 접기 안에만 둔다.

### 2.4 인계 카드(`HandoffCard`)
- 제목 "운영자 인계", 인수 상태 배지 유지.
- 본문 순서: 한글 요약(summary_ko) → 결정사항 표(결과·원인 판단·자동 조치·운영자 결정) → 다음 단계(한글, 최대 3개) → 인계 인수 버튼 → 원문(인계 사유·권장 조치 원문)은 `<details>` 안.
- 해석 전에는 "한글 요약을 준비 중입니다" 상태와 결정사항의 규칙 문장만 보이며 영어 원문 목록을 대신 펼치지 않는다.

### 2.5 유지할 안전 경계
조치 승인·거절 확인, 인계 인수는 권한 아님, 운영자 종료 조건·사유·확인, 명령 계획→사전 검사→부작용→"정말 진행하시겠습니까?", Secret 전용 입력, 변경 PR·단계 승격 카드, 종료 사건 입력 차단, 분석 제어(일시정지·재개·중단 확인). 모두 02 대화 안에서 동작해야 한다.

## 3. 검증 기준
- 백엔드: 새 필드 단위/DB 시험(판단 과정 5단계, Group 내용, 근거 N=items, 인계 요약·결정, 해석 항목 생성·규칙 미적용, 대상 지시 보류/대체 반영/422, 이벤트 detail). 기존 전체 백엔드 시험 회귀.
- 화면: 단위 시험, `npm run build`, 모의 브라우저 시험(1440/768/375). U1 두 영역·상시 근거열/03열 없음, U2 각 노드 클릭 → 정확한 카드 이동·펼침, U3 근거 N행, 판단 과정 5단계와 Group 내용 표시, 단계 채팅 전송 → ack 즉시·대상 표시·반영 상태 갱신, 인계 카드 원문 목록 기본 미표시, 상세 패널 열기/교체/닫기/Esc, 기존 승인·인계·종료·명령·PR 회귀. 캡처를 직접 보고 한글 잘림·겹침 확인.
- 실배포 검증은 부모가 수행(캠페인 진행 중이므로 안전한 중단 지점에서 API 재기동).
