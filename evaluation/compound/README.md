# Compound incident benchmark (C1–C3)

LiveProbe와 단일 모델을 비교하는 복합 장애 세 개다. 설계, 지표, 결과는
`docs/plans/compound-incident-benchmark-2026-11-26.md`에 있다.

| 파일 | 역할 |
| --- | --- |
| `fixture/` | `shopfront` Spring Boot JAR(stock/order 두 역할)과 DB 초기화 SQL |
| `infra.py` | 로컬 재현 구성 요소 |
| `scenarios.py` | C1·C2·C3 시나리오 정의 |
| `bench.py` | 재현과 공개 로그 묶음 생성, `--remedy`로 조치를 적용하고 soak로 판정 |
| `baseline.py` | Terra 6.1 xhigh 단일 프롬프트 실행 |
| `grade.py` | 1차 규칙 채점(보조 수단) |
| `answer_keys.json` | 정답표 |
| `k8s.py` | 정식 비교용 dev 배포 키트와 주입 절차 |
| `test_compound.py` | 도커 없이 도는 검사 |

`infra.py`와 `scenarios.py`의 세부는 다음과 같다.
- `infra.py`: docker 컨테이너와 호스트 측 제어 루프 에뮬레이션
  - kubelet liveness
  - Service endpoint: `svcproxy.py`, nginx
  - 운영 Job
- `scenarios.py`
  - 설정
  - 장애 타임라인
  - 조치 기본 동작
  - 지속 복구 판정

정답표(`answer_keys.json`)에는 사용 범위 제한이 있다. 묶음, 프롬프트, RAG, 배포물에 넣지 않는다.

```bash
# 1) JAR 빌드: 저장소 밖에서 오프라인으로
cp -R evaluation/compound/fixture/{pom.xml,src} "$BUILD" && (cd "$BUILD" && mvn -o -q -DskipTests package)
# 2) 장애 재현과 공개 묶음
python3 -B -m evaluation.compound.bench --scenario C1 --jar "$BUILD/target/shopfront-1.4.2.jar" --out "$OUT/c1"
# 3) 단일 모델 기준선
python3 -B -m evaluation.compound.baseline --bundle "$OUT/c1/bundle.txt" --out "$OUT/c1/terra.json"
# 4) 그 답의 조치를 적용하고 soak로 판정
python3 -B -m evaluation.compound.bench --scenario C1 --jar ... --out "$OUT/c1-soak" --remedy probe_liveness_group,order_single_attempt
```

`bench.py`는 끝나면 자신의 컨테이너(`lpcb-<시나리오>-*`)를 지운다. 같은 시나리오 두 개를 동시에 돌리지 않는다.
