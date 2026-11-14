#!/usr/bin/env python3
"""Summarize observed campaign records, preserving missing usage and planned-vs-observed group distinctions."""
from collections import defaultdict
import csv
import json
from pathlib import Path
import statistics


FINAL_STATES={'completed','fault_unobserved','analysis_incomplete','failed','indeterminate','interrupted'}


def is_final_record(row):
    """실제 주입 기록은 quiet 이후의 기록·사용량 표식이 모두 있어야 확정한다."""
    return (row.get('state') in FINAL_STATES and
            (not row.get('injection_started_at') or
             bool(row.get('recorded_at') and row.get('usage_snapshot_at'))))


def usage(calls):
    unique={}
    for r in calls:
        if not isinstance(r.get('id'),int):raise ValueError('observed agent run ID is required')
        if r['id'] in unique and unique[r['id']]!=r:raise ValueError('conflicting snapshots of the same agent run')
        unique[r['id']]=r
    rows=list(unique.values())
    missing=sum(r.get('input_tokens') is None or r.get('output_tokens') is None for r in rows)
    known_input=sum((r.get('usage_summary') or {}).get('known_input_tokens', r.get('input_tokens') or 0) for r in rows)
    known_output=sum((r.get('usage_summary') or {}).get('known_output_tokens', r.get('output_tokens') or 0) for r in rows)
    missing_attempt_counts=[]
    for r in rows:
        incomplete=r.get('input_tokens') is None or r.get('output_tokens') is None
        count=(r.get('usage_summary') or {}).get('missing_usage_attempts')
        if count is None:
            # A legacy multi-attempt row cannot reveal how many attempts lacked usage.
            count=(1 if incomplete else 0) if (r.get('attempts') or 1)==1 else (None if incomplete else 0)
        missing_attempt_counts.append(count)
    costs=[r['estimated_cost_usd'] for r in rows if r.get('estimated_cost_usd') is not None
           and r.get('input_tokens') is not None and r.get('output_tokens') is not None]
    return {'calls':len(rows),'attempts':sum(r.get('attempts') or 1 for r in rows),
            'known_input_tokens':known_input,'known_output_tokens':known_output,'known_tokens':known_input+known_output,
            'total_tokens':known_input+known_output if not missing else None,'missing_usage_calls':missing,
            'missing_usage_attempts':sum(missing_attempt_counts) if all(n is not None for n in missing_attempt_counts) else None,
            'estimated_cost_usd':round(sum(costs),8) if len(costs)==len(rows) else None,
            'known_estimated_cost_usd':round(sum(costs),8),'missing_price_calls':len(rows)-len(costs),
            'actual_billed_usd':None,'currency_basis':'API 단가 환산 추정치(구독 청구액 아님)',
            'duration_ms':sum(r.get('duration_ms') or 0 for r in rows),
            'failed_calls':sum(r.get('status')=='failed' for r in rows)}


def incident_calls(detail):
    """Join existing API audit metadata by real run ID; never mutate the snapshot."""
    summaries={}
    for event in detail.get('audit') or []:
        payload=event.get('detail') or {}
        if event.get('event')=='agent_run' and 'usage_summary' in payload:
            rid=payload.get('agent_run_id')
            if rid in summaries and summaries[rid]!=payload['usage_summary']:
                raise ValueError('conflicting usage audit summaries of the same agent run')
            summaries[rid]=payload['usage_summary']
    return [{**r, **({'usage_summary':summaries[r['id']]} if r.get('id') in summaries else {})}
            for r in (detail.get('agent_runs') or [])]


def _cost_text(g):
    if g.get('estimated_cost_usd') is not None:return f"{g['estimated_cost_usd']:.4f}"
    return f"불완전(알려진 {g['known_estimated_cost_usd']:.4f})"


