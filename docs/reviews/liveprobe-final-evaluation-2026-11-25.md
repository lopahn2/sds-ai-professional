# LiveProbe 장애 대응 비교 최종 보고서

2026년 11월 25일 · 저장된 시험 결과 재집계 · 커널 OOM 제외

## 핵심 결론

- **조치 후 실제 서비스 복구**를 확인한 사례는 LiveProbe 11개, Astra와 Sol 각 54개, Terra 55개, Luna 45개였다. 다섯 방식 모두 대응 시간이 기록된 동일 50개 사례에서도 LiveProbe 11개, 단독 모델 34~44개로 차이가 유지됐다. LiveProbe에서 사건이 생성되지 않은 18개는 분석 미실행으로 따로 확인했다. 그중 7개는 서비스가 자연 회복했고 11개는 관측 종료까지 장애가 지속됐다. [공식 실행 원장](../../.hermes/evidence/liveprobe-comparison-2026-11-23/live-repair-status-dev-native-campaign-355.csv)
- **AI 환산 비용**은 동일 49개 사례에서 LiveProbe가 Astra보다 **53.4% 낮았다**. Sol·Terra·Luna보다는 높았다. 동일 50개 사례의 기록상 대응 시간 합은 LiveProbe가 네 단독 모델보다 **2.21~3.48배** 길었다. 비용 절감 신호는 Astra 비교에서 확인됐으며, 같은 수준의 복구 성과와 더 빠른 대응을 함께 달성한다는 가설은 입증되지 않았다. [공식 실행 원장](../../.hermes/evidence/liveprobe-comparison-2026-11-23/live-repair-status-dev-native-campaign-355.csv) · [비교 판정과 집계 코드](../../evaluation/comparison_summary.py)

## 시험 범위와 판정

전체 캠페인의 진행 현황은 **355/375회 완료**였다. 이 보고서의 비교 범위는 **68개 시나리오 × 5개 방식 = 340회**다. 기본 20개, 신규 Kubernetes 18개, Application 30개를 포함한다. 커널 `OOMKilled` 주입이 시험 조건인 `F2-01`, `F2-03`, `F2-05`, `F2-07`, `F2-10`, `F2-13`, `F2-15`를 제외했다. 제외한 35회는 완료 15회와 미실행 20회다. Application의 `delivery-09`, `delivery-29`, `notification-20`은 격리 서비스의 메모리 증가·업무 오류를 검증한 사례로 포함했다. [선정 카탈로그](../../evaluation/catalogs/comparative-2026-11-23.json) · [커널 OOM 정의](../../evaluation/dev_oom_case.py) · [Application 메모리 관측](../plans/liveprobe-comparison-2026-11-23.md)

