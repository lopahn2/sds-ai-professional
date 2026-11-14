"""실패 결과도 보존하되 원문·임의 확장 필드를 제거하는 정제 결과 원장.

정제는 증거 보존용이며 통과 판정이 아니다. 행이 유효한 모양이어도 실패·상충은 그대로 남긴다.
"""
import re
from .safety import test_file

HEX = re.compile(r'^[a-f0-9]{64}$')
CODE = re.compile(r'^[a-z][a-z0-9_]{1,100}$')
SYMBOL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
IMAGE = re.compile(r'^\S+@sha256:[a-f0-9]{64}$')
OUTCOMES = ('written', 'not_written', 'unknown')


def error_code(value):
    if value is None:
        return None
    code = value.get('code') if isinstance(value, dict) else value
    return code if isinstance(code, str) and CODE.fullmatch(code) else 'error_code_invalid'


def digest(value):
    return value if isinstance(value, str) and HEX.fullmatch(value) else None


def tests_evidence(value):
    value = value if isinstance(value, dict) else {}
    safe = {key: value.get(key) if type(value.get(key)) is bool else None
            for key in ('before_failed', 'after_passed', 'regression_executed')}
    safe.update({key: value.get(key) if type(value.get(key)) is int and value[key] >= 0 else None
                 for key in ('total', 'failed')})
    safe['report_sha256'] = digest(value.get('report_sha256'))
    safe['executed_tests'], safe['rows_rejected'] = [], 0
    rows = value.get('executed_tests')
    if not isinstance(rows, list):
        return safe
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get('id'), str) or test_file(row['id']) is None or
            test_file(row['id']) != row.get('file') or row.get('status') not in ('passed', 'failed', 'skipped', 'error') or
            row.get('phase') not in ('before', 'after')):
            safe['rows_rejected'] += 1
            continue
        saved = {key: row[key] for key in ('id', 'file', 'status', 'phase')}
        if 'error_code' in row:
            saved['error_code'] = error_code(row['error_code'])
        safe['executed_tests'].append(saved)
    return safe


def target_rows(value):
    if not isinstance(value, list):
        return []
    result = []
    for row in value:
        if not isinstance(row, dict):
            continue
        safe = {}
        for key in ('environment', 'service', 'uid', 'resource_version', 'container', 'system'):
            item = row.get(key)
            if isinstance(item, str) and SYMBOL.fullmatch(item):
                safe[key] = item
        for key in ('image', 'previous_image'):
            item = row.get(key)
            if isinstance(item, str) and len(item) <= 1000 and IMAGE.fullmatch(item):
                safe[key] = item
        if row.get('write_outcome') in OUTCOMES:
            safe['write_outcome'] = row['write_outcome']
        if safe:
            result.append(safe)
    return result


def verification_rows(value):
    if not isinstance(value, list):
        return []
    result = []
    for row in value:
        if not isinstance(row, dict):
            continue
        safe = {}
        for key in ('environment', 'service'):
            item = row.get(key)
            if isinstance(item, str) and SYMBOL.fullmatch(item):
                safe[key] = item
        image = row.get('image')
        if isinstance(image, str) and len(image) <= 1000 and IMAGE.fullmatch(image):
            safe['image'] = image
        business = row.get('business')
        if isinstance(business, dict):
            receipt = {}
            if type(business.get('ok')) is bool:
                receipt['ok'] = business['ok']
            profile = business.get('profile_id')
            if isinstance(profile, str) and SYMBOL.fullmatch(profile):
                receipt['profile_id'] = profile
            status = business.get('status_code')
            if type(status) is int and 100 <= status <= 599:
                receipt['status_code'] = status
            if digest(business.get('body_sha256')):
                receipt['body_sha256'] = business['body_sha256']
            if 'error' in business:
                receipt['error_code'] = error_code(business['error'])
            safe['business'] = receipt
        if 'error' in row:
            safe['error_code'] = error_code(row['error'])
        if safe:
            result.append(safe)
    return result
