# 🔧 Service 연결 대상 소실 감지와 대기 사건 종결

2026-11-22 · LiveProbe 로컬 관제 · dev / prd

---

배포나 재기동 중 준비된 Service 연결 대상이 잠깐 사라지는 경우를 줄이기 위해, **같은 Service에서 준비된 연결 대상 0개를 10분 연속 관측한 뒤** 사건 감지 신호를 발행한다. 완료 기준은 직접 감시와 Prometheus 모두 같은 시간 기준을 적용하고, 기존 소명 확인 대기 건의 종결 사유를 사건별로 기록하는 것이다.

## 감지 기준

- LiveProbe 직접 감시의 기본값과 최소값은 600초다. 준비된 연결 대상을 관측하거나 조회에 실패하거나 같은 이름의 Service가 다른 UID로 교체되면 누적 시간을 초기화한다. 소실 신호에는 실제 연속 관측 시간을 남긴다. [설정](../../liveprobe/app/config.py) · [Service 감시기](../../liveprobe/app/watcher.py)
- Prometheus의 `TargetServiceNoEndpoints`도 기본 그룹과 dev/prd 그룹에서 `for: 10m`을 적용한다. 중간에 대상이 준비되면 다음 소실부터 10분을 다시 잰다. [기본 경보](../../deploy/base/observability/prometheus/prometheus.yaml) · [dev/prd 생성기](../../scripts/render-p18-observability.py)

## 검증

- Python 감시기·대시보드 관측·상관관계·경보 수신 회귀는 45개 통과했고, 테스트 PostgreSQL을 사용하는 16개는 환경 조건으로 건너뛰었다. 599초와 600초 경계, 복구, 조회 단절, Service UID 교체를 확인했다. [감시기 시험](../../liveprobe/tests/test_watcher.py)
- Prometheus 3.15.0으로 9분 30초 무발화, 10분 발화, 중간에 정상화된 뒤 다시 10분 지속될 때 발화를 확인했다. 실제 dev/prd 생성 규칙도 같은 시나리오에 통과했다. 기존 Prometheus 수집 설정 시험의 4개 실패는 변경 전에도 재현됐다. [경보 재생 사례](../../deploy/base/observability/prometheus/rules_test.yml)

## 운영 반영

Prometheus는 기존 시계열 PVC를 사용해 재가동했다. 서비스 경유 규칙 API에서 두 그룹 모두 유지 시간 600초와 평가 상태 `ok`를 확인했다. LiveProbe API의 유휴 시점을 기다리는 동안 운영 분석이 이어졌고, 사용자는 분석 중단 가능성을 수락하고 즉시 교체를 승인했다.

클러스터 조치·배포에 관련된 실행과 임대가 모두 0건일 때 API 이미지를 교체했다. 22:46 KST에 배포 세대 98이 준비됐다. 실행 중인 컨테이너에서 감지 기준 600초, 4175에서 홈과 준비 API HTTP 200, dev·prd 연결 대상 감시기 `running`을 확인했다. 교체 당시 진행 중인 네트워크 분석 사건 #1630은 재개 대기 뒤 처리 실패로 기록됐다. 기존 기록이 해당 사건에 남아 있다. [사건 #1630](http://127.0.0.1:4175/incidents/1630)

## 종결 기록

22:28~22:29 KST에 4175의 운영자 소명 확인 API로 최초 대기 38건을 종결했다. 교체 대기 중 새로 소명 확인 대기에 들어온 dev 사건 2건을 22:35와 22:39에 추가 처리해 Kubernetes **40건(dev 26, prd 14)**을 종결했다. 각 사건에 저장된 복구 관측 시각과 대상, 원인 또는 장애 지속 시간을 확정하기 어려운 경우 그 한계를 종결 사유에 기재했다. 처리 직후 Kubernetes와 Application 모두 소명 확인 대기 **0건**이었다. 기존 증거·조치·검증 이력과 새 운영자 판단은 해당 사건에서 함께 조회할 수 있다. [장애 관제](http://127.0.0.1:4175/incidents) · [종결한 prd/business-api 사건](http://127.0.0.1:4175/incidents/1612)
