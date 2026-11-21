# 통합 장애 관제 최종 계약·회귀 감사

작업: `t_7f5a998e` · 2026-11-20 01:54–02:13 KST · Hermes 검증 레인

## 판정과 담당 경계

최신 감독자 배정에 따라 제품 소스·dashboard·실행기 권한·mixed coordinator는 수정하지 않고 계약과 격리 회귀만 확인했다. 저장소는 `codex/unified-incident-product`의 공유 미커밋 작업본이다. 다른 레인의 변경을 되돌리거나 커밋·푸시·병합하지 않았다.

- 대시보드 빌드, 단위 203건, 단계 선택 2건, Chromium 화면 회귀 16건, PostgreSQL 기반 핵심 회귀 133건은 통과했다.
- Application 표준 전체 검사는 고유 시험 286건 중 284건 통과·2건 실패였다. 하위 검사까지 포함한 JUnit은 384건 중 382건 통과·2건 실패·오류/생략 0건이다. 전체 통과로 판정하지 않는다.
- 실패 2건은 두 번째 보완 회차에서 고정된 시험 시계와 실제 브라우저 시각이 달라 `qa_observed_time_invalid`로 거절되는 경우다. 제품의 시간 검증을 바꾸지 않고 시험 장치의 작업 시작 시각만 갱신한 별도 진단에서는 두 건 모두 통과했다. 이 진단은 표준 스위트 수정·재검증을 대신하지 않는다.
- 주 계획 문서는 감사 중 다른 레인이 갱신했다. 최신 본문에 `prd-risk/v2`, 원래 업무 귀속, 복구 재시도와 운영 전환 대기가 반영돼 있어 재작성하지 않았다. 이 문서는 해당 계획의 별도 재실행 근거다.
- 이 감사는 실제 운영 통합 API 전환, 외부 계정 설정 또는 prd 배포 승인을 뜻하지 않는다. 켜진 로컬 관제와 역할별 외부 자격 미설정은 구별한다.

## 직접 실행한 검사

원본 증거 디렉터리: 저장소의 `.hermes/evidence/t_7f5a998e-final-audit/`. 임시 소스·SQLite는 `/Users/hwany/.hermes/cache/scratch/` 아래에만 만들었다. 실서비스 비밀값·운영 DB·인증 파일은 읽지 않았다.

| 검사 | 직접 관측한 결과 | 증거 |
|---|---|---|
| Application 전체 | 286개 수집, 고유 2개 실패. JUnit 384개 중 실패 2, 오류·생략 0. 238.971초 | `application-collection.log`, `application.log`, `application.xml` |
| 통합·혼합·관측·허용 조치·dispatch | 133개 통과, 오류·생략 0. 폐기 PostgreSQL의 별도 DB/스키마만 사용 | `core.log`, `core.xml` |
| React 순수 시험 | 203개 통과, 생략 0 | `dashboard-unit.log` |
| 단계 선택 추가 시험 | 2개 통과 | `step-panel.log` |
| TypeScript·Vite | exit 0. 운영 번들을 덮어쓰지 않고 별도 출력 | `dashboard-dist/` 및 이 감사의 실행 결과 |
| 실제 Chromium·모의 관제 API | 16개 통과. 기존 사건 진입·원문 접근 동의·권한·보완 멱등 재시도·작업 필터·지도/알림 선택 유지. 지도는 1440×900과 1920×1080 모두 검사 | `browser.log`, `browser-results/` |
| 거절 원인 진단 | 두 실패 경로에서 `qa_observed_time_invalid`; dev 검증과 출시 소스 트리 재구성은 정상 | `missing-diagnostic.log` |
| 시험 시계 진단 | `Acceptance.run_job` 직전에만 `h.now=time.time()`을 적용한 별도 프로세스에서 2개 통과. 제품/시험 파일 변경 없음 | `missing-clock-diagnostic.log`, `missing-clock-diagnostic.xml` |