def summarize(records,planned_total=None):
    if len({r['id'] for r in records})!=len(records):raise ValueError('duplicate case records: select one attempt explicitly')
    case_rows=[];planned=defaultdict(list);observed=defaultdict(list);systems=defaultdict(list);roles=defaultdict(list)
    matrix=defaultdict(lambda:defaultdict(int))
    owned_incidents={};owned_runs={};allcalls=[]
    for r in records:
        details=r.get('incidents') or []
        calls=[];groups=[]
        for d in details:
            iid=d['incident']['id']
            if iid in owned_incidents and owned_incidents[iid]!=r['id']:
                raise ValueError(f'incident {iid} attributed to multiple cases')
            owned_incidents[iid]=r['id']
            group=(d.get('routing') or [{}])[-1].get('level') or 'UNOBSERVED'
            observed[group].append(d)
            if group not in groups:groups.append(group)
            incident_runs=incident_calls(d)
            for call in incident_runs:
                rid=call.get('id');owner=(r['id'],iid)
                if not isinstance(rid,int):raise ValueError('observed agent run ID is required')
                if rid in owned_runs and owned_runs[rid]!=owner:
                    raise ValueError(f'agent run {rid} attributed to multiple cases or incidents')
                owned_runs[rid]=owner
            calls+=incident_runs
        u=usage(calls)
        row={'id':r['id'],'system':r['system'],'planned_group':r['planned_group'],'observed_groups':groups,
             'state':r.get('state'),'detected_incidents':len(details),'restored':(r.get('restoration') or {}).get('verified'),
             'fault_observed':(r.get('fault_observation') or {}).get('verified'),**u}
        case_rows.append(row);planned[r['planned_group']].append(row);systems[r['system']].append(row);allcalls+=calls
        matrix[r['planned_group']]['|'.join(groups) or 'NO_INCIDENT']+=1
        for c in calls:roles[(c.get('model'),c.get('reasoning_effort'),c.get('role'))].append(c)
    def group_summary(rows):
        full=[r['total_tokens'] for r in rows if r['total_tokens'] is not None and r['calls']>0]
        states=defaultdict(int)
        for x in rows:states[x['state']]+=1
        return {'cases':len(rows),'states':dict(sorted(states.items())),'with_model_calls':sum(r['calls']>0 for r in rows),
                'restored_cases':sum(r['restored'] is True for r in rows),'fault_observed_cases':sum(r['fault_observed'] is True for r in rows),
                'calls':sum(r['calls'] for r in rows),'known_tokens':sum(r['known_tokens'] for r in rows),
                'missing_usage_calls':sum(r['missing_usage_calls'] for r in rows),
                'missing_usage_attempts':sum(r['missing_usage_attempts'] for r in rows)
                    if all(r['missing_usage_attempts'] is not None for r in rows) else None,
                'mean_complete_case_tokens':statistics.mean(full) if full else None,
                'median_complete_case_tokens':statistics.median(full) if full else None,
                'known_estimated_cost_usd':round(sum(r['known_estimated_cost_usd'] for r in rows),8),
                # 모든 건의 가격이 완전 관측일 때만 그룹 비용을 확정한다.
                'estimated_cost_usd':round(sum(r['estimated_cost_usd'] for r in rows),8)
                    if rows and all(r['estimated_cost_usd'] is not None for r in rows) else None,
                'mean_case_estimated_cost_usd':round(statistics.mean(r['estimated_cost_usd'] for r in rows if r['calls']>0),8)
                    if any(r['calls']>0 for r in rows) and all(r['estimated_cost_usd'] is not None for r in rows if r['calls']>0) else None,
                'actual_billed_usd':None}
    coverage={'planned':planned_total,'recorded':len(records),'not_run':planned_total-len(records)} if planned_total else None
    return {'schema':'liveprobe_campaign_summary/v1','executed_case_records':len(records),'coverage':coverage,'case_rows':case_rows,
            'planned_vs_observed':{k:dict(sorted(v.items())) for k,v in sorted(matrix.items())},
            'planned_groups':{k:group_summary(v) for k,v in sorted(planned.items())},
            'systems':{k:group_summary(v) for k,v in sorted(systems.items())},
            'observed_groups':{k:{'incidents':len({d['incident']['id'] for d in ds}),
                                  **usage([c for d in ds for c in incident_calls(d)])} for k,ds in sorted(observed.items())},
            'by_model_effort_role':[{'model':k[0],'effort':k[1],'role':k[2],**usage(v)} for k,v in sorted(roles.items(),key=lambda kv:str(kv[0]))],
            'overall':usage(allcalls),
            'limits_ko':['실제 구독 청구액은 미관측이다. 가격은 API 가격표 환산 추정치이다.',
                         '설계 그룹은 실제 라우터 판정을 강제하지 않는다. HIGH_RISK·미관측도 별도 집계한다.',
                         '서로 다른 장애의 기술통계이며 동일 사건 교차 비교가 아니므로 인과적 절감률을 주장하지 않는다.',
                         '시나리오 실행기·개발 작업자의 토큰은 앱 장애 분석 agent_runs와 별도이다.',
                         '한 시도라도 입력 또는 출력 사용량이 없으면 해당 합계와 전체 비용은 null이다. 명시적 0은 관측값이다.',
                         'known 토큰은 관측된 부분합이며 완전합이 아니다. 신규 agent_run audit의 usage_summary를 ID로 연결한다.',
                         'audit가 없는 과거 재시도의 알려진 부분합은 복원할 수 없고 누락 시도 수는 null이다. 누락 호출 수와 시도 수는 다르다.']}


