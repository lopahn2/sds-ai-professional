"""No real models/credentials/DB/cluster: fake subprocesses and new temporary SQLite."""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as NS

import pytest

from app.llm.base import LLMError, LLMResult, Usage
from app.llm.progress import PublicOutput, active, observe, provider_attempt
from app.runtime_budget import complete


def test_completed_public_fields_arrive_before_rest_and_split_secret_stays_private():
    records = []
    with observe(lambda summary, data: records.append(data)), provider_attempt('r', 'm', 'test'):
        out = PublicOutput(secrets=('a-custom-secret',))
        out.add('{"summary_ko":"분석 중 password=abc')
        assert not any(r['text'] for r in records)
        out.add('def a-custom-secret", "confidence":')
        assert '[REDACTED]' in records[-1]['text']
        assert 'abcdef' not in str(records) and 'a-custom-secret' not in str(records)
        assert '분석 중' in records[-1]['text']
        out.add('0.8,"internal":"never display"}')
        assert 'never display' not in str(records)
    assert records[-1]['phase'] == 'succeeded'


def test_nested_and_escaped_fake_field_names_do_not_become_public_output():
    rows = []
    with observe(lambda _, data: rows.append(data)), provider_attempt('r', 'm', 'test'):
        PublicOutput().add(json.dumps({'private': {'summary': 'private'}, 'other': '\"summary\":\"spoof\"'}))
    assert not any(r['text'] for r in rows)


def test_context_isolated_between_parallel_calls_and_observer_failure_is_nonfatal():
    def call(tag):
        rows = []
        with observe(lambda _, data: rows.append(data)), provider_attempt(tag, 'm', 'test'):
            PublicOutput().add(json.dumps({'summary_ko': tag}))
        assert active() is None
        return rows
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(call, ('first', 'second'))
    assert {r['role'] for r in first} == {'first'} and {r['role'] for r in second} == {'second'}
    assert first[0]['attempt_id'] != second[0]['attempt_id']
    with observe(lambda *_: (_ for _ in ()).throw(RuntimeError('UI unavailable'))), provider_attempt('r', 'm', 'test'):
        PublicOutput().add('{"summary":"ok"}')


def test_pool_failure_and_success_get_separate_attempts_without_changing_usage():
    from app.llm.codex_pool import Account, CodexPoolProvider
    def fail(*args):
        raise LLMError('quota', 'test only', usage=Usage(3, 1))
    def succeed(*args):
        PublicOutput().add('{"summary":"succeeded"}')
        return LLMResult('ok', {}, Usage(5, 2))
    pool = CodexPoolProvider([Account('first', NS(name='fake', complete=fail)),
                             Account('second', NS(name='fake', complete=succeed))])
    rows = []
    with observe(lambda _, data: rows.append(data)):
        result = complete(None, pool, 'r', 'prompt', {}, 'm', None, 5)
    assert result.usage.input_tokens == 8 and result.provider_attempts == 2
    attempts = {r['attempt_id'] for r in rows}
    assert len(attempts) == 2
    assert rows[1]['phase'] == 'failed' and rows[-1]['phase'] == 'succeeded'


def test_codex_message_observed_before_process_exit_and_private_events_ignored(tmp_path):
    import os
    from app.llm.codex_cli import CodexCLIProvider
    marker = tmp_path / 'exited'
    script = tmp_path / 'fake-codex'
    output = {'summary_ko': '공개 응답', 'summary': 'public'}
    events = [{'type':'item.completed','item':{'type':'reasoning','text':'hidden'}},
              {'type':'item.completed','item':{'type':'agent_message','text':json.dumps(output)}}]
    script.write_text('#!/usr/bin/env python3\nimport sys,json,time,pathlib\nsys.stdin.read()\n' +
        '\n'.join(f'print({json.dumps(json.dumps(e))},flush=True)' for e in events) +
        f'\ntime.sleep(0.2)\npathlib.Path({str(marker)!r}).write_text("done")\n' +
        'print(json.dumps({"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":2}}),flush=True)\n')
    script.chmod(0o700)
    received = []
    def sink(_, data):
        received.append(data)
        if data['text']:
            assert not marker.exists(), 'the message must be delivered while the process is still running'
    # Check only responding events: succeeded is correctly emitted after process termination.
    with observe(lambda s, d: sink(s, d) if d['phase'] == 'responding' else None):
        result = complete(None, CodexCLIProvider(str(script)), 'r', 'p', {'type':'object','properties':{
            'summary':{'type':'string'}, 'summary_ko':{'type':'string'}}}, 'm', None, 3)
    assert result.data == output and received and 'hidden' not in str(received)


def test_codex_stream_timeout_still_kills_process(tmp_path):
    from app.llm.codex_cli import CodexCLIProvider
    script = tmp_path / 'fake-codex'
    script.write_text('#!/bin/sh\ncat >/dev/null\nsleep 20\n')
    script.chmod(0o700)
    with observe(lambda *_: None), pytest.raises(LLMError) as caught:
        complete(None, CodexCLIProvider(str(script)), 'r', 'p', {}, 'm', None, 0.15)
    assert caught.value.kind == 'timeout'


