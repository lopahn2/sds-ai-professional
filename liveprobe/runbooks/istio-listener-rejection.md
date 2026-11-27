---
title: Istio Envoy 리스너 설정 거부
source: internal-runbook
applies_to: [TargetProxyListenerUpdateRejected, LDS, xDS, NACK]
risk: high
---

# Istio Envoy 리스너 설정 거부

`envoy_listener_manager_lds_update_rejected` 증가는 관측한 프록시가 새 리스너 설정을 거부했음을 뜻한다. 기존 리스너가 요청을 계속 처리할 수 있다. 집계 지표 `pilot_total_xds_rejects{type="lds"}`는 제어면의 거부 건수를 교차 확인하는 데 쓰며, 개별 프록시와 경로는 각 관측 자료로 연결한다.

1. 경보에 기록된 Pod UID와 현재 프록시 이미지 버전을 확인한다. 설정 배포 이력과 프록시의 LDS 거부 시각을 대조한다.
2. 프록시의 거부 기록에서 리스너 이름, 설정 버전, 거부 사유를 찾는다. 최근 Gateway·VirtualService·EnvoyFilter 변경과 비교한다. 버전 비호환은 거부된 설정과 버전 근거로 판단한다.
3. 변경 대상 리스너의 호스트, 포트, 경로에 독립 요청을 보내고 기존 경로 결과를 별도로 기록한다. 영향받은 요청의 증거가 없으면 미관측으로 표시한다.
4. 복구 확인에는 해당 프록시의 최신 LDS 수락, 이전에 실패했던 경로의 정상 응답, 기존 경로 유지가 필요하다. 조치 권한이 필요한 경우 조사 근거와 검증 조건을 운영자에게 전달한다.

[Istio 지표 목록](https://istio.io/latest/docs/reference/commands/istioctl/) · [Envoy LDS](https://www.envoyproxy.io/docs/envoy/latest/configuration/listeners/lds)
