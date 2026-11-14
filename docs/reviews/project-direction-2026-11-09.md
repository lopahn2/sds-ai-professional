# 🧭 LiveProbe 기능·UX 정돈안

2026-11-09 · Codex · `현황 검토` `데이터 보존`

---

> 💡 **핵심 판단** — K8s와 Application 두 제품은 유지한다. 문제는 확장 기능의 존재가 아니라, 두 제품의 책임과 진입점이 겹치고 저장 이력의 조회가 새로운 실행과 섞이며 실제 제공 상태가 분명하지 않다는 점이다.

> ✅ **11월 11일 구현 결과** — 원본 프로젝트 소스에 역할 분리, 전체 이력 검색, 조회와 해석 요청 분리, 보존형 내보내기를 반영했다. 아래 문제 사례는 변경 전 관측이며, 현재 검증 수준은 다음 표에 기록했다.

| 운영자가 하는 일 | 변경 후 동작 |
| --- | --- |
| 기존 K8s 사건 열기 | 저장된 근거·해석·모델을 표시한다. 추가 해석은 비용 안내와 명시적 요청을 거친다. |
| 코드 오류 대응 | K8s는 기존 변경의 패치·시험·처리 상태를 제공한다. 신규 변경 생성과 환경 승격은 Application에서 수행하며, 기존 K8s 승격 API는 410을 반환한다. |
| 과거 실행 찾기 | K8s 환경·상태·출처·검색어와 Application 환경·상태·검색어를 전체 이력에 먼저 적용한다. 양쪽 모두 페이지 이동을 지원한다. |
| 결과 읽기 | 지도 그룹에서 자원 종류와 수를 구분한다. 비용은 부분합과 결측 수를 함께 표시한다. Application은 현재 단계·다음 행동과 모델 실행 구성을 표시하고, 자동 갱신 때 작성 중인 기록을 보존한다. |
| 시험 자료 재사용 | 제품별 DB에서 원문 대신 참조와 해시를 가진 스냅샷을 새 파일로 내보낸다. 기존 오프라인 평가 명령이 스냅샷의 해시를 확인하고 역할별 비용을 집계한다. 사용법은 원본 프로젝트 README의 ‘저장된 실행으로 역할별 비용 확인’에 있다. |

**검증 환경.** 원본의 기존 미커밋 작업을 보존한 채 변경 파일을 대조했다. 대시보드 단위 시험 143개, 대시보드 브라우저 시험 30개, API·Application 시험 146개(별도 subtest 54개), 독립 PostgreSQL 회귀 시험 65개를 통과했다. Application 브라우저에서는 조회 권한, 전체 이력 검색, 갱신 경합, 승인 대상 변경 및 375·768·1280·1440px 너비를 확인했다. PostgreSQL 시험은 새 메모리 전용 컨테이너에서 수행했다. pytest는 설정된 서버에 자기 전용 DB를 만들어 사용한다.

**배포 상태.** 이번 변경은 로컬 소스와 테스트에 반영했다. 기존 클러스터는 갱신하지 않았다. 11월 9일 확인 시 Application의 선언된 Ingress·Service·Deployment가 없어 주소가 404를 반환했다. 실제 Application 전용 인증과 dev/stg 배포 인수는 열려 있다. 운영 DB 전체의 전후 해시 대조는 수행하지 않았다.

### 11월 11일 후속 적용 · 전체 이력 검색과 서비스 지도

사용자가 지정한 이번 범위는 소스와 검증까지다. 최신 원본 프로젝트의 변경 파일 15개가 격리 검증본과 일치함을 확인했다.