단독 모델은 최초 오류 로그와 조사·복구 요청으로 시작하고, 이후 필요한 자료를 시험 도구로 조회했다. LiveProbe는 시험별 수집·분류·분석·조치 절차를 사용했다. 포함한 68개 사례 모두에서 다섯 방식의 초기 입력 지문이 일치했다. 비교에는 dev 실워크로드, 별도 Kubernetes 시험 클러스터, 격리 Application 시험 환경이 들어 있다. [시험 프로토콜](../plans/liveprobe-comparison-2026-11-23.md#입출력과-판정) · [원본 영수증 대조](../../.hermes/evidence/liveprobe-comparison-2026-11-23/campaign-355-source-reconciliation-20261125/verification.json)

- **조치 후 복구**는 해당 방식이 적용한 조치에 이어 독립 업무 확인 또는 리소스 확인을 통과한 기록이다.
- **자연 회복**은 장애 만료 등으로 정상 응답을 다시 확인한 기록이다. 자동 복구 건수에는 합산하지 않았다.
- **분석 후 미복구**는 대응이 실행됐고 관측 구간 내 조치 후 복구를 확인하지 못한 기록이다. **미감지·장애 지속**은 시험기가 장애 지속을 확인했으나 LiveProbe 사건이 생성되지 않아 분석이 실행되지 않은 경우다. 이 경우는 분석 후 미복구와 구분하며 비용을 0으로 대체하지 않았다. [판정 코드](../../evaluation/comparison_summary.py)

## 방식별 복구 결과

각 방식은 68개 독립 서비스 결과를 보유한다. 원장의 LiveProbe `unrecovered` 44개 중 사건 미감지 11개를 별도 열로 옮겼다. 자연 회복 13개 중 7개도 사건 없이 회복됐으므로 실행이 관측된 LiveProbe 대응은 50개다. [공식 실행 원장](../../.hermes/evidence/liveprobe-comparison-2026-11-23/live-repair-status-dev-native-campaign-355.csv) · [미감지 원인 분류](../../evaluation/comparison_summary.py)

| 방식 | 서비스 결과 관측 | 조치 후 복구 | 자연 회복 | 분석 후 미복구 | 미감지·장애 지속 |
| --- | ---: | ---: | ---: | ---: | ---: |
| LiveProbe | 68 | **11** | 13 | 33 | 11 |
| GPT-6.2 Astra · xhigh | 68 | 54 | 13 | 1 | — |
| GPT-6.2 Sol · high | 68 | 54 | 13 | 1 | — |
| GPT-6.1 Terra · medium | 68 | 55 | 13 | — | — |
| GPT-6 Luna · low | 68 | 45 | 13 | 10 | — |

아래는 **조치 후 복구**만 시험군별로 나눈 결과다. 신규 Kubernetes에서 LiveProbe는 11개 사례를 감지하지 못했고, 사건이 관측된 7개에서도 시험 중 조치 후 복구가 없었다. Application에서는 LiveProbe의 나머지 22개가 운영자 이관으로 끝났다. 이관 사유는 수정 미지원 18개, 근거 인용 검증 실패 3개, 시험·빌드 증빙 검증 실패 1개였다. 사유 기록은 개별 이관에 대한 독립적인 적정성 판정까지 제공하지 않는다. [공식 실행 원장](../../.hermes/evidence/liveprobe-comparison-2026-11-23/live-repair-status-dev-native-campaign-355.csv) · [이관 처리 코드](../../liveprobe/app/application/worker.py)

| 시험군 | 사례 수 | LiveProbe | Astra | Sol | Terra | Luna |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 기본 | 20 | 3 | 7 | 7 | 7 | 6 |
| 신규 Kubernetes | 18 | 0 | 18 | 18 | 18 | 18 |
| Application | 30 | 8 | 29 | 29 | 30 | 21 |

## 방식별 대응 시간·토큰·비용

대응 시간은 원장의 `wall_duration_ms`를 초로 변환했다. 사례에 따라 조사 시작, 서비스 오류, 사건 종료, 모델 평가의 경계가 다르므로 **시험 기록상 대응 구간**으로 해석한다. 다섯 방식에 시간이 모두 기록된 50개 사례는 기본 13개, 신규 Kubernetes 7개, Application 30개다. 토큰과 비용은 두 값과 모델 호출이 모두 관측된 49개 사례를 사용했다. 시간만 확인된 `F1-12`는 비용·토큰 집계에서 제외했다. 비용은 원장에 기록된 모델 단가 기반 USD 환산액이며 구독 청구액과 별개다. [공식 실행 원장](../../.hermes/evidence/liveprobe-comparison-2026-11-23/live-repair-status-dev-native-campaign-355.csv) · [대응 시간 집계](../../evaluation/comparison_summary.py) · [단독 모델 비용 산식](../../evaluation/single_baseline.py)

| 방식 | 대응 시간 중앙값 · 50개 | 대응 시간 합 · 50개 | 총 토큰 · 49개 | 비용 합 · 49개 | 사례당 비용 중앙값 · 49개 |
| --- | ---: | ---: | ---: | ---: | ---: |
| LiveProbe | 84.15초 | 9,461.73초 | 3,848,699 | **$10.1362** | $0.0624 |
| GPT-6.2 Astra | 68.00초 | 3,277.49초 | 3,515,352 | $21.7383 | $0.4220 |
| GPT-6.2 Sol | 58.80초 | 3,000.42초 | 3,358,495 | $5.5644 | $0.1168 |
| GPT-6.1 Terra | 51.06초 | 2,719.15초 | 3,380,994 | $1.0522 | $0.0200 |
| GPT-6 Luna | 80.07초 | 4,283.41초 | 8,556,937 | $0.4094 | $0.0074 |

### 시험군별 비용과 시간

Astra 대비 비용 차이는 시험군마다 달랐다. 전체 순 절감액의 87.1%는 Application에서 나왔고, 신규 Kubernetes의 공통 관측 7개에서는 LiveProbe 비용이 Astra보다 27.8% 높았다. 아래 표에서 음수는 LiveProbe 비용 절감, 양수는 비용 증가다. [공식 실행 원장](../../.hermes/evidence/liveprobe-comparison-2026-11-23/live-repair-status-dev-native-campaign-355.csv)

| 시험군 | 공통 시간 / 비용 사례 수 | 대응 시간 중앙값 · LiveProbe | 대응 시간 중앙값 · 단독 모델 4종 범위 | 비용 합 · LiveProbe / Astra | Astra 대비 비용 차이 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 기본 | 13 / 12 | 246.69초 | 45.18~64.02초 | $2.7281 / $4.8310 | −43.5% |
| 신규 Kubernetes | 7 / 7 | 238.56초 | 31.47~47.86초 | $2.7708 / $2.1678 | +27.8% |
| Application | 30 / 30 | 45.98초 | 56.46~98.65초 | $4.6373 / $14.7395 | −68.5% |

Application에서 LiveProbe의 대응 중앙값 45.98초에는 운영자 이관 22개가 포함된다. 이관 22개의 중앙값은 39.17초, 조치 후 복구 8개의 중앙값은 321.31초였다. 원본 대조에서 LiveProbe는 소스 수정 대상 9개 중 8개를 복구했고, 설정 복원이 필요한 21개는 모두 이관했다. 단독 모델은 격리 시험기가 제공한 설정 복원 도구로 그 21개 중 14~21개를 복구했다. 전체 대응 중앙값을 비교할 때 종료 결과와 사용할 수 있었던 조치를 함께 봐야 한다. [Application 150회 원자료 대조](../../.hermes/evidence/liveprobe-comparison-2026-11-23/application-comparison-audit-20261125.json) · [공식 실행 원장](../../.hermes/evidence/liveprobe-comparison-2026-11-23/live-repair-status-dev-native-campaign-355.csv)

### 다섯 방식이 모두 조치로 복구한 사례

공통 10개 사례에서 LiveProbe 비용은 Astra보다 45.4% 낮았고 대응 시간 합은 3.20배였다. 이 10개는 기본 3개와 Application 7개로 구성된다. [공식 실행 원장](../../.hermes/evidence/liveprobe-comparison-2026-11-23/live-repair-status-dev-native-campaign-355.csv)

| 방식 | 대응 시간 합 · 10개 | 총 토큰 · 10개 | 비용 합 · 10개 |
| --- | ---: | ---: | ---: |
| LiveProbe | 2,551.97초 | 1,119,852 | $3.1277 |
| GPT-6.2 Astra | 797.68초 | 817,510 | $5.7257 |
| GPT-6.2 Sol | 803.43초 | 800,187 | $1.4635 |
| GPT-6.1 Terra | 703.25초 | 786,765 | $0.2620 |
| GPT-6 Luna | 1,016.19초 | 2,001,677 | $0.1028 |

## 가설 판정과 해석 한계

- **비용과 고성능 호출.** 과제계획서 초안의 30% 비용 절감 기준은 Astra와 공동 관측한 49개 사례에서 수치상 넘었다. 같은 사례의 Astra 호출은 LiveProbe 24회, Astra 단독 247회로 90.3% 줄었다. 양쪽에서 토큰·비용을 모두 확인한 범위의 결과이며, 전체 범위의 복구 품질을 유지한 비용 절감으로 일반화할 수 없다. [과제계획서](../../과제계획서.md#개선-목표) · [공식 실행 원장](../../.hermes/evidence/liveprobe-comparison-2026-11-23/live-repair-status-dev-native-campaign-355.csv)
- **원인과 근거.** 독립 검토 기록이 있는 Kubernetes 13개 사례에서 최초 분석의 상위 3개 후보에 실제 원인이 모두 포함됐다. 원장에는 LiveProbe 22개 사례의 인용 367개 중 353개가 모델에 제시된 근거와 일치한 것으로 기록됐다. 표본 일치율은 96.2%다. 전체 68개 사례의 원인 적중률이나 원본 로그 일치율을 산출할 정도로 검토 범위가 넓지 않다. 역할·모델 조합의 기준 일치와 운영자 이관 적정성도 전체 범위에서 미검증이다. [원인 순위 독립 검토](../../.hermes/evidence/liveprobe-comparison-2026-11-23/dev-kubernetes-cause-rank-adjudication-through-f411.json) · [검증 근거 대조](../../.hermes/evidence/liveprobe-comparison-2026-11-23/campaign-355-source-reconciliation-20261125/verification.json)
- **운영 권한과 버전.** 단독 모델의 조치는 독립 시험기가 해당 시험 자원에 한해 실행했다. LiveProbe는 배포된 권한과 허용 조치에 따라 동작했다. 신규 Kubernetes의 차이에는 탐지 범위와 권한 차이도 포함된다. 공식 원장은 당시 실행 결과를 유지한다. 뒤에 고친 감시 경로의 로그 재생과 회귀 통과는 그 원장에 추가하지 않았다. [시험 경계와 후속 검증 기록](../plans/liveprobe-comparison-2026-11-23.md) · [Admission 장애 LiveProbe 기록](../../.hermes/evidence/liveprobe-comparison-2026-11-23/live-repair-v37/KI-02/liveprobe.json) · [같은 장애의 단독 시험 기록](../../.hermes/evidence/liveprobe-comparison-2026-11-23/live-repair-v37/KI-02/single_astra.json)

### 관제 홈 시간과 메모리

기준 커밋 `47e2bbd`와 개선본을 비교한 기존 계측에서 **8개 조건 모두 시간·메모리 10% 개선 기준을 충족**했다. 조건별 중앙값 기준으로 홈 표시 시간은 26.3~46.0%, 브라우저·API·DB 프로세스 트리의 합산 최대 RSS는 12.8~21.3% 줄었다. 활성 사건 12개·200개, 관측원 응답 재생 지연 10ms·200ms, 화면 폭 375px·1,440px 조합이다. 96회 계측에는 기준본, 직전 작업본, 개선본이 각각 32회 포함된다. 서비스 지도와 사건 표시, 기존 RAG 검색 결과는 각 비교에서 일치했다. [8개 조건의 집계](../../.hermes/evidence/liveprobe-comparison-2026-11-23/atlas-fonts-20261125/final-browser-summary.json) · [96회 원자료](../../.hermes/evidence/liveprobe-comparison-2026-11-23/atlas-fonts-20261125/final-browser-runs.jsonl)

Application 분석이 실제로 동시에 실행된 별도 4회에서 홈 표시 시간 중앙값은 3,058.85ms에서 1,729.90ms로 43.45% 줄었다. 분석 프로세스까지 포함한 합산 최대 RSS 중앙값은 1,124.34MiB에서 963.02MiB로 14.35% 줄었다. 이 결과는 활성 사건 200개, 재생 지연 200ms, 1,440px 화면 한 조건에 해당한다. 실제 클러스터 복구 조치 중의 성능과 다른 동시 분석 부하는 관측 범위 밖이다. [동시 분석 계측 요약](../../.hermes/evidence/liveprobe-comparison-2026-11-23/atlas-active-analysis-20261125/summary.json) · [계측 원자료](../../.hermes/evidence/liveprobe-comparison-2026-11-23/atlas-active-analysis-20261125/runs.jsonl)

## 재집계 근거

원장의 `case`와 `arm`을 한 행의 기준으로 사용했다. 커널 OOM 정의의 7개 ID를 제거한 뒤 각 사례에 다섯 방식이 하나씩 존재하는지 확인했다. 복구 건수는 원장의 상태별로 집계했고, 장애가 지속되면서 사건이 없었던 기록은 판정 코드가 지정한 미감지 사유로 식별했다. 대응 시간과 비용·토큰은 지표별로 값이 있는 동일 사례의 교집합을 사용했다. 비용 절감률은 `1 − (LiveProbe 합계 ÷ 단독 모델 합계)`로 구했다. 원장 SHA-256은 `cd530ad195fff74a68d21f502cb2038ed68b03dccf1c597b4c9f7c37291c129c`다. 보고서 작성을 위해 새 장애를 실행하거나 기존 원장·로그·DB를 수정하지 않았다. [공식 실행 원장](../../.hermes/evidence/liveprobe-comparison-2026-11-23/live-repair-status-dev-native-campaign-355.csv) · [비교 집계 코드](../../evaluation/comparison_summary.py) · [원본 영수증 대조](../../.hermes/evidence/liveprobe-comparison-2026-11-23/campaign-355-source-reconciliation-20261125/verification.json)
