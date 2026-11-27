# 🔎 LiveProbe Kubernetes 장애 유형 확장 설계

2026-11-22 · Rancher Desktop / K3s · 운영 관측 시각은 한국 시간

---

**우선 탐지할 것은 자원 지표 공급 중단과 그로 인한 자동 확장 실패다.** 11월 22일 실제 클러스터에서 지표 API의 연결 대상이 사라졌고, 같은 시간 `istiod`의 HPA가 CPU 지표를 읽지 못했다. 지표 API의 복구 시각은 20:33:55, HPA가 유효한 지표를 다시 얻은 시각은 20:34:01이었다. 운영자에게는 한 사건에서 공급원과 영향받은 기능을 함께 보여줄 수 있다. 업무 요청 실패나 버전 변경이 원인이었다는 증거는 이 관측에 없다.

## 조사 범위와 완료 기준

기존 자동 감지 9종과 경고 이력을 대조하고, **버전·구성요소 호환성 11종과 워크로드·노드 상태 7종**을 설계했다. 한 장애가 여러 행의 증거를 만들 수 있다. 가령 저장소 드라이버 변경으로 인한 Pod 마운트 실패는 원인과 결과가 연결된 사건 한 건이다. 합의한 완료 기준은 유형별 판정 조건·오탐 경계를 정의하고 대표 사례를 재생해 결과를 보고하는 것이다. 산출물은 이 설계와 오프라인 검증 스크립트다. [기존 자동 감지와 저장 이력](../operations/liveprobe-incident-types-2026-11-22.md) · [판정 사례 재생](../../evaluation/k8s_incident_design_replay.py)

## 1. 버전과 관련해 감시할 인프라 장애 11종

업그레이드 전에 호환표나 선언에서 발견한 항목은 **업그레이드 주의**로 표시한다. 실행 중에는 실패한 기능, 대상, 시각을 먼저 제시한다. 같은 대상과 변경 버전을 지목하는 오류나 변경 전후 증거가 있을 때 원인에 버전을 기재한다. 읽기 실패, 일부 노드만 조회된 결과, 버전을 알 수 없는 자체 빌드는 각각 **미관측**으로 남긴다.