Application 정상 인수 경로의 HTTP 접수→수정 전 실제 시험 실패→수정 후 통과→폐기 서비스의 Chromium QA→사람 병합·승인 지문 검사→대역 prd 적용·복구→종료 후 같은 사건의 후속 메모는 표준 전체 검사에서도 통과했다. 외부 GitHub·모델·registry·Kubernetes/prd는 해당 시험의 대역이다. 표준 검사의 주문 브라우저 시험은 별도 폐기 루프백 화면을 조작한다. 이 결과를 운영자 등록 실제 dev 다섯 서비스의 새 QA 영수증이나 운영 승인 이력으로 계산하지 않는다. 실제 dev 다섯 서비스 검증과 후보 이미지 대조는 [주 계획의 감독자 검증 기록](../plans/unified-operator-console-2026-11-20.md)에 별도로 있다.

## 보완 회차 실패와 후속 확인

실패 시험:

1. `test_console_release_acceptance.py::test_missing_registered_scenario_starts_one_round[still-missing]`: 두 번째 `tests_missing` 뒤 기대 `handoff` 대신 `qa_failed`.
2. 같은 시험의 `[registered-test-added]`: 실제 QA `outcome=passed` 뒤 기대 `awaiting_prd_approval` 대신 `qa_failed`.

`Acceptance.__init__`은 `h.now`를 생성 때 고정하고, `run_job`은 recovery 작업에서만 다시 맞춘다. 두 회차의 실제 하위 시험과 Chromium 실행이 60초를 넘기면 QA 영수증 시각이 제어기 시험 시계보다 미래가 된다. `qa_verdict`는 `observed_at > now + 60`을 거절한다. 진단에서 두 번째 회차의 거절 코드가 이 조건과 일치했으며, 작업마다 시험 시각을 맞추면 두 분기가 모두 기대 상태에 도달했다.

담당 QA/제어기 레인이 인수 장치의 시계를 일관되게 만든 뒤 수정 없는 표준 명령으로 재실행해야 한다. 미래 영수증 제한을 늘리거나 거절을 건너뛰는 제품 변경은 필요하지 않다. 두 실패 원본과 진단 결과는 모두 보존했다. 이 공유 작업본의 표준 실패와 감독자가 별도 후보 환경에서 기록한 전체 통과를 하나의 결과로 합치지 않는다.

## 현재 구현과 대조한 계약

### 프로토콜·역할·상태

- 기존 `dev-prd/v2` 사람 시험·임대는 유지한다. 새 통합 릴리스 또는 QA 필수 설정은 `dev-qa-prd/v3`다. 서버가 정하며 브라우저가 고르지 않는다.
- v3 민감 작업은 source/cluster/qa 역할로 분리한다. 역할 없는 legacy 신원은 v3 일감을 받거나 구형 미귀속 임대를 갱신·보고할 수 없다. 작업은 인증 주체에 묶인다. 기존 v2 호환 임대와 v3 신규 접수의 분리는 이번 표준 시험에서도 통과했다.
- dev 코드 검증 뒤 연결 사건은 `dev_infra_pending`에서 코어의 인프라 결정을 기다린다. 독립 사건은 이 코어 관문이 없다. 순서는 코드 dev 검증→인프라 확인→prd 대상 읽기·위험/계획 지문 확정→QA→사람 승인→prd 확인→복구 관측이다.
- 내부 연결 이름은 `mixed_pending()`→`UnifiedService.advance_mixed(application_id, release_id)`→`record_mixed_infra(application_id, result)`다. 서버가 정한 mixed 필수 관문에는 `not_required`가 통하지 않는다. 검증 영수증은 현재 release·dev image·action·지문·pending 이후 시각과 대조한다. QA 대기열 및 승인에서도 관문을 다시 확인한다.
- `work=approval|qa_fix|recovery`와 `mega_stage`는 같은 Application 상태 투영을 사용한다. 요약의 `accepted`는 영수증의 자체 `outcome`과 별개다. 누락 필드는 과거 성공으로 채우지 않는다.

근거: `app/application/control.py:305,431–565`, `app/application/qa_release.py:58–100,487–640`, `app/application/browser_access.py:10–36`, `tests/test_unified_incidents.py`, `tests/test_mixed_remediation.py` (경로는 `liveprobe/` 기준).

