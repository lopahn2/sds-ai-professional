"""2026-11-18 model lineup: a ConfigMap written for the previous lineup runs on the current tier ids.

Only model placement fields are mapped. Business resources, namespaces, allowlists, other providers, policies and
prices keep their exact values even when a name happens to equal a previous model id.
"""
from __future__ import annotations

import copy

import yaml

from app.config import GPT_HIGH, GPT_LOW, GPT_MID, AppConfig, current_lineup, load_app_config

OLD_MID, OLD_HIGH = 'gpt-6.1-sol', 'gpt-6-astra'


def _legacy_config() -> dict:
    return {
        # Virtual business resources deliberately named like previous model ids.
        'dependencies': {OLD_MID: [OLD_HIGH], 'order': [OLD_MID]},
        'target_namespaces': ['dev', OLD_HIGH],
        'historical_namespaces': [OLD_MID],
        'safe_actions': {'deployments': [OLD_MID, OLD_HIGH],
                         'ownership_labels': {'app': OLD_HIGH},
                         'managed_label': {'liveprobe.io/managed': 'true'},
                         'configmap_versions': {OLD_MID: [{'name': f'{OLD_MID}-v1', 'sha256': 'a' * 64}]}},
        'llm': {
            'provider': 'codex_cli',
            'roles': {'router': {'model': GPT_LOW, 'effort': 'low'},
                      'signal_analyst': {'model': GPT_LOW, 'effort': 'low'},
                      'result_verifier': {'model': OLD_MID, 'effort': 'low'},
                      'evidence_curator': {'model': OLD_MID, 'effort': 'medium'},
                      'safety_verifier': {'model': OLD_HIGH, 'effort': 'xhigh'}},
            'provider_roles': {
                'codex_cli': {'router': {'model': GPT_LOW, 'effort': 'low'},
                              'safety_verifier': {'model': OLD_HIGH, 'effort': 'xhigh'}},
                # A non-OpenAI provider keeps exactly what was written.
                'claude_cli': {'router': {'model': 'claude-haiku-4-5', 'effort': None}}},
            'prices': {OLD_MID: {'input': 2.0, 'cached_input': 0.1, 'output': 10.0},
                       OLD_HIGH: {'input': 10.0, 'cached_input': 1.0, 'output': 50.0},
                       GPT_LOW: {'input': 0.1, 'cached_input': 0.01, 'output': 0.5},
                       GPT_MID: {'input': 2.0, 'cached_input': 0.1, 'output': 10.0},
                       GPT_HIGH: {'input': 8.0, 'cached_input': 0.4, 'output': 40.0}},
        },
        'assist': {'operator_planner': {'model': OLD_MID, 'effort': 'medium'},
                   'code_fixer': {'model': OLD_MID, 'effort': 'high'}},
        'router': {'model_quality': {GPT_LOW: 0.5, OLD_MID: 0.75, OLD_HIGH: 1.0},
                   'model_throughput': {GPT_LOW: 1.0, OLD_MID: 0.6, OLD_HIGH: 0.3}},
        'workflows': {'model_ladder': {'openai': [GPT_LOW, OLD_MID, OLD_HIGH],
                                       'anthropic': ['claude-haiku-4-5', 'claude-sonnet-5', 'claude-opus-5-5']},
                      'group_roles': {'openai': {'k8s_analyst': {'model': OLD_HIGH, 'effort': 'high'},
                                                 'coordinator': {'model': OLD_MID, 'effort': 'medium'}},
                                      'anthropic': {'k8s_analyst': {'model': 'claude-opus-5-5', 'effort': 'high'}}}},
    }


def test_only_placement_fields_are_mapped():
    raw = _legacy_config()
    before = copy.deepcopy(raw)
    out = current_lineup(raw)
    assert raw == before, 'input data must not be mutated'
    # Placements -> current ids.
    assert out['llm']['roles']['result_verifier']['model'] == GPT_MID
    assert out['llm']['roles']['safety_verifier'] == {'model': GPT_HIGH, 'effort': 'xhigh'}
    assert out['llm']['provider_roles']['codex_cli']['safety_verifier']['model'] == GPT_HIGH
    assert out['assist']['code_fixer'] == {'model': GPT_MID, 'effort': 'high'}
    assert out['workflows']['model_ladder']['openai'] == [GPT_LOW, GPT_MID, GPT_HIGH]
    assert out['workflows']['group_roles']['openai']['k8s_analyst']['model'] == GPT_HIGH
    assert out['router']['model_quality'] == {GPT_LOW: 0.5, GPT_MID: 0.75, GPT_HIGH: 1.0}
    assert out['router']['model_throughput'] == {GPT_LOW: 1.0, GPT_MID: 0.6, GPT_HIGH: 0.3}
    # Same-named business resources, namespaces and allowlists are preserved byte for byte.
    for key in ('dependencies', 'target_namespaces', 'historical_namespaces', 'safe_actions'):
        assert out[key] == before[key], key
    # Other providers, prices and the anthropic ladder/groups stay as written.
    assert out['llm']['provider_roles']['claude_cli'] == before['llm']['provider_roles']['claude_cli']
    assert out['llm']['prices'] == before['llm']['prices']
    assert out['workflows']['model_ladder']['anthropic'] == before['workflows']['model_ladder']['anthropic']
    assert out['workflows']['group_roles']['anthropic'] == before['workflows']['group_roles']['anthropic']


def test_estimate_key_is_not_overwritten_when_current_id_is_already_listed():
    out = current_lineup({'router': {'model_quality': {OLD_HIGH: 0.2, GPT_HIGH: 0.95}}})
    assert out['router']['model_quality'] == {GPT_HIGH: 0.95}


def test_ladder_does_not_duplicate_when_both_ids_are_listed():
    out = current_lineup({'workflows': {'model_ladder': {'openai': [GPT_LOW, OLD_MID, GPT_MID, OLD_HIGH]}}})
    assert out['workflows']['model_ladder']['openai'] == [GPT_LOW, GPT_MID, GPT_HIGH]


def test_legacy_configmap_file_loads_with_current_lineup(tmp_path):
    path = tmp_path / 'config.yaml'
    data = _legacy_config()
    # Namespaces must stay valid identifiers for AppConfig; keep the same-named resources elsewhere.
    data['target_namespaces'], data['historical_namespaces'] = ['dev', 'prd'], ['stg']
    path.write_text(yaml.safe_dump(data, allow_unicode=True))
    cfg = load_app_config(str(path))
    assert isinstance(cfg, AppConfig)
    assert cfg.llm.roles['safety_verifier'].model == GPT_HIGH
    assert cfg.assist.code_fixer.model == GPT_MID
    assert cfg.workflows.model_ladder['openai'] == [GPT_LOW, GPT_MID, GPT_HIGH]
    assert cfg.safe_actions.deployments == [OLD_MID, OLD_HIGH]
    assert cfg.dependencies[OLD_MID] == [OLD_HIGH]
    # Previous-lineup prices remain for estimating recorded runs; the current ids carry the new price.
    assert cfg.llm.prices[OLD_HIGH]['input'] == 10.0
    assert cfg.llm.prices[GPT_HIGH]['input'] == 8.0


def test_shipped_configmap_uses_current_lineup():
    from pathlib import Path
    shipped = yaml.safe_load((Path(__file__).resolve().parents[2] / 'deploy/environments/liveprobe-config.yaml').read_text())
    models = {rm['model'] for rm in shipped['llm']['roles'].values()}
    assert models == {GPT_LOW, GPT_MID, GPT_HIGH}
    assert current_lineup(shipped) == shipped
