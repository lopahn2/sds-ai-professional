# 🛰️ 로컬 관제 서버 재연결

2026-11-18 · Codex · 서버 복구 / 데이터 보존

---

## 요청과 범위

사용자가 서버 열기를 요청했다. 로컬 4175 연결을 복구하고 실제 준비 상태·사건 조회를 확인한다. 새 통합 브랜치의 클러스터 적용·DB 이전·기능 활성화는 포함하지 않는다.

## 원인과 승인

CoreDNS Pod `coredns-5f5694d56b-vbfj2`가 컨테이너 이름 충돌로 CreateContainerError 상태였다. 내부 DNS 연결 거부로 관제 API와 DB의 sidecar 시작이 지연됐다. 자동 승인 검토가 시스템 DNS 재생성을 차단한 뒤, 사용자가 **DNS Pod 한 개 복구 허용**을 명시 승인했다.

## 수행과 확인

- 기존 DNS Pod UID `943de0f5-994a-4c40-abfc-2a50a10a7c38`와 실행 직전 resourceVersion을 조건으로 해당 Pod만 삭제했다. Deployment 설정·DB·PVC·Secret·업무 Pod는 변경하지 않았다.
- 새 DNS Pod `coredns-5f5694d56b-knzm9`, UID `18a14990-7abf-4895-85f7-827d5b850d95`의 준비 완료를 확인했다.
- 기존 관제 API·Application API·두 DB는 이후 정상 기동했다. 해당 Pod들을 직접 삭제하거나 설정하지 않았다.
- `http://liveprobe.localhost/` 화면, `/api/readyz`, `/api/incidents?limit=1`에서 HTTP 200을 확인했다. `http://127.0.0.1:4175/`는 현재 배포된 화면으로 이동한다.
- 통합 브랜치 API(`/api/unified/catalog`)는 현재 배포본에서 404다. 새 소스 미리보기를 구 API와 혼용하지 않고 설치된 화면/API를 연결했다. 통합 기능을 운영에 적용한 것으로 간주하지 않는다.

로컬 연결 프로세스·DNS 삭제 전 영수증은 `/private/tmp/liveprobe-local-server-20261118/`에 있다. Secret 본문과 업무 DB 원본은 조회하지 않았다. 이 기록은 로컬 작업 기록이며 원격 푸시는 수행하지 않았다.
