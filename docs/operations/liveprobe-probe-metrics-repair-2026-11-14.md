🛠️
# 상태 확인·자원 지표 복구

2026-11-14 · Codex · LiveProbe 운영 복구

---

> ✅ **운영 반영 완료** — 프록시의 간헐적 503과 Docker 재시작 후 Pod 지표 누락을 수정했다. 실행기 교체 시 컨테이너 102개의 ID·PID·시작 시각과 기존 PVC 11개의 UID·볼륨 연결을 유지했다. 이후 Grafana만 DB를 보존해 새 PVC로 옮기고 교체했다.

## 합의 범위와 수정

사용자의 “문제를 해결해야지 그럼” 요청과 기존 실제 운영 적용 승인을 기준으로 상태 확인 실패와 자원 지표 누락을 해결했다. DB·PVC·과거 사건·시험 자료를 삭제하지 않고, dev/prd 애플리케이션 프로세스를 재시작하지 않는 조건이다. Jira는 사용하지 않았다. `/deep-interview-and-go`의 로컬 작업 기록 및 Claude 검토는 Hermes 목표 작업 `t_58da272f`로 진행했다.

| 문제 | 확인한 원인 | 적용 |
|---|---|---|
| prd delivery·business-api·delivery-member·trade-member의 간헐적 상태 확인 503 | 앱의 5초 keep-alive 종료 경계에서 Envoy가 연결을 재사용해 `UC connection_termination`을 반환 | 각 앱만 선택하는 Istio Sidecar의 inbound HTTP idle timeout을 2초로 설정. xDS 반영으로 앱 프로세스 유지. outbound 클러스터와 재시도 정책 유지 |
| Grafana 상태 확인의 간헐적 1초 제한 초과 | `/api/health`가 1초를 넘으면 준비 실패로 기록. DB는 emptyDir에 있어 단순 교체하면 자료 손실 위험 | DB 쓰기를 잠시 정지하고 전체 파일을 PVC로 복사·해시 비교 및 SQLite 무결성 검사. PVC를 연결한 뒤 readiness 5초·Recreate 적용. 새 PV는 Retain |
| Pod 지표가 7/43만 표시되고 istiod HPA가 CPU를 읽지 못함 | Docker 29.5.2의 live-restore가 none 네트워크에 이름 없는 인터페이스를 복원. 네트워크 통계 오류가 CPU·메모리 응답 전체를 실패시킴 | 설치 버전과 동일한 원본 커밋에서 복원 경로 한 곳만 보정. 정상 생성 경로와 같은 `srcName != ""` 조건을 적용. endpoint·route·resolver·CNI는 보존 |

프록시 소스는 `deploy/optional/istio/delivery-inbound-idle-timeout.yaml`, `domain-inbound-idle-timeout.json`에 저장했다. 실행기 패치는 `deploy/optional/runtime/docker-null-network-live-restore.patch`다. 원본 Docker 커밋은 `568f755ebeb1ac9c6a8febbda6cd371ea0a9630b`, Go는 1.25.10이다.

VM의 `/usr/bin/dockerd` 원본은 변경하지 않았다. `/etc/conf.d/docker`의 `DOCKERD_BINARY`만 `/usr/local/libexec/liveprobe/dockerd-29.5.2-null-restore`를 가리킨다. `live-restore: true`로 daemon만 교체했다. 보안 옵션·스토리지·플러그인은 기존 실행기와 같음을 격리 시험에서 확인했다.

## 검증과 완료 기준

- [x] 수정 전 원본 실행기의 격리 재시작에서 stats API 500을 재현했다.
- [x] 수정 후 같은 시험 컨테이너에서 stats API 200과 실제 CPU·메모리 사용량을 확인했다. PID·시작 시각·저장 값·외부에서 붙인 eth0 및 라우트를 유지했다.
- [x] 실제 Envoy 1.38.5와 delivery 앱을 격리 실행했다. 정상 요청 8건은 200, readiness/liveness 장애는 각각 503, 해제 후 각각 200이었다. 유휴 연결 종료 7건도 확인했다. 성공 응답으로 오류를 숨기지 않는다.
- [x] 운영 Pod 43/43이 신선한 CPU·메모리 지표를 제공한다. istiod HPA는 `ScalingActive=True / ValidMetricFound`로 실제 CPU 사용률을 계산한다.
- [x] 네 서비스에서 18~26분간 관찰한 상태 확인 2,678건이 모두 성공했다.
- [x] 실행 중이던 컨테이너 102/102의 ID·PID·시작 시각을 유지했고, PVC 11/11의 UID·볼륨 연결을 유지했다.
- [x] 두 관제 화면과 사건 조회 API가 200을 반환한다. stg 과거 이력 44건 및 Application 이력 9건을 계속 조회할 수 있다. 화면에서 dev/prd 각각 준비 Pod 9/9를 확인했다.

