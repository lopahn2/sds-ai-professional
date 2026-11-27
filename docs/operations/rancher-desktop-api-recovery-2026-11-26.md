# Rancher Desktop Kubernetes API 복구

## 장애와 복구 근거

2026-11-26 00:32 KST에 호스트 `127.0.0.1:6443`과 게스트의 6443 포트에 리스너가 없었다. k3s 로그는 `attempt to write a readonly database`로 시작이 실패했음을 보여준다. 게스트 커널은 `/dev/vda1` 쓰기 I/O 오류에 이어 JBD2 저널 중단과 ext4 읽기 전용 전환을 기록했다. `/mnt/data`와 `/tmp`에는 `emergency_ro`가 표시됐다. 게스트 파일시스템은 68% 사용 중이었고 같은 시각 호스트 디스크 여유는 약 2.6GiB였다. 임시 시험 VM 이미지 반입 중 호스트 저장 공간이 부족해진 시점에 최초 오류가 기록됐다. [호스트 k3s 로그](</Users/hwany/Library/Logs/rancher-desktop/k3s.log>) · [최초 장애 기록](../plans/liveprobe-comparison-2026-11-23.md#남은-oom-재현과-공유-환경-상태) · [현재 dev 사전 확인](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/preflight.json)

복구 후보 `/private/tmp/lp-rancher-boot-candidate-in7t82rn/data.disk`는 별도 APFS 복사본이다. ext4 읽기 전용 검사, Kine SQLite 무결성 및 보존 WAL 반영 검증을 마쳤다. 이전 읽기 전용 대조에서 PostgreSQL 볼륨 5개의 일반 파일 10,204개가 보존 백업과 일치했다. 같은 SQLite 파일과 서버 설정을 로컬 네임스페이스에서 기동한 과거 확인에서 `/readyz`가 `ok`였고, 네임스페이스별 보존 자원 건수가 일치했다. [복구 후보 검사](../../.hermes/evidence/liveprobe-comparison-2026-11-23/rancher-offline-clone-20261125/stage-recovery-disk.log) · [데이터 파일 대조](../../.hermes/evidence/liveprobe-comparison-2026-11-23/rancher-offline-clone-20261125/postgres-file-comparison.json)

## 공유 VM 전환 조건

- Rancher Docker 런타임에도 같은 디스크가 붙어 있다. 정지 전에 확인한 실행 항목은 Kubernetes 관리 컨테이너 89개와 독립 컨테이너 2개다. 별도 `backend` 프로젝트의 `coru-postgres`는 `unless-stopped` 정책과 Docker 볼륨을 사용하며 현재 `unhealthy`다. LiveProbe 시험 컨테이너 한 개는 자동 재시작 정책이 없다.
- 복구 후보를 읽기 전용으로 마운트해 `coru-postgres` 볼륨을 대조했다. 일반 파일 1,349개의 상대 경로와 SHA-256이 모두 현재 볼륨과 일치했다.
- 공유 VM이 멈춘 것을 확인하고 원본 디스크를 같은 파일시스템에 보존한다. 복구 후보의 APFS 복사본을 현재 디스크 경로에 배치해 동일한 Rancher 인스턴스를 시작한다. 전환에 쓰는 복사본과 원본을 각각 식별해 잘못된 경로 교체를 차단한다.
- 호스트 디스크 여유, 게스트 ext4 쓰기 상태, Kubernetes `/readyz`, 기존 노드 UID, 보존 자원, dev 서비스 및 4175의 실제 관제 API를 확인한다. 실패 시 시작을 중지하고 원본 경로를 되돌릴 수 있도록 두 이미지를 보존한다.

## 적용 및 검증

2026-11-26 00:48 KST에 원본을 유지한 상태에서 복구 후보를 APFS 복제하고 공유 VM을 중지했다. 정지 과정에서 원본 디스크 수정 시각이 바뀌어 교체 조건이 충족되지 않았다. 실행기는 복구 후보를 보존하고 원래 디스크 경로 그대로 Rancher를 시작했다. 재부팅 중 ext4 저널 내부 복구가 완료돼 `/mnt/data`가 쓰기 가능 상태로 마운트됐으며, 00:49에 Kubernetes `/readyz`가 `ok`를 반환했다. 커널은 파일시스템 검사 필요 플래그도 기록했다. 파일시스템 전체 검사 및 복구본 전환은 실행하지 않았다. [전환 기록](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/rancher-api-switch.log)

00:51에 기존 단일 노드는 `Ready`였다. dev에서 복제본이 설정된 Deployment 17개가 모두 준비됐고, Kubernetes API 프록시로 보낸 주문·재고 준비 요청은 둘 다 `UP`을 반환했다. PVC 14개는 모두 `Bound`였다. 4175 화면과 `/api/status`, 사건 조회는 HTTP 200이었고 Application 모델 작업자도 준비됐다. 공유 VM의 `coru-postgres`는 자동 재시작 후 `healthy`였다. 재시작 정책이 없던 별도 LiveProbe 시험 컨테이너는 재기동 후 조회되지 않았고, Rancher 호스트 진단 로그는 새 실행부터 다시 기록됐다. [복구 검증 원본](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/rancher-api-recovered.json)

비교 시작 기준을 별도로 조회했다. CPU 2,920m/4,000m와 메모리 8,420MiB/11,945MiB에서 70% 상한을 넘어 장애 주입은 시작하지 않았다. [재시작 후 사전 검사](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/preflight-after-api-recovery.json)

## 시험 실행 경로 확인

01:42에 기본 Codex 샌드박스에서 `kubectl`의 루프백 소켓 연결이 `operation not permitted`로 거부됐다. 같은 `rancher-desktop` 컨텍스트를 승인된 호스트 실행에서 호출했을 때 `/readyz`는 `ok`였다. 장애 시험의 사전 검사가 로컬 실행 권한 차단, API 연결 거부, 인증·인가 실패, 요청 시간 초과를 구분하도록 수정했다. 클러스터 설정을 변경할 필요 없이 호스트에서 기존 자격 증명으로 검사를 통과했고, 노드 CPU 2,109m/4,000m와 메모리 7,718MiB/11,945MiB를 읽었다. Kubernetes API 경유로 dev의 주문·재고 업무 요청도 정상 응답했다. [승인된 호스트 검사](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/preflight-host-20261126-014246.json) · [기본 샌드박스 검사](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/preflight-sandbox-20261126-014241.json) · [시험 코드](../../evaluation/matched_recovery_campaign.py)

03:28에 승인된 호스트에서 두 경로를 다시 확인했다. `kubectl get --raw=/readyz`는 `ok`를 반환했고, `kubectl get deployments -n dev`에서 복제본이 설정된 Deployment 18개가 모두 준비 상태였다. 정식 시험의 `require_ready_api()`도 동일한 자격으로 통과했고, 로컬 관제 화면과 `/api/status`는 HTTP 200을 반환했다. [시험 코드](../../evaluation/matched_recovery_campaign.py)

03:31에 호출자 재시험을 준비하면서 CPU 3,014m/4,000m가 70% 상한을 넘었다. API 연결이 확인된 뒤 자원 기준 때문에 주입이 중지됐다는 점과 실제 측정값을 사전 검사에 표시했다. 사전 검사와 격리 실행 관련 회귀 테스트 16개가 통과했다. [실제 사전 검사](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/preflight-host-20261126-0331.json) · [회귀 테스트](../../evaluation/tests/test_matched_recovery_campaign.py)

08:04에 `rancher-desktop`에서 `/readyz` 응답 `ok`, 기존 단일 노드 `Ready`, dev 예약 시험 Deployment `1/1`, dev 시험 PVC 2개 `Bound`를 다시 확인했다. 정식 시험의 동일한 검사도 API에 연결됐으며, 노드 CPU 3,265m/4,000m가 70% 상한을 넘어 새 시험 시작을 제한했다. 이미 접수된 Application 시험은 계속 진행됐다. [08:04 사전 검사 원본](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/preflight-host-20261126-0806.json)

08:15에 dev 시험용 API와 모델 작업자 전환을 원복한 뒤 Kubernetes `/readyz`의 `ok`, Application `/readyz`의 `ready=true`, 기존 카탈로그 지문과 dev 시험 Deployment `1/1`을 확인했다. 08:18의 관제 `/api/status`는 HTTP 200이었다. 같은 시각 시험 사전 검사는 API에 연결했고 CPU 2,616m/4,000m, 메모리 8,415MiB/11,945MiB를 읽었다. 메모리가 70% 기준을 넘어 새 장애 주입은 시작하지 않았다. [원복 검증](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/app-native-restore-0811.json) · [08:18 사전 검사](../../.hermes/evidence/liveprobe-matched-dev-2026-11-26/preflight-host-20261126-post-restore.json)
