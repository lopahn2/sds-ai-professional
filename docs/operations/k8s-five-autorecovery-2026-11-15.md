# 🧪 K8s 장애 5건 자동 복구 검증

2026-11-15 · Codex · dev 실환경 검증 · 기존 자료 보존

---

> ✅ **완료** — 5개 업무의 실제 이미지 가져오기 실패를 감지하고 AI가 롤백을 실행해 복구했다. 아래 성공 사건은 모두 운영자 인계 없이 종료됐다. 앞선 실패·인계 기록은 삭제하거나 성공으로 바꾸지 않았다.

## 결과

| 대상 | 사건 | AI 수준 | 자동 조치 | 결과 |
|---|---|---|---|---|
| dev/order | [#1407](http://127.0.0.1:4175/incidents/1407) | SIMPLE | Action 113 · rollout_undo | resolved / succeeded |
| dev/payment | [#1408](http://127.0.0.1:4175/incidents/1408) | SIMPLE | Action 114 · rollout_undo | resolved / succeeded |
| dev/catalog | [#1414](http://127.0.0.1:4175/incidents/1414) | SIMPLE | Action 116 · rollout_undo | resolved / succeeded |
| dev/notification | [#1415](http://127.0.0.1:4175/incidents/1415) | SIMPLE | Action 117 · rollout_undo | resolved / succeeded |
| dev/inventory | [#1416](http://127.0.0.1:4175/incidents/1416) | SIMPLE | Action 118 · rollout_undo | resolved / succeeded |

최종 비교: **23:43:41 KST**. dev 9개와 prd 9개 Deployment 모두 준비 완료이며, 양쪽 원래 파드의 UID·컨테이너 ID·재시작 횟수가 그대로다. dev 배포 설정은 시험 전 값으로 복구됐고 prd 배포 설정은 바뀌지 않았다. PVC 12개의 UID·연결 볼륨·상태가 동일하다. `inventory_dev.inventory.stock` 5개 행의 전후 SHA256도 일치한다. 다른 DB 테이블 전체의 내용 해시를 검사한 것은 아니다.

화면, API health, API readiness가 모두 HTTP 200이다. DB 연결, 분석 작업자, 백그라운드 감시가 정상이며 RAG 활성 청크는 64개다. 완료 시점의 open/analyzing/acting/action_pending 사건은 각각 0건이다.

## 합의된 범위와 판정 기준

- 사용자 요청은 Kubernetes에 한정한 자동 조치 대상 장애 5건이다. 기존 승인에 따라 dev에만 주입하고 prd는 조회했다.
- 과거 자동 롤백 성공 사건 #992, #993, #994, #995, #1052를 참고했다.
- Deployment의 주 컨테이너 이미지만 존재하지 않는 시험 태그로 변경했다. maxSurge=1, maxUnavailable=0을 유지해 기존 정상 파드는 계속 서비스했다.
- **서로 다른 장애 유형 5가지나 복잡 장애 5건을 검증한 결과가 아니다.** 이미지 가져오기 실패 한 유형을 업무 5개에서 검증했다. 신규 배포는 실제 실패했지만 전체 서비스 중단은 유발하지 않았다.
- 합격은 새 사건 감지, rollout_undo succeeded, 사건 resolved, 배포 원복, 기존 파드 보존을 모두 만족해야 한다. 단순 정상 관측이나 수동 원복은 성공 집계에서 제외한다.
- DB·PVC·저장 이력을 삭제하지 않았다. 모델 판단, 사건 상태, Action 성공 값을 직접 변경하지 않았다.

## 인증서와 실행 경계 보정

기존 실행기는 옛 liveprobe-target 환경만 다뤘고 dev 권한이 없었다. 현재 저장소의 네임스페이스·대상별 고정 이미지 검증 구현을 반영하고 dev의 위 5개 Deployment에만 get/patch 및 ReplicaSet list 권한을 추가했다. prd 쓰기 권한과 Secret 조회 권한은 추가하지 않았다.

첫 inventory 시험에서 롤백 서버 인증서와 CA가 모두 2026-10-27 만료됐음을 확인했다. 최초 갱신 실행은 자동 승인 검토가 발급용 개인키 저장과 신뢰 변경의 명시적 승인 부족으로 거절했으며 실행되지 않았다. 이후 사용자가 **“인증서·신뢰 설정 갱신 후 5건 검증 계속”**을 명시 승인해 다음을 적용했다.

- 새 rollback-admission-tls-20261115, rollback-ca-issuer-20261115 Secret 생성. 기존 TLS Secret 보존.
- API와 검증 웹훅에 기존+신규 CA 신뢰 묶음 적용. 실행기는 새 TLS Secret 사용.
- 새 서버 인증서 만료: **2027-11-15 13:41:33 UTC**. 인증서 검증을 켠 API→실행기 HTTPS 연결 확인.
- 서명·인증 없는 요청은 403, 실행기 권한으로 시도한 이미지 외 replica 변경은 실제 서버 dry-run에서 거부됨.
- 업무 파드는 재시작하지 않았다. 이후 코드 수정 반영 시 관제 API만 교체했다.

개인키는 Kubernetes Secret 및 제한된 저장소 밖 발급 디렉터리에만 있다. 증거 문서에는 키, 인증 토큰, 전체 Secret, 운영 설정 원문을 넣지 않았다.

## 검증 중 수정한 조기 인계 원인

1. **후보 번호 계약:** 검토 응답은 1부터 번호를 주는데 수용 검사는 0부터 해석했다. 0 기반 계약과 번호표를 명시하고 잘못된 번호는 독립 검토자에게 한 번만 재작성을 요청한다. 번호를 임의 변환하거나 지지 판정을 만들어 넣지 않는다.
2. **정상 리비전 정보 누락:** 긴 근거의 중간이 생략되면서 직전 정상 ReplicaSet의 가용성도 빠졌다. 수집한 현재/직전 리비전·가용 수·이미지를 제한된 근거 보기 앞에 둔다. 전체 이력과 다른 출처·RAG 공간은 유지한다. 직전 리비전이 불량하거나 없으면 그대로 표시한다.
3. **원문 인용 오류:** SIMPLE 역할에서 원문과 맞지 않는 인용은 해당 역할에 한 번만 재작성을 요청한다. 검토자의 번호·인용 오류가 동시에 있어도 추가 호출은 합계 한 번이다. 반복 오류와 올바른 반대 판정은 자동 조치를 차단한다.

독립 검증, 인용 일치 검사, 신뢰도, 허용 대상, 서명 승인, 이미지 외 변경 거부, 리소스 버전 비교를 유지했다. HIGH_RISK 이관 정책도 변경하지 않았다. 추가 모델 호출은 기존 실행 원장과 비용 기록 경로를 거친다.

관련 회귀 검사 **47개 통과**, 실행기·서명 승인 검사 **23개 통과**, git diff --check 통과. 수정한 세 모듈의 로컬 SHA256과 실제 API 파일 SHA256 일치 확인. 전체 프로젝트 테스트를 다시 실행한 결과는 아니다.

order/payment는 인증서 갱신 후 성공했고, catalog/notification/inventory는 세 가지 코드 보완까지 반영한 서버에서 성공했다. 후자의 세 건은 형식 재시도 없이 분석자·검토자가 각각 한 번 응답해 성공했다. 형식 재시도 경로는 회귀 검사로 검증했다.

## 실패·보조 사건도 보존

| 사건 | 실제 결과 | 성공 집계에서 제외한 이유 |
|---|---|---|
| #1406 inventory | handed_off / Action 112 failed | 만료 인증서로 롤백 연결 실패. 시험 이미지 수동 원복 |
| #1409 catalog | handed_off / Action 없음 | 후보 번호 불일치. 시험 이미지 수동 원복 |
| #1410 catalog | SIMPLE→NORMAL 후 handed_off / Action 없음 | 직전 리비전의 정상 근거가 제한된 보기에 누락. 수동 원복 |
| #1411 catalog | resolved / observed / Action 없음 | 원복 후 잔여 경고에서 현재 정상 상태를 관측했을 뿐 자동 조치를 실행하지 않음 |
| #1412 catalog | handed_off / Action 없음 | 인용문이 원문과 일치하지 않아 차단. 수동 원복 |
| #1413 catalog | handed_off / Action 115 failed, executed=false | 같은 실패 파드의 후속 경고가 새 사건으로 생성됨. 수동 원복 14:31:33 UTC 이후 14:31:36에 롤백이 제안됐고 실행기가 거부함. API 메시지는 거부/연결 오류를 통합하므로 상세 거부 원인은 확정하지 않음 |

위 5개의 인계 사건은 실패 이력으로 남아 있다. 자동 복구된 5건과 별개이며, 완료 숫자를 맞추기 위한 상태 변경이나 운영자 종료 처리를 하지 않았다. 시험 소유 이미지 변경은 모두 원복됐다.

## RAG와 스윔레인

성공한 5건 중 order/payment/catalog/notification 4건은 분석자·검토자가 image-pull-crashloop.md를 실제 인용했다. inventory #1416은 수집된 Kubernetes 근거로 판단했고 저장된 RAG 인용은 없으므로 RAG 사용 사례로 집계하지 않는다. 검색 결과를 받았다는 사실과 실제 인용을 구분해 원문 및 문서 위치를 증거에 남겼다.

스윔레인은 주체를 가로 열에, 시작부터 종료까지를 세로 방향에 배치했다. RAG 검색·출처 검증·안전한 종료 요약 재색인 외에도 Prometheus/Alertmanager, K8s Watch/API, Loki/Alloy, Hubble/Retina, 역할별 모델 선택, 독립 검증, 제한된 재시도, 안전 실행, PostgreSQL 실행 이력, SSE를 각 단계에 반영했다. 다이어그램의 전체 목표 흐름을 이번 한 장애 유형의 검증 결과와 혼동하지 않는다.

## 검증 증거

- [최종 배포·파드·PVC·재고 자료 비교](../evidence/k8s-five-autorecovery-2026-11-15/final-result.json)
- [성공 5건의 실제 Action·Agent·RAG 인용](../evidence/k8s-five-autorecovery-2026-11-15/successful-incidents.json)
- [실패·보조 사건 기록](../evidence/k8s-five-autorecovery-2026-11-15/other-trial-incidents.json)
- [인증서·권한 경계 검증](../evidence/k8s-five-autorecovery-2026-11-15/renewal-verification.json)
- [실행 중 코드 일치 확인](../evidence/k8s-five-autorecovery-2026-11-15/deployed-source-verification.json)
- [화면·API·작업자 상태](../evidence/k8s-five-autorecovery-2026-11-15/server-health.json)
- [검증 요약과 한계](../evidence/k8s-five-autorecovery-2026-11-15/verification-summary.json)
- [구현 변경분](../evidence/k8s-five-autorecovery-2026-11-15/implementation.patch)

실행 중 API: liveprobe/liveprobe@sha256:f4bc363b2cbb7d8799f2e56f34da07e589d75c6745d05e01e00586042b37f79f.

실행기: liveprobe/rollback-controller@sha256:d65b604a5ce60cafa182025b08fde62975cf5c15880da6b920ad5761eacb3d08.

작업 소스: /private/tmp/liveprobe-publish-20261115/source. 검증 자료와 구현 변경분을 연결된 작업 폴더에도 보관했다. 이 작업에서 원격 저장소 push는 하지 않았다.