### 서버 소유 필수 시험 명세

| 서비스 | 등록 시험 파일 | 전달할 업무 계약 |
|---|---|---|
| order | `src/test/java/io/liveprobe/target/order/OrderControllerTest.java` | `returnsInventoryData`, `notFoundIsNotRetried`: 상품 200의 주문 가능·재고 일치, 없는 상품의 404와 재시도 없음 |
| catalog | `tests/test_business.py` | `CatalogTests.test_search_detail_and_prices`: 검색 건수·가격, 상세 설명·재고와 200 |
| payment | `tests/test_business.py` | `test_history_has_fixed_retention_and_monotonic_ids`: 최근 100건 보존과 서로 다른 결제 ID |
| notification | `tests/test_business.py` | `test_enqueue_and_complete_all_channels`: 격리 fixture의 세 채널과 조회한 queue depth |
| delivery | `test/index.js` | `송장 등록·조회·순차 단계 변경·지연 목록`: 격리 지연 송장과 조회 결과 |

`Catalog.missing_controller_test(service)`가 등록 언어·프로필·test root를 대조해 `round_request.qa_requirement`에 정확한 파일·시험명·기대값을 저장한다. worker는 이를 다시 카탈로그와 대조하고 실행기 보고의 임의 파일 경로나 자유문을 소스 편집 지시로 사용하지 않는다. 첫 누락에서 이전 영수증을 보존한 보완 회차를 한 번 열고 새 소스를 수집한다. 다시 누락되면 인계한다. 현재 UI 지원은 위 다섯 서비스이며 inventory의 독립 UI는 등록돼 있지 않다. 격리 시험의 등록·쓰기 준비 동작을 실제 dev QA에서 재생하지 않는다.

근거: `app/application/catalog.py:14–28,68–80`, `app/application/qa_release.py:584–610,705–743`, `app/application/worker.py:221–234`.

### 영수증·위험·복구

- `qa-browser/v1`: 영수증 digest, release/artifact/plan, 등록 dev origin, dev 검증 이미지와 현재 이미지, 출시한 소스 트리·시험 파일 지문을 대조한다. 통과는 실제 GET 네트워크·DOM·스크린샷·독립조회 일치 증거가 있어야 한다. 실행기는 Deployment UID·generation·ready replica와 소유 ready Pod의 실제 image ID를 QA 전후 확인한다. 중간 배포 변화는 확인 불가다.
- `prd-risk/v2`: diff 미해석·대상/원복/복구 점검 부재는 불완전이다. 정적 검사에서 데이터 구문이 안 보이는 것은 `no_data_path_observed`이지 데이터 영향 없음의 증명이 아니다. 파괴적 변경은 현재 승인 경로에서 차단한다. 설명문이나 자기 보고로 이 관문을 우회하지 않는다.
- `prd-recovery/v1`: 제어기가 수집한 해당 사건·서비스·환경의 실제 실패 GET이 등록된 업무 경로와 일치해야 원래 업무에 귀속된다. 같은 서비스의 다른 경로나 `/healthz`의 정상 응답으로 해결하지 않는다. 성공 로그 관측과 허용된 무해 점검 두 경로 모두 귀속된 원래 오류 지문·HTTP status가 필요하다.
- 배포 시각은 저장된 `prd_verifying` 전이에서만 얻는다. 시각이 없으면 `recovery_deployment_time_unobserved`로 인계하고 현재 시각을 만들어 넣지 않는다. 미관측/확인 불가는 최대 3회, 후속 시도는 최소 60초 뒤에 수령한다. 원래 업무 미귀속이면 무해 점검을 허용하지 않고 계속 확인 불가일 때 `recovery_original_operation_unattributed`로 인계한다. 관련 오류를 다시 관측하면 즉시 인계한다.

