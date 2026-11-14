"""직접 등록한 읽기 오류 fixture만 격리 복사하고 로컬 빌드한다."""
import json
from pathlib import Path
import uuid
from .profiles import digest,manifest_image
from .source import source_files,safe_path,parse_patch,apply_changes,snapshot
from .worker import durable_write

FIXTURES={'catalog-title-key':{'service':'catalog','patch':'deploy/samsung/application-fixtures/catalog-title-key.patch'}}


class FixtureBuilder:
    def __init__(self,registry,host,state_root):
        self.registry=registry;self.host=host;self.state_root=Path(state_root).resolve()

    def run(self,fixture,environment,dry_run=False):
        if environment!='dev':raise ValueError('fixture_dev_only')
        if fixture not in FIXTURES:raise ValueError('fixture_unregistered')
        registration=FIXTURES[fixture];profile=self.registry.get(registration['service'])
        if dry_run:return {'ok':True,'fixture':fixture,'environment':environment,'writes':0,'dry_run':True}
        workspace=self.state_root/'workspaces'/('fixture-'+uuid.uuid4().hex)
        workspace.mkdir(mode=0o700,parents=True)
        (workspace/'.application-owner').write_text(workspace.name)
        retained={'proof':None}
        from .build import cleanup_workspace
        try:return self._run(fixture,environment,registration,profile,workspace,retained)
        finally:cleanup_workspace(workspace,workspace.parent/'cleanup',retained['proof'])

    def _run(self,fixture,environment,registration,profile,workspace,retained):
        root=self.registry.root/profile['source_root']
        files=source_files(root)
        original={p:digest(t.encode()) for p,t in files.items()}
        for name,text in files.items():
            path=safe_path(workspace,name);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
        diff=(self.registry.root/registration['patch']).read_text()
        changes=parse_patch(diff,workspace,profile['test_root'])
        if set(changes)!={'app.py'}:raise ValueError('fixture_patch_scope_invalid')
        apply_changes(workspace,changes,False,profile['test_root'])
        files=source_files(workspace);hashes={p:digest(t.encode()) for p,t in files.items()};tree=digest(hashes)
        receipt=self.host.build(workspace,profile,tree)
        image=manifest_image(receipt['image'],profile['image_repository'])
        if not receipt.get('manifest_observed') or receipt.get('source_tree_sha256')!=tree:
            raise ValueError('build_receipt_mismatch')
        if self.host.observe_manifest(image)!=image:raise ValueError('source_manifest_mismatch')
        # A dirty local baseline is not a git commit. Null is truthful and valid.
        proof={'image':image,'profile_id':profile['id'],'base_commit':None,'file_sha256':hashes,
               'source_root':str(workspace),'provenance_kind':'local_build_receipt'}
        snapshot(workspace,profile,image,proof)  # exact same proof consumer as source
        if original!={p:digest(t.encode()) for p,t in source_files(root).items()}:
            raise ValueError('fixture_original_source_changed')
        proofpath=self.state_root/'provenance'/(profile['service']+'-'+image.rsplit(':',1)[1]+'.json')
        retained['proof']=str(proofpath)
        durable_write(proofpath,proof)
        durable_write(workspace/'.application-receipt.json',{'fixture':fixture,'diff_sha256':digest(diff.encode()),
                      'original_file_sha256':original,'build':receipt,'proof':proof})
        return {'ok':True,'fixture':fixture,'environment':environment,'image':image,'proof_file':str(proofpath)}

    def deploy(self,result,kube):
        from .deploy import Deployer,summarize
        if result.get('environment')!='dev' or result.get('fixture') not in FIXTURES:raise ValueError('fixture_dev_only')
        service=FIXTURES[result['fixture']]['service']
        profile=self.registry.get(service);proof=json.loads(Path(result['proof_file']).read_text())
        snapshot(proof['source_root'],profile,result['image'],proof)
        if self.host.observe_manifest(result['image'])!=result['image']:raise ValueError('source_manifest_mismatch')
        target=summarize(kube.read('dev',service),'dev',service,self.registry)
        target['previous_image']=target.pop('image')
        release='fixture-'+uuid.uuid4().hex
        payload={'stage':'dev','targets':[target],'artifact':{'image':result['image'],'affected_services':[service]},
                 'authorization_kind':'auto_dev','release_id':release,'attempt_id':release,'idempotency_key':release}
        deployer=Deployer(self.registry,kube,self.state_root/'deployments')
        durable_write(self.state_root/'fixture-deployments'/(release+'.json'),payload)
        durable_write(self.state_root/'last-fixture-deployment.json',{'fixture':result['fixture'],'payload':payload})
        return deployer.deploy(payload,1)

    def confirm_deployment(self,kube):
        from .deploy import Deployer
        path=self.state_root/'last-fixture-deployment.json'
        if path.is_symlink() or not path.is_file():raise ValueError('fixture_deployment_missing')
        saved=json.loads(path.read_text());payload=saved['payload']
        if saved['fixture'] not in FIXTURES or payload['stage']!='dev':raise ValueError('fixture_dev_only')
        deployer=Deployer(self.registry,kube,self.state_root/'deployments')
        result=deployer.reconcile(payload,1)  # read-only UID/manifest/rollout confirmation
        if result['ok'] and result['write_outcome']=='written':deployer.release_confirmed(payload)
        return result