- 두 제품의 사건 목록에서 **기간·시스템·서비스**를 펼치면 당시 시스템 ID와 서비스 이름으로 저장 이력을 검색할 수 있다. 시작일과 종료일은 한국시간 기준이고 종료일 당일 전체를 포함한다. 서비스·시스템은 정확히 일치하는 값으로 검색한다. 날짜와 서비스 조건은 DB에서 총건수 집계와 페이지 분할 전에 적용한다.
- 지도 기본 보기에서는 같은 환경·시스템 내부의 정상 Ingress가 단일 구조 연결로 Deployment에 도달할 때 해당 경로를 접는다. **접힌 Ingress 펼치기**로 전체 자원을 다시 보거나 대상 상세에서 경로 원문을 확인한다. 장애·경고, 관측 트래픽, 계약 연결, 여러 대상, 소속 불명은 접기 대상에서 제외한다.
- 검증 결과는 대시보드 단위 시험 144개, 검색·지도 브라우저 시험 30개, 격리 SQLite 기반 API 회귀 20개, Application 브라우저 시나리오 6개와 프런트 빌드 통과다. 새 검사는 한국시간 자정 직전·직후, 역순 기간, 서비스 정확 일치, 검색 URL 유지, 단일·분기·경고 Ingress를 포함한다.
- 기존 사건이나 시험 데이터의 스키마·레코드 변경은 없다. 이번 작업에서 로컬 클러스터는 조회만 했다.

