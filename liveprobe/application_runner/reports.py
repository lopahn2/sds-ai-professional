"""Native JUnit/TAP만 정규 ID로 변환한다. 콘솔 파일명은 proof가 아니다."""
import re
import xml.etree.ElementTree as ET


def junit_rows(text,language,phase):
    root=ET.fromstring(text);rows=[]
    for case in root.iter('testcase'):
        name=case.get('name','');cls=case.get('classname','')
        if not name:raise ValueError('native_test_id_missing')
        if language=='java':
            file='src/test/java/'+cls.replace('.','/')+'.java';identifier=cls+'#'+name
        else:
            file=case.get('file')
            if not file:
                parts=cls.split('.')
                end=next((i+1 for i,p in enumerate(parts) if p.startswith('test_')),None)
                if end is None:raise ValueError('native_test_file_missing')
                file='/'.join(parts[:end])+'.py'
            module=file[:-3].replace('/','.')
            owner=cls[len(module)+1:] if cls.startswith(module+'.') else ''
            identifier=file+'::'+(owner.replace('.','::')+'::' if owner else '')+name
        status=next((s for tag,s in (('error','error'),('failure','failed'),('skipped','skipped')) if case.find(tag) is not None),'passed')
        rows.append({'id':identifier,'file':file,'status':status,'phase':phase})
    return rows


def tap_rows(text,file,phase):
    rows=[]
    # Node TAP nested suites are not silently flattened into fake leaf IDs.
    # Canonical supported IDs are top-level native node:test names.
    for m in re.finditer(r'^(not ok|ok) \d+ - (.+?)(?:\s+#\s+(SKIP|TODO)\b.*)?$',text,re.M):
        rows.append({'id':file+'::'+m[2],'file':file,
                     'status':'skipped' if m[3] else 'failed' if m[1]=='not ok' else 'passed','phase':phase})
    return rows


def totals(rows):
    return {'total':len(rows),'failed':sum(r['status']=='failed' for r in rows),
            'errors':sum(r['status']=='error' for r in rows),'executed_tests':rows}


def test_file(identifier,language):
    if language=='java':
        m=re.fullmatch(r'([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+)#([\w]+)',identifier)
        if not m:raise ValueError('test_id_required')
        return 'src/test/java/'+m[1].replace('.','/')+'.java'
    if '::' not in identifier:raise ValueError('test_id_required')
    file,*names=identifier.split('::')
    if not all(names):raise ValueError('test_id_required')
    return file
