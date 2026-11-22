# P21 · 서비스 지도 재구성과 naria 전체 정리

- 대상: `dashboard/` 운영 관제 › 서비스 지도(`/`)와 대시보드 전체의 여백·간격·색·글자 크기·굵기·곡률. 백엔드·실행 권한·인증·클러스터 자원 구성은 범위 밖이다.
- 기준 디자인 시스템: `lopahn2/naria-design-system` main(`@naria/ui` 1.0.1, `vendor/naria-ui-1.0.1.tgz`와 같은 판). 디자인 시스템 저장소는 수정하지 않는다.
- 요청(2026-11-21): "운영 관제 › 서비스 지도의 대시보드 디자인을 세련되게 다시 구성하라. 시안을 만들어 보여 주고 선택을 받아라. 라이브프로브 전체에서 margin·padding·색 배열 등이 naria를 따르지 않은 것은 모두 고쳐라."
- 작업 위치: Codex 작업 폴더 `~/.codex/worktrees/unified-liveprobe/sds-ai-professional`(`codex/unified-incident-product`, 현재 배포 화면의 원본). 커밋·push는 하지 않는다.

이 문서는 `AGENTS.md`에 따라 합의 범위·완료 기준·진척·검증 근거를 기록한다. `/deep-interview-and-go`는 이 세션에 없어서, 시안 선택과 진행 방식 질문으로 범위를 확인했다.

## 1. 합의한 결정 (사용자 응답)

