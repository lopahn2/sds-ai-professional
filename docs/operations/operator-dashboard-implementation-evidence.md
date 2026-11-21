# 운영 대시보드 작업 검증 메모 (진행 중)

작업: t_7d34fbd9. 운영 배포는 감독자가 담당하며 이 문서는 로컬 구현/격리 검증만 기록한다.

## 합의 범위

- 두 화면에서 개인 bearer 수동 입력 제거. 브라우저가 pending 요청을 만들고 개인 자격 파일을 사용하는 CLI가 승인한다. 역할 선택만으로 권한을 주지 않는다.
- HttpOnly/SameSite 쿠키, 메모리 CSRF, scope/만료/취소, 실행기 분리, prd 승인 성공 후 일회 소멸.
- 로컬 루프백 게이트웨이 키가 있는 무토큰 요약 투영. Host나 전달 헤더만으로 로컬 사용자를 인증하지 않는다.
- 게이트웨이 front Host와 upstream Host는 배포자 고정값. 기존 ingress TCP를 재사용한다. 교차 출처 문서 진입 예외는 /와 /index.html의 HTML GET/HEAD만 허용한다.
- 실제 호출 직전 dispatch/v1의 안전한 메타데이터를 기록한다. 프롬프트/지시 원문은 저장하지 않는다. 실제 호출을 group보다 먼저 선택하고 운영자가 고른 단계는 유지한다.

## 관측된 검증

- test_dispatch_events.py: 8 passed (단일 call_id, 소비 ID 일치, 프라이버시, 구버전 narrative).
- test_human_sessions.py + test_local_gateway.py: 7 passed (실제 TCP, 고정 Host, 교차 출처 제한, *.localhost CLI loopback 접속 포함).
- Application 기본 API/분리/merge 선택: 43 passed.
- React 순수 시험 npm test: 189 passed.
- 새 focusStep/dispatchFor 시험 node --test test/stepPanel.test.mjs: 2 passed.
- 현재 단계 패널 포함 tsc + Vite 빌드 통과. 홈 변경 후 빌드는 별도 재실행한다.

## 아직 완료로 판단하지 않은 항목

- 전체 Application 회귀 첫 실행에서 실패 발생: 모델 ID 기대값이 이전 값인 2건, 403/422 경계 2건, 이전 bearer UI 시험들, pytest helper 오수집 1건. 변경에 맞는 기대값 및 경계 수정 후 재실행 필요.
- 실제 Chromium 무토큰 목록/필터/poll/상세/역할 진입 시험을 추가했으며 실행 결과를 후속 기록한다.
- 독립 UI 기존 실패 보고서 브라우저 시험과 통합 UI 역할·현재 단계 선택의 브라우저 회귀는 추가 확인 필요.
- 홈·패널의 최종 시각 검수와 reduced-motion, 전체 모델/dispatch 선택 재실행 필요.

커밋/푸시/운영 자격 열람/운영 DB 변경은 수행하지 않았다. 이 문서는 완료 증명서가 아니다.

## 최종 보완 화면의 격리 검증

- `92133f8` 기반 수정 작업본의 TypeScript·Vite 프로덕션 빌드 성공. 독립 Application 정적 UI 회귀 24건 통과.
- Chromium, 로컬 모의 API: 1440×940와 375×844에서 완료 사건 #971의 결과 기본 표시 → 전체 대화·이력 → 흐름 노드 키보드 선택 → 단계 표시 → 기본 단계 복귀를 확인했다. 완료 사건을 표본으로 표시하지 않았으며 반복하는 흐름선도 없었다.
- 375px에서 단계 패널이 141px로 좁아지는 문제를 세로 배치로 수정했다. 재촬영에서 데스크톱 패널 695px, 모바일 351px이며 가로 넘침이 없었다. 모바일 모션 감소 설정에서 패널 애니메이션이 꺼졌다.
- 동일한 두 화면에서 catalog 모의 404는 독립 Application 링크만 표시했고 사람 세션·통합 폼은 요청하지 않았다. 모의 403과 500은 각각 권한·조회 실패 안내로 구분했다. 독립 Application의 변경된 안내 문구를 양쪽 크기에서 렌더링했다. 이 세션에서 운영 배포나 실제 권한 승인은 실행하지 않았다.
- 홈도 같은 두 크기의 Chromium에서 검증했다. 현재 영향 서비스·자동 분석·승인 대기와 전체 기간 이관이 따로 보였고, 첫 사건 링크와 진단 토글을 확인했다. `execution`, `response-queue`, `stepPanel` 테스트 14건이 통과했다(재시도, 역순·중복 이벤트, 선택 유지, 미관측과 0 분리 포함).
- 역할 검증을 재실행했다: `test_human_browser`, `test_human_sessions`, `test_local_gateway`, `test_core_release`, `test_core_invariants`, `test_unified_release_flow` 27건 통과. 실제 Chromium의 요약·역할 시작, 쿠키·CSRF·범위·일회 승인, 합성 사건의 tester dev 시험과 prd-approver 승격 및 교차 역할 거부를 각각 검증했다. 두 역할이 브라우저에서 실제 승인까지 진행한 별도 합성 검수는 운영 문서 32–38행에 기록되어 있다. `test_dispatch_events`를 포함한 별도 재실행에서 DB 연동 4건은 pgvector 접속 설정 미제공으로 건너뛰었고 이전 실행의 백엔드 기록과 혼동하지 않는다.