목차: [판단 기준](#판단-기준) · [현재 기준선](#현재-기준선) · [문제와 근거](#문제와-근거) · [목표 기능 구조](#목표-기능-구조) · [데이터 활용](#데이터-활용) · [실행 계획](#실행-계획) · [확인 결과와 남은 결정](#확인-결과와-남은-결정)

## 판단 기준

`과제계획서.md`의 핵심은 이상 감지 → 근거 수집 → 난이도·위험도 판단 → Agent Group 및 역할별 모델 선택 → 검증·승격 → 안전 조치/이관 → 결과·비용 기록이다. 계획서는 초안이며 이후 사용자가 합의한 확장을 무효화하지 않는다.

| 유지할 것 | 근거와 제품 내 위치 |
| --- | --- |
| K8s 대상 LiveProbe | 인프라·워크로드 장애의 감지, 적응형 분석, 검증, 허용된 운영 조치 |
| LiveProbe for Application | 사용자가 명시한 별도 확장 제품. 소스·앱 구성 오류 분석, 수정, 시험, dev/stg 검증과 정확한 버전의 prd 승인 |
| 전체 클러스터 지도·AI 흐름·운영자 대화 | P15 인터뷰에서 명시 요청. 삭제보다 정보 우선순위와 목적을 정돈 |
| 삼성 3시스템과 Kafka | 사용자 합의에 따른 관측·시험 대상. 플랫폼 기능과 대상 업무 시스템의 설명을 구분 |
| 기존 사건·실행·시험·승인·변경 원장 | 성공·실패·중단·과거 시스템 분류까지 보존. 검색·회귀·평가 근거로 활용 |
| 제품별 DB·사건·실행 권한 분리 | 별도 Application 계약의 중요한 경계. 화면을 정돈한다는 이유로 저장소와 승인을 합치지 않음 |

관련 합의: [Application 명세](/Users/hwany/sds-ai-professional/.hermes/plans/liveprobe-application-brief.md:15), [제품 격리 계약](/Users/hwany/sds-ai-professional/.hermes/plans/liveprobe-application-contract.md:5), [지도·대화 합의](/Users/hwany/sds-ai-professional/docs/plans/revamp/P15-layout-revision-interview.md:5), [시스템 독립화](/Users/hwany/sds-ai-professional/.hermes/plans/system-independence-clarification.md:1).

## 현재 기준선

이 채팅 작업트리와 원본 폴더의 Git HEAD는 모두 `31c2f87f6d14712309eaeb255dc2574eaab1eb0a`이지만 파일 내용은 다르다.

| 기준 | 실제 상태 | 이번 조사에서의 취급 |
| --- | --- | --- |
| `/Users/hwany/.codex/worktrees/f99b/sds-ai-professional` | P15 커밋 기준. 최신 Application 구현 없음 | 지침 수정과 이 검토 문서 저장 위치 |
| `/Users/hwany/sds-ai-professional` | P16/P18/Application 등 다수 미커밋 변경 | 현재 제품의 소스·합의 조사 기준 |
| `http://liveprobe.localhost` | 실제 지도·사건 목록·사건 상세 열람 가능 | 브라우저에서 확인한 사실만 기록 |
| `http://application.liveprobe.localhost/` | 이번 접속에서 `404 page not found` | rancher-desktop의 liveprobe-system에서 해당 Ingress·Service·Deployment 모두 NotFound 확인 |

원본은 조사 시 추적 파일 변경 103개, 미추적 파일 20,222개였다. 미추적 수에는 증거·산출물이 포함되므로 모두 제품 소스라고 해석하지 않는다. 낡은 작업트리만 수정하거나 원본 전체를 복사하면 최신 기능 누락 또는 비공개 자료 혼입이 생긴다. 구현 전 필요한 제품 파일의 목록·해시를 고정하고 변경 범위를 분리해야 한다.

## 문제와 근거

### 1. 과거 기록을 보는 동작이 새 AI 작업을 만든다 — 최우선

K8s 사건의 `GET /api/incidents/{iid}/narrative`가 `worker.seen()`과 `worker.request(..., idle=True)`를 호출한다. 사용자는 과거 분석을 읽으려 했는데 해석 작업과 예산 확인, 실패 기록이 추가될 수 있다. 화면 재방문이 실행 원장을 바꾸면 시험 결과를 재현하고 비교하기도 어려워진다.

이번에 기존 사건 #1275를 열었을 때 오늘 시각의 한글 해석 실패 단계와 과거 비용 미관측에 따른 예산 차단 문구가 표시되었다. 이 부수효과를 확인한 뒤 탭을 닫았다. 후속 확인에서 조회 전용 사건 API로 해석 실패 행 #5590(2026-11-09 22:09:58 KST)을 확인했다. 모델에 요청이 전달됐는지는 확인할 수 없다.

**정돈:** 조회 API는 저장된 해석만 반환한다. 실행 중 해석은 실행 파이프라인에서 처리하고, 종료 사건의 추가 해석은 비용·작업이 발생함을 표시하는 명시적 동작으로 분리한다. 기존 원문·해석·실패 이력은 보존한다.

근거: [main.py:528](/Users/hwany/sds-ai-professional/liveprobe/app/main.py:528), [interpret.py:438](/Users/hwany/sds-ai-professional/liveprobe/app/interpret.py:438), [화면 polling](/Users/hwany/sds-ai-professional/dashboard/src/IncidentRoom.tsx:31).

### 2. K8s 제품과 Application의 코드 수정·배포 책임이 겹친다

K8s API는 P18 `ChangeService`를 기동하고 변경 생성·승격 API를 제공한다. K8s 사건 상세에도 `ChangeRequestCard`가 남아 있다. 이 경로는 단계별 확인·PR/병합 흐름을 갖지만 새 Application은 수정·시험 후 dev/stg 자동 진행, 사용자 시험과 prd 승인이라는 별도 계약을 갖는다. 두 경로가 동시에 존재하는 사실과 신규 요청의 권장 경로가 화면에서 드러나지 않는다.

**사용자 결정:** K8s는 감지·운영 완화/복구, Application은 신규 코드 수정·시험·배포를 맡는다. 기존 변경 요청은 과거 처리 경로로 계속 열람하며 미완료 작업을 강제 취소하거나 다른 제품으로 옮기지 않는다. 이관을 추가할 경우 원사건 참조와 새 사건 ID를 명시한다. 단순 이동 링크를 자동 이관이라고 부르지 않는다. 이 기능 경계는 11월 9일 사용자 답변으로 확정했다.

근거: [main.py:93](/Users/hwany/sds-ai-professional/liveprobe/app/main.py:93), [change_api.py:149](/Users/hwany/sds-ai-professional/liveprobe/app/change_api.py:149), [IncidentRoom.tsx:206](/Users/hwany/sds-ai-professional/dashboard/src/IncidentRoom.tsx:206), [ChangeRequestCard.tsx:13](/Users/hwany/sds-ai-professional/dashboard/src/ChangeRequestCard.tsx:13).

### 3. 저장 이력을 제대로 찾을 수 없다

실제 사건 목록은 전체 **1,249건**을 표시했지만 환경 필터는 현재 조회한 **최대 50건**에만 적용된다고 안내한다. 서버에서 전체 이력을 필터링한 뒤 페이지를 나누는 방식이 아니다. Application 사건 목록은 최신 200건만 반환하며 페이지·검색이 없다. 검토 장부도 화면에서 200건 고정 조회한다.

**정돈:** 환경·시스템·서비스·상태·기간·출처를 서버에서 먼저 필터링하고 같은 조건으로 총건수와 페이지를 계산한다. 전체 이력과 출처 미확인 이력을 항상 찾을 수 있게 한다. 이력 열람에 신규 모델 호출이나 배포 작업은 필요하지 않다.

근거: [Incidents.tsx:27](/Users/hwany/sds-ai-professional/dashboard/src/Incidents.tsx:27), [main.py:451](/Users/hwany/sds-ai-professional/liveprobe/app/main.py:451), [Application control.py:124](/Users/hwany/sds-ai-professional/liveprobe/app/application/control.py:124), [Ledger.tsx:13](/Users/hwany/sds-ai-professional/dashboard/src/Ledger.tsx:13).

### 4. 시험·운영·인계 상태가 같은 숫자로 섞인다

평가 사건은 `eval/...` fingerprint로 저장되지만 사건 목록과 오늘 현황은 출처를 구분하지 않는다. 헤더의 진행 건수는 `open/analyzing/action_pending/acting`이고 개요는 최근 1시간 인계까지 포함한다. 지도에는 별도의 미인수 인계 시간창도 있다. 실제 화면에서 헤더 4건·개요 6건이 동시에 나타났으며 소스에 집계 기준 차이가 확인된다. 그 순간의 차이 전부가 이 원인이라고 단정하지는 않는다.

**정돈:** `자동 처리 중`, `승인 대기`, `미인수 인계`, `종료 이력`을 공통 정의로 계산한다. 시간창이 다른 지도 경고는 기준을 명시한다. 운영/평가/출처 미확인 필터는 조회 범위만 바꾸며, 숫자를 맞추기 위해 과거 사건을 종료하거나 삭제하지 않는다.

근거: [evalrun.py:121](/Users/hwany/sds-ai-professional/liveprobe/app/evalrun.py:121), [main.py:406](/Users/hwany/sds-ai-professional/liveprobe/app/main.py:406), [liveSnapshot.ts:3](/Users/hwany/sds-ai-professional/dashboard/src/liveSnapshot.ts:3), [flows.py:422](/Users/hwany/sds-ai-professional/liveprobe/app/flows.py:422).

### 5. 지도는 크지만 서비스 식별과 장애 판단이 어렵다

실제 prd 지도는 비즈니스 2·트레이드 9·딜리버리 6을 “개 서비스”로 표시했다. 코드상 이 수치는 그려진 자원 노드 수다. 선언상 Deployment와 Ingress 수의 합이 각각 2·9·6과 일치한다. 같은 이름의 두 카드가 별도 서비스처럼 보이고 자원 종류 대신 시스템 이름이 반복된다. 1280×720 화면에서 전체 맞춤으로 글씨가 작아지는 것도 확인했다.

**정돈:** 우선 자원 수와 서비스 수를 정확히 구분하고 종류를 표시한다. 기본 지도는 서비스 단위로 읽기 쉽게 묶고, 선택 시 Ingress·Service·Deployment·Pod와 연결 근거를 확인하도록 한다. 원본 자원과 연결을 삭제하지 않으며, 소속·관계가 불명확한 자원을 추측해 병합하지 않는다. 기존 지도 중심 진입과 세 시스템 경계는 유지한다.

근거: [TopologyMap.tsx:16](/Users/hwany/sds-ai-professional/dashboard/src/TopologyMap.tsx:16), [집계:32](/Users/hwany/sds-ai-professional/dashboard/src/TopologyMap.tsx:32), [mapModel.ts:85](/Users/hwany/sds-ai-professional/dashboard/src/mapModel.ts:85).

### 6. Application 화면은 사용자 작업보다 내부 단계와 인증 입력이 먼저 보인다

별도 제품임을 알리는 K8s 쪽 진입 안내가 없고, Application 소스는 조회용·시험자·승인자 토큰 입력, 실행 준비, 긴 단계별 원문과 지문, 승인 폼을 나열한다. 자동 진행 제품인데 화면 갱신은 수동 새로고침 중심이다. 이 항목은 현재 소스에 대한 평가이며 404 상태에서 실제 Application 화면을 확인했다고 주장하지 않는다.

**정돈:** 첫 화면에 제품의 목적과 연결 상태를 표시한다. 상세는 `현재 단계 / 원인·근거 / 변경 내용 / 시험 결과 / 환경별 상태 / 지금 필요한 행동` 순으로 읽히게 한다. 원문·상세 지문은 펼침으로 보존하고 자동 갱신 시 작성 중인 시험 기록과 선택을 유지한다. 권한 입력은 필요한 단계에 노출하되 인증 제거·권한 통합·토큰 영속 저장으로 우회하지 않는다.

근거: [Shell.tsx:54](/Users/hwany/sds-ai-professional/dashboard/src/Shell.tsx:54), [Application index.html:15](/Users/hwany/sds-ai-professional/liveprobe/app/application/static/index.html:15), [app.js:226](/Users/hwany/sds-ai-professional/liveprobe/app/application/static/app.js:226).

### 7. 비용·준비 상태·문서가 실제 의미와 다르다

- narrative API 비용 합계는 누락값을 0으로 더하지만 목록 집계는 일부 누락 시 null을 유지한다. 실제 #1275 화면은 `추정 불가`를 표시했다. 모든 배포 화면이 0을 보여 준다는 주장이 아니라 API별 의미가 다른 결함이다.
- Application 준비 상태의 `model`은 실행 옵션·홈 경로·바이너리 존재로 계산되지만 화면에는 `모델 연결`로 표시한다. 인증·최근 호출 성공이 확인된 것과 다르다.
- README는 아직 Kiali 첫 화면과 별도 `/map`을 설명하지만 현재 소스는 자체 지도이며 `/map`을 `/`로 보낸다. 새 제품의 구현/패키징/실배포 인수 상태도 한곳에서 파악하기 어렵다.
- 11월 7일 배포 검토의 `no_ship` 항목과 렌더러/저장 선언의 Codex PVC 차이가 남아 있다. 클러스터 조회에서 선언된 Ingress·Service·Deployment가 없었고, 현재 접속 404와 일치한다.

**정돈:** 확인된 비용 부분합과 총비용 미수집을 구분하고 당시 모델·가격을 유지한다. 구성됨·연결 관측됨·최근 실행 성공·사용자 승인 대기를 별도로 표시한다. README에는 실제 제공 경로와 검증 수준을 명시한다.

근거: [narrative.py:126](/Users/hwany/sds-ai-professional/liveprobe/app/narrative.py:126), [main.py:429](/Users/hwany/sds-ai-professional/liveprobe/app/main.py:429), [Application readyz:95](/Users/hwany/sds-ai-professional/liveprobe/app/application/main.py:95), [배포 검토](/Users/hwany/sds-ai-professional/docs/evidence/application/deploy-20261107/read-only-review.md:97).

## 목표 기능 구조

| 기능 영역 | 사용자가 바로 이해할 내용 | 유지할 경계 |
| --- | --- | --- |
| K8s 운영 관제 | 어느 환경·시스템이 어떤 상태인지, 처리·승인·인계가 몇 건인지 | 지도·실측·계약 연결 구분, 플랫폼 자체와 업무 대상 구분 |
| K8s 장애 대응 | 감지 원인 → Group 선택 이유 → 역할·모델 → 검증 → 조치/이관 | 기존 안전조치와 운영자 확인 기반 명령 경계 |
| Application 장애 대응 | 어떤 코드 문제를 왜 수정했고 어떤 시험·배포가 끝났는지 | 독립 사건·실행기·DB, dev/stg와 prd 승인 차이 |
| 사건·실행 이력 | 과거 성공/실패/시험을 찾아 당시 근거·모델·비용을 읽는 곳 | 읽기 전용. 새 분석은 별도 실행 |
| 설정 | 현재 자동화 정책, 역할별 모델, 연결·수집 상태 | 과거 실행의 모델·정책 스냅샷 불변 |

새 종합 평가 대시보드를 추가하기보다 기존 사건·검토 기록의 검색과 원장 접근을 먼저 개선한다. 플랫폼 정돈을 이유로 대상 업무 시스템·Kafka를 다시 설계하거나 종료된 P18 캠페인을 재개하지 않는다.

## 데이터 활용

> 🔒 **보존 원칙** — 운영 DB뿐 아니라 기존 시험 DB의 사건·로그·실행·승인·실패 기록도 삭제하지 않는다. 필터·표시·파생 산출물로 정리한다. 과거 데이터를 현재 정답이나 현재 성능으로 덮어쓰지 않는다.

1. **출처 표시:** 확실한 `eval/` 표식과 평가 manifest 연결만 사용한다. 제목·날짜·서비스 이름으로 시험 여부를 추측하지 않는다. 나머지는 출처 미확인 상태로 조회 가능하게 둔다.
2. **이력 재사용:** 저장 근거·한글 해석·선택 Group·모델·승격·비용·조치 결과를 연결한다. 과거 모델과 시스템 이름은 당시 사실로 보존하고 현재 매핑은 별도 정보로 제공한다.
3. **불변 스냅샷:** 제품별 읽기 전용 exporter로 사건/실행 ID, 출처, 당시 설정, 근거 참조, 입력·출력 해시, 비용 결측을 담은 정제 JSON을 만든다. 저장된 원본을 바꾸지 않고 내보낸 시점·스키마 버전·해시를 남긴다.
4. **오프라인 분석:** 이미 있는 `evaluation.offline evaluate/shadow/utility`에 적합한 계약으로 연결한다. 단순 DB export만으로 정답 라벨과 비교 가능한 입력이 생겼다고 가정하지 않는다. 누락은 unknown, 조건이 다른 실행은 별도 집단으로 유지한다.
5. **새 분석과 분리:** 과거 실행 열람/비교에는 모델 호출·K8s 변경·배포 job 생성이 없어야 한다. 현재 상태로 새 분석을 요청하면 새 ID·새 근거로 시작하고 원사건 참조만 남긴다. 오래된 근거를 실행기에 재주입하거나 신선도 검사를 완화하지 않는다.
6. **보존 검증:** 변경 전에 존재한 ID 집합·불변 본문 해시·참조 관계가 이후에도 유지되는지 검증한다. 동시 운영으로 새 행이 추가되거나 기존 진행 사건이 합법적으로 갱신되는 것은 별도로 설명한다. 총행수 일치만으로 보존을 판정하지 않는다.

기존 활용 코드: [offline.py:32](/Users/hwany/sds-ai-professional/evaluation/offline.py:32), [role_replay.py:42](/Users/hwany/sds-ai-professional/evaluation/role_replay.py:42), [사례표 export](/Users/hwany/sds-ai-professional/scripts/export_case_results.py:272). DB에서 이 도구로 연결하는 범용 exporter는 이번 조사에서 확인되지 않았다.

## 실행 계획

아래는 **로컬 작업 목록**이다. 외부 이슈 연결은 필요하지 않다. 기능 경계는 사용자 답변으로 확정했다.

| ID | 순서·작업 | 주요 파일/범위 | 완료 기준 | 상태 |
| --- | --- | --- | --- | --- |
| 정돈-00 | Jira 규칙 폐기 | 두 작업 폴더의 AGENTS.md | 스킬에 남은 Jira 단계도 적용하지 않음을 명시 | 완료 |
| 정돈-01 | 최신 기준선 고정 | 원본 미커밋 제품 파일 목록·해시, 로컬 계획 | 원본과 같은 제품 파일을 선별 복제하고 SHA-256 기록 | 완료 |
| 정돈-02 | 이력 조회의 실행 부수효과 제거 | main.py narrative, interpret.py, Application storage/main | 과거 화면 반복 조회 시 모델/해석 job/AgentRun 추가 0; readiness가 DB 초기화하지 않음 | 소스 반영·격리 검증 |
| 정돈-03 | 두 제품의 역할과 진입 정리 | Shell, IncidentRoom, ChangeRequestCard, Application UI/API | K8s/앱 목적 명확; 기존 변경 이력·미완료 작업 보존; 신규 경로 구분 | 소스 반영·격리 검증 |
| 정돈-04 | 전체 이력 검색·페이지·출처 | K8s main/Incidents/Ledger, Application control/contracts/UI | 50·200건 밖의 기존 사건 검색; 같은 필터의 total/페이지 일치; unknown 접근 유지 | 소스 반영·격리 검증 |
| 정돈-05 | 지도와 집계 의미 정리 | TopologyMap/mapModel/overviewModel, overview/liveSnapshot/flows | 자원/서비스 수 구분; 처리·승인·인계 일치; 원본 노드·연결 상세 접근 | 집계·자원 종류 표시 반영 |
| 정돈-06 | 앱 단계와 다음 행동 중심 화면 | Application static, 읽기 응답 | 자동 진행 갱신, 작성 내용 보존, 실패/사람 대기 명확; 승인 지문 변경 시 재확인 | 소스 반영·격리 검증 |
| 정돈-07 | 비용·준비 상태 일관성 | accounting/narrative/API, Application readiness/UI | 실제 0과 미수집 구분; 바이너리 존재를 연결 성공으로 표시하지 않음 | 소스 반영·격리 검증 |
| 정돈-08 | 보존형 export·기존 평가 연결 | 제품별 읽기 exporter, evaluation/offline 계약 | 원본 불변, 동일 snapshot/정의 재현, 미수집 unknown, 새 모델 호출 0 | 소스 반영·격리 검증 |
| 정돈-09 | 문서·제공 경로·인수 검증 | README, deploy/application, 로컬 작업 원장 | 404 원인 확인, 선언/실행 상태 구분, 코드/패키지/실배포 완료 분리 | 문서·미배포 상태 확인 |

### 구현 순서와 의존성

`정돈-01 → 정돈-02 → 정돈-04/07 → 정돈-03/05/06 → 정돈-08 → 정돈-09` 순으로 진행한다. 데이터 접근·비용 정의를 먼저 바로잡고 화면에 연결한다. Application 기존 인수 차단을 별도로 기록하며 아직 접근할 수 없는 제품에 링크만 추가해 정상 서비스라고 표시하지 않는다.

구현 중 신규 DB 컬럼이 필요하면 nullable/additive 변경과 기존 기록의 읽기 호환만 허용한다. 사건/테이블 합치기, 강제 상태 재작성, CASCADE 삭제는 범위에 없다. 기존 코드 변경 작업의 진행·승인 의미를 바꾸는 전환은 사용자 답변과 현재 미완료 작업 조사 이후 명시한다.

### 검증과 실패 처리

- 먼저 순수 함수·모의 API·프런트 빌드와 브라우저 회귀를 실행한다. pytest 세션이 새로 만든 DB 안에서만 스키마를 초기화한다. 테스트 서버 접속 주소가 없으면 PostgreSQL 통합 시험은 건너뛴다.
- 이력 조회 시험은 정상/누락/실패 해석 사건을 반복 조회해 job·모델·DB 쓰기가 0임을 확인한다. 진행 중 분석 파이프라인의 정상 해석은 별도 검증한다.
- 검색은 첫 페이지에 대상이 없지만 뒤 페이지에 있는 자료, 출처 미확인, 과거 시스템 명칭, 빈 결과, 잘못된 필터, 동시 신규 사건을 포함한다.
- 지도·알림·목록은 같은 상태와 필터에서 같은 의미를 나타내야 한다. 원문·자원 종류·연결 provenance는 상세에서 접근 가능해야 한다.
- 자동 갱신은 오래된 응답이 현재 선택을 덮지 않고, 작성 중인 기록과 포커스를 유지해야 한다. 승인 대상이 바뀌면 기존 확인만 무효화하며 자동 승인을 생성하지 않는다.
- 비용은 전부 관측/일부 누락/전부 누락/실제 0의 네 경우를 확인한다. 과거 비용·모델을 현재 값으로 다시 쓰지 않는다.
- 1440/1280/768/375px에서 핵심 이름·상태·다음 행동을 읽고 이동할 수 있어야 한다. 전체 자원을 작게 축소해 넣는 것만으로 지도 검증을 통과시키지 않는다.
- 배포가 필요한 경우 정확한 변경 리소스만 다루고 데이터 자원을 교체하지 않는다. 소스 확인, 빌드, 배포 요청, 실제 제공 상태를 각각 기록한다. 404·권한 부족·미인증·실행기 미연결은 차단 사유로 표시하고 완료를 주장하지 않는다.
- 새 장애 주입, 대규모 평가 재개, prd 자동 승인, 원격 공개는 이 정돈안에 포함하지 않는다. 기존 저장 데이터로 가능한 검증을 우선한다.

## 확인 결과와 남은 결정

### 저장된 사건으로 확인한 사례

#1275의 기존 상세 API를 읽고 `evaluation.role_replay.role_utility`로 집계했다. 수집 근거 10건, 역할 실행 4건(라우터·근거 정리·안전 검증·해석)이 있었다. 비용 미수집 2건을 제외한 확인 부분합은 $0.200362이고 전체 비용은 미수집이다. 이 금액은 구독 청구액이나 다른 모델로 재실행한 결과를 뜻하지 않는다.

### 이번에 완료한 것

- [x] 과제계획서와 후속 인터뷰·현재 두 제품 소스 비교
- [x] 원본 미커밋 구현과 P15 작업트리 차이 확인
- [x] 실제 K8s 지도·전체 사건 수·페이지 한정 필터 확인
- [x] 과거 상세 조회의 해석 작업 부수효과 확인 후 조회 종료
- [x] Application 문서상 진입 주소의 현재 404 확인
- [x] 양쪽 AGENTS.md에 Jira 미사용·스킬 내 Jira 요구 무효·로컬 추적 명시
- [x] 세 개의 독립 읽기 검토 결과를 합쳐 위 계획 작성
- [x] 신규 코드 수정·배포를 Application으로 모으는 기능 경계 확인
- [x] 사용자 역할 분리 결정 및 작업 재개 지시
- [x] 원본 소스 수정·격리 회귀·스냅샷 보존 검사
- [ ] 실제 클러스터 배포·전용 인증·dev/stg 인수

### 실행하지 않은 것과 한계

DB 삭제·초기화·시험 데이터 정리, 장애 주입, 배포, 승인·조치·종료 버튼 실행, Jira 쓰기는 수행하지 않았다. DB를 직접 조회하거나 백업·전체 보존성을 검증하지도 않았다. 브라우저에서 기존 사건 상세를 여는 동작이 서버의 해석 예약에 연결된 것은 위에 별도 기록했으며, 모든 조회가 무부수효과였다고 주장하지 않는다.

Application 소스·기존 증거가 있다는 사실은 현재 연결 성공 또는 dev/stg 인수 완료를 의미하지 않는다. 로컬 클러스터에서 선언된 앱 리소스 부재를 확인했다. 실제 앱 전용 인증과 dev/stg 인수는 별도 운영 작업이다.

저장소가 지정한 `/deep-interview-and-go`의 [워크플로 파일](/Users/hwany/.hermes/skill-bundles/deep-interview-and-go.yaml)은 계획 검토 후 **“Do not begin implementation until the user explicitly approves the plan.”**을 요구한다. Jira 단계는 이번 사용자 지시로 폐기하며, 모델·Hermes 전용 실행 지시는 현재 도구와 충돌하지 않는 범위에서만 취급한다. 사용자가 역할 분리를 확정하고 작업 재개를 지시하여 이 범위로 구현을 시작했다.