상세 실측값은 [최종 검증 JSON](../evidence/liveprobe-recovery-2026-11-14/final-repair.json), [최종 관찰](../evidence/liveprobe-recovery-2026-11-14/final-observation.json), [독립 검토](../evidence/liveprobe-recovery-2026-11-14/runtime-review.md)에 있다. 이전 기록을 덮거나 삭제하지 않았다.

> ℹ️ **경고는 이력으로 유지** — 19:47 daemon 교체 구간의 일시적 exec-probe 경고와 이전 실패 기록은 남아 있다. 현재 정상 여부는 준비 상태와 새 경고 증가 여부로 검증했다. 이전 기록을 지워 정상으로 보이게 하지 않았다.

Grafana 추가 수정은 `deploy/base/observability/grafana/grafana.yaml`에 반영했다. 백업 당시 대시보드 0개·데이터 소스 2개·사용자 1개의 DB를 바이트 단위로 동일하게 복사했고, 새 Grafana의 `/api/health`에서 `database=ok`와 Ready 상태를 확인했다. 기존 PVC 11개에 Grafana PVC 1개를 추가했으며 기존 볼륨은 삭제하지 않았다. [이관 증거](../evidence/liveprobe-recovery-2026-11-14/grafana-migration.json)에 해시·무결성 결과·백업 위치를 기록했다. DB 백업 자체는 자격 정보를 포함할 수 있어 저장소에 넣지 않았다.

## 재적용·원복

프록시 설정 재적용:

```sh
kubectl --context rancher-desktop apply -f deploy/optional/istio/delivery-inbound-idle-timeout.yaml
kubectl --context rancher-desktop apply -f deploy/optional/istio/domain-inbound-idle-timeout.json
```

프록시 원복은 위 두 파일에 있는 Sidecar 리소스만 `kubectl delete -f`로 제거한다. 앱을 재시작하지 않는다. 원래 연결 경쟁이 재발할 수 있으므로 관찰 없이 원복하지 않는다.

실행기 재빌드: 원본 커밋을 별도 디렉터리에 받아 패치를 적용한 뒤 다음 명령을 실행한다. 기존 운영 data-root를 빌드·시험 디렉터리로 사용하지 않는다.

```sh
patch -p1 -i /absolute/path/to/docker-null-network-live-restore.patch
GOTOOLCHAIN=go1.25.10 CGO_ENABLED=0 GOOS=linux GOARCH=arm64 GOMAXPROCS=2 \
  go build -p 2 -mod=vendor -tags 'netgo osusergo static_build nri_no_wasm seccomp' \
  -ldflags '-X github.com/moby/moby/v2/dockerversion.Version=29.5.2 -X github.com/moby/moby/v2/dockerversion.GitCommit=568f755e-liveprobe-null-restore' \
  -o /absolute/output/dockerd-fixed ./cmd/dockerd
```

검토된 `daemon/libnetwork/sandbox_linux.go` SHA256은 `80302953a687e998d38e0349034c4ef32ab4bbd29611d3597b986d9b5c3bcd95`, 현재 배포 바이너리 SHA256은 `79ade0836d745c58666644f49c7f171e240de236ee1982706e170fd7a9f70f29`다. 빌드 경로가 달라지면 바이너리 해시는 달라질 수 있다. 재배포 전 격리 시험과 프로세스/PVC 식별자 스냅샷을 다시 확인한다.

Grafana는 앞으로 같은 PVC를 사용한다. 이전 emptyDir 구성으로 되돌리면 빈 DB로 시작하므로 그렇게 원복하지 않는다. 장애 시 현재 PVC를 유지한 채 보존한 백업을 별도 복구 볼륨에서 검증하고 교체한다. 전체 Rancher Desktop overlay 렌더와 Grafana Deployment/PVC의 서버 dry-run을 통과했다.

VM의 원본 서비스 설정은 `/var/lib/liveprobe-runtime-repair/docker-conf.before`에 보존했다. 실행기 원복이 필요하면 `live-restore`가 켜져 있는지 확인한 뒤 이 파일을 `/etc/conf.d/docker`로 복원하고 Docker daemon만 재시작한다. 원복하면 기존 지표 복원 결함도 돌아온다. Docker data-root·네트워크 DB·PVC는 삭제하거나 초기화하지 않는다.

이 실행기는 공식 원본에 최소 패치를 적용한 로컬 빌드다. Rancher Desktop 업데이트·VM 재생성 후에는 버전과 지표 수집을 확인하고, 공식 수정 버전이 제공되면 교체한다. CoreDNS·metrics-server의 앞선 5초 probe 조정은 K3s 관리 리소스이므로 K3s 패키지 갱신 후에도 별도 확인이 필요하다.

원본 근거: [Docker 해당 커밋](https://github.com/moby/moby/tree/568f755ebeb1ac9c6a8febbda6cd371ea0a9630b), [Istio Sidecar 설정](https://istio.io/latest/docs/reference/config/networking/sidecar/).
