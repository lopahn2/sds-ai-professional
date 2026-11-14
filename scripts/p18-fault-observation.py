#!/usr/bin/env python3
"""장애별 실제 관측 성분 판정. 수집은 하지 않으며 활성화 응답을 증거로 쓰지 않는다."""
from datetime import datetime
import math
import re

# 실제 업무 소스에서 확인한 서로 다른 예외 발생 함수다.
CODE_FRAMES={
    'payment':('calculate_fee','refund_quote','receipt'),
    'catalog':('search_products','product_detail','price_response'),
    'notification':('normalize_channel','retry_notification','list_notifications'),
    'member':('login','normalizeMemberName','serializeProfile'),
    'delivery':('transition','parseArrivalDate','serializeEta'),
    'order':('calculateOrderAmount','normalizeOrderItem','serializeOrderAmount'),
    'inventory':('calculateReservationQuantity','lookupStockItem','serializeStockQuantity'),
}
CODE_NAMES=('code_bug','code_bug_validation','code_bug_serialization')
JOB_STACKS={'payment':('SettlementError','background_job'),'catalog':('IndexRebuildError','background_job'),
            'notification':('DispatchJobError','background_job'),'member':('SessionCleanupError','backgroundJob'),
            'delivery':('DelayScanError','backgroundJob'),'order':('OrderExpiryJobException','expirePendingOrders'),
            'inventory':('StockReconcileException','reconcileStock')}
INVARIANT_STACKS={'payment':('DuplicateChargeError','check_invariants'),'catalog':('PriceConsistencyError','check_invariants'),
                  'notification':('QueueStalledError','check_invariants'),'member':('SessionIntegrityError','checkIntegrity'),
                  'delivery':('StatusRegressionError','checkIntegrity'),'order':('OversellException','checkInvariant'),
                  'inventory':('OversellException','checkInvariant')}


def _frame(text,name):
    return bool(re.search(r'(?m)(?:, in |\bat [^\n]*\b)'+re.escape(name)+r'(?:\s*\(|\s*$)',text))


def _exception_stack(text,name):
    return bool(re.search(r'\b'+re.escape(name)+r':',text) and
                (re.search(r'(?m)^\s*at\s+\S',text) or ('Traceback (most recent call last):' in text and 'File "' in text)))


def _number(value):
    return type(value) in (int,float) and math.isfinite(value)


def _scoped_logs(facts):
    if not facts.get('since') or facts.get('logs_since')!=facts['since']:return ''
    return '\n'.join(row.get('text','') for row in facts.get('logs',[]))


def _new_oom(facts):
    before={(c['uid'],c['name']):c.get('restartCount',0) for c in facts.get('before_containers',[])}
    for c in facts.get('containers',[]):
        changed=c.get('restartCount',0)>before.get((c['uid'],c['name']),0)
        for state in (c.get('state',{}),c.get('lastState',{})):
            ended=state.get('terminated') or {}
            if ended.get('reason')!='OOMKilled':continue
            recent=False
            try:
                recent=datetime.fromisoformat(ended['finishedAt'].replace('Z','+00:00'))>=datetime.fromisoformat(facts['since'].replace('Z','+00:00'))
            except (KeyError,ValueError,TypeError):pass
            if changed or recent:return ended
    return None


def _restart_deltas(facts):
    before={(c['uid'],c['name']):c.get('restartCount',0) for c in facts.get('before_containers',[])}
    return [(c,c.get('restartCount',0)-before.get((c['uid'],c['name']),0)) for c in facts.get('containers',[])]