def attempt_rows(attempts):
    """집계에서 제외한 이전 시도도 상태·토큰을 함께 공개한다."""
    rows=[]
    for a in attempts or []:
        calls=[c for d in (a.get('incidents') or []) for c in incident_calls(d)]
        excluded=a.get('excluded_incidents') or []
        excluded_calls=[c for d in excluded for c in incident_calls(d)]
        rows.append({'id':a.get('id'),'state':a.get('state'),'path':a.get('path'),
                     'usage_scope':'attributed_incidents_only','excluded_incidents_count':len(excluded),
                     'excluded_observed_calls':usage(excluded_calls)['calls'],**usage(calls)})
    return rows


def write_report(records,output,planned_total=None,attempts=None,in_progress=None):
    output.mkdir(parents=True,exist_ok=True);result=summarize(records,planned_total)
    if in_progress is not None:
        result['in_progress']=sorted(set(in_progress)-{r['id'] for r in records})
        if result['coverage'] is not None:
            result['coverage']['in_progress']=len(result['in_progress'])
            result['coverage']['not_run']-=len(result['in_progress'])
    result['excluded_attempts']=attempt_rows(attempts)
    (output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    with (output/'cases.csv').open('w',newline='') as f:
        fields=['id','system','planned_group','observed_groups','state','fault_observed','restored','detected_incidents',
                'calls','attempts','known_input_tokens','known_output_tokens','total_tokens','missing_usage_calls','missing_usage_attempts',
                'estimated_cost_usd','missing_price_calls','actual_billed_usd','duration_ms','failed_calls']
        w=csv.DictWriter(f,fields,extrasaction='ignore');w.writeheader()
        for r in result['case_rows']:w.writerow({**r,'observed_groups':'|'.join(r['observed_groups'])})
    cov=result['coverage']
    lines=['# 장애 캠페인 관측 결과','',
           f"기록 {len(records)}건"+(f" / 계획 {cov['planned']}건, "+
               (f"진행 중 {cov['in_progress']}건, " if 'in_progress' in cov else '')+
               f"미실행 {cov['not_run']}건" if cov else '')+'. 실제 청구액은 미관측이므로 토큰을 주 비교값으로, API 단가 환산 추정 비용을 보조값으로 쓴다.','',
           *(['진행 중 사례: '+', '.join(result['in_progress'])+' — 완료 전 토큰·비용은 집계하지 않음.','']
             if result.get('in_progress') else []),
           '## 설계 그룹별 비교','',
           '|설계 그룹|기록|상태 분포|장애 관측|원복 확인|모델 호출|알려진 토큰|사용량 누락 호출|평균 토큰(완전 관측)|중앙값 토큰|추정 비용(USD)|',
           '|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    def fmt(v):return f"{v:.1f}" if v is not None else '미관측'
    for group,g in result['planned_groups'].items():
        states=', '.join(f"{k} {v}" for k,v in g['states'].items())
        lines.append(f"|{group}|{g['cases']}|{states}|{g['fault_observed_cases']}|{g['restored_cases']}|{g['calls']}|{g['known_tokens']}|{g['missing_usage_calls']}|{fmt(g['mean_complete_case_tokens'])}|{fmt(g['median_complete_case_tokens'])}|{_cost_text(g)}|")
    lines+=['','## 설계 그룹 → 실제 라우팅','','|설계 그룹|실제 라우팅 분포|','|---|---|']
    for group,dist in result['planned_vs_observed'].items():lines.append(f"|{group}|{', '.join(f'{k} {v}' for k,v in dist.items())}|")
    lines+=['','## 실제 라우팅 그룹별','','|실제 그룹|사건|모델 호출|알려진 토큰|사용량 누락 호출|추정 비용(USD)|','|---|---:|---:|---:|---:|---:|']
    for group,g in result['observed_groups'].items():lines.append(f"|{group}|{g['incidents']}|{g['calls']}|{g['known_tokens']}|{g['missing_usage_calls']}|{_cost_text(g)}|")
    lines+=['','## 시스템별','','|시스템|기록|상태 분포|장애 관측|원복 확인|알려진 토큰|추정 비용(USD)|','|---|---:|---|---:|---:|---:|---:|']
    for system,g in result['systems'].items():
        lines.append(f"|{system}|{g['cases']}|{', '.join(f'{k} {v}' for k,v in g['states'].items())}|{g['fault_observed_cases']}|{g['restored_cases']}|{g['known_tokens']}|{_cost_text(g)}|")
    if result['excluded_attempts']:
        lines+=['','## 집계에서 제외한 이전 시도(보존)','',
                '아래 토큰·비용은 각 시도에 귀속된 사건만 집계한다. 0이어도 시도 전체 비용이 0이라는 뜻이 아님. '+
                '제외 관측 호출은 저장된 제외 사건 스냅샷의 호출 수이며, 원본은 경로의 result.json에 보존한다.','',
                '|사례|상태|경로|귀속 토큰|귀속 추정 비용(USD)|제외 사건|제외 관측 호출|','|---|---|---|---:|---:|---:|---:|']
        for a in result['excluded_attempts']:
            lines.append(f"|{a['id']}|{a['state']}|{a['path']}|{a['known_tokens']}|{_cost_text(a)}|{a['excluded_incidents_count']}|{a['excluded_observed_calls']}|")
    lines+=['','## 해석 한계','']+['- '+x for x in result['limits_ko']]
    (output/'report.md').write_text('\n'.join(lines)+'\n');return result


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--records',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--planned-total',type=int,default=180);p.add_argument('--extra-attempt',type=Path,nargs='*',default=[]);a=p.parse_args()
    directories=sorted({p.parent for kind in ('result.json','progress.json') for p in a.records.glob('*/'+kind)
                        if p.parent.name not in ('aggregate','attempts')})
    loaded=[json.loads((d/('result.json' if (d/'result.json').exists() else 'progress.json')).read_text())
            for d in directories]
    # 아직 원복·최종 snapshot 전인 진행 중 기록은 집계하지 않고 따로 센다.
    records=[x for x in loaded if is_final_record(x)];in_progress=sorted(x['id'] for x in loaded if not is_final_record(x))
    attempts=[]
    for path in sorted(a.records.glob('attempts/*/result.json'))+list(a.extra_attempt):
        x=json.loads(path.read_text());attempts.append({**x,'path':str(path.parent)})
    result=write_report(records,a.output,a.planned_total,attempts,in_progress)
    print(json.dumps({'cases':len(records),'in_progress':in_progress,'excluded_attempts':len(attempts),'usage':result['overall']},ensure_ascii=False))
