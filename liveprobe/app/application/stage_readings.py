"""Shape operator readings without transferring source snapshots, raw logs or patch bodies."""
from .safety import safe_source_path, scrub


def file_excerpt(diff, path):
    from ..redact import redact
    lines = diff.splitlines()
    headers = [i for i in range(len(lines)-1) if lines[i].startswith(('--- a/', '--- /dev/null'))
               and lines[i+1].startswith(('+++ b/', '+++ /dev/null'))]
    for offset, start in enumerate(headers):
        if lines[start:start+2] not in (['--- a/'+path, '+++ b/'+path], ['--- /dev/null', '+++ b/'+path],
                                       ['--- a/'+path, '+++ /dev/null']):
            continue
        end = headers[offset+1] if offset+1 < len(headers) else len(lines)
        while end > start+2 and lines[end-1].startswith(('index ', 'diff --git ')):
            end -= 1
        value = redact(scrub('\n'.join(lines[start:end])))
        return {'file': path, 'diff': value[:16000], 'truncated': len(value) > 16000}
    return {'file': path, 'diff': None, 'truncated': False}


def select(data, *keys):
    return {key: data[key] for key in keys if isinstance(data, dict) and key in data}


def stage_document(document):
    data = document.get('data') or {}
    collection = data.get('collection') or {}
    items = []
    for row in collection.get('evidence') or []:
        items.append({**select(row, 'id', 'service', 'environment', 'source', 'observed_at', 'quality'),
                      'has_original': bool(row.get('text'))})
    patch = data.get('patch') or {}
    paths = [line[6:] for line in str(patch.get('diff') or '').splitlines()
             if line.startswith('+++ b/') or line.startswith('--- a/')]
    patch_view = select(patch, 'summary_ko', 'risk_ko', 'tests_added', 'diff_sha256')
    patch_view['changed_files'] = list(dict.fromkeys(path for path in paths if safe_source_path(path)))
    routes = data.get('analysis_routing') or {}
    display = data.get('display_readings') or {}
    projected = select(data, 'round', 'release', 'test_build', 'unified_origin', 'merge_review')
    projected.update(
        supplements=[select(row, 'status') for row in data.get('supplements') or []],
        current_version_checks=[select(row, 'status', 'observed_at', 'image', 'operation', 'release_id',
                                       'promotion_unnecessary', 'reason_ko') for row in data.get('current_version_checks') or []],
        patch=patch_view, analysis_routing=select(routes, 'model_level', 'effective_level'),
        collection={'evidence': items, 'observed_at': collection.get('observed_at'),
                    **select(collection, 'log_window')},
        display_readings={key: value for key, value in display.items()
                          if key.startswith(('evidence/', 'patch/', 'signal/'))},
        rounds=[select(row, 'round', 'closed_at', 'patch_summary_ko', 'release', 'test_build', 'merge_review')
                for row in data.get('rounds') or []])
    return {**select(document, 'id', 'state', 'created_at', 'environment', 'service', 'system',
                     'handoff_classification', 'error'),
            'reason': (display.get('signal/reason') or {}).get('ko') or '', 'data': projected}
