"""앱 전용 출력 schema. 모델은 실행 명령·빌드 이미지·대상을 선택하지 못한다."""
CITATION = {'type': 'object', 'properties': {'ref': {'type': 'string'}, 'quote': {'type': 'string'}},
            'required': ['ref', 'quote'], 'additionalProperties': False}
CITATIONS = {'type': 'array', 'items': CITATION, 'minItems': 1, 'maxItems': 20}


def schema(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


CLASSIFIER = schema({'level': {'type': 'integer', 'minimum': 1, 'maximum': 3},
    'risk': {'type': 'string', 'enum': ['low', 'medium', 'high']}, 'reason_ko': {'type': 'string'},
    'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1}, 'citations': CITATIONS})
ANALYSIS = schema({'summary_ko': {'type': 'string'}, 'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1},
    'fixable': {'type': 'boolean'}, 'unresolved': {'type': 'array', 'items': {'type': 'string'}}, 'citations': CITATIONS})
REVIEW = schema({'summary_ko': {'type': 'string'}, 'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1},
    'accepted': {'type': 'boolean'}, 'unresolved': {'type': 'array', 'items': {'type': 'string'}}, 'citations': CITATIONS})
# Optional observations have no effect on acceptance. Only unresolved blockers stop execution.
for role_schema in (ANALYSIS, REVIEW):
    role_schema['properties']['follow_up'] = {'type': 'array', 'items': {'type': 'string'}}

PATCH = schema({'source_snapshot_id': {'type': 'string'}, 'diff': {'type': 'string', 'maxLength': 100000},
    'summary_ko': {'type': 'string'}, 'risk_ko': {'type': 'string'},
    'tests_added': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1},
    'code_refs': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1}})