def test_openai_field_stream_uses_public_delta_and_keeps_final_usage():
    from app.llm.openai_api import OpenAIAPIProvider
    response = NS(status='completed', output=[], output_text='{"summary":"done"}', usage=NS(input_tokens=3, output_tokens=2))
    class Stream:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def __iter__(self):
            yield NS(type='response.reasoning.delta', delta='hidden reasoning')
            yield NS(type='response.output_text.delta', delta='{"summary":"do')
            yield NS(type='response.output_text.delta', delta='ne","confidence":')
            assert any(r['text'] for r in rows)
            yield NS(type='response.output_text.delta', delta='1}')
        def get_final_response(self): return response
    client = NS(responses=NS(stream=lambda **kw: Stream()))
    rows = []
    with observe(lambda _, d: rows.append(d)):
        result = complete(None, OpenAIAPIProvider('arbitrary-key', client=client), 'r', 'p', {}, 'm', None, 3)
    assert result.usage.input_tokens == 3 and 'hidden reasoning' not in str(rows)


def test_run_stream_is_read_only_and_replays_from_cursor(tmp_path, monkeypatch):
    from isolated_sqlite import isolated_k8s_db, table_counts
    from app import db, main, runlog
    from app.models import Incident
    with isolated_k8s_db(tmp_path):
        with db.session() as session:
            inc = Incident(namespace='dev', workload='test', fingerprint='stream-test', title='test',
                           signal_reasons=[], status='analyzing')
            session.add(inc); session.flush(); iid = inc.id
        # Never use the broker's incident-event history for output chunks.
        monkeypatch.setattr(runlog.broker, 'publish', lambda *_: (_ for _ in ()).throw(AssertionError('global fanout')))
        runlog.emit(iid, 'agent_progress', 'public', {'call_id':'c', 'attempt_id':'a'})
        runlog.emit(iid, 'agent_progress', 'public 2', {'call_id':'c', 'attempt_id':'a'})
        before = table_counts()
        async def read():
            request = NS(headers={}, query_params={'since':'1'}, is_disconnected=lambda: asyncio.sleep(0, result=False))
            response = await main.run_event_stream(iid, request, 'test-user')
            event = await anext(response.body_iterator)
            assert event.event == 'run_event' and event.id == '2'
            assert json.loads(event.data)['summary'] == 'public 2'
            await response.body_iterator.aclose()
        asyncio.run(read())
        assert table_counts() == before


def test_stream_rejects_invalid_cursor_and_missing_incident(tmp_path):
    from isolated_sqlite import isolated_k8s_db
    from app import main
    from fastapi import HTTPException
    with isolated_k8s_db(tmp_path):
        for value, status in (('-1', 400), ('abc',400), ('1'*20,400), ('9'*19,400), ('0',404)):
            with pytest.raises(HTTPException) as caught:
                asyncio.run(main.run_event_stream(999, NS(headers={}, query_params={'since':value}), 'user'))
            assert caught.value.status_code == status


def test_engine_records_live_output_and_graph_keeps_exact_call_identity(tmp_path):
    from isolated_sqlite import isolated_k8s_db
    from app import db, runlog
    from app.config import AppConfig, Settings
    from app.engine import Engine
    from app.flowgraph import build_flow
    from app.llm import ROUTER_SCHEMA, RouterOutput
    from app.models import Incident
    with isolated_k8s_db(tmp_path):
        with db.session() as session:
            inc = Incident(namespace='dev', workload='test', fingerprint='engine-stream-test',
                           title='test', signal_reasons=[], status='analyzing')
            session.add(inc); session.flush(); iid = inc.id
        class Provider:
            name = 'test'
            def complete(self, *args):
                PublicOutput().add('{"rationale_ko":"근거를 비교했습니다.",')
                live = runlog.events_since(iid)
                assert any(e['kind'] == 'agent_progress' and e['detail']['text'] for e in live)
                assert not any(e['kind'] == 'agent_finished' for e in live)
                output = {'level':'NORMAL', 'rationale':'observed facts', 'confidence':0.8,
                          'rationale_ko':'근거를 비교했습니다.'}
                return LLMResult(json.dumps(output), output, Usage(2, 3))
        engine = Engine(AppConfig(), Settings(), None, Provider(), None)
        result = engine._call(iid, 'router', 'test prompt', ROUTER_SCHEMA, RouterOutput, [])
        assert result.ok
        events = runlog.events_since(iid)
        ids = {e['detail']['call_id'] for e in events if e['kind'].startswith('agent_')}
        assert len(ids) == 1
        with db.session() as session:
            router = next(n for n in build_flow(session, iid)['nodes'] if n['id'] == 'router')
        assert router['agent_run_id'] == result.agent_run_id
        assert router['call_id'] in ids


def test_stream_preserves_existing_local_access_guard():
    from app.main import app
    from fastapi.testclient import TestClient
    client = TestClient(app, base_url='http://untrusted.example')
    response = client.get('/api/incidents/999/events/stream?since=0')
    assert response.status_code == 403