근거: `app/application/qa_release.py:180–321,338–464,643–702`, `application_runner/executor.py:171–212`, `application_runner/recovery_probe.py`. 이번 표준 실행은 브라우저·복구 helper 음성 시험을 포함하지만, 제어기 영수증 변조/원래 업무 귀속/3회 고갈 각각에 대한 전용 회귀 파일까지 새로 추가한 것은 아니다. 해당 가드는 소스 대조와 기존 통합 시험 범위로 구분한다.

### 보완과 종료 후 기록

두 주소 `POST /api/incidents/{root}/code-action/supplements`와 `POST /api/application/v1/incidents/{native}/supplements`는 `{request_id, kind: revise|rerun_qa, note_ko}`를 받는다. 개인의 `incident:create`, 브라우저 같은 출처·CSRF, 연결 사건 출처 검증을 유지한다. 같은 요청 번호·내용의 재전송은 한 건이고 다른 내용은 충돌이다. 쓰기 중 보완은 결과가 확인된 안전 경계까지 보류한다.

완료 사건의 `revise`는 같은 사건의 읽기 전용 `followup_note`다. 작성자·시각·멱등 번호, 당시 closure·복구 영수증을 보존하고 `effects:'none'`으로 남긴다. 재분석·재배포·새 사건 생성은 없다. 완료 사건의 `rerun_qa`는 허용하지 않는다. 이 의미와 정상 종료 후 원래 사건의 기록 보존은 표준 인수 및 통합 회귀에서 통과했다.

## 운영·도구 경계 및 인계

1. 운영 통합 API/모델 작업자 전환은 감독자와 소유자의 별도 확인 범위다. 현재 로컬 홈이 켜져 있다는 사실을 새 통합 writer 및 외부 실행 준비 완료로 읽지 않는다. 실행 중 분석·임대가 없는지도 전환 직전에 다시 확인한다.
2. 외부 역할별 runner 인증·Kubernetes 최소 권한·PR 병합 조회용 Git 접근은 운영 준비 항목이다. 이 감사는 값을 만들거나 읽지 않았고 미설정을 구현 실패 또는 승인 우회로 처리하지 않았다.
3. 격리 검증용 컨테이너 `lp-t7f5-final-audit`(ID `e9dafcd1242354f95769b0faafc59f0095310211c44163e05db4679199211bbc`, loopback `127.0.0.1:32769`, 영구 볼륨 없음)를 새로 만들었다. 시험이 끝난 뒤 `docker stop`이 headless 승인 정책으로 거부돼 자동 정리하지 못했다. 감독자가 이름과 ID를 대조한 후 이 감사용 자원만 정리해야 한다. 기존 검토/운영 컨테이너는 건드리지 않았다.
4. `npx`의 package threat-intelligence 조회가 시간 초과돼 첫 브라우저 명령은 실행 전 거부됐다. 설치/다운로드 없는 기존 로컬 `node_modules/@playwright/test/cli.js`로 같은 격리 시험을 실제 실행했다. 컨테이너 종료 승인 거부는 다른 경로로 우회하지 않았다.
5. 감사 중 보드가 `blocked`로 바뀌어 heartbeat가 거절됐다. 보드의 마지막 자동 실행은 provider 모델 지원 거절로 남아 있지만 이 감사 프로세스의 검사 결과와는 별개다. Hermes 설정이나 모델은 수정하지 않았다. 카드 전체 완료나 운영 전환을 선언하지 않는다.

주요 로그·소스 지문은 같은 증거 디렉터리의 `manifest.json`에 기록했다. 소스 지문은 감사 종료 시점 스냅숏이며, 공유 작업본이 검사 기간 내내 불변이었다거나 감독자의 후보 이미지와 같다는 추가 증명은 아니다.

## 11월 20일 저녁 재검증

오전 감사에서 실패한 두 QA 보완 회차는 시험 장치의 시계를 바로잡은 뒤 같은 표준 스위트에서 모두 통과했다. 고유 시험 286개와 하위 검사 포함 384개 전부 통과했다. 같은 날 21:48 KST에 실제 로컬 통합 API와 4175도 전환했다. 기존 감사 결과는 당시 실행의 기록이다. [현재 운영 상태와 검증 범위](../plans/unified-operator-console-2026-11-20.md#로컬-적용-상태)