## 배포 인계 확인

- 통합 대시보드 TypeScript·Vite 프로덕션 빌드 통과. 단위 및 단계 선택 시험 192건 통과.
- `operator-dashboard.mock.spec.mjs`: 실제 Chromium, 모의 API, 375·768·1440px에서 12건 통과. 노드 선택 → 현재 단계 복귀 → 전체 대화·이력 전환 → 단계 패널 복귀, 모션 감소, 카탈로그 404 링크 전용 및 403/500 오류 분리를 확인했다.
- `liveprobe/application_tests`: 254건 및 하위 시험 96건 통과. 실제 로컬 ASGI·Chromium의 무토큰 첫 사건 요약, 역할 확인 시작, 좁은 화면과 실패 시험 결과 표시를 포함한다. 독립 UI는 별도 번들링 없이 ASGI가 정적 파일을 제공한다.
- 이 빌드의 운영 배포 및 실호스트 최종 점검은 감독자 인계 범위다. 위 결과는 운영 배포나 전체 Playwright 회귀의 성공을 뜻하지 않는다.
- 위에서 생략된 DB 검증은 운영 DB와 분리한 임시 로컬 pgvector에서 보완했다. `test_dispatch_events.py` 전체 8건 통과, 생략 없음. 실제 API 호출 경계에서 호출 ID·전달된 운영자 메시지 ID·이벤트 저장 및 구버전 서사 계약을 검증했다.

## 재검토: 신규 회귀 2건

- 단가 시험은 현재 버전 `2026-11-18`과 신규·과거 모델 여섯 개의 캐시 포함 추정값을 확인한다. 기존 단가표와 저장된 실행 이력은 변경하지 않았다.
- 통합 API는 bearer와 세션 승인·권한 재확인에 같은 활성 Application 인증 설정을 사용한다. 수정 전 설정 교체 후 인증 없는 상세 요청이 `503`이던 것을 재현했다. 격리 pgvector에서 수정 후 인증 없음 `401`, 합성 조회자의 상세 조회 `200`과 사건 생성 `403`을 각각 확인했다. CLI에서 승인한 쿠키 세션에도 같은 제한이 적용되며, 활성 인증 파일을 조회 범위(`incident:read`)가 없는 설정으로 교체하면 bearer와 기존 세션 모두 상세 요청을 `403`으로 거부한다.
- `test_llm_providers.py`, `test_model_lineup_config.py`, `test_unified_incidents.py`: 67건 통과(격리 pgvector, 생략 없음). `test_human_sessions.py`, `test_unified_application.py`, `test_core_release.py`: 32건 통과. 기존 K8s 실패 30건은 수정 범위 밖으로 전체 스위트는 재실행하지 않았다. UI와 운영 배포는 변경하지 않았다.

## 추가 재검토: Chromium 세션 시작 순서

- `test_ui_failure_browser.mjs`는 클릭 전에 정확한 세션 생성 POST의 `201` 대기를 등록하고, 응답의 `pending` 상태·조회 범위·승인 코드를 확인한 뒤 CLI 승인 `200`과 표시된 역할을 검증한다. 생성 직후 별도 `current` 조회를 하던 경합을 없앴다. 앱 동작은 바꾸지 않았다.
- 실제 로컬 API·합성 데이터로 390px/1440px 모두 통과했다. Application 전체 재실행은 254건과 하위 시험 96건 통과. JUnit과 브라우저 증거는 `/private/tmp/liveprobe-t7d34fbd9.520frTjx/`에 저장했다.
