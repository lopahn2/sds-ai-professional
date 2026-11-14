#!/usr/bin/env python3
"""Generate a frozen 6-system x30 application-fault catalog. This file never injects faults."""
from collections import Counter
from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEMS = ('order', 'payment', 'member', 'catalog', 'delivery', 'notification')
BUSINESS = {
    'order': ('주문·재고', ['주문 금액 계산', '상품 코드 검증', '주문 응답 변환'], '주문 만료 정리', '재고 초과 판매', 'inventory'),
    'payment': ('결제', ['수수료 계산', '환불 견적 조회', '영수증 금액 서식'], '정산 배치', '중복 청구', 'member'),
    'member': ('회원', ['로그인 프로필 조회', '회원 이름 정규화', '프로필 날짜 서식'], '세션 정리', '세션 불일치', 'catalog'),
    'catalog': ('상품', ['검색어 해석', '상세 할인 정보', '가격 응답 서식'], '색인 재생성', '오래된 가격', 'inventory'),
    'delivery': ('배송', ['배송 단계 전환', '도착 예정일 검증', '예정 시간 응답 변환'], '배송 지연 감시', '배송 상태 역행', 'notification'),
    'notification': ('알림', ['발송 채널 정규화', '재발송 대상 조회', '목록 템플릿 서식'], '발송 배치', '발송 대기열 정체', 'member'),
}


def fault(name, **params):
    return {'fault': name, 'params': params}


def recipes():
    # Design groups describe scenario complexity. Production routing is never forced to these groups.
    return [
        ('SIMPLE', '단일 업무 고정 지연', [fault('latency', seconds=2)], 'runtime', 'latency'),
        ('SIMPLE', '모든 업무 요청 503', [fault('error_rate', ratio=1)], 'runtime', '5xx'),
        ('SIMPLE', '실행 중 프로세스 반복 종료', [fault('crash')], 'runtime', 'restart'),
        ('SIMPLE', '준비 확인만 실패', [fault('not_ready')], 'runtime', 'readiness'),
        ('SIMPLE', '생존 확인 실패와 재시작', [fault('liveness_fail')], 'runtime', 'liveness'),
        ('SIMPLE', '업무 요청 멈춤과 시간 초과', [fault('hang', seconds=40)], 'runtime', 'timeout'),
        ('SIMPLE', '간헐적 상태 조회 예외', [fault('intermittent_exception', ratio=0.5)], 'runtime', 'exception'),
        ('SIMPLE', 'CPU 과점유', [fault('cpu_spin', threads=1)], 'runtime', 'cpu'),
        ('SIMPLE', '런타임 메모리 누수', [fault('memory_leak', mib_per_second=8)], 'runtime', 'memory'),
        ('SIMPLE', '업무 처리 슬롯 고갈', [fault('worker_exhaustion')], 'runtime', 'pool'),
        ('NORMAL', '업무 계산 결함 배포', [fault('code_bug')], 'release', 'exception'),
        ('NORMAL', '입력·조회 처리 결함 배포', [fault('code_bug_validation')], 'release', 'exception'),
        ('NORMAL', '응답 변환 결함 배포', [fault('code_bug_serialization')], 'release', 'exception'),
        ('NORMAL', '배치 작업 실패', [fault('background_job_failure', interval_seconds=3)], 'runtime', 'background_exception'),
        ('NORMAL', '업무 정합성 위반', [fault('business_invariant')], 'runtime', 'invariant'),
        ('NORMAL', '외부 연동 연결 거부', [fault('dependency_failure')], 'runtime', 'dependency'),
        ('NORMAL', '간헐적 꼬리 지연', [fault('latency_spike', ratio=0.5, seconds=3)], 'runtime', 'latency'),
        ('NORMAL', '잘못된 시작 설정 배포', [fault('startup_config_error')], 'release', 'restart'),
        ('NORMAL', '일부 503와 꼬리 지연', [fault('error_rate', ratio=0.4), fault('latency_spike', ratio=0.6, seconds=3)], 'runtime', '5xx_latency'),
        ('NORMAL', '메모리 누수와 처리 지연', [fault('memory_leak', mib_per_second=4), fault('latency', seconds=1)], 'runtime', 'memory_latency'),
        ('COMPLEX', '계산·입력 두 경로 결함 배포', [fault('code_bug'), fault('code_bug_validation')], 'release', 'multiple_exceptions'),
        ('COMPLEX', '계산·응답 변환 두 경로 결함 배포', [fault('code_bug'), fault('code_bug_serialization')], 'release', 'multiple_exceptions'),
        ('COMPLEX', '입력·응답 변환 두 경로 결함 배포', [fault('code_bug_validation'), fault('code_bug_serialization')], 'release', 'multiple_exceptions'),
        ('COMPLEX', '배치 실패와 응답 변환 결함', [fault('background_job_failure', interval_seconds=3), fault('code_bug_serialization')], 'release', 'background_and_http'),
        ('COMPLEX', '업무 정합성과 입력 처리 결함', [fault('business_invariant'), fault('code_bug_validation')], 'release', 'invariant_and_http'),
        ('COMPLEX', '연동 실패와 고정 지연', [fault('dependency_failure'), fault('latency', seconds=2)], 'runtime', 'dependency_latency'),
        ('COMPLEX', '처리 슬롯 고갈과 간헐적 예외', [fault('worker_exhaustion'), fault('intermittent_exception', ratio=0.5)], 'runtime', 'pool_exception'),
        ('COMPLEX', 'CPU 포화와 꼬리 지연', [fault('cpu_spin', threads=2), fault('latency_spike', ratio=0.7, seconds=4)], 'runtime', 'cpu_latency'),
        ('COMPLEX', '메모리 누수와 업무 계산 결함', [fault('memory_leak', mib_per_second=4), fault('code_bug')], 'release', 'memory_exception'),
        ('COMPLEX', '배포된 시작 설정 오류와 준비 실패', [fault('startup_config_error'), fault('not_ready')], 'release', 'startup_and_readiness'),
    ]


