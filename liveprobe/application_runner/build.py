"""추가 시험을 먼저 실행한 후 소스 변경·재시험·한 번의 최종 빌드."""
import json
import ast
import re
import shutil
from pathlib import Path
import uuid
from .profiles import digest, manifest_image
from .source import safe_path, source_files, parse_patch, apply_changes


def test_command(profile, paths):
    language = profile['language']
    if language == 'node':
        if not paths or any(not p.startswith('test/') or not p.endswith('.js') for p in paths):
            raise ValueError('node_test_path_required')
        return ['node','--test','--test-reporter=tap',*sorted(paths)]
    if language == 'java':
        return ['mvn','-o','-B','-Dstyle.color=never','test']
    if language=='domain':return ['python','-B','/runner/unittest_harness.py']
    return ['python','-B','-m','pytest','-q','-p','no:cacheprovider','-o','junit_family=xunit1','--junitxml=/reports/pytest.xml','tests']


def final_manifest(inspection, repository):
    candidates = [i for i in inspection.get('RepoDigests',[]) if i.startswith(repository+'@')]
    if len(set(candidates)) != 1:
        raise ValueError('manifest_digest_not_observed')
    return manifest_image(candidates[0], repository)


def changed_test_ids(old,new,language):
    def ids(text):
        if language in ('python','domain'):
            result={}
            tree=ast.parse(text)
            for cls in tree.body:
                if isinstance(cls,(ast.FunctionDef,ast.AsyncFunctionDef)) and cls.name.startswith('test_'):
                    result[cls.name]=ast.dump(cls,include_attributes=False)
                if isinstance(cls,ast.ClassDef):
                    for node in cls.body:
                        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name.startswith('test_'):
                            result[cls.name+'::'+node.name]=ast.dump(node,include_attributes=False)
            return result
        if language=='node':
            return {m[1]:m[0] for m in re.finditer(r"\b(?:test|it)\(\s*['\"]([^'\"]+)['\"][\s\S]*?(?=\b(?:test|it)\(|\Z)",text)}
        return {m[1]:m[0] for m in re.finditer(r'@Test\b[\s\S]*?void\s+(\w+)\s*\([^)]*\)[\s\S]*?(?=@Test\b|\Z)',text)}
    previous=ids(old);current=ids(new)
    return sorted(name for name,body in current.items() if previous.get(name)!=body)


def cleanup_workspace(workspace,cleanup_root,proof_file=None):
    from .worker import durable_write
    status={'path':str(workspace.resolve())}
    if proof_file:
        status.update(status='retained',reason='provenance_source_reference',proof_file=str(Path(proof_file).resolve()))
    else:
        try:
            owner=workspace/'.application-owner'
            owned=not workspace.is_symlink() and not owner.is_symlink() and owner.read_text()==workspace.name
        except OSError:owned=False
        if not owned:status.update(status='unresolved',reason='workspace_ownership_mismatch')
        else:
            try:shutil.rmtree(workspace);status.update(status='removed',reason='no_provenance_reference')
            except OSError:status.update(status='unresolved',reason='workspace_cleanup_failed')
    durable_write(Path(cleanup_root)/(workspace.name+'.json'),{'workspace':status})


