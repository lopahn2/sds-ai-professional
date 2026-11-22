# 🗂️ 단계 카드 브리핑 스택과 서비스 지도 타일

2026-11-21 · LiveProbe · 운영자 관제실

---

## 합의한 범위 (2026-11-21 인터뷰)

| 항목 | 합의 |
|---|---|
| 대상 | 단계 관제의 12개 카드(감지·근거 수집·대응 경로 결정·원인 조사·독립 검증·조치 실행·시험·빌드·dev 검증·운영 승인·복구 확인·결과·소명·운영자 인계), Kubernetes와 Application 공통 |
| 쓰임 | 장애 대응 중에도 보고, 이전 단계 판단이 현재 작업으로 어떻게 이어졌는지 함께 본다 |
| 시안 | A · 브리핑 스택: 머리 → (결정) → 이어받음 → 브리프 → 데이터 |
| 첫눈에 | 작업 중이면 지금 하는 작업, 지금까지 찾은 결과·원인, 카드의 목표를 정리본으로 먼저 보이고 그 아래 데이터 |
| 모델 | 작업한 모델은 태그로 올려 우측 상단에 둔다 |
| 카드군 | 관측 = 변화 중심, 분석 = 결론 + 목표 점검(백엔드 차단 규칙), 수정·QA = 수정 전 → 후, 승인·종결 = 결정 맨 위 |
| 근거 | 출처별로 묶고 같은 출처는 한 행으로 합친다. Application 소스 인용은 모델에 보내지 않고 고정 라벨, 펼치면 코드 |
| 한글 정리 | 앞으로: 로그·코드가 많은 문장은 요약 정리해 한국어로, 표시 필터에 걸리면 숨기되 '실패'라고 쓰지 않음, K8s 인용은 한글 정리. 과거: 보충 해석 없이 해석 상태 문구만 `과거 데이터 없음`(사실 미관측 문구는 유지) |
| 서비스 지도 | 노드 박스는 시안 C · 상태 점 타일: 고정 폭, 준비는 점만, 이상·미관측만 글자 |
| 작업 위치 | Codex 작업물을 모두 커밋한 뒤 같은 작업 폴더에서 시작 |
| 반영 | 검증 후 대시보드와 API·모델 작업자를 4175 로컬 k3s에 반영, 롤백 명령 기록 |

## 완료 기준

1. 12개 카드 모두 '머리 → (결정) → 이어받음 → 브리프 → 데이터' 순서이고 `.step-lead`가 1개다.
2. 모델 태그가 카드 우측 상단과 판단 기록 행마다 보인다.
3. 원인 조사 목표 점검이 백엔드 차단 규칙과 같은 판정을 낸다(경계값 단위 시험).
4. 근거는 출처별로 묶이고 같은 ref는 한 행에 인용 수로 표시된다. 원시 `source:` ref는 펼치기 전에 보이지 않는다.
5. 화면 어디에도 '한글 해석 실패/미관측/미기록'이 없다. 과거 데이터는 '과거 데이터 없음', 새 데이터의 실패는 생략된다.
6. 승인·종결 카드에서는 결정이 브리프보다 위에 있다.
7. 지도 노드는 고정 폭 타일이고 1990px에서 늘어나지 않는다. 정상은 점만, 이상은 글자로 보인다.
8. naria 검증 0 FAIL, 단위·e2e에서 기준선 대비 새 실패 0건, 375/768/1440 캡처, 운영 반영 후 GET 확인.

## 구현

| 영역 | 파일 | 동작 |
|---|---|---|
| 브리프 계산 | `dashboard/src/stageBrief.ts`, `stageGoals.ts`, `evidenceGroups.ts`, `readingText.ts` | 톤·모델 태그·이어받음·진행 중·첫 줄·사실·목표·남은 것·판단 기록·근거를 순수 함수로 계산 |
| 카드 화면 | `StageCard.tsx`, `StageBriefView.tsx`, `SemanticStagePanel.tsx`, `ApplicationCardReading.tsx`, `StageReadings.tsx`, `semanticStage.css` | 시안 A 순서, 건수 달린 펼침 행, 결정 슬롯 |
| 결정 슬롯 | `IncidentRoom.tsx`, `ApplicationPage.tsx` | 코드 조치 결정·조치 승인·인계·릴리스 검토 카드를 카드 맨 위로 |
| 한글 정리 | `liveprobe/app/reading_era.py`, `readings_ko.py`, `interpret.py`, `flowgraph.py`, `application/display_readings.py`, `application/activity.py` | 요약 프롬프트·검증, 컷오프 이전 `legacy` 상태, K8s 인용 정리, 재시도 코드 노출 |
| 지도 타일 | `ServiceAtlas.tsx`, `serviceAtlas.css` | 176px 고정 타일, 준비 칩은 점만 |

## 진척

- [x] Codex 작업물 185건을 영역별 5개 커밋으로 고정(d5fed04 · 4d12e70 · e0209d4 · 2173122 · f6fc6b4)
- [x] 기준선: naria 3 FAIL(Codex 단계 흐름 CSS) · mock e2e 1440 110 통과/27 실패 · 768 107 통과/27 실패
- [x] 브리프·근거·목표·표시 규칙 모듈과 단위 시험
- [x] 카드 틀·결정 슬롯·이어받음
- [x] 한글 정리 백엔드와 시험
- [x] 서비스 지도 타일
- [x] 전체 e2e 비교, 캡처, 운영 반영(2026-11-22 13:00 KST)
- [x] 실데이터에서 찾은 첫 줄·중복 문제를 고쳐 대시보드 재반영

## 검증

