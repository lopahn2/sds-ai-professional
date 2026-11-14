"""기존 실행 스냅샷에 사건 전체 예산과 비밀 없는 시도 계층을 저장한다."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
import time
import uuid

from .llm.base import LLMError, Usage

_active = ContextVar('incident_runtime', default=None)
_records = ContextVar('provider_attempt_records', default=None)
_trace = ContextVar('provider_call_trace', default=None)


@contextmanager
def call_trace(logical_call_id, validation_round):
    token = _trace.set({'logical_call_id': logical_call_id, 'validation_round': validation_round})
    try:
        yield
    finally:
        _trace.reset(token)


def aggregate_usage(values):
    return Usage(**{name: sum(getattr(u, name) for u in values)
                    if values and all(getattr(u, name) is not None for u in values) else None
                    for name in ('input_tokens', 'output_tokens', 'cached_input_tokens', 'reasoning_output_tokens')})


class IncidentRuntime:
    """저장소 행 잠금 아래 예약하며 공급자 호출 동안에는 잠금을 보유하지 않는다."""
    def __init__(self, cfg, storage, *, clock=time.time, max_attempts=None, deadline_s=None):
        self.cfg, self.storage, self.clock = cfg, storage, clock
        self.max_attempts = max_attempts if max_attempts is not None else 2 * max(cfg.workflows.max_agent_calls.values())
        self.deadline_s = deadline_s if deadline_s is not None else cfg.workflows.deadline_s

    def reserve(self, role, prompt, model, timeout, account=None):
        from .runtime_resume import fingerprint
        price = self.cfg.llm.prices.get(model) or {}
        if 'input' not in price or 'output' not in price:
            raise LLMError('budget', '모델 비용 미관측으로 호출 중단')
        projection = ((len(prompt) / 3 + self.cfg.workflows.projected_input_overhead_tokens) * price['input']
                      + self.cfg.workflows.projected_output_tokens * price['output']) / 1e6
        with self.storage() as state:
            if state.get('legacy_unobserved_cost_runs', 0):
                raise LLMError('budget', '과거 호출 비용 미관측으로 사건 예산 확인 필요')
            state.setdefault('started_at', self.clock())
            state.setdefault('trace_id', uuid.uuid4().hex)
            elapsed = max(self.clock() - state['started_at'], state.get('quota_wait_s', 0))
            display_only = state.get('_display_only', False) and role == 'interpreter'
            if display_only:
                # 분석 완료 뒤의 화면 열람은 과거 벽시계 만료가 아니라 누적 예약 시간을 쓴다.
                # 호출·비용 원장과 분석 중 소비 시간은 그대로 유지한다.
                elapsed = max(state.get('analysis_time_s', 0), state.get('quota_wait_s', 0)) + state.get('display_time_s', 0)
            if (state.get('provider_attempts', 0) >= self.max_attempts
                    or elapsed + timeout > self.deadline_s
                    or state.get('charged_usd', 0) + state.get('reserved_usd', 0) + projection
                       > max(self.cfg.workflows.max_cost_usd.values())):
                raise LLMError('budget', '사건 전체 호출·비용·시간 예산 소진')
            if display_only:
                state['display_time_s'] = state.get('display_time_s', 0) + timeout
            else:
                state['analysis_time_s'] = max(state.get('analysis_time_s', 0), elapsed + timeout)
            state['provider_attempts'] = state.get('provider_attempts', 0) + 1
            state['reserved_usd'] = state.get('reserved_usd', 0) + projection
            span = {'span_id': uuid.uuid4().hex, 'trace_id': state['trace_id'],
                    'parent': 'interpretation' if role == 'interpreter' else 'routing' if role == 'router' else 'analysis',
                    'role': role, 'model': model, 'account': account,
                    'input_sha256': fingerprint({'prompt': prompt, 'model': model}), 'status': 'running',
                    'projected_usd': projection,
                    **(_trace.get() or {'logical_call_id': uuid.uuid4().hex, 'validation_round': 1})}
            state.setdefault('spans', []).append(span)
            return span['span_id'], projection

    def finish(self, span_id, projection, usage, error_kind):
        from .llm import estimate_cost
        with self.storage() as state:
            span = next(s for s in state['spans'] if s['span_id'] == span_id)
            cost = estimate_cost(self.cfg, span['model'], usage)
            state['reserved_usd'] = max(0, state.get('reserved_usd', 0) - projection)
            state['charged_usd'] = state.get('charged_usd', 0) + (projection if cost is None else cost)
            span.update(status='failed' if error_kind else 'succeeded', error_kind=error_kind,
                        input_tokens=usage.input_tokens, output_tokens=usage.output_tokens, cost_usd=cost)
            for name in ('input_tokens', 'output_tokens'):
                value = getattr(usage, name)
                state['known_' + name] = state.get('known_' + name, 0) + (value if value is not None else 0)
                missing = 'missing_' + name
                state[missing] = state.get(missing, 0) + int(value is None)
                state[name] = None if state[missing] else state['known_' + name]

    def wait(self, seconds):
        with self.storage() as state:
            state.setdefault('started_at', self.clock())
            state.setdefault('trace_id', uuid.uuid4().hex)
            state.setdefault('provider_attempts', 0)
            elapsed = max(self.clock() - state['started_at'], state.get('quota_wait_s', 0))
            if elapsed + seconds > self.deadline_s:
                raise LLMError('budget', '한도 대기가 사건 시간 예산을 초과함')
            state['quota_wait_s'] = state.get('quota_wait_s', 0) + seconds
            state.setdefault('wait_spans', []).append({'span_id': uuid.uuid4().hex,
                'trace_id': state['trace_id'], 'parent': 'quota_wait', 'wait_s': seconds,
                **(_trace.get() or {})})


def attempt(provider, role, prompt, schema, model, effort, timeout, *, account=None):
    runtime = _active.get()
    reservation = runtime.reserve(role, prompt, model, timeout, account) if runtime else None
    usage, kind, duration = Usage(), None, 0
    try:
        from .llm.progress import provider_attempt
        with provider_attempt(role, model, getattr(provider, "name", "custom")):
            result = provider.complete(role, prompt, schema, model, effort, timeout)
        usage, duration = result.usage, result.duration_ms
        return result
    except Exception as exc:
        usage, kind, duration = getattr(exc, 'usage', Usage()), getattr(exc, 'kind', 'provider'), getattr(exc, 'duration_ms', 0) or 0
        raise
    finally:
        if reservation:
            runtime.finish(*reservation, usage, kind)
        records = _records.get()
        if records is not None:
            records.append((usage, duration))


def complete(runtime, provider, role, prompt, schema, model, effort, timeout):
    records = []
    token, record_token = _active.set(runtime), _records.set(records)
    try:
        try:
            result = (provider.complete(role, prompt, schema, model, effort, timeout)
                      if getattr(provider, 'manages_provider_attempts', False)
                      else attempt(provider, role, prompt, schema, model, effort, timeout))
        except LLMError as exc:
            exc.usage = aggregate_usage([u for u, _ in records])
            exc.duration_ms = sum(d for _, d in records)
            exc.provider_attempts = len(records)
            raise
        result.usage = aggregate_usage([u for u, _ in records])
        result.duration_ms = sum(d for _, d in records)
        result.provider_attempts = len(records)
        return result
    finally:
        _records.reset(record_token)
        _active.reset(token)


def for_incident(cfg, iid, *, display=False):
    from sqlalchemy import select
    from . import db
    from .models import AgentRun, Incident, WorkflowRun
    from .llm import estimate_cost
    @contextmanager
    def storage():
        with db.session() as session:
            # 행 생성과 다른 실행 사이에도 사건 단위 예약을 직렬화한다.
            inc = session.scalar(select(Incident).where(Incident.id == iid).with_for_update())
            if inc is None:
                raise LLMError('budget', '사건이 없어 호출 중단')
            rows = list(session.scalars(select(WorkflowRun).where(WorkflowRun.incident_id == iid)
                                       .order_by(WorkflowRun.id).with_for_update()))
            row = next((r for r in rows if (r.snapshot or {}).get('incident_runtime')), None)
            row = row if row is not None else (rows[-1] if rows else None)
            if row is None and display:
                row = WorkflowRun(incident_id=iid, status='succeeded', phase='interpretation',
                                  reason='화면 해석 예산 원장; 분석·조치 실행 아님', snapshot={})
                session.add(row)
                session.flush()
                rows.append(row)
            if row is None:
                raise LLMError('budget', '사건 실행 원장이 없어 호출 중단')
            snapshot = dict(row.snapshot or {})
            state = dict(snapshot.get('incident_runtime') or {})
            if not state:
                # 과거 사건도 이미 관측된 호출·비용을 0으로 리셋하지 않는다.
                history = list(session.scalars(select(AgentRun).where(AgentRun.incident_id == iid)))
                state = {'provider_attempts': 0, 'charged_usd': 0, 'analysis_time_s': 0,
                         'legacy_unobserved_cost_runs': 0}
                for prior in history:
                    attempts = getattr(prior, 'attempts', None)
                    attempts = attempts if type(attempts) is int and attempts >= 0 else 1
                    if not attempts:
                        continue
                    state['provider_attempts'] += attempts
                    cost = getattr(prior, 'estimated_cost_usd', None)
                    if cost is None:
                        cost = estimate_cost(cfg, getattr(prior, 'model', ''), Usage(
                            input_tokens=getattr(prior, 'input_tokens', None),
                            output_tokens=getattr(prior, 'output_tokens', None)))
                    if cost is None:
                        state['legacy_unobserved_cost_runs'] += 1
                    else:
                        state['charged_usd'] += cost
                    duration = getattr(prior, 'duration_ms', None)
                    state['analysis_time_s'] += (max(0, duration / 1000) if duration is not None
                                                 else cfg.llm.timeout_s * attempts)
            state['_display_only'] = display and not any(r.status in ('running', 'resume_pending') for r in rows)
            yield state
            state.pop('_display_only', None)
            row.snapshot = {**snapshot, 'incident_runtime': state}
    return IncidentRuntime(cfg, storage)
