# 🔎 디자인 리뉴얼 후 전체 검증

날짜: 2026-11-15 (한국 시간) · 작성: Codex · 실행: 요청받은 현재 세션

**판정: 전체 통과 아님. 리뉴얼의 운영자 대응 표시 오류 2건을 새로 재현했고, 기존 시험 실패와 Application 실행기 준비 실패를 확인했다.**

## 목차

- [실행 범위](#실행-범위)
- [새로 확인한 리뉴얼 오류](#새로-확인한-리뉴얼-오류)
- [전체 시험 결과](#전체-시험-결과)
- [기존 문제와 실패 원인](#기존-문제와-실패-원인)
- [보안 및 데이터 보존](#보안-및-데이터-보존)
- [검증 근거와 후속 완료 기준](#검증-근거와-후속-완료-기준)

## 실행 범위

- 요청 시각 01:42:17 → 4시간 대기 → 05:42:17 이후 `main`을 `pull --ff-only`로 갱신했다. 실제 pull 완료 UTC: `2026-11-14T20:42:34.075465+00:00`.
- 대상 커밋: `2146f442a831519dcbc20db7883c93c6210536c5`. 비교 기준: `b34cb6eac123be01517f203709757f9e07e92925`. 최종 원격 확인에서도 대상 커밋이 main이었다.
- 별도 세션·예약 자동화·하위 에이전트로 넘기지 않았다. 공개 소스 체크아웃에서 실행했으며 다른 세션의 작업 파일을 변경하지 않았다.
- 화면 단위/타입/빌드, Docker 이미지, 3개 화면 폭 E2E, PostgreSQL 백엔드, 평가, 배포/운영 스크립트, 주문·재고·결제·상품·알림·배송·회원·도메인 서비스를 검사했다.
- 실제 운영 API는 조회만 했다. 조치·배포 테스트는 격리 또는 모의 환경에서 실행했다. 운영 클러스터에 새 장애를 주입하거나 이번 소스를 배포하지 않았다.

## 새로 확인한 리뉴얼 오류

| 우선순위 | 재현 결과 | 원인과 필요한 수정 |
| --- | --- | --- |
| P1 | 미인수 이관 1건과 분석 중 1건을 보고하는 응답에서 “운영자 결정이 필요한 장애는 없습니다”라고 표시하고 인수할 사건을 대기열에 표시하지 않는다. | [Overview.tsx:69](https://github.com/lopahn2/sds-ai-professional/blob/2146f442a831519dcbc20db7883c93c6210536c5/dashboard/src/Overview.tsx#L69)는 전체 서버 집계와 별개인 지도 표시용 일부 사건 목록으로 대응 여부를 판단한다. 미인수 이관은 전체 진행 목록의 상태에도 포함되지 않는다. 전체 집계와 일치하는 대응 목록/바로가기를 제공하고, 지도 표시 기간 밖 사건을 대응 없음으로 판단하지 않아야 한다. |
| P2 | API가 이미 인수한 사건을 `claimed: true`로 보내도 대기열은 `claimed: false`, “인수 필요”로 표시한다. | [responseQueue.ts:34](https://github.com/lopahn2/sds-ai-professional/blob/2146f442a831519dcbc20db7883c93c6210536c5/dashboard/src/responseQueue.ts#L34)는 `handoff.claimed_by`만 읽지만 [overview API](https://github.com/lopahn2/sds-ai-professional/blob/2146f442a831519dcbc20db7883c93c6210536c5/liveprobe/app/main.py#L416)는 `claimed` 필드를 반환한다. 두 응답 계약을 정규화하고 실제 API 모양으로 회귀시험해야 한다. |

첫 오류는 [브라우저 재현 결과](../evidence/renewal-full-validation-2026-11-15-2146f44/queue-contract-probe.json)와 [화면](../evidence/renewal-full-validation-2026-11-15-2146f44/queue-contract-probe.png), 두 번째는 [응답 계약 재현](../evidence/renewal-full-validation-2026-11-15-2146f44/claimed-contract-probe.json)으로 확인했다. 합성 사건만 사용했으며 운영 사건의 상태를 바꾸지 않았다.

실제 데이터에서도 미인수 이관 1,175건과 진행 중 0건이 표시되지만 대기열은 비어 있었다. 이 누적 값에는 과거 사건이 포함되므로 모두 현재 장애라는 뜻은 아니다. 과거 미인수 기록과 현재 대응할 일을 명확히 구분하는 UX가 필요하다.

## 전체 시험 결과

| 범위 | 최종 결과 | 설명 |
| --- | --- | --- |
| 화면 단위시험 | 175 통과 | 최신 잠금 파일로 설치 |
| 타입 검사·웹 빌드·Docker 빌드·nginx 설정 | 통과 | 컨테이너는 읽기 전용 파일 시스템에서도 설정 검사 통과 |
| 리뉴얼 후 E2E, 375/768/1440px | 176 통과 / 93 실패 / 7 건너뜀 | 총 276건 |
| 리뉴얼 전 E2E, 동일 폭·조건 | 158 통과 / 93 실패 / 7 건너뜀 | 총 258건, 별도 포트의 기준 버전 |
| 리뉴얼 전후 비교 | 기존 실패 93건 동일, 기존 시험에서 새 실패 0건 | 새 리뉴얼 시험 18건은 모두 통과. 위 2개 오류는 기존 자동 시험이 다루지 않는 응답 조건에서 추가 재현 |
| 실제 API 연결 화면 | 9개 조합 통과 | 운영 관제·장애 목록·관제실 × 3개 폭. JS 오류 0, 가로 넘침 0 |
| 백엔드 전체 | 24 failed, 1417 passed, 11 subtests passed in 405.27s (0:06:45) | 2GiB 전용 시험 DB로 재검증. 연결 오류 0 |
| RAG 관련 독립 재검증 | 15 통과 | 전체 시험의 스키마 오염을 분리하면 정상 |
| 평가 | 79 통과 / 2 건너뜀 | 공개하지 않은 과거 저장소가 필요한 2건은 명시적으로 건너뜀 |
| 배포·운영 스크립트 | 425 통과 / 18 실패 / 2 건너뜀 | 전역 차단 훅을 설치하는 worker 시험을 별도 프로세스로 분리. 하위 시험 334건 추가 통과 |
| 별도 선택한 DB 재시작·Kafka 경계 시험 | 2 실패 | 두 시험 모두 dev/prd 카탈로그와 생성기의 dev/stg/prd 계약 불일치에서 중단 |
| 사건 작업자 복구 독립 시험 | 9 통과 / 12 실패 | 21건. 실행 소유권·중단/복구 계약과 시험 대역의 정합성은 추가 확인 필요 |
| 주문·재고 | 180 통과 | 주문 122, 재고 단위 55, 실제 DB 통합 3. 최초 통합시험 1건 연결 시간 초과 후 같은 절차 재실행에서 모두 통과 |
| 결제·상품·알림 | 147 통과 | 각각 51·48·48 |
| 배송·회원 | 71 통과 | Node 22에서 각각 35·36 |
| 도메인 서비스 | 15 통과 | 새 PostgreSQL에서 업무·HTTP·TLS 계약 검증 |

실패한 시험을 성공으로 바꾸기 위한 제품 소스 수정은 하지 않았다. 전체 실패 목록과 원인은 [기계 판독 결과](../evidence/renewal-full-validation-2026-11-15-2146f44/summary.json)에 보존했다.

## 기존 문제와 실패 원인

### 운영 준비 상태

- Kubernetes 관제 `/api/healthz`, `/api/readyz`는 200이고 DB·백그라운드·사건 작업자가 준비 상태였다. Prometheus/Kubernetes/Hubble/Envoy 수집 상태도 연결로 보고됐다.
- Application `/api/application/v1/readyz`는 **503**이다. DB, 사람/실행기 인증, 프로필, 모델 구성은 준비됐지만 **`runner: false`**다. 모델 최근 성공 기록과 실행기 연결 준비는 별개다. 실행기 연결을 복구하고 실제 작업 수신까지 확인해야 운영 준비 완료로 볼 수 있다.
- 위 상태는 기존에 배포된 서버의 조회 결과다. 새 프론트엔드는 변경 요청을 405로 막는 전용 중계 서버를 통해 실제 데이터와 연결했다.

### 신규 설치와 시험의 재현성

1. [기반 생성기](https://github.com/lopahn2/sds-ai-professional/blob/2146f442a831519dcbc20db7883c93c6210536c5/scripts/samsung_infra.py#L17)가 아직 `dev/stg/prd`를 고정하고 있어 현재 `dev/prd` 카탈로그의 렌더링을 거부한다. README에 기재된 기존 설치 제한이 다시 재현됐다. stg를 되살리는 방식으로 통과시키면 안 된다.
2. 백엔드 실패 24건 중 7건은 [SQLite 시험 도우미](https://github.com/lopahn2/sds-ai-professional/blob/2146f442a831519dcbc20db7883c93c6210536c5/liveprobe/tests/isolated_sqlite.py#L45)가 공용 SQLAlchemy metadata의 계산 열을 제거하고 복원하지 않아 이후 PostgreSQL 시험에 영향을 주는 경우다. 관련 15건을 새 프로세스에서 실행하면 통과했다.
3. 나머지 백엔드 실패는 dev/stg/prd 및 옛 승격 계약 8건, 비공개 과거 증거 의존 5건, 자동 해석 요청의 옛 기대 1건, 캐시 fingerprint가 빠진 대역 1건, 스트리밍 이전 이벤트/응답 기대 2건이다. 실제 기능을 퇴역한 계약으로 되돌리는 대신 합의된 현재 계약과 시험을 맞춰야 한다.
4. 스크립트 실패는 빈 ServiceAccount 토큰 Secret도 일괄 금지하는 옛 검사 5건, 개인 경로의 CEL 준비 자료를 요구하는 시험 2건, 옛 환경 수/입력 지문 기대 2건, 기반 생성기 계약 불일치 9건이다. `test_p18_incident_worker_recovery.py`의 전역 audit hook은 같은 프로세스의 다른 시험을 차단하므로 분리 실행해야 한다. 분리 후에도 남는 복구 시험 12건은 미해결로 기록했다.
5. 브라우저 실패는 리뉴얼 전후 같은 시험 목록에서 발생했다. 접힌 과거 분석 카드의 옛 선택자/표시 방식과 지도·상세 표시 계약 등에 관련된다. 동일 실패라는 사실이 모든 기존 실패가 무해하다는 뜻은 아니다.

백엔드·배포·서비스 소스와 Python 의존성은 이번 리뉴얼 커밋에서 바뀌지 않았다. 브라우저는 기준 버전을 실제 재실행하여 비교했고, 백엔드는 현재 커밋 전체 실행과 실패 원인 분리로 검사했다.

### 시험 환경에서 바로잡은 사항

- 최초 시험 DB 512MiB에서 OOM 및 연결 오류 29건이 발생했다. 소유 표식이 있는 시험 DB만 2GiB로 늘려 전체 시험을 다시 실행했고 연결 오류는 사라졌다. 최초 로그도 함께 보존했다.
- 호스트 Node 26에서 배송·회원의 `node --test test`가 0건을 찾았으므로 통과로 계산하지 않았다. 프로젝트 기준 Node 22에서 실제 진입점 `test/index.js`를 실행해 71건을 검증했다.
- 운영 DB·기존 저장 이력은 시험 DB로 사용하지 않았다.

## 보안 및 데이터 보존

- Gitleaks로 전체 공개 Git 변경 이력 및 새 naria 압축 패키지를 풀어 검사했다. 탐지 0건. 이는 검사 범위에서 탐지되지 않았다는 결과이며 보안 무결성 전체를 보증하는 표현은 아니다.
- `npm audit`는 개발 의존성 `vite 6.4.3`, `source-map-js 1.2.1`에서 높은 위험도 2건을 보고했다. 두 버전은 리뉴얼 전부터 같았다. [source-map-js 권고](https://github.com/advisories/GHSA-68fv-2mgg-jv7q), [Vite 권고](https://github.com/advisories/GHSA-rq7h-c2jc-7f22)를 참고해 개발 도구 의존성을 갱신하고 재검증할 필요가 있다. 이 경고를 정적 nginx 배포의 원격 실행 취약점으로 단정하지 않았다.
- DB/PVC/Secret/운영 이력은 변경하거나 삭제하지 않았다. 이번에 생성한 임시 시험 DB만 소유 표식을 확인한 뒤 정리했다. 실제 화면 캡처와 상세 시험 로그는 로컬에 보존하고 원격 저장소에 게시하지 않았다.
- 테스트한 소스 커밋은 변경하지 않았다. 기존 main에 대한 추가 커밋·운영 배포 없이 검증 기록만 남겼다.

## 검증 근거와 후속 완료 기준

- [전체 결과/실패 목록](../evidence/renewal-full-validation-2026-11-15-2146f44/summary.json) · [pull 기록](../evidence/renewal-full-validation-2026-11-15-2146f44/pull.json)
- [리뉴얼 E2E](../evidence/renewal-full-validation-2026-11-15-2146f44/e2e-report.json) · [기준 버전 E2E](../evidence/renewal-full-validation-2026-11-15-2146f44/e2e-baseline-report.json)
- [백엔드 전체](../evidence/renewal-full-validation-2026-11-15-2146f44/backend-confirmed.xml) · [RAG 독립 시험](../evidence/renewal-full-validation-2026-11-15-2146f44/backend-rag-isolated.xml)
- [운영 준비 상태](../evidence/renewal-full-validation-2026-11-15-2146f44/runtime-readonly.json) · [실제 화면 9개 조합](../evidence/renewal-full-validation-2026-11-15-2146f44/live-ui-smoke.json)
- [운영 관제 화면](../evidence/renewal-full-validation-2026-11-15-2146f44/live-overview-1440.png) · [모바일 관제실](../evidence/renewal-full-validation-2026-11-15-2146f44/live-room-375.png)
- [Git 비밀정보 검사](../evidence/renewal-full-validation-2026-11-15-2146f44/secrets.json) · [패키지 비밀정보 검사](../evidence/renewal-full-validation-2026-11-15-2146f44/vendor-secrets.json) · [의존성 검사](../evidence/renewal-full-validation-2026-11-15-2146f44/npm-audit.json)

- [ ] 미인수 집계와 대응 안내가 지도 표시 기간과 무관하게 일치한다.
- [ ] 실제 `claimed` API 응답을 사용해 인수 완료 사건을 다시 인수 요청으로 표시하지 않는다.
- [ ] Application 실행기가 준비 상태이며 실제 작업 수신을 확인한다.
- [ ] dev/prd 기반 생성기가 독립 설치에서 렌더링·사전 검증을 통과한다.
- [ ] 공개 저장소에서 비공개 자료 없이 시험을 재현하며, SQLite/전역 훅이 다른 시험에 영향을 주지 않는다.
- [ ] 남은 실패의 기대 계약을 정리한 뒤 전체 시험과 보안 의존성을 다시 검증한다.