def cases():
    result = []
    for system in SYSTEMS:
        title, bugs, job, invariant, dependency = BUSINESS[system]
        for index, (group, description, injections, source, signal) in enumerate(recipes(), 1):
            target = 'inventory' if system == 'order' and any(f['fault']=='business_invariant' for f in injections) else system
            result.append({'id': f'{system}-{index:02d}', 'system': system, 'namespace': 'dev', 'service': target,
                           'planned_group': group, 'description_ko': title + ' · ' + description,
                           'business_context': {'code_paths': bugs, 'background_job': job, 'invariant': invariant, 'dependency': dependency},
                           'source': source, 'injections': deepcopy(injections), 'ttl_seconds': 900,
                           'expected_signal_family': signal,
                           'restore': 'baseline_image_and_restart' if source=='release' else 'clear_and_restart',
                           'execution_state': 'not_run', 'observed_group': None,
                           'notes_ko': '설계 그룹은 실제 라우팅 정답을 강제하지 않음. 한 건 안의 결합 장애만 허용하고 다른 건과 동시 주입하지 않음.'})
    assert len(result)==180 and len({r['id'] for r in result})==180
    return result


def write(output):
    output.mkdir(parents=True, exist_ok=True)
    data = {'schema': 'liveprobe_fault_catalog/v1', 'version': 'p18c-v1',
            'claim': '설계 목록이며 실제 주입·감지·복구 결과가 아님', 'cases': cases()}
    encoded = (json.dumps(data, ensure_ascii=False, indent=2)+'\n').encode()
    path=output/'catalog.json'
    if path.exists() and path.read_bytes()!=encoded:
        raise RuntimeError('고정 카탈로그 변경 금지: 새 버전과 출력 폴더를 사용하세요')
    path.write_bytes(encoded)
    sha=hashlib.sha256(encoded).hexdigest();(output/'catalog.sha256').write_text(sha+'  catalog.json\n')
    with (output/'catalog.csv').open('w',newline='') as f:
        writer=csv.writer(f);writer.writerow(['id','system','service','planned_group','source','description_ko','injections','execution_state'])
        for r in data['cases']:writer.writerow([r[k] for k in ('id','system','service','planned_group','source','description_ko')]+[json.dumps(r['injections'],ensure_ascii=False),r['execution_state']])
    lines=['# 시스템별 장애 30건 고정 목록','', '설계 그룹과 실제 판정 그룹은 별도 기록한다. 총180개이며 아직 실행 결과가 아니다.', '', f'카탈로그 SHA256: `{sha}`', '']
    for system in SYSTEMS:
        lines += ['## '+system, '', '|ID|설계 그룹|주입 위치|장애|','|---|---|---|---|']
        for r in data['cases']:
            if r['system']==system:lines.append(f"|{r['id']}|{r['planned_group']}|{r['service']} · {r['source']}|{r['description_ko']}|")
        lines.append('')
    (output/'catalog.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({'cases':len(data['cases']),'per_system':dict(Counter(r['system'] for r in data['cases'])), 'sha256':sha},ensure_ascii=False))


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,default=ROOT/'docs/evidence/P18/campaign');args=p.parse_args();write(args.output)
