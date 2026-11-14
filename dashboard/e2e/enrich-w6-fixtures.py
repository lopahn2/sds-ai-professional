"""P18 W6 모의 fixture 보강: 기존 narrative/events 기록에서 FlowNode.at·card, narrative.handoff_view를 계약(P18-W6-contract.md 1장) 모양으로 채운다.
값은 같은 fixture의 기존 기록(events의 evidence_collected·routing_decided·agent_finished·escalated, raw 한글 해석)에서만 가져온다.
한 번 더 실행해도 같은 결과(멱등). 실행: python3 e2e/enrich-w6-fixtures.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).parent / 'fixtures'
REF = {'deployment': 'E1_deployment', 'pod_status': 'E2_pods', 'service': 'E3_service', 'endpoints': 'E3_service', 'k8s_event': 'E5_recent_events',
       'prometheus': 'E6_metrics', 'loki': 'E7_error_logs', 'node': 'E8_node', 'configmap': 'E9_config', 'code_refs': None}
SOURCE_KO = {'deployment': 'Deployment 상태', 'pod_status': 'Pod 상태', 'service': 'Service 정의', 'endpoints': 'Service 연결 대상', 'k8s_event': '최근 Kubernetes 이벤트',
             'prometheus': 'Prometheus 지표', 'loki': 'Loki 오류 로그', 'node': '노드 상태', 'configmap': 'ConfigMap 설정'}
LEVEL_KO = {'SIMPLE': '단순', 'NORMAL': '보통', 'COMPLEX': '복잡', 'HIGH_RISK': '고위험'}
GROUP_KO = {'SIMPLE': '단순 그룹', 'NORMAL': '보통 그룹', 'COMPLEX': '복잡 그룹', 'HIGH_RISK': '고위험 그룹'}
STATUS_KO = {'planned': '예정', 'running': '실행 중', 'done': '완료', 'failed': '실패', 'skipped': '건너뜀', 'escalated': '상위 모델 승격'}
BUDGET = {'SIMPLE': None, 'NORMAL': 1.3, 'COMPLEX': 2.3, 'HIGH_RISK': None}
# 사건별 규칙 문장 대응(routing_decided.rationale의 규칙 접두 문장 → 한국어). 원문은 reason_raw로 그대로 둔다.
CASE = {
    971: dict(features=['준비된 Pod 0개', '반복 재시작 있음', '메모리 초과 종료 있음', '최근 배포 있음', 'Service 연결 대상 없음', '호출 서비스 실패 있음(주문 서비스 503)', '감지 신호: OOMKilled', '수집 출처 7종, 수집 오류 없음'],
              rule='메모리 초과·지연 신호에 해당해 보통으로 판정 · 여러 출처 분석이 필요하고 자동 조치는 하지 않음', final='AI가 보통→복잡으로 올렸고, 상향 의견은 받아들여 복잡으로 확정'),
    987: dict(features=['준비된 Pod 1개(이전 버전)', '반복 재시작 없음', '메모리 초과 종료 없음', '최근 배포 있음(버전 107, 0.3분 전)', 'Service 연결 대상 있음', '호출 서비스 실패 없음', '감지 신호: ErrImagePull', '수집 출처 7종, 수집 오류 없음'],
              rule='최근 배포 직후 이미지 내려받기 실패 규칙에 해당해 단순으로 판정', final='규칙 판정에 AI 분류도 동의해 단순으로 확정'),
    990: dict(features=['준비된 Pod 1개', '반복 재시작 없음', '메모리 초과 종료 없음', '최근 배포 있음', 'Service 연결 대상 있음', '호출 서비스 실패 미관측', '감지 신호: NetworkDrop', '수집 출처 8종, 수집 오류 1건(Loki)'],
              rule='네트워크 차단(NetworkDrop) 규칙에 해당해 고위험으로 판정 · 자동 조치 없이 운영자 인계(재지정 불가)', final='고위험 규칙이 우선하며 AI 분류도 동의해 고위험으로 확정'),
    976: dict(features=['준비된 Pod 1개', '반복 재시작 없음', '메모리 초과 종료 없음', '최근 배포 있음', 'Service 연결 대상 있음', '호출 서비스 실패 없음', '감지 신호: 연결 풀 고갈', '수집 출처 7종, 수집 오류 없음'],
              rule='메모리 초과·지연 신호에 해당해 보통으로 판정 · 여러 출처 분석이 필요하고 자동 조치는 하지 않음', final='규칙 판정에 AI 분류도 동의해 보통으로 확정'),
    1976: dict(features=['준비된 Pod 1개', '반복 재시작 없음', '메모리 초과 종료 없음', '최근 배포 있음', 'Service 연결 대상 있음', '호출 서비스 실패 없음', '감지 신호: 연결 풀 고갈', '수집 출처 7종, 수집 오류 없음'],
               rule='메모리 초과·지연 신호에 해당해 보통으로 판정 · 여러 출처 분석이 필요하고 자동 조치는 하지 않음', final='규칙 판정에 AI 분류도 동의해 보통으로 확정'),
}
HANDOFF_990 = {
    'summary_ko': '네트워크 차단 신호는 있지만 실패 경로·원인·영향 범위를 확인하지 못해, 자동 조치 없이 운영자에게 인계했습니다.',
    'summary_raw': 'HIGH_RISK handoff: NetworkDrop is reported for liveprobe-target/p14-net-sink, but the failing network path and cause are unverified.',
    'decisions': [
        {'key': 'outcome', 'label_ko': '결과', 'value_ko': '운영자 인계 · 자동 복구 아님'},
        {'key': 'cause', 'label_ko': '원인 판단', 'value_ko': '네트워크 차단 신호는 확인했으나 실패 경로와 원인은 미확인 (신뢰도 0.62, 독립 검증: 근거 부족)'},
        {'key': 'auto_action', 'label_ko': '자동 조치', 'value_ko': '실행하지 않음 — 네트워크 차단은 고위험 규칙이라 자동 조치를 허용하지 않습니다.'},
        {'key': 'operator', 'label_ko': '운영자 결정', 'value_ko': '원인을 확인한 뒤 대화로 조치를 지시하거나 장애를 종료하세요.'},
    ],
    'next_steps_ko': ['p14-net-sink를 호출하는 서비스의 연결 오류를 확인하세요.', '네트워크 정책과 Service 연결 대상이 의도와 같은지 확인하세요.', 'Loki 조회 오류를 해소한 뒤 애플리케이션 로그를 다시 확인하세요.'],
    'next_steps_raw': ['kubectl -n liveprobe-target get networkpolicy -o yaml', 'kubectl -n liveprobe-target get endpoints p14-net-sink', 'Re-run the Loki query for liveprobe-target/p14-net-sink after fixing the 400 Bad Request'],
    'interpreting': False,
}


def first_sentence(text):
    if not text:
        return None
    for mark in ('. ', '다. '):
        if mark in text:
            return text.split(mark)[0] + mark.strip()
    return text


def enrich(iid):
    np, ep = ROOT / f'narrative-{iid}.json', ROOT / f'events-{iid}.json'
    n, ev = json.loads(np.read_text()), json.loads(ep.read_text())
    events, inc = ev['events'], n['incident']
    case = CASE[iid]
    by_kind = lambda k: [e for e in events if e['kind'] == k]
    collected = by_kind('evidence_collected')[0]
    routing = by_kind('routing_decided')[0]['detail']
    finished = {e['detail'].get('agent_run_id'): e for e in by_kind('agent_finished')}
    started = {}
    for e in by_kind('agent_started'):
        started.setdefault(e['detail']['role'], []).append(e['at'])
    level = routing['level']
    raw_ko = {}
    for r in n['raw']:
        if r.get('ko') and r['ref'] not in raw_ko:
            raw_ko[r['ref']] = r['ko']
    errors = {x['source']: x['error'] for x in collected['detail'].get('errors', [])}
    items = []
    for k, src in enumerate(collected['detail']['sources']):
        ref = REF.get(src)
        ok = src not in errors
        items.append({'id': iid * 100 + k + 1, 'source': src, 'source_ko': SOURCE_KO.get(src, src), 'target': f"{inc['namespace']}/{inc['workload']}",
                      'ok': ok, 'error': errors.get(src), 'summary_ko': first_sentence(raw_ko.get(ref)) if ok and ref else None, 'ref': ref, 'collected_at': collected['at']})
    rationale = routing['rationale']
    llm_raw = rationale.split(': ', 1)[1] if ': ' in rationale and 'LLM' in rationale else rationale
    llm_raw = rationale[rationale.index('LLM'):].split(': ', 1)[1] if 'LLM' in rationale else None
    router_node = next(x for x in n['flow']['nodes'] if x['type'] == 'router')
    agent_models = [x['model'] for x in n['flow']['nodes'] if x['type'] in ('agent', 'verify') and x['model']]
    selected = agent_models[0] if agent_models else None
    steps = [
        {'key': 'features', 'title_ko': '관측 특징 확인', 'detail_ko': '분류기에 들어간 관측 사실입니다.', 'items_ko': case['features'], 'source': 'rules'},
        {'key': 'rule', 'title_ko': '규칙 판정', 'detail_ko': case['rule'], 'items_ko': [], 'source': 'rules'},
        {'key': 'llm', 'title_ko': 'AI 분류 의견', 'detail_ko': f"{LEVEL_KO[level]} 의견 · 신뢰도 {routing['confidence']:.2f} — {router_node['conclusion_ko']}", 'items_ko': [], 'source': 'llm'},
        {'key': 'final', 'title_ko': '최종 결정', 'detail_ko': case['final'], 'items_ko': [], 'source': 'rules'},
        {'key': 'model', 'title_ko': '분석 모델 선택', 'detail_ko': f"적격 후보 중 가중 점수(품질 0.6·비용 0.25·처리량 0.15)가 가장 높은 {selected} 선택" if selected else '분석 모델 선택 기록 없음', 'items_ko': [], 'source': 'policy'},
    ]
    routing_card = {'kind': 'routing', 'routing_id': iid * 10 + 1, 'level': level, 'level_ko': LEVEL_KO[level], 'decided_by': routing.get('decided_by', 'rules+llm'), 'decided_by_ko': '규칙 + AI 분류',
                    'confidence': routing['confidence'], 'steps': steps, 'final_reason_ko': case['final'], 'reason_raw': rationale,
                    'llm': {'level': level, 'confidence': routing['confidence'], 'rationale_raw': llm_raw or router_node['conclusion_raw'], 'rationale_ko': router_node['conclusion_ko']},
                    'model_policy': {'selected_model': selected, 'reason_ko': steps[4]['detail_ko'], 'candidates': [{'model': selected, 'eligible': True, 'score': 0.82, 'reason_ko': '품질·비용·처리량 가중 점수 최고'}] if selected else []},
                    'interpreting': False}
    group_started = by_kind('group_composed')
    escalations = by_kind('escalated')
    progress = []
    if group_started:
        progress.append(f"Group 시작: {GROUP_KO[level]} · 정책 {group_started[0]['detail'].get('policy_version', '미기록')}")
    for e in escalations:
        d = e['detail']
        progress.append(f"{next((x['role_ko'] for x in n['flow']['nodes'] if x.get('role') == d['role']), d['role'])} 인용 확인 실패로 {d['from_model']} → {d['to_model']} 상위 모델 재실행")
    done = by_kind('completed')
    runs = [x for x in n['flow']['nodes'] if x['type'] in ('agent', 'verify')]
    if done:
        calls = len([x for x in runs if x['status'] in ('done', 'escalated', 'failed')])
        progress.append(f"Group 종료: {'검증 통과' if done[0]['detail'].get('outcome') == 'resolved' else '운영자 인계'} · Agent 호출 {calls}회 · 추정 {inc['total_cost_usd']:.4f} USD")
    roles, seen = [], set()
    for x in runs:
        if x['role'] in seen:
            continue
        seen.add(x['role'])
        last = [y for y in runs if y['role'] == x['role']][-1]
        roles.append({'role': x['role'], 'role_ko': x['role_ko'], 'job_ko': x['job_ko'], 'model': last['model'], 'effort': last['effort'], 'status': last['status']})
    group_status = '진행 중' if inc['status'] in ('open', 'analyzing', 'acting', 'action_pending') else '종료'
    group_card = {'kind': 'group', 'routing_id': routing_card['routing_id'], 'level': level, 'group_ko': GROUP_KO[level],
                  'decision': {'level_ko': LEVEL_KO[level], 'decided_by_ko': '규칙 + AI 분류', 'confidence': routing['confidence'], 'final_reason_ko': case['final'], 'steps': steps},
                  'roles': roles, 'budget': {'max_agent_calls': None, 'max_cost_usd': BUDGET[level], 'deadline_s': None}, 'progress_ko': progress, 'status_ko': group_status}
    finished_role = {}
    for e in by_kind('agent_finished'):
        finished_role.setdefault(e['detail']['role'], []).append(e)
    terminal = inc['status'] in ('resolved', 'handed_off', 'failed', 'closed')
    for x in n['flow']['nodes']:
        t = x['type']
        if t == 'signal':
            x['at'] = inc['created_at']
            x['card'] = {'kind': 'signal', 'what_ko': n['headline']['what'], 'place_ko': f"{inc['namespace']} · {inc['workload']}", 'source': 'alertmanager', 'reason': inc['title'].split(' on ')[0],
                         'signal_count': inc['signal_count'], 'first_at': inc['created_at']}
        elif t == 'evidence':
            x['at'] = collected['at']
            x['card'] = {'kind': 'evidence', 'total': len(items), 'ok': sum(i['ok'] for i in items), 'failed': sum(not i['ok'] for i in items), 'items': items}
            x['label'] = f"근거 수집 {len(items)}건"
        elif t == 'router':
            x['at'] = by_kind('routing_decided')[0]['at']
            x['card'] = routing_card
        elif t == 'group':
            x['at'] = (group_started or by_kind('routing_decided'))[0]['at']
            x['card'] = group_card
        elif t in ('agent', 'verify'):
            rid = int(x['id'].split(':')[1]) if x['id'].startswith('run:') else None
            fin = finished.get(rid)
            later = [y for y in runs if y['depth'] > x['depth'] and y['status'] in ('planned', 'running')]
            cands = [{'cause_raw': c.get('cause', ''), 'confidence': c.get('confidence'), 'verifier': None} for c in (fin['detail'].get('root_cause_candidates') or [])] if fin else []
            if fin and not cands and x.get('conclusion_raw'):
                cands = [{'cause_raw': first_sentence(x['conclusion_raw']), 'confidence': fin['detail'].get('confidence'), 'verifier': None}]
            verdict = ('supported' if level != 'HIGH_RISK' else 'insufficient') if t == 'verify' and x['status'] == 'done' else None
            # 계약: agent의 at = AgentRun.created_at(시작 시각). 같은 역할의 재실행은 흐름 순서대로 시작 기록에 대응시킨다.
            same = [y['id'] for y in runs if y['role'] == x['role'] and y['status'] != 'planned']
            starts = started.get(x['role'], [])
            x['at'] = starts[same.index(x['id'])] if x['id'] in same and same.index(x['id']) < len(starts) else None
            x['card'] = {'kind': 'agent', 'agent_run_id': rid, 'summary_raw': x.get('conclusion_raw'), 'summary_ko': x.get('conclusion_ko'), 'candidates': cands,
                         'verdict': verdict, 'verdict_ko': {'supported': '근거로 지지', 'insufficient': '근거 부족'}.get(verdict),
                         'next_action': ('handoff' if level == 'HIGH_RISK' else 'act') if x['status'] == 'done' and t == 'verify' else ('investigate_more' if x['status'] == 'done' else None),
                         'next_action_ko': ('운영자 인계' if level == 'HIGH_RISK' else 'Kubernetes 조치 제안') if x['status'] == 'done' and t == 'verify' else ('추가 조사 필요' if x['status'] == 'done' else None),
                         'counter_evidence_count': 1 if x['role'] == 'challenger' else 0, 'error': None,
                         'accepts_steering': (not terminal) and (x['status'] in ('planned', 'running') or bool(later)), 'steer_target_role': x['role']}
        elif t == 'action':
            aid = int(x['id'].split(':')[1])
            prop = [e for e in by_kind('action_proposed') if e['detail'].get('action_id') == aid]
            ex = [e for e in by_kind('action_executed') if e['detail'].get('action_id') == aid]
            typ = (prop[0]['detail'].get('type') if prop else None) or 'unknown'
            x['at'] = (prop or ex or [{'at': None}])[0]['at']  # 계약: action = Action.created_at(제안 시각)
            x['card'] = {'kind': 'action', 'action_id': aid, 'type': typ, 'type_ko': x['label'], 'target': f"Deployment/{inc['workload']}", 'status': 'succeeded' if ex else 'proposed',
                         'status_ko': '실행 후 복구 검증 통과' if ex and by_kind('verification_result') else '제안됨', 'changes': [f"{k}={v}" for k, v in (next((e['detail']['params'] for e in prop if isinstance(e['detail'].get('params'), dict)), {}) or {}).items()]}
        elif t == 'outcome':
            x['at'] = inc['updated_at'] if done else None  # 계약: outcome = incident.updated_at (결과 대기 중이면 미기록)
            status_ko = {'resolved': '자동 복구 완료', 'handed_off': '운영자 인계', 'failed': '처리 실패', 'closed': '운영자 종료'}.get(inc['status'], '결과 대기')
            x['card'] = {'kind': 'outcome', 'status': inc['status'], 'status_ko': status_ko, 'handoff': HANDOFF_990 if iid == 990 else None}
    if iid == 990:
        n['handoff_view'] = HANDOFF_990
        inc['handoff'] = {'recommended_steps': HANDOFF_990['next_steps_raw']}
        inc['status_reason'] = 'HIGH_RISK operator handoff: NetworkDrop path unverified; no automatic action (non-overridable).'
    else:
        n['handoff_view'] = None
    np.write_text(json.dumps(n, ensure_ascii=False, indent=2) + '\n')


for iid in CASE:
    enrich(iid)
print('보강 완료:', ', '.join(map(str, CASE)))
