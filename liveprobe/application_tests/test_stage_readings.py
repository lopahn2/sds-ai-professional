import copy
import json

from app.application.stage_readings import file_excerpt, stage_document


def test_stage_projection_preserves_audit_metadata_without_raw_evidence_or_supplements():
    source = {'id': 'incident-8', 'state': 'awaiting_prd_approval', 'reason': 'RAW REASON', 'data': {
        'source_snapshot': {'files': {'business.py': 'RAW SOURCE'}},
        'collection': {'evidence': [{'id': 'e:1', 'text': 'RAW LOG', 'observed_at': 100}]},
        'patch': {'diff': '--- a/business.py\n+++ b/business.py\n@@\n+RAW DIFF\n--- a/secret.json\n+++ b/secret.json',
                  'tests_added': ['tests/test_inventory.py::test_reserve'], 'summary_ko': '예약 검사를 수정했습니다.'},
        'supplements': [{'status': 'pending', 'notes_ko': 'RAW OPERATOR NOTE'}],
        'current_version_checks': [{'release_id':'r1','status':'passed','receipt':{'body':'RAW RECEIPT'}}],
        'rounds': [{'round': 1, 'test_build': {'ok': True}, 'source_snapshot': 'RAW SNAPSHOT'}],
        'display_readings': {'evidence/0':{'state':'ready','ko':'연결을 확인했습니다.'}},
    }}
    original = copy.deepcopy(source)
    result = stage_document(source)
    assert source == original
    assert 'RAW' not in json.dumps(result)
    assert result['data']['collection']['evidence'] == [{'id':'e:1','observed_at':100,'has_original':True}]
    assert result['data']['supplements'] == [{'status':'pending'}]
    assert result['data']['patch']['changed_files'] == ['business.py']
    assert result['data']['current_version_checks'] == [{'release_id':'r1','status':'passed'}]
    assert result['data']['rounds'][0]['test_build'] == {'ok':True}


def test_file_excerpt_is_scoped_to_path_and_redacted():
    diff = ('--- a/one.py\n+++ b/one.py\n@@\n+value=1\n'
            '--- a/two.py\n+++ b/two.py\n@@\n+password=some-private-value')
    first = file_excerpt(diff, 'one.py')
    assert 'value=1' in first['diff'] and 'two.py' not in first['diff']
    second = file_excerpt(diff, 'two.py')
    assert 'value=1' not in second['diff'] and 'some-private-value' not in second['diff']
    assert file_excerpt(diff,'three.py')['diff'] is None