class Builder:
    def __init__(self, workspace_root, transport, provenance_root=None):
        self.workspace_root=Path(workspace_root)
        self.provenance_root=Path(provenance_root) if provenance_root else self.workspace_root/'provenance'
        self.transport=transport

    def run(self, source, patch, profile):
        from .worker import durable_write
        workspace=self.workspace_root/('application-'+uuid.uuid4().hex)
        workspace.mkdir(mode=0o700,parents=True)
        (workspace/'.application-owner').write_text(workspace.name)
        retained={'proof':None}
        try:
            return self._run(source,patch,profile,workspace,retained)
        finally:cleanup_workspace(workspace,self.workspace_root/'cleanup',retained['proof'])

    @staticmethod
    def assert_tree(workspace,expected):
        # source_files의 제외 규칙으로 새 숨김 파일/산출물을 빌드 문맥에 숨기지 않는다.
        observed={}
        try:
            for path in workspace.rglob('*'):
                if path.is_symlink():raise ValueError('source_tree_changed')
                if path.is_dir():continue
                name=path.relative_to(workspace).as_posix()
                if name=='.application-owner':
                    if path.read_text()!=workspace.name:raise ValueError('source_tree_changed')
                    continue
                safe_path(workspace,name)
                observed[name]=digest(path.read_bytes())
        except (ValueError,OSError):raise ValueError('source_tree_changed') from None
        if observed!={p:digest(t.encode()) for p,t in expected.items()}:raise ValueError('source_tree_changed')

    def _run(self, source, patch, profile, workspace, retained):
        if not source.get('provenance_verified') or source['profile_id'] != profile['id']:
            raise ValueError('source_unverified')
        if patch['source_snapshot_id'] != source['id'] or patch['diff_sha256'] != digest(patch['diff'].encode()):
            raise ValueError('patch_snapshot_mismatch')
        for name,text in source['files'].items():
            path=safe_path(workspace,name)
            if digest(text.encode()) != source['file_sha256'][name]:
                raise ValueError('source_mismatch')
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(text)
        changes=parse_patch(patch['diff'],workspace,profile['test_root'])
        if 'Dockerfile' in changes:
            raise ValueError('build_recipe_patch_forbidden')
        from .reports import test_file
        added=patch.get('tests_added',[])
        if not added or len(set(added))!=len(added):raise ValueError('added_regression_required')
        files=sorted({test_file(identifier,profile['language']) for identifier in added})
        if any(name not in changes or not name.startswith(profile['test_root']+'/') for name in files):
            raise ValueError('added_regression_required')
        for identifier in added:
            file=test_file(identifier,profile['language'])
            name=identifier.split('#',1)[1] if profile['language']=='java' else identifier.split('::',1)[1]
            if name not in changed_test_ids(source['files'].get(file,''),changes[file],profile['language']):
                raise ValueError('added_test_method_required')
        modes=['trade','delivery','business'] if profile['language']=='domain' else [None]
        apply_changes(workspace,changes,True,profile['test_root'])
        expected_before={**source['files'],**{p:t for p,t in changes.items() if p.startswith(profile['test_root']+'/')}}
        self.assert_tree(workspace,expected_before)
        before=[]
        for mode in modes:
            selected={**profile,'mode':mode} if mode else profile
            try:report=self.transport.tests(workspace,selected,'before',added)
            finally:self.assert_tree(workspace,expected_before)
            before.append(report)
        for report in before:
            self.validate_report(report,added,phase='before',regression_ids=None)
        # Runtime test errors count only when the exact added ID was observed;
        # collection/config failures have no matching rows and fail validation.
        if not all(r['exit_code']==1 and r['failed']+r.get('errors',0)>0 for r in before):
            raise ValueError('before_not_reproduced')
        apply_changes(workspace,changes,False,profile['test_root'])
        expected_after={**source['files'],**changes}
        self.assert_tree(workspace,expected_after)
        after=[]
        for mode in modes:
            selected={**profile,'mode':mode} if mode else profile
            try:report=self.transport.tests(workspace,selected,'after',added)
            finally:self.assert_tree(workspace,expected_after)
            after.append(report)
        for report in after:
            self.validate_report(report,added,phase='after',regression_ids=None)
        if not all(r['exit_code']==0 and r['failed']==0 and not r.get('errors',0) for r in after):
            raise ValueError('after_not_passed')
        self.assert_tree(workspace,expected_after)
        files=expected_after
        tree=digest({p:digest(t.encode()) for p,t in files.items()})
        receipt=self.transport.build(workspace,profile,tree)
        self.assert_tree(workspace,expected_after)
        manifest_image(receipt['image'],profile['image_repository'])
        if not receipt.get('manifest_observed') or receipt['source_tree_sha256'] != tree:
            raise ValueError('build_receipt_mismatch')
        from .worker import durable_write
        proof={'image':receipt['image'],'profile_id':profile['id'],'base_commit':None,
               'file_sha256':{p:digest(t.encode()) for p,t in files.items()},
               'source_root':str(workspace.resolve()),'provenance_kind':'local_build_receipt'}
        proofpath=self.provenance_root/(profile['service']+'-'+receipt['image'].rsplit(':',1)[1]+'.json')
        retained['proof']=str(proofpath.resolve())
        durable_write(proofpath,proof)
        reports={'before':before,'after':after,'modes':modes}
        integrity={'status':'fixed_source_plus_exact_patch',
                   'before_file_sha256':{p:digest(t.encode()) for p,t in expected_before.items()},
                   'after_file_sha256':proof['file_sha256']}
        (workspace/'.application-receipt.json').write_text(json.dumps({'tests':reports,'build':receipt,
            'source_integrity':integrity,'semantic_adequacy':'not_assessed'},ensure_ascii=False))
        return {'ok':True,'tests':{'before_failed':True,'after_passed':True,'regression_executed':True,
                'total':sum(r['total'] for r in after),'failed':0,'executed_tests':[t for r in before+after for t in r['executed_tests']],
                'report_sha256':digest(reports)},
                'artifact':{'image':receipt['image'],'source_snapshot_id':source['id'],'diff_sha256':patch['diff_sha256'],
                'source_tree_sha256':tree,'build_receipt_sha256':digest(receipt)},'error':None}

    @staticmethod
    def validate_report(report,added,phase=None,regression_ids=None):
        if not isinstance(report.get('total'),int) or report['total']<1:
            raise ValueError('empty_test_report')
        rows=report.get('executed_tests',[])
        # Legacy helper callers may describe a filename+method map, but native
        # pipeline always supplies canonical IDs, never filename-only evidence.
        expected=[file+'::'+name.replace('.','::') for file,names in regression_ids.items() for name in names] if regression_ids else added
        canonical=all('::' in identifier or '#' in identifier for identifier in expected)
        if not canonical:
            if not set(added).issubset({r.get('file') for r in rows if r.get('status') in ('passed','failed','error')}):
                raise ValueError('regression_not_executed')
            selected=[r for r in rows if r.get('file') in added]
        else:
            selected=[]
            for identifier in expected:
                matching=[r for r in rows if r.get('id')==identifier and r.get('phase')==phase]
                if not matching:
                    raise ValueError('regression_id_not_executed' if regression_ids else 'regression_not_executed')
                if len(matching)!=1:raise ValueError('duplicate_test_id')
                selected.extend(matching)
        if phase=='before' and not all(r.get('status') in ('failed','error') for r in selected):raise ValueError('regression_not_failed')
        if phase=='after' and not all(r.get('status')=='passed' for r in selected):raise ValueError('regression_not_passed')