| 항목 | 결과 |
|---|---|
| 단위 시험 | 대시보드 236/236(새 `tests/stage-card.test.ts` 14건 포함). 백엔드 새 시험 `tests/test_readings_display.py` 5건, `application_tests/test_display_readings.py` 8건, 기존 해석·서사·설명·활동 시험(핵심 214건, Application 29건) 통과 |
| 타입·빌드 | `tsc --noEmit` 통과, `vite build` 통과 |
| naria | `naria-verify.py`(P21 기준 원본 `d536b75`) 159건 모두 naria 근거, FAIL 0. 기준선의 3건(단계 흐름 3px·9px)은 4px·8px로 고침 |
| mock e2e 전체 | 1440 116 통과/27 실패, 768 113/27, 375 113/27. 실패 목록은 변경 전 기준선(1440 110/27, 768 107/27, 375 107/33)과 같고 새 실패 0건. 새 `stage-card` spec 6건은 세 폭 모두 통과 |
| 의도한 시험 수정 | `unified.spec.mjs`·`application-original.mock.spec.mjs`는 접힌 판단 기록·시간순 관측 칸을 먼저 연다. 기준선에서 이미 실패하던 p18-w6-room·p15-incident-room의 옛 노드 id 선택은 이번 범위에서 옮기지 않았다 |
| 캡처(git 제외, 작업 폴더에 보관) | mock: `backups/stage-card-20261122/mock/width-{375,768,1440}-*.png`(완료·작업 중·과거 기록·인용 불일치·승인·K8s). 운영: `backups/stage-card-20261122/live2/app-6a14-*.png`, `live/k8s-1501-*.png` |
| 운영 확인(GET만) | 4175가 새 번들 7개 파일을 그대로 제공. #1501(감지·근거 수집·대응 경로·원인 조사·독립 검증·결과)과 Application `6a14d02a…`(감지~운영자 인계 8단계)를 375/768/1440으로 열어 단계마다 첫 줄 1개, 해석 상태 문구 0, 가로 넘침 0. GET 외 요청 0건 |
| 실데이터에서 찾은 것 | 저장된 한국어 판단 8개 중 6개가 예외 클래스명을 포함해 표시 필터에 통째로 숨겨지고 있었다(이전 화면의 '한글 해석 미관측' 상당수가 이 경우). 표시 경계는 그대로 두고 표시 가능한 문장만 골라 첫 줄에 쓴다 |

## 반영과 롤백

2026-11-22 12:59 KST에 실행 중인 워크플로·조치·명령·Application 작업이 0건임을 읽기 전용으로 확인하고, 각 배포의 세대와 이전 이미지가 그대로일 때만 바뀌는 JSON patch(서버 dry-run 먼저)로 교체했다. 한글 정리 컷오프는 `READINGS_V2_SINCE = 1795319520`(2026-11-22 12:52 KST)이다.

| 배포 | 이전 | 새 이미지 | 세대 |
|---|---|---|---|
| liveprobe-api | `liveprobe/liveprobe@sha256:cf35e7428724…` | `liveprobe/liveprobe@sha256:94c72c0a3478…` | 96 → 97 |
| liveprobe-application-model-worker | `liveprobe/liveprobe@sha256:038cfd35fc55…` | `liveprobe/liveprobe@sha256:2bae02afcc14…` | 5 → 6 |
| dashboard | `liveprobe/dashboard@sha256:8c283a6611f9…` | `…698cd3ca01ff…` → 재반영 `…64ecdd96fa16…` | 63 → 64 → 65 |

- 후보 이미지는 각자의 운영 이미지 위에 바뀐 파일 6개(`reading_era.py`·`readings_ko.py`·`interpret.py`·`flowgraph.py`·`application/display_readings.py`·`application/activity.py`)만 얹었다. 두 운영 이미지 안의 기존 5개 파일이 커밋된 수정 전 파일과 같음을 먼저 확인했다. 기반 레이어 유지·이미지 설정 동일·파일 6/6 일치·모듈 가져오기·k3s 가시성을 오프라인에서 검증했다.
- 반영 뒤 실행 중인 API의 파일 6개가 후보와 같고, `/api/overview`·Application readyz가 200이었다. 기존 기록 지문(K8s 1,526건, Application 10건·이벤트 274건·작업 41건)이 바뀌지 않았다. 대시보드 재반영 때는 K8s 워크플로 1건이 실행 중이었고, 정적 파일만 바꾸는 교체라 그대로 진행했다.
- 스키마 변경은 없다. 새 해석 행은 남아도 이전 코드가 무시한다.
- 롤백(대시보드 → 작업자 → API 순서, 각각 같은 방식의 조건부 patch 뒤 `rollout status`):

```bash
K="kubectl --context rancher-desktop -n liveprobe-system"
$K patch deployment/dashboard --type=json -p '[{"op":"replace","path":"/spec/template/spec/containers/0/image","value":"liveprobe/dashboard@sha256:8c283a6611f9f9a2b9945f0d0070851e9b25933c47540079c75264b4417e3aac"}]'
$K patch deployment/liveprobe-application-model-worker --type=json -p '[{"op":"replace","path":"/spec/template/spec/containers/0/image","value":"liveprobe/liveprobe@sha256:038cfd35fc55f21916240098561c03bea7c841ccc552a42796ca3e8c388f85da"}]'
$K patch deployment/liveprobe-api --type=json -p '[{"op":"replace","path":"/spec/template/spec/containers/0/image","value":"liveprobe/liveprobe@sha256:cf35e7428724dc4ac06314addc6fb999f18e9abd2d5210965fb42009a62df7b8"}]'
```

`rollout undo`는 쓰지 않는다. 반영 기록과 스크립트는 `backups/stage-card-20261122/`(git 제외)에 있다.