def assess(case,facts):
    components={}
    for injection in case['injections']:
        name=injection['fault'];evidence={};verified=False
        if name=='memory_leak':
            ended=_new_oom(facts)
            verified=ended is not None
            if verified:evidence={'kind':'kernel_oom','termination':ended}
            elif case['service'] in ('order','inventory') and 'OutOfMemoryError' in _scoped_logs(facts):
                verified=True;evidence={'kind':'jvm_heap_oom'}
        elif name in CODE_NAMES:
            expected=CODE_FRAMES.get(case['service'])
            frame=expected[CODE_NAMES.index(name)] if expected else None
            matching=[p for p in facts.get('probes',[]) if name in p.get('faults',[]) and p.get('status')==500]
            verified=bool(frame and matching and _frame(_scoped_logs(facts),frame))
            evidence={'expected_frame':frame,'matching_500_responses':len(matching),'frame_observed':bool(frame and _frame(_scoped_logs(facts),frame))}
        elif name in ('background_job_failure','business_invariant'):
            expected=(JOB_STACKS if name=='background_job_failure' else INVARIANT_STACKS).get(case['service'])
            text=_scoped_logs(facts)
            verified=bool(expected and expected[0] in text and _frame(text,expected[1]))
            evidence={'expected_exception':expected[0] if expected else None,'expected_frame':expected[1] if expected else None}
        elif name=='not_ready':
            endpoints=facts.get('ready_endpoints')
            verified=bool((facts.get('health') or {}).get('status')==503 and type(endpoints) is int and endpoints==0)
            evidence={'readiness_status':(facts.get('health') or {}).get('status'),'ready_endpoints':endpoints}
        elif name=='liveness_fail':
            restarted=any(delta>0 for _,delta in _restart_deltas(facts))
            verified=bool((facts.get('liveness') or {}).get('status')==503 and restarted)
            evidence={'liveness_status':(facts.get('liveness') or {}).get('status'),'new_restart':restarted}
        elif name in ('crash','startup_config_error'):
            repeated=[{'uid':c['uid'],'new_restarts':delta} for c,delta in _restart_deltas(facts) if delta>=2 and
                      any((state.get('terminated') or {}).get('exitCode')==1 for state in (c.get('state',{}),c.get('lastState',{})))]
            config_stack=any(_frame(_scoped_logs(facts),frame) for frame in ('load_settings','validateConfiguration','validateStartupConfiguration'))
            verified=bool(repeated and (name=='crash' or config_stack))
            evidence={'repeat_exit1':repeated,'configuration_stack':config_stack}
        elif name=='hang':
            hangs=[]
            for probe in facts.get('probes',[]):
                duration=probe.get('http_seconds');timeout=probe.get('timeout_seconds')
                if probe.get('http_timeout') is True and _number(duration) and _number(timeout) and timeout>0 and duration>=min(timeout,injection.get('params',{}).get('seconds',40)):
                    hangs.append({'http_seconds':duration,'timeout_seconds':timeout})
            verified=bool(hangs);evidence={'observed_http_timeouts':hangs}
        elif name=='worker_exhaustion':
            failed=[p for p in facts.get('probes',[]) if p.get('status')==503 and _number(p.get('http_seconds')) and p['http_seconds']>=10]
            verified=bool(failed and _exception_stack(_scoped_logs(facts),'WorkerPoolExhausted'))
            evidence={'delayed_503_responses':len(failed),'required_wait_seconds':10}
        elif name=='dependency_failure':
            failed=[p for p in facts.get('probes',[]) if p.get('status')==502];text=_scoped_logs(facts)
            refused=bool(re.search(r'connection refused|ECONNREFUSED',text,re.IGNORECASE))
            verified=bool(failed and refused and _exception_stack(text,'DependencyError'))
            evidence={'observed_502_responses':len(failed),'connection_refused':refused}
        elif name=='intermittent_exception':
            failed=[p for p in facts.get('probes',[]) if p.get('status')==500];text=_scoped_logs(facts)
            classes=[kind for kind in ('TransientStateError','IntermittentError','IntermittentException') if _exception_stack(text,kind)]
            verified=bool(failed and classes);evidence={'observed_500_responses':len(failed),'exception_classes':classes}
        elif name=='cpu_spin':
            cores=facts.get('cpu_cores');verified=bool(_number(cores) and cores>=0.15)
            evidence={'cpu_cores':cores if _number(cores) else None,'minimum_cores':0.15}
        elif name=='error_rate':
            failed=[p for p in facts.get('probes',[]) if p.get('status')==503]
            verified=bool(failed);evidence={'observed_503_responses':len(failed)}
        elif name in ('latency','latency_spike'):
            threshold=injection.get('params',{}).get('seconds',2 if name=='latency' else 3)
            times=[p['http_seconds'] for p in facts.get('probes',[]) if _number(p.get('http_seconds')) and p.get('status') is not None]
            long=[v for v in times if v>=threshold];short=[v for v in times if v<threshold]
            verified=bool(_number(threshold) and threshold>0 and long and (name=='latency' or short))
            evidence={'threshold_seconds':threshold,'http_seconds':times,'long_responses':len(long),'short_responses':len(short)}
        components[name]={'verified':verified,'reason':'필수 실제 관측 충족' if verified else '해당 장애의 필수 실제 관측 없음','evidence':evidence}
    return {'verified':bool(components) and all(row['verified'] for row in components.values()),'components':components}
