# LiveProbe 동일 장애 출발점 재평가

2026년 11월 25일까지 보존된 원장과 실행 기록 재분석

## 결론

기존 dev에서 **동일한 장애 상태, 같은 정상 상태로 돌아갈 수 있는 조치 범위, 동일한 복구 확인 기준**이 확인된 시험은 이미지 가져오기 실패 **3건, 다섯 방식의 실행 15회**였다. 첫 비교에서 LiveProbe는 3건 중 2건, 단독 모델 네 종은 각각 3건을 조치로 복구했다. 다섯 방식이 모두 복구한 공통 2건에서 LiveProbe의 복구 시간 합은 가장 길었고, 모델 환산 비용은 Astra·Sol보다 낮았다. [사례 연결과 원본 지문](../../.hermes/evidence/liveprobe-comparison-2026-11-23/campaign-355-source-reconciliation-20261125/verification.json) · [이미지 시험 기록](../plans/liveprobe-comparison-2026-11-23.md#dev-주문재고-이미지-실패-비교)

이 세 사례에서 LiveProbe는 Kubernetes 신호로 사건을 만들었고 단독 모델은 같은 이미지 오류를 시험기로부터 전달받았다. 따라서 결과는 **동일하게 재현된 장애의 최초 관측부터 업무 복구까지 각 방식이 걸린 시간과 비용**을 보여준다. 첫 모델 호출에 들어간 입력과 조회 도구까지 동일함을 증명할 기록은 **0건**이다. 모델 판단력만의 우열이나 전체 장애 유형의 성과는 이 자료로 확정할 수 없다. [오류 관측·입력·원복 실행기](../../evaluation/k8s_dev_image_trial.py)

## 사례 분류와 채택 기준

OOM을 제외한 원장에는 68개 사례·340개 실행이 있다. 각 사례에서 다섯 방식의 시험판 지문, 최초 오류의 비교 지문과 저장된 실행 기록의 SHA-256을 대조했다. 68개 모두 지문은 일치했다. 지문은 장애 재현이 일치했음을 확인하는 데 사용했고, 입력 전달 경로·조치 범위·시간과 종료 판정의 일치는 실행 코드와 영수증으로 별도 판정했다. 아래 표는 재집계 대상의 선정 근거이며, 채택한 세 건만 성과 표에 사용했다. [공식 원장](../../.hermes/evidence/liveprobe-comparison-2026-11-23/live-repair-status-dev-native-campaign-355.csv) · [실행별 지문 목록](../../.hermes/evidence/liveprobe-comparison-2026-11-23/campaign-355-source-reconciliation-20261125/verification.json)

| 시험 종류 | 사례 수 | 채택 여부와 근거 |
| --- | ---: | --- |
| 기존 dev의 이미지 가져오기 실패 | 3 | **채택.** `F1-01`, `F1-07`, `F1-13`. 같은 이미지 오류를 재현했고 각 방식이 원래 이미지로 원복할 수 있었다. 복구 확인에 같은 업무 요청을 사용했다. |
| 기존 dev의 반복 종료·연결 풀·지연·짧은 연결 장애 | 13 | 제외. 다섯 방식의 서비스 정상화는 시험 설정의 만료로 관측됐다. 대응 방식이 실행한 복구의 시간과 비용을 직접 대조할 수 없다. |
| 기존 dev의 Service 선택자 불일치 | 4 | 제외. `F4-01`, `F4-03`, `F4-09`, `F4-11`. LiveProbe는 고위험 이관 정책을 탔고 단독 모델에는 선택자 원복 도구가 열려 있었다. |
| 신규 Kubernetes 인프라·버전 호환·워크로드 장애 | 18 | 제외. `KI-01`~`KI-11`, `KW-01`~`KW-07`. 별도 노드나 제어면을 사용한 시험을 포함한다. 배포 제품이 받는 신호와 단독 모델이 받는 최초 오류 또는 실행할 수 있는 조치의 동등성이 확인되지 않았다. |
| Application 설정 복원 | 21 | 제외. 단독 모델이 이용한 격리 시험기의 설정 원복 기능에 대응하는 LiveProbe 실행 경로가 없었다. |
| Application 소스 수정 | 9 | 제외. 같은 최초 오류 지문과 소스 기준선을 확인했으나, LiveProbe는 트레이스와 소스 판본을 묶어 접수하고 이미지 빌드·후보 확인까지 실행했다. 단독 모델은 최초 로그에서 도구 조회를 시작했고, 수정한 시험 프로세스의 복구를 확인했다. 입력과 측정 흐름이 다르다. |

자연 회복 13건은 `F1-04·06·12`, `F3-02·05·07·08·09·11·14`, `F4-02·08·12`다. Application의 소스 수정 9건은 `catalog-24`, `delivery-25·29`, `member-13·23`, `order-11·21`, `payment-12·22`다. 제외한 결과는 원장에 그대로 남아 있다. 제외는 결과의 성공 여부와 무관하게 실험 조건으로 정했다. [자연 회복 판정 코드](../../evaluation/comparison_summary.py) · [선택자 장애 원기록](../../.hermes/evidence/liveprobe-comparison-2026-11-23/live-repair-v36/F4-01/liveprobe.json) · [신규 유형 카탈로그](../../evaluation/catalogs/comparative-2026-11-23.json) · [Application 실행 대조](../../.hermes/evidence/liveprobe-comparison-2026-11-23/application-comparison-audit-20261125.json) · [Application 제품 실행기](../../evaluation/application_release_trial.py) · [단독 실행기](../../evaluation/live_repair.py)

## 채택한 이미지 실패 3건

세 사례는 기존 dev의 재고·주문 Deployment에서 새 Pod의 이미지 가져오기 실패를 재현했다. 각 사례에서 기존 정상 Pod와 Service는 계속 업무 요청에 응답했다. 복구는 새 롤아웃이 정상 이미지와 준비 상태를 회복하고, 시험기의 정리 전에 독립 확인 두 차례를 통과했을 때 인정했다. LiveProbe는 `rollout_undo`, 단독 모델은 시험 도구의 이미지 필드 변경을 사용했으며 같은 버전으로 돌아갔다. 사용자 요청이 계속 실패하는 서비스 장애의 복구 시간으로 일반화할 수 없다. [dev 기준선과 검증](../operations/liveprobe-dev-only-comparison-gate-2026-11-23.md) · [실행기](../../evaluation/k8s_dev_image_trial.py)

| 사례 | 기존 dev 대상 | LiveProbe 첫 비교 결과 | 단독 모델 4종 |
| --- | --- | --- | --- |
| `F1-01` | 재고 | 이관. 모델 조치 0건 | 모두 원복 |
| `F1-07` | 주문 | 자동 원복 | 모두 원복 |
| `F1-13` | 재고 | 자동 원복 | 모두 원복 |

`F1-01`은 원장이 분석 지침 보완 뒤의 성공 재시험을 채택했다. 이 보고서는 최초로 다섯 방식을 대조한 제품 실행의 이관을 사용했고, 다른 두 사례는 복구 전후 업무 검증을 충족한 원장 기록을 사용했다. 재시험에서 LiveProbe는 82.359초에 원복했지만 최초 비교 분자에는 합산하지 않았다. [F1-01 첫 제품 실행](../../.hermes/evidence/liveprobe-comparison-2026-11-23/live-repair-v36/F1-01/liveprobe.json) · [F1-01 보완 뒤 재시험](../../.hermes/evidence/liveprobe-comparison-2026-11-23/live-repair-v36/F1-01/retry/grounded-image-observation.json) · [F1-07 제품 실행](../../.hermes/evidence/liveprobe-comparison-2026-11-23/live-repair-v36/F1-07/liveprobe.json) · [F1-13 제품 실행](../../.hermes/evidence/liveprobe-comparison-2026-11-23/live-repair-v36/F1-13/liveprobe.json)

## 방식별 복구 시간과 비용

복구 시간은 시험기가 첫 이미지 오류를 관측한 때부터 원래 배포가 준비되고 업무 확인을 마친 때까지다. LiveProbe의 `wall_duration_ms − first_error_ms`와 단독 실행의 `first_error_to_verified_ms`를 대조했다. 시간·토큰·비용은 다섯 방식이 **모두 실제 복구한 `F1-07`, `F1-13` 두 건**에서 계산했다. `F1-01` LiveProbe 실행에는 확인된 복구 시각이 없어 이 표의 시간 집계에 넣지 않았다. 비용은 USD 모델 단가 환산액이며, 당시 구독 사용 요금과 구분된다. [시간 및 판정 집계 코드](../../evaluation/comparison_summary.py) · [공식 원장](../../.hermes/evidence/liveprobe-comparison-2026-11-23/live-repair-status-dev-native-campaign-355.csv)

| 방식 | 조치 후 원복 · 3건 | 공통 복구 시간 중앙값 · 2건 | 공통 복구 시간 합 · 2건 | 총 토큰 · 2건 | 환산 비용 · 2건 |
| --- | ---: | ---: | ---: | ---: | ---: |
| LiveProbe | 2/3 | 87.77초 | 175.55초 | 110,107 | $0.117910 |
| GPT-6.2 Astra | 3/3 | 48.77초 | 97.53초 | 113,630 | $0.830448 |
| GPT-6.2 Sol | 3/3 | 44.85초 | 89.70초 | 114,759 | $0.214150 |
| GPT-6.1 Terra | 3/3 | 35.87초 | 71.74초 | 89,055 | $0.030122 |
| GPT-6 Luna | 3/3 | 49.95초 | 99.90초 | 193,794 | $0.011470 |

이 범위에서 LiveProbe는 Astra와 Sol보다 적은 토큰으로 낮은 환산 비용을 기록했다. Terra는 LiveProbe보다 토큰·비용이 모두 적었고, Luna는 토큰이 더 많았지만 환산 비용은 낮았다. 숫자는 두 사례의 관측 합계다. 원래 보고서의 68개 사례 수치는 방식별로 달랐던 시험 조건에서 관측한 기록으로 보존하며, 여기서는 대조군 간 성능 결론에 사용하지 않았다. 새 장애 시험이나 노드 생성 없이 기존 실행 자료만 사용했다. [기존 보고서](liveprobe-final-evaluation-2026-11-25.md) · [공식 원장](../../.hermes/evidence/liveprobe-comparison-2026-11-23/live-repair-status-dev-native-campaign-355.csv)