| 질문 | 선택 |
| --- | --- |
| 서비스 지도 시안 | **B · 시스템 레인 지도** (A 상태판, C 대응 우선과 비교. 시안 캔버스: https://claude.ai/artifact/HLMDqrT9npxF2mZji9UT4B ) |
| 구현 위치 | Codex 작업 폴더에 직접 |
| 배포 | 검증 후 로컬 클러스터 대시보드까지 배포 |
| Codex 동시 작업 발견 후 | 지도 파일은 지금, 겹치는 naria 정리·빌드·시험·배포는 Codex 현재 작업이 멈추고 빌드가 되면 진행 |

## 2. 서비스 지도(시안 B) 구현 계약

- 시스템 = 가로 레인(왼쪽 이름·요약, 오른쪽 서비스 노드 열). 레인 사이는 1px 구분선만 두고 상자를 겹치지 않는다(카드 안 카드 제거).
- 레인 순서: 서로 연결된 시스템을 붙이고, 가장 많이 호출되는 시스템을 호출하는 시스템들 사이에 둔다. 연결된 서비스를 레인 앞에 둔다. 순서는 이름과 연결로만 정하고 상태로 바꾸지 않는다(`atlasLanes`).
- 서비스 노드 = naria DiagramNode 규격(경계·control 곡률·미세한 그림자·28px 아이콘 칸) + dev/prd 환경 칩. 칩은 naria `StatusIndicator`(점+문구)이며, 미관측은 속 빈 점선 점으로 정상과 구분한다.
- 상태 톤: 사건·HTTP 5xx·중단·차단 = 장애·오류(danger), 일부 비정상·연결 대상 없음·통신 경고 = 주의(warning), 준비 = ok, 나머지 = 미관측(`readingTone`). 사건이 있어도 준비된 Pod의 관측 문구는 `준비`다.
- 연결선은 노드 사이 빈칸으로만 지난다. 같은 열·행이고 사이에 다른 노드가 없으면 곧게, 아니면 열 사이 간격으로 돌아간다(`wirePath`). 관측 = 실선, 선언 = 점선, 도착 쪽 화살표. 600px 이하 한 열에서는 선을 그리지 않고 노드의 `→ 대상 · 선언` 문구로 남긴다.
- 지도 머리: 제목(18px subheading) + `시스템 · 서비스 · 연결 · 기준 시각`. 수집원 네 개가 모두 연결되면 버튼 하나(`수집원 4개 연결됨`), 문제가 있으면 문제 수집원만 따로 보인다. 누르면 오른쪽이 수집 진단으로 바뀐다.
- 캔버스 아래 독립 행: naria `ZoomControls`(80–160%, 전체 보기) + naria `StatusIndicator`·`Legend` 범례 + 연결선 숨기기.
- 오른쪽(운영자 개입): 대기 건수 배지, `승인 대기 · 영향 서비스 · 자동 분석·조치` 집계(완전히 조회되지 않으면 `—`), 운영 승인 버튼(대기 있으면 primary), 개입 대기 목록(naria ListRow 구성, 상태 배지), 자동 처리 중, 이관 이력(현재 집계와 별도).
- 데스크톱(1200px 초과)은 페이지가 스크롤되지 않는다. 지도와 오른쪽이 각자 영역 안에서만 스크롤한다. 840px 이하는 세로로 쌓는다.

## 3. naria 정리 범위와 기준

- 색: 모든 하드코딩 색을 `var(--nds-*)` 의미 토큰으로. 다크 다이어그램 작업면은 naria 다크 토큰 값(#11181c·#20292d·#303b40·#707d83·#f6f8f8·#b6c0c4, 강조 #b9c8da, ok #89dcb0, warn #f3ca68, danger #ff9da6, 경로 변경 #80d9cc). 보라색 없음.
- 글자: 굵기 400/600만(Wanted Sans 400/600). 크기는 naria 단계(12·13·14·16·18·24, 배지 11). 12px 미만 본문 금지.
- 곡률: small 6 · control 10 · card 16 · panel 24 · pill, naria 구성요소 고유값(메뉴·태그 8, 알림·표 12)만 예외.
- 간격: 4px 기반 척도(2·4·6·8·12·16·20·24·32…). naria 구성요소에 대응하는 공용 클래스(btn·badge·input·select·card·table·탭·태그·메뉴·알림·토스트)는 naria 구성요소 규격 치수 그대로. 위치 오프셋(top·left 등)도 같은 척도.
- 글자 간격·줄 높이: 글자 크기별 naria type 토큰(`--nds-type-*-tracking`, `--nds-type-*-line`)과 naria 구성요소 값만. px 줄 높이 금지.
- 컨트롤 높이: naria Button 36·44·52, Tabs 46, Tag 32, 입력·대화 입력창은 컨트롤 높이(44), 체크박스 20.
- 흰색·옛 변수: `#fff`·`white` 대신 `--nds-surface`·`--nds-on-primary`. 옛 변수(`--line`·`--accent` 등)는 `.nds-theme` 안에서 naria 토큰으로 다시 정의.
- 기준 원본: `lopahn2/naria-design-system` main `d536b75`(2026-11-15)의 `packages/ui/src/tokens.css`·`styles.css`, modern 팔레트는 naria `paletteVariables("modern", light|dark)` 계산값.
- 도구: 변환과 검증은 이 작업의 보조 스크립트로 수행한다. 검증 스크립트와 출력은 `docs/evidence/P21-service-map-naria/naria/`에 남기고, 결과는 화면 캡처와 시험으로도 확인한다.

## 4. 완료 기준

1. 서비스 지도가 시안 B 구조로 동작하고, 기존 계약(사건 진입·선택 상세·수집 진단·이관 이력 분리·미관측 ≠ 0)을 지킨다.
2. 1440×900·1280×800·1920×1080에서 운영 관제 페이지 무스크롤, 1280×800에서 실데이터 서비스 14개가 잘림 없이 보인다. 375/390·768에서 가로 넘침이 없다.
3. 대시보드 CSS, Application 관제 화면 CSS, 다이어그램 TS 색에 naria 근거 없는 값이 남지 않는다. 엄격 검증에서 모든 값이 naria 토큰이거나 naria 구성요소 CSS의 같은 속성·값(줄 번호 제시)이어야 하고, 근거 없음은 0건이다.
4. 단위 시험·타입 검사·빌드·Playwright 모의 시험이 통과한다(기존 실패는 기준선과 구분해 기록).
5. 로컬 클러스터 대시보드에 배포하고 실배포 화면을 다시 캡처한다.

## 5. 진척

- [x] 현재 화면·데이터 분석, 시안 3개(A·B·C) 제작, 사용자 선택(B)
- [x] 서비스 지도 B 구현: `ServiceAtlas.tsx`, `serviceAtlasModel.ts`(`readingTone`·`chipLabel`·`atlasLanes`·`laneSummary`·`linkSummary`·`caseSummary`·`wirePath`), `serviceAtlas.css`, `Overview.tsx`(수집 진단 naria 구성)
- [x] 단위 시험 추가(`tests/service-atlas.test.ts`: 상태 톤, 레인 순서, 사건 요약·칩 문구, 연결선 경로·장애물 회피)
- [x] naria 전체 정리 적용(Codex 작업 종료 확인 후): CSS 15개 파일 702곳 변환, `naria.css` P21 구성요소 블록, 전역 포커스 규칙, 독립 Application 관제 화면 CSS
- [x] `DESIGN.md` P21 절
- [x] 빌드·시험·화면 검증(새 배치에 맞춰 375px 알림 시험 1건의 바깥 클릭 지점 수정)
- [x] 로컬 클러스터 배포와 실배포 캡처
- [x] naria 2차 정리: 엄격 검증 도입(글자 간격·줄 높이·위치 오프셋·컨트롤 높이·흰색 리터럴·TS 색까지), 근거 없는 값 0건, `DESIGN.md` 규칙 추가
- [x] 2차 재빌드·단위·화면 시험·재배포와 실배포 재캡처

## 6. 기록해 둘 일

- Codex가 01:36에 다시 빌드한 이미지 `liveprobe/dashboard:semantic-37f2e55570ac0672`에는 지도 1차본(1280px 칩 줄바꿈·태블릿 연결선 관통 수정 전)이 포함됐다. 최종본 배포로 덮는다.
- 01:29–01:43 기준선 Playwright 실행이 git 추적 밖의 `dashboard/e2e/artifacts`에 375px 캡처 23장을 덮어쓰고 기록 56줄(포트 4195)을 추가했다. 이후 시험은 `LIVEPROBE_EVIDENCE_ROOT`·`LIVEPROBE_W6_EVIDENCE_ROOT`·`LIVEPROBE_E2E_OUTPUT`을 모두 작업 폴더 밖으로 지정했다.
- `e2e/p15-overview.spec.mjs:33`(알림 패널 바깥 클릭으로 닫기)은 `.atlas-map-head`의 (5,5)를 눌렀다. 새 지도에서는 375px에서 그 지점이 알림 패널 머리 아래에 깔려 실패했다(원본 묶음은 통과, naria 정리 전후 묶음 모두 실패 → 지도 배치 때문). 시험 의도(패널 밖 pointerdown이면 닫힘)는 그대로 두고, 모든 폭에서 패널 밖인 상단 `현재 위치` 탐색 막대의 (2,2)를 누르도록 바꿨다. 수정한 시험은 최종 묶음 375·768·1440과 원본 묶음 375에서 통과한다.
- 남은 Playwright 실패는 모두 이 작업 전부터 있던 것이다. 이 작업 전 원본 묶음(지도 변경 전 Codex `dashboard/dist`를 01:27에 보존한 사본)에 같은 폭으로 다시 돌려 똑같이 실패함을 확인했다(7절). 대부분 Codex의 통합 사건 화면 전환 뒤 갱신되지 않은 옛 화면 계약(옛 위상 지도·옛 관제실·옛 시스템 묶음)을 본다. 이 작업 범위가 아니어서 고치지 않았다.
- `index-*.css`가 약 1.9MB인 것은 `@naria/ui`가 글꼴(woff) 두 개를 CSS 안에 넣어 싣기 때문이며 이 작업 전과 같다(naria 정리로 약 10KB 늘었다).
- (정정) 1차 배포 때 이 문서는 "naria에는 글자 간격 토큰이 없다"고 적고 글자 간격을 예외로 남겼는데, 틀린 서술이었다. naria `tokens.css`에는 `--nds-type-*-tracking`이 있다. 1차 점검이 남은 값을 "naria 구성요소 원값"이라고 한 것도 한 줄씩 대조하지 않은 주장이었다. 2차 정리에서 엄격 검증(7.2)으로 바꾸고, 근거 없는 값을 모두 고쳤다.
- 2차 정리에서 고친 것(대시보드 CSS 약 140곳 — 규칙 단위 97건, 배경 `#fff` 일괄 34곳, 후속 8건, 죽은 규칙 1건 삭제, 옛 변수 연결 블록 — 와 Application 관제 CSS 9종, TS 색 8건):
  - 글자 간격 11곳(.08em·.06em·−.3px·−.25px·−.35px·−0.01em·−0.02em→크기별 토큰), 위치 오프셋 6곳(−11→−12, −15→−16, 1→2, −1→0, 모바일 메뉴 10px 16px→10px 12px).
  - px·비naria 줄 높이 30여 곳(→ naria 어휘·토큰), 컨트롤 높이 20여 곳(30·32·28·34·38·40·42px → naria 36·44·46·32).
  - `#fff`·`white` 48곳(→ `--nds-surface`·`--nds-on-primary`), 죽은 `color-mix(…#000)` hover 규칙 삭제(naria `filter: brightness(0.92)` 규칙만 남김), 고리 그림자 3곳(naria 후광·맥박 모양).
  - 다크 작업면 hex 3곳(→ `--diagram-bg`·`--diagram-reroute`), 옛 변수 21개를 naria 토큰에 연결, 다이어그램 TS 색(연결선 5색·라벨·격자, 쓰이지 않는 `EDGE_COLOR`) → naria 라이트·다크 토큰 값.
  - Application 관제 화면의 두 번째 `:root`가 강조 연한 색을 naria 기본 청록(#e2f4f1)으로 덮어써 modern 팔레트(#edf0f2)와 어긋나던 것도 토큰 연결로 바로잡았다.
- 커밋(2026-11-21, 사용자 결정):
  - 커밋한 것: `dashboard/` 전체와 이 문서, 텍스트 증거(`docs/evidence/P21-service-map-naria/naria/`의 `naria-verify.py`·`scan-native.cjs`·`modern-palette.json`·`verify-*.txt`). `dashboard/` 전체에는 이 작업이 기대는 Codex의 미커밋 대시보드 작업도 함께 들어간다. 커밋 직전(15:12부터) Codex가 같은 폴더에서 작업을 다시 시작해서, 커밋(15:28:27)에는 Codex의 stage-motion 변경(Codex가 15:25 전에 배포)과 진행 중이던 stage-rail 변경 일부도 들어갔다. 그래서 커밋 내용은 어느 배포 묶음(이 작업의 generation 60 포함)과도 똑같지 않다. 커밋 트리만 따로 꺼내 확인한 결과는 타입 검사·빌드 통과, 단위 시험 219건 통과다. Codex의 그 뒤 변경은 커밋하지 않았다.
  - 커밋하지 않은 것: `liveprobe/`(백엔드·Application 관제 화면, 이 작업의 Application 관제 CSS 수정 포함)·테스트·배포 쪽 변경. Codex 작업과 함께 남겼다.
  - 화면 캡처 PNG는 `.gitignore`(`docs/evidence/`) 규칙대로 로컬에만 있다.
- 범위 밖: `services/*/static`(라이브프로브가 감시하는 예제 서비스 화면)과 `.hermes/evidence`의 과거 빌드 사본은 라이브프로브 UI가 아니어서 고치지 않았다.

## 7. 검증 근거 (2026-11-21)

### 7.1 단위·타입·빌드 (최종 = 2차 정리 후)

- `npm test`: 219건 통과, 실패 0(1차·2차 모두).
- `npm run build`(`tsc --noEmit` + `vite build`): 성공. 작업 폴더 `dashboard/dist`는 시험·배포에 쓴 최종 묶음과 파일 해시가 같다(전체 해시 `d83aa9f4626cb9f9`; `index-LB2nmkAF.js`·`index-Dk5HC-Vz.css`·`Overview-Bph7Af-F.js`·`Overview-CFstvFoz.css`).
- 1차 배포 묶음은 전체 해시 `8322e385187f214f`였다.

### 7.2 naria 엄격 검증 (2차 정리 후)

검증 스크립트 `docs/evidence/P21-service-map-naria/naria/naria-verify.py`가 아래 값을 하나씩 분류한다. `token`은 naria 토큰이나 토큰 척도 안의 값이다. `naria`는 naria `packages/ui/src/styles.css`(main `d536b75`)에 같은 속성·값이 있는 값으로, 줄 번호를 함께 적는다. `FAIL`은 근거가 없는 값이다.

- 검사 대상: 모든 색(hex·키워드·rgb), 간격·위치 오프셋, 곡률, 글자 크기·굵기, 글자 간격(그 규칙의 글자 크기에 맞는 naria 값인지), 줄 높이(px 금지, naria 어휘 1·1.18·1.3·1.4·1.5·1.6·1.65·1.7과 토큰), 버튼·입력 높이, 그리고 TS·TSX 안의 색.
- 결과:
  - 대시보드(`dashboard/src` CSS 16개 + TS·TSX): **FAIL 0**, naria 근거 158건(`verify-dashboard.txt`).
  - Application 관제 화면(`liveprobe/app/application/static/styles.css`): **FAIL 0**, naria 근거 5건(`verify-application-console.txt`).
- naria 근거가 붙은 값의 예:
  - 글자 크기 13px → `.nds-label` 등, 11px → `.nds-field-count`·`.nds-badge`.
  - 곡률 8px → `.nds-tag`·`.nds-color-input`, 12px → `.nds-combobox-popup`·Alert, 13px → `.nds-toast`.
  - 간격: Card `22px 22px 0`·`5px`, Badge `3px 8px`·gap `5px`, Button sm `7px 11px`, Input `11px 13px`, Tabs `10px 4px`, 내비게이션 `10px 12px`·gap `9px`, DataTable `13px 16px`, DescriptionList `13px 0`, DiagramNode·Toast gap `10px`.
  - 글자 간격: 18px −0.015em(`.nds-text--subheading`), 24px −0.025em(`.nds-text--heading`), Card 제목 17px −0.02em(`.nds-card h3`).
- 토큰 정의 확인:
  - 대시보드가 쓰는 naria 토큰 51개(`--nds-type-*-tracking`·`--nds-type-small-line`·`--nds-control-height` 등)가 배포 묶음의 `@naria/ui` 1.0.1 CSS에 모두 정의돼 있다.
  - Application 관제 화면은 쓰는 변수 33개를 자기 `:root`에 모두 정의한다.
- 선언 안에 hex가 남은 곳은 naria가 hex로 정의한 값 세 가지 6곳뿐이다.
  - dialog 배경막 `#0b141865`(`.nds-dialog::backdrop`)
  - 다크 그림자 `0 4px 24px #0002`(다크 `--nds-shadow`)
  - 경로 변경 청록 `#086d72`(`tokens.css`의 기본 accent·focus 값; modern 팔레트가 accent를 바꾸므로 별도 의미색으로 고정)
- 값 검사로는 잡히지 않는 "스타일이 아예 없는 기본 컨트롤"도 화면에서 찾았다(브라우저 기본 테두리색·inset 테두리·13.33px 글꼴·기본 버튼 배경). 모의 화면 10곳은 0건이었다. 실서버(GET만 허용)에서는 두 곳이 나왔다.
  - 관제실 결과 소명 양식의 `textarea`
  - 설정 관측 수집원의 `select`
  - 둘 다 이 작업 전부터 스타일 규칙이 없던 곳이다. naria Field(세로·간격 8px)·Label(13px/600)·Input(44px·11px 13px·16px·strong-border·control 곡률)·Textarea(110px)·Select 규격으로 그렸고, 재배포 뒤 같은 검사로 0건을 확인한다(7.6).
- 다크 다이어그램 작업면의 naria 다크 토큰 값(#11181c·#303b40·#80d9cc 등)은 `--diagram-*` 변수 정의에만 hex로 둔다. 라이트 테마에서는 `var()`로 다크 토큰을 참조할 수 없기 때문이다.

### 7.3 Playwright 모의 전체 시험 (127건, 결과·캡처는 작업 폴더 밖에 저장)

| 묶음 | 폭 | 통과 | 실패 | 건너뜀 |
| --- | --- | --- | --- | --- |
| naria 1차 정리 전(지도 변경 포함) | 1440 | 98 | 28 | 1 |
| 1차 배포 묶음 | 1440 · 768 · 375 | 98 · 91 · 90 | 28 · 32 · 33 | 1 · 4 · 4 |
| 2차 정리 묶음 | 1440 · 768 · 375 | 98 · 91 · 91 | 28 · 32 · 32 | 1 · 4 · 4 |
| **최종(배포) 묶음** | 1440 · 768 · 375 | 98 · 91 · 91 | 28 · 32 · 32 | 1 · 4 · 4 |

- 각 단계의 실패 목록은 폭마다 앞 단계와 같다. 2차 정리와 최종 묶음에서 새로 생긴 실패는 없다. 375가 33 → 32로 준 것은 아래 알림 시험 수정 때문이다.
- 1440 실패 28건은 이 작업 전 원본 묶음 1440에 다시 돌려도 모두 실패한다. 768·375에서만 더 나오는 `p19-detail` 4건(:17·:45·:58·:96)도 원본 묶음의 같은 폭에서 실패한다.
- 지도 배치 때문에 생긴 실패는 375의 `p15-overview:33` 1건뿐이었다. 6절처럼 시험을 고쳤고 세 폭에서 모두 통과한다.
- 서비스 지도 전용 `service-atlas.mock.spec.mjs` 5건은 모든 묶음·세 폭에서 통과했다.
- 원본에서도 실패하는 기존 목록: `human-access:4`, `p15-incident-room:31`, `p15-overview:8·50·58·64·80`, `p15-review3:6·20`, `p15-states:5`, `p16-diagrams:37·78·102·128`, `p18-envs:11·64`, `p18-w6-room:44·59·79·199·294·339·346·361`, `p19-detail:78`(좁은 폭에서는 `:17·:45·:58·:96` 추가), `p20-renewal:79`, `samsung-systems:16·50`.
- 2차 정리 전후 모의 화면 10개(운영 관제·장애 관제·관제실 971·검토 기록·설정 × 1440·390)는 페이지 스크롤·가로 넘침 수치가 모두 같다. 픽셀 차이는 글자 간격(브랜드·레인 제목)과 다이어그램 조작 버튼 36px·연결선 색에 한정된다.

### 7.4 배포 전 화면 측정

- 실데이터 스냅샷 재생(최종 묶음, `docs/evidence/P21-service-map-naria/map/final-live-snapshot-*`): 1440×900·1280×800에서 페이지·지도 스크롤 0, 칩 잘림 0. 768·390에서 가로 넘침 0.
- naria 정리 전후 비교(모의 데이터, `docs/evidence/P21-service-map-naria/naria/before-after-*`): 모든 화면에서 가로 넘침 0. `/incidents` 1440은 naria 입력 높이 때문에 목록이 148px 길어져 스크롤된다(목록 화면이라 허용). `/ledger` 390은 159px 스크롤된다(모바일 허용).

### 7.5 로컬 클러스터 배포

- 방법: 보조 스크립트가 현재 배포 이미지(같은 nginx 기반)에서 정적 묶음과 `nginx.local-operator.conf`만 바꾼 이미지를 만든다. 이미지 안 파일 8개의 해시와 nginx 경로 24개(`scripts/verify-local-dashboard-image.sh`)를 확인한다. 그다음 generation과 현재 이미지를 조건으로 건 JSON patch를 서버 dry-run으로 먼저 확인하고 `deployment/dashboard`에 적용한다. API 등 다른 자원은 바꾸지 않았다.
- 배포 이력(모두 롤아웃 성공):

| generation | 이미지 | 내용 |
| --- | --- | --- |
| 57 | `sha256:510f8a08…` | 작업 전(Codex 배포, 지도 1차본 포함) |
| 58 | `p21-182f4206da5c8cc3` = `sha256:e95e1194…` | 지도 B + naria 1차 |
| 59 | `p21-beb87a9f2aa990fb` = `sha256:413f46fd…` | naria 2차(엄격 검증) |
| **60** | `p21-c3d9f5b32cbcbb5f` = `sha256:38b5698ef42da21ca3e6411962f0b10d59f68347fb12e2c824255c7fac7bb820` | 최종: 기본 모양 컨트롤 2곳 naria 규격 |

- 되돌리기:
  - 직전(2차)으로: `kubectl --context rancher-desktop -n liveprobe-system set image deployment/dashboard dashboard=liveprobe/dashboard@sha256:413f46fd82d2408f3f89ff21057c6c28eac903a4f821c7a970735f69b96e03e1`
  - 작업 전으로: `kubectl --context rancher-desktop -n liveprobe-system set image deployment/dashboard dashboard=liveprobe/dashboard@sha256:510f8a08dc711ab5f865d1cf8a4de1ea5ad2d0cba74e0c31fca152bc15357619`
- generation 60 뒤에 Codex가 같은 작업 폴더에서 자기 작업(stage-motion·stage-rail·compact-graph)을 얹어 61–63을 배포했다. 16시 기준 실서버 63의 CSS에도 이 작업의 규칙(결과 소명 양식, `--nds-type-*-tracking` 등)이 그대로 들어 있다.
- Application 관제 화면 CSS(`liveprobe/app/application/static`)는 API 이미지에 들어가는 파일이다. 이 화면은 지금 서비스되지 않아서 API는 다시 배포하지 않았다. 다음 API 이미지 빌드 때 함께 나간다.

### 7.6 실배포 캡처 (최종 generation 60, 운영자 게이트웨이 `127.0.0.1:4175`, GET만 허용, 차단된 요청 0건)

- 게이트웨이가 최종 묶음(`index-LB2nmkAF.js`·`index-Dk5HC-Vz.css`·`Overview-Bph7Af-F.js`·`Overview-CFstvFoz.css`)을 내보낸다.
- 지도(실데이터, 시스템 4·서비스 14·연결 2·수집원 4개 연결됨):

| 화면 | 페이지 스크롤 | 지도 스크롤 | 가로 넘침 | 칩 잘림 |
| --- | --- | --- | --- | --- |
| 1920×1080 | 0 | 0 | 0 | 0 |
| 1440×900 | 0 | 0 | 0 | 0 |
| 1280×800 | 0 | 0 | 0 | 0 |
| 768×1000 | 1164(세로 쌓기) | 0 | 0 | 0 |
| 390×844 | 1982(세로 쌓기) | 0 | 0 | 0 |

- 1280×800에서는 노드 보조 줄(`장애 1건 · → business-api · 선언`)이 말줄임으로 줄어든다. 전체 문구는 `title`에 남고, 연결은 지도 연결선으로, 사건은 선택 상세의 `연결된 사건`으로도 보인다.
- 장애 관제·검토 기록·설정의 1440·390 화면도 가로 넘침 0이다.
- 관제실(`/incidents/1553`)에서 다이어그램 조작 버튼과 단계 버튼은 36px(naria Button sm)이다. 결과 소명 양식은 naria Field 모양이다(`room-form-before-after-1440.png`).
- 브라우저 기본 모양 컨트롤 검사(`scan-native.cjs`, 운영 관제·장애 관제·관제실 6곳·검토 기록·설정): 배포 전 2건 → 최종 0건.
- 파일: `docs/evidence/P21-service-map-naria/deployed/`(최종 배포 캡처 13장 + 소명 양식 전후 1장).

## 8. 후속: 노드 박스 압축 (2026-11-21 인터뷰)

1990px 화면에서 노드 카드가 레인을 3~4칸으로 나눠 약 436px까지 늘어나고 dev/prd 칩이 카드 폭을 나눠 갖는 문제를 고쳤다. 시안 세 가지(2줄 콤팩트 카드 · 1줄 행 · 상태 점 타일)를 실제 크기로 보이고 소유자가 **C · 상태 점 타일**을 골랐다.

- 타일 폭을 176px로 고정하고 레인 트랙은 `repeat(auto-fill, 176px)`로 왼쪽부터 채운다. 600px 이하는 두 열로 채운다.
- 준비 상태 칩은 `dev ●`처럼 점만 보이고 낱말(`준비`)은 화면 낭독기 이름과 도움말에 남긴다. 장애·주의·미관측일 때만 글자와 톤을 보인다.
- 경고 아이콘은 장애·주의 타일에만 둔다. 연결선은 기존 실측(ResizeObserver)으로 새 위치에 다시 그린다.
- 범위·완료 기준·검증은 `docs/plans/stage-card-brief-2026-11-21.md`가 함께 기록한다.