| 유형 | 어떤 버전 경계가 있는가 | 사건을 열 근거와 오탐 경계 |
|---|---|---|
| 1. 배포에 필요한 API 미제공 | Ingress의 `networking.k8s.io/v1beta1`은 Kubernetes 1.22, CronJob의 `batch/v1beta1`은 1.25, HPA의 `autoscaling/v2beta2`는 1.26부터 제공 종료. 반대 방향으로 LiveProbe의 `ValidatingAdmissionPolicy` 안정판은 1.30부터 제공된다. [API 이전 지침](https://kubernetes.io/docs/reference/using-api/deprecation-guide/) · [승인 정책](https://kubernetes.io/docs/reference/access-authn-authz/validating-admission-policy/) | 대상 클러스터에서 제공하는 그룹·버전·종류(GVK)와 렌더링한 선언을 대조한다. 미제공이면 사전 주의, 같은 리소스에 실제 적용을 시도한 도구가 해당 GVK 미지원으로 실패하면 배포 장애다. 개별 객체 조회 404, 권한 403, discovery 타임아웃으로는 API 제거를 판정할 수 없다. |
| 2. Admission 웹훅 프로토콜 불일치 | API 서버와 웹훅이 공통으로 처리할 `AdmissionReview` 버전이 필요하다. LiveProbe 롤백 웹훅 선언은 `v1`을 받고 실패 정책이 `Fail`이다. [웹훅 협상 규칙](https://kubernetes.io/docs/reference/access-authn-authz/extensible-admission-controllers/) · [현재 선언](../../deploy/base/liveprobe/rollback-controller.yaml) | 대상 변경 요청이 실패하고, 해당 웹훅 호출이 협상 또는 응답 버전 오류를 명시해야 한다. 정상적인 정책 거부, 인증서 만료, 웹훅 Service 연결 대상 소실은 서로 다른 원인이다. 영향은 해당 웹훅의 규칙에 일치한 요청으로 한정한다. |
| 3. 제어면과 kubelet 버전 차이 | kubelet 1.25 이상은 접속할 API 서버보다 높을 수 없고 최대 세 부 버전까지 낮을 수 있다. HA API 서버 간 허용 차이는 한 부 버전이다. [버전 차이 정책](https://kubernetes.io/releases/version-skew-policy/) | 모든 접속 대상 API 서버와 노드별 kubelet을 비교한다. 허용 범위 이탈은 사전 주의다. 같은 노드의 등록·호출 실패와 해당 버전 조합을 지목하는 오류까지 있어야 버전 원인이 유력하다. `NotReady` 한 건이나 로컬 kubectl 버전 차이로 업무 장애를 판정할 수 없다. |
| 4. 컨테이너 런타임과 CRI 협상 실패 | Kubernetes 1.26 이상 kubelet에는 CRI v1이 필요하다. 1.24의 내장 dockershim 제거 뒤에도 K3s는 별도 어댑터로 Docker를 사용할 수 있다. [CRI 버전](https://kubernetes.io/docs/concepts/containers/cri/) · [K3s의 Docker 지원](https://docs.k3s.io/advanced#using-docker-as-the-container-runtime) | kubelet이 실제로 사용하는 런타임 소켓에서 CRI v1 서비스 미구현이 확인되고, 그 노드가 등록 또는 컨테이너 생성을 실패해야 한다. 소켓 경로 오류·권한 실패는 별도 원인이다. Docker 제품 버전만으로 지원 여부를 단정할 수 없다. |
| 5. Linux cgroup 변경으로 kubelet 기동 실패 | 1.35부터 kubelet의 `failCgroupV1` 기본값이 참이다. cgroup v1 Linux 노드는 기본 설정에서 기동을 거부한다. [Kubernetes cgroup 전환](https://kubernetes.io/blog/2026/10/06/kubernetes-cgroups-v2-shift/) | 호스트의 실제 cgroup 계층, kubelet 유효 설정, 같은 노드의 기동 거부 기록을 함께 본다. 드라이버 이름 `cgroupfs`만으로 cgroup v1을 추정하지 않는다. 로컬 노드는 cgroup v2에 `cgroupfs`를 사용한다. |
| 6. CNI 규격 불일치로 새 Pod 네트워크 구성 실패 | 런타임이 요청하는 CNI 설정 규격과 설치한 플러그인의 지원 규격이 맞아야 한다. 과거 containerd 1.6.0~1.6.3에는 조합에 따른 네트워크 설정 오류가 문서화됐다. [CNI 규격](https://www.cni.dev/docs/spec/) · [Kubernetes의 재현 조건](https://kubernetes.io/docs/tasks/administer-cluster/migrating-from-dockershim/troubleshooting-cni-plugin-related-errors/) | 2분간 같은 노드의 서로 다른 Pod 2개 이상에서 샌드박스 생성이 실패하고 CNI 오류가 지원하지 않는 규격을 명시하면 노드 단위 사건으로 묶는다. 한 Pod면 해당 Pod 영향만 기록한다. IP 주소 고갈과 이미 가동 중인 Pod 간 패킷 드롭은 별도 경로다. |
| 7. 기존 Endpoints 기반 관제에서 연결 대상 누락 | Kubernetes 1.33부터 기존 Endpoints API는 사용 중단 권고 대상이다. 대상이 1,000개를 넘는 대형 Service나 이중 스택에서는 기존 API의 표현 한계가 있다. [Endpoints 변경과 EndpointSlice](https://kubernetes.io/blog/2025/04/24/endpoints-deprecation/) | 같은 Service 소유관계·주소 계열·준비 조건으로 전체 EndpointSlice 주소를 중복 제거한다. 같은 시점의 기존 응답과 두 번 비교하고 잘림 표식 또는 재현되는 주소 누락을 확인한다. 관제 누락은 수집 진단에, 실제 요청 실패가 확인된 서비스는 영향 목록에 표시한다. 사용 중단 경고 자체는 정상 조회에서도 나온다. |
| 8. 자원 지표 공급 중단·일부 누락으로 HPA 판단 실패 | Metrics Server 0.8.x의 명시 지원 대상은 Kubernetes 1.31 이상이다. 지원 범위 안에서도 런타임 통계 오류, kubelet 수집 실패, 지표 API 연결 대상 소실이 각각 일어날 수 있다. [지원표](https://github.com/kubernetes-sigs/metrics-server#compatibility-matrix) · [지표 수집 경로](https://kubernetes.io/docs/tasks/debug/debug-cluster/resource-metrics-pipeline/) | `metrics.k8s.io` APIService가 `MissingEndpoints`이고 같은 시간 자원 지표를 쓰는 HPA가 `ScalingActive=False / FailedGetResourceMetric`이면 공급원 한 건에 HPA를 연결한다. API가 살아 있어도 안정적으로 실행 중인 같은 Pod들이 두 수집 주기 연속 누락되면 kubelet 통계를 대조한다. 신규 Pod, 잘못된 HPA 요청 설정, 외부 지표 어댑터 실패는 따로 판정한다. |
| 9. Istio 업그레이드 뒤 설정 전파·서비스 경로 실패 | 11월 22일 공식 표에서 Istio 1.30의 지원 범위는 Kubernetes 1.32~1.36이다. Istio 제어면은 데이터면보다 한 부 버전 앞설 수 있고, 데이터면이 앞서는 조합은 미지원이다. [Istio 지원표](https://istio.io/latest/docs/releases/supported-releases/) · [프록시 동기화 진단](https://istio.io/latest/docs/ops/diagnostic-tools/proxy-cmd/) | 같은 리비전·프록시에 보낸 설정이 지원하지 않는 필드로 거부되고 해당 경로의 요청도 실패하면 서비스 영향을 연결한다. 동기화 지연만 보이면 배포 진행 상태다. Kubernetes 1.37로 올리면서 Istio 1.30을 유지하는 계획은 사전 주의다. |
| 10. CoreDNS 버전 변경 뒤 이름 조회 실패 | CoreDNS 1.6.0은 `-cpu` 플래그와 Kubernetes 플러그인의 `resyncperiod` 옵션을 제거했다. 자체 빌드에서는 포함된 플러그인도 달라질 수 있다. [CoreDNS 릴리스 기록](https://coredns.io/2019/07/28/coredns-1.6.0-release/) | 새 리비전이 같은 옵션을 명시해 설정 읽기·기동에 실패하면 DNS 배포 장애다. 기존 Service 이름의 조회가 서로 다른 업무 Pod에서 실패할 때 DNS 서비스 영향도 연결한다. 이전 복제본이 계속 정상 응답하거나, 올바르게 존재하지 않는 이름 하나만 조회한 경우를 구분한다. |
| 11. 내장 볼륨 드라이버 제거·CSI 이전 누락 | AWS EBS 내장 드라이버는 1.27부터 외부 CSI 드라이버로 작업이 넘어간다. Ceph RBD는 1.31에 내장 드라이버와 해당 CSI 이전 지원이 함께 제거됐다. 스토리지마다 이전 경로가 다르다. [Kubernetes 볼륨별 경계](https://kubernetes.io/docs/reference/storage/volume-types/) | 영향을 받는 PV 유형·대상 노드·드라이버를 대조한다. 해당 볼륨의 연결/마운트가 실패하고 제거된 플러그인 또는 필요한 드라이버 미등록이 확인돼야 버전 관련 사건이다. 로컬은 `rancher.io/local-path`를 사용하므로 CSIDriver 0개는 그 자체로 장애가 아니다. |

## 2. 워크로드와 노드에서 새로 구별할 7종

아래 시간과 횟수는 LiveProbe에 제안하는 초기값이다. 오래 걸리는 정상 배포와 실제 장애 기록을 더 재생한 뒤 서비스별 조정이 가능하다. 기존 감시기는 현재 Pod의 이미지·재시작 대기 사유 등을 검사한다. 경고 화면에서 읽을 수 있는 사유가 모두 자동 사건이 되는 것은 아니다. [Pod·이벤트 감시기](../../liveprobe/app/watcher.py) · [기존 Service 판정](../../liveprobe/app/service_health.py)

| 운영자가 볼 장애 | 사건을 열 조건 | 제외 또는 영향 구분 |
|---|---|---|
| 노드 배치 실패 | 같은 현재 Pod UID에 노드가 배정되지 않고 `PodScheduled=False / Unschedulable`이 2분 이상 연속 관측. 스케줄러가 제시한 자원·taint·affinity 등 확인된 제약을 요약 | scheduling gate 대기, 짧은 Pending, 교체된 UID, 삭제 중인 Pod, 관측이 끊긴 구간은 제외 |
| 필수 설정 참조 오류 | 일반·초기화 컨테이너에서 `CreateContainerConfigError`가 같은 UID·컨테이너에 60초 이상 두 번 관측되고, 필수 참조나 키 누락이 대상 오류에 명시됨 | 선택 참조와 권한·연결 실패를 구분. Secret 값 없이 참조 종류와 누락 여부만 표시 |
| 영속 볼륨 연결·마운트 실패 | 노드에 배정된 현재 Pod의 PVC 참조와 `FailedAttachVolume` 또는 `FailedMount`가 일치하고, 2분간 새 실패가 반복되며 그 볼륨을 계속 기다림 | 성공한 과거 마운트 재시도, ConfigMap·Secret 투영 오류 제외. 드라이버를 확인한 경우에만 CSI 원인을 붙임 |
| 복제본 생성 거부 | 현재 배포 세대의 ReplicaSet에서 `ReplicaFailure=True / FailedCreate`, 목표보다 적은 복제본이 1분 이상 두 차례 관측. 확인된 quota 또는 admission 거부 사유를 함께 표시 | Pod가 한 개도 만들어지기 전에도 열 수 있음. 이전 ReplicaSet 경고 제외 |
| 롤아웃 진행 기한 초과 | Deployment가 현재 세대를 관측했고 `Progressing=False / ProgressDeadlineExceeded`. 목표 복제본이 1개 이상이고 일시정지 상태가 아니며 새 배포가 미완료 | 이전 복제본이 계속 요청을 처리하면 ‘새 버전 배포 중단’으로 표시. 업무 중단은 별도로 확인 |
| 반복 상태 확인 실패로 가용성 저하 | 같은 Pod·컨테이너·probe에서 2분 안에 새 실패 3회 이상. readiness는 실제 준비 상태 또는 준비 연결 대상 감소, liveness/startup은 해당 컨테이너 재시작과 대조 | 누적 이벤트, 이미 회복한 단발 실패, 서로 다른 probe의 합산 제외 |
| 노드 가용성 상실·자원 압박 | 같은 노드의 `Ready=False/Unknown`이 2분 이상 두 차례 관측되거나, Disk/Memory/PIDPressure와 새 퇴출·준비 복제본 감소가 함께 관측됨 | 노드 조회 실패는 미관측. 같은 노드의 여러 네임스페이스는 원인 한 건에 연결하고 각 서비스 영향은 개별 확인 |

## 3. 실제 환경과 대조한 결과

| 확인 방법 | 결과 | 해석 |
|---|---|---|
| API 서버·노드·클라이언트 버전, 런타임 상태 조회 | 단일 arm64 노드. API 서버·kubelet은 K3s `v1.36.2+k3s1`, 로컬 kubectl은 `v1.36.2`. Docker `29.5.2`, 런타임 CRI v1, Linux cgroup v2, kubelet `failCgroupV1=true` | 조사한 노드에는 kubelet 부 버전 차이, CRI v1 미지원, cgroup v1 기동 차단 조건이 없었다. HA 사례는 오프라인으로 평가했다. |
| Rancher Desktop 기본 오버레이 렌더링과 동일 클러스터 API discovery 대조 | 선언 97개, 고유 GVK 17개 중 17개 제공. `ValidatingAdmissionPolicy`와 웹훅 구성이 포함됨 | 이 오버레이의 API 제공 여부를 검증했다. 타사 차트, 실제 쓰기 승인, 업그레이드 목표 클러스터의 제공 여부는 별도 검사 대상이다. [조사한 오버레이](../../deploy/overlays/rancher-desktop/kustomization.yaml) |
| CNI·DNS·스토리지·메시 조회 | Flannel VXLAN, CNI 설정 규격 `0.3.1`, CoreDNS `1.14.4`, local-path-provisioner `0.0.36`, CSIDriver 0개, Istiod `1.30.5` | CNI 설정 규격과 플러그인 바이너리 버전은 서로 다른 값이다. 플러그인 버전은 미관측. 조회 시 보존된 dev/prd 기록에서 `FailedCreatePodSandBox`는 0건이었다. |
| dev·prd의 기존 Endpoints와 EndpointSlice 조회 | Endpoints는 사용 중단 권고와 함께 응답. dev 14개, prd 9개. EndpointSlice도 각각 14개와 9개. 잘림 표식 0개 | 운영 수집기는 기존 Endpoints를 읽고, 서비스 계정에는 EndpointSlice 조회 권한이 있다. 객체 수 일치는 개별 주소까지 같음을 입증하지 않는다. [Kubernetes 수집 코드](../../liveprobe/app/k8s.py) · [읽기 권한](../../deploy/base/liveprobe/liveprobe.yaml) |
| 자원 지표·HPA를 시간차 재조회 | Metrics Server `0.8.1`. 20:32~20:33에 지표 API `Available=False / MissingEndpoints`, istiod HPA `ScalingActive=False / FailedGetResourceMetric`. 20:35:16 재조회에서 두 상태의 마지막 정상 전이 시각은 각각 **20:33:55**, **20:34:01** | HPA의 지표 기반 계산 재개가 확인됐다. 실제 복제본 증가나 업무 요청 영향은 미관측이다. 이번 지표 장애를 특정 바이너리 결함에 귀속할 근거는 없다. |
| 저장된 운영 복구 기록 재검토 | 11월 14일 Docker 29.5.2의 네트워크 통계 오류 당시 Pod 지표가 **7/43**만 수집됐다. 당시 기록에는 수정 전후 격리 시험과 복구 후 **43/43**, HPA 지표 정상 확인이 있다. | 같은 HPA 증상이라도 이번엔 API 연결 대상 소실, 과거엔 런타임의 일부 통계 누락이었다. 원인 분리에 사용할 회귀 자료다. [당시 진단과 검증](../operations/liveprobe-probe-metrics-repair-2026-11-14.md) |
| dev·prd Warning을 이벤트 객체와 누계로 나누어 조회 | 20:31:53에 Pod `Unhealthy`는 dev 35개 객체(누계 2,197회), prd 36개(1,133회). 두 환경의 마지막 Pod `NodeNotReady` 경고는 19:53:03, 조회 당시 노드는 Ready였고 세 압박 조건 모두 거짓. 당시 보존 범위에서 `FailedScheduling`, `FailedMount`, `FailedAttachVolume`은 0건 | 누계는 발생한 사건 수가 아니다. 이벤트 보존 기간 밖의 발생 여부는 알 수 없다. |

## 4. 대표 판정 검증

저장소 루트에서 `python3 evaluation/k8s_incident_design_replay.py`를 실행하면 Python 3.10 이상에서 각 입력의 기대값과 실제 판정을 대조할 수 있다. 표준 라이브러리만 사용한다. **43건 중 43건 통과.** 이 사례들은 대상·시각·오류 종류를 정규화한 입력이다. [재생 스크립트](../../evaluation/k8s_incident_design_replay.py)

| 검증한 기준 | 사례 | 의미 있는 경계 |
|---|---:|---|
| Pod 배치 실패 | 11 | 지속 119초/120초, 다른 UID, 중간 회복, 읽기 단절·긴 표본 공백, 노드 배정, scheduling gate, 삭제 중인 Pod |
| 미제공 GVK | 7 | 사전 발견과 같은 대상의 실제 적용 실패, 다른 객체, 일반 404, discovery 403·503 |
| 지표 공급원과 HPA 연결 | 7 | HPA 2개를 공급 장애 한 건에 연결, 중복 이벤트, 다른 지표, 오래된 시각, HPA 조회 실패 |
| 제어면·kubelet 버전 차이 | 10 | HA에서 가장 높은 서버와 낮은 서버를 모두 검사, 허용 3개/초과 4개 부 버전, 다른 노드·과거 오류, 불완전한 버전 목록 |
| kubelet의 cgroup v1 기동 거부 | 8 | 1.34와 1.35의 기본값, 설정 재정의, 실제 거부 사유, 다른 기동 오류, 로컬과 같은 v2 환경, 유효 설정·호스트 계층 미관측 |

별도로 현재 제품의 Pod·이벤트 감시기에 격리 입력을 넣었다. 기존 양성 대조군인 이미지 가져오기 실패는 신호 1건을 냈고, `CreateContainerConfigError`, `ContainerCreating`, 배치 실패 상태와 `FailedScheduling`·`FailedMount`·`Unhealthy`·ReplicaSet `FailedCreate` 단독 경고는 각각 신호 0건이었다. 기존 경보와 신규 설계를 구별하는 검증이다. 실클러스터 증거는 읽기 전용 관측이고, 운영 버전 교체·실장애 주입·자동 복구 종단 시험은 이번 검증에 포함하지 않았다. [현재 감시기](../../liveprobe/app/watcher.py)

## 5. LiveProbe 반영 순서

1. **지표 공급과 확장 영향을 먼저 연결한다.** 인프라 원인은 클러스터·구성요소 UID·발생 구간으로 한 건을 열고, HPA·서비스 영향은 별도 연결한다. 이벤트 UID와 누계의 증가분만 사용한다. 같은 구간의 재조회·감시기 재시작으로 새 사건이 생기지 않아야 한다. 화면 예시는 **‘자원 지표 복구 · istiod 자동 확장 지표 정상’**, 그 아래 **‘지표 API 20:33:55 복구 → HPA 20:34:01 계산 재개 · 업무 요청 영향 미관측’**이다. 인용한 이벤트 원문은 상세에서 연다.
2. **기존 읽기 권한으로 워크로드 판정을 넓힌다.** 전체 EndpointSlice를 읽고 기존 Endpoints와 병행 대조한 뒤 조회 기준을 전환한다. 배치·설정·복제본·롤아웃에는 현재 UID와 소유자를 확인한다. Pod가 아직 만들어지지 않은 경우에는 상위 ReplicaSet의 생성 거부를 독립 근거로 쓴다. 새 신호는 통합 목록의 Kubernetes 분류와 연결한다. [현재 사건 분류](../../liveprobe/app/router.py) · [통합 목록 분류](../../liveprobe/app/console_history.py)
3. **업그레이드 전후를 같은 대상 기준으로 비교한다.** 목표 선언과 API, 노드별 버전, 드라이버와 애드온 리비전을 사전 점검한다. 업그레이드 이후 같은 대상에서 실패한 기능, 마지막 정상 버전과 첫 실패 시각을 기록한다. 호환성 주의나 미관측만으로 자동 롤백을 실행할 근거는 없다. 공급원 정상과 영향 기능 정상은 각각 두 차례 신선한 관측으로 확인하고 시각을 나누어 표시한다.

제품 서비스 계정은 현재 Pod·이벤트·Service·노드·Deployment·ReplicaSet과 EndpointSlice 등을 읽는다. 이 조사에 사용한 운영자 조회 범위와 차이가 있다. HPA·APIService·스토리지 메타데이터는 기능별 최소 읽기 권한을 설계하고, 호스트·kubelet 기동 오류는 이미 승인된 수집원이 제공하는 요약으로 확인한다. 데이터가 없는 환경에는 해당 원인을 **미관측**으로 표시한다. 이번 작업에서 운영 권한·배포·DB·사건 이력은 변경하지 않았다. [현재 RBAC](../../deploy/base/liveprobe/liveprobe.yaml)
