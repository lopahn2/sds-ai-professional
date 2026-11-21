"""공통 JSON 작업의 실제 제한 실행 함수. 다른 제품 API를 import하지 않는다."""
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import re
import time
from .build import Builder
from .deploy import Deployer,summarize
from .profiles import digest
from .source import snapshot
from .worker import durable_write,redact

ARTIFACT_KEYS=('image','source_snapshot_id','diff_sha256','source_tree_sha256','build_receipt_sha256')


class Executor:
    def __init__(self,registry,kube,host,state_root,config):
        self.registry=registry;self.kube=kube;self.host=host;self.state_root=Path(state_root);self.config=config
        self.guard=lambda:None
        self.observe=lambda phase,file=None: None

    def artifact_key(self,artifact):
        return digest({k:artifact[k] for k in ARTIFACT_KEYS})

    def artifact_receipt(self, key):
        return Path(self.config.get('artifact_receipts_dir') or self.state_root/'artifacts') / (key+'.json')

    def execute(self,job):
        self.guard()
        # 쓰기 transport도 호출 직전에 같은 lease 가드를 검사한다.
        self.kube.guard=self.guard;self.host.guard=self.guard
        self.kube.write_guard=getattr(self,'write_guard',self.guard)
        self.host.write_guard=getattr(self,'write_guard',self.guard)
        kind=job['kind'];payload=job['payload']
        from .worker import ROLE_KINDS
        role=self.config.get('runner_role','legacy')
        if role not in ROLE_KINDS or kind not in ROLE_KINDS[role]:raise ValueError('runner_role_job_forbidden')
        if kind=='investigate':
            from .source import map_stack
            env,service=payload['environment'],payload['service']
            profile=self.registry.get(service,payload['profile_id'])
            target=summarize(self.kube.read(env,service),env,service,self.registry)
            if target['image']!=payload['image']:raise ValueError('source_target_changed')
            self.observe('pin_source')
            source=self.source({'target':target,'profile_id':profile['id']})['source_snapshot']
            findings=[]
            for frame in payload['frames']:
                self.guard()
                try:path=map_stack(frame['file'],profile,source)
                except ValueError:continue
                lines=source['files'][path].splitlines()
                n=frame['line']
                if type(n) is not int or not 1<=n<=len(lines):continue
                self.observe('read_file',path)
                findings.append({'file':path,'line':n,'file_sha256':source['file_sha256'][path],
                                 'excerpt':redact(lines[n-1])[:700]})
            return {'ok':True,'image':source['base_image'],'snapshot_id':source['id'],'findings':findings}
        if kind=='collect':
            self.observe('inspect_resources')
            return self.collect(payload)
        if kind=='source':
            self.observe('pin_source')
            return self.source(payload)
        if kind=='test_build':return self.test_build(payload)
        if kind=='qa':
            self.observe('test_journey')
            return self.qa(job)
        if kind=='current_version_check':
            from .current_version import check_current_version
            self.observe('observe_original_request')
            return check_current_version(self.registry, self.kube, job)
        if kind=='recovery':
            # Read-only prd observation of the incident's own attributed operation (runtime lane helper).
            from .recovery_probe import run_recovery
            self.observe('observe_original_request')
            return run_recovery(self.registry,self.kube,payload)
        if kind in ('deploy','verify','reconcile'):
            original=payload
            if kind=='reconcile':
                if payload.get('original_kind')!='deploy':raise ValueError('unsupported_reconciliation')
                original=payload['original_payload']
            key=self.artifact_key(original['artifact'])
            saved=self.artifact_receipt(key)
            if not saved.is_file():raise ValueError('tested_artifact_receipt_required')
            if self.config.get('artifact_receipts_dir'):
                metadata = saved.lstat()
                if saved.is_symlink() or metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
                    raise ValueError('tested_artifact_receipt_untrusted')
            receipt=json.loads(saved.read_text())
            if receipt.get('ok') is not True:raise ValueError('tested_artifact_receipt_required')
            if self.config.get('artifact_receipts_dir') and receipt.get('artifact') != original['artifact']:
                raise ValueError('tested_artifact_receipt_mismatch')
            self.observe({'verify':'verify_release','reconcile':'reconcile_write','deploy':'apply_release'}[kind])
            deployer=Deployer(self.registry,self.kube,self.state_root/'deployments')
            if kind=='verify':
                deadline = time.monotonic() + 90
                while True:
                    self.guard()
                    try:
                        verification = deployer.verify(payload)
                        break
                    except ValueError as exc:
                        if str(exc) not in ('rollout_not_observed', 'pod_manifest_not_observed', 'business_http_failed') or time.monotonic() >= deadline:
                            raise
                        time.sleep(2)
                result={'ok':True,'write_outcome':'not_written','verification':verification,'error':None}
                durable_write(self.state_root/'promotions'/(key+'-'+payload['stage']+'.json'),result)
                return result
            if kind=='reconcile':
                result=deployer.reconcile(original,job['fencing_token'])
                if (payload.get('confirm_metadata_only') is True and original.get('stage') == 'dev' and
                    result.get('error') == 'metadata_only_observed'):
                    deployer.release_confirmed(original)
                    result=dict(result,write_outcome='not_written',error='metadata_only_released')
                return result
            if payload['stage'] == 'prd':
                previous='dev'
                gate=self.state_root/'promotions'/(key+'-'+previous+'.json')
                if not gate.is_file() or not json.loads(gate.read_text()).get('ok'):
                    raise ValueError('previous_stage_not_verified')
            return deployer.deploy(payload,job['fencing_token'])
        raise ValueError('unsupported_job')

    def collect(self,payload):
        service=payload['service'];env=payload['environment']
        self.registry.target({'service':service,'environment':env})
        if payload['catalog_sha256']!=self.registry.catalog_sha256:raise ValueError('catalog_mismatch')
        since=payload['since']
        if not isinstance(since,str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z',since):
            raise ValueError('invalid_log_since')
        datetime.fromisoformat(since.replace('Z','+00:00'))
        from .observations import utc_now,bounded,deployment_text,rollout_text
        doc=self.kube.read(env,service)
        target=summarize(doc,env,service,self.registry)
        observed_at=utc_now()
        dependencies=set(self.registry.catalog['dependencies'].get(service,{}).values())
        logs=[];evidence=[]
        def evidence_row(selected,source,text,when):
            if not isinstance(text,str) or not text.strip():return
            clean,quality=bounded(text)
            evidence.append({'id':f'E{len(evidence)+1}-{selected}','service':selected,'environment':env,
                             'observed_at':when,'source':source,'text':clean,'quality':quality})
        for selected in sorted({service,*dependencies}):
            self.registry.target({'service':selected,'environment':env})
            self.guard()
            selected_doc=doc if selected==service else self.kube.read(env,selected)
            selected_target=target if selected==service else summarize(selected_doc,env,selected,self.registry)
            when=observed_at if selected==service else utc_now()
            evidence_row(selected,'deployment',deployment_text(selected_doc,selected_target),when)
            evidence_row(selected,'rollout',rollout_text(selected_doc),when)
            self.observe('read_logs')
            for row in self.kube.logs(env,selected,since):
                if not isinstance(row.get('text'),str) or not row['text'].strip():continue
                when=row.get('observed_at') or utc_now()  # receipt time of actual HTTP observation
                clean,_=bounded(row['text'])
                logs.append({'service':selected,'environment':env,'observed_at':when,'text':clean})
                from .detect import exception_samples
                samples = exception_samples(row['text'])
                for sample in [row['text'], *samples]:
                    evidence_row(selected, 'pod_log', sample, when)
            if hasattr(self.kube,'events'):
                for row in self.kube.events(env,selected,selected_doc['metadata']['uid']):
                    evidence_row(selected,'k8s_event',row['text'],row['observed_at'])
        return {'ok':True,'target':target,'logs':logs,'evidence':evidence,'observed_at':observed_at}

    def source(self,payload):
        target=payload['target'];p=self.registry.target(target)
        self.registry.get(target['service'],payload['profile_id'])
        current=summarize(self.kube.read(target['environment'],target['service']),target['environment'],target['service'],self.registry)
        if current!=target:raise ValueError('source_target_changed')
        directory=self.config.get('provenance_dir')
        if not directory:raise ValueError('source_proof_not_provisioned')
        image=target['image']
        proofpath=Path(directory)/(target['service']+'-'+image.rsplit(':',1)[1]+'.json')
        if proofpath.is_symlink() or not proofpath.is_file():raise ValueError('source_proof_not_provisioned')
        stat=proofpath.stat()
        if stat.st_uid!=os.getuid() or stat.st_mode&0o022:raise ValueError('source_proof_not_trusted')
        proof=json.loads(proofpath.read_text())
        if proof.get('provenance_kind')!='local_build_receipt':raise ValueError('source_proof_not_trusted')
        # 로컬 receipt만으로 현재 manifest 존재를 추정하지 않는다.
        if self.host.observe_manifest(image)!=image:raise ValueError('source_manifest_mismatch')
        root=Path(proof['source_root'])
        if not root.is_absolute() or root.is_symlink():raise ValueError('source_root_invalid')
        result=snapshot(root,p,image,proof)
        durable_write(self.state_root/'snapshots'/(result['id']+'.json'),result)
        return {'ok':True,'source_snapshot':result,'error':None}

    def test_build(self,payload):
        source=payload['source_snapshot']
        p=next((x for x in self.registry.profiles.values() if x['id']==payload['profile_id']),None)
        if not p:raise ValueError('unregistered_profile')
        if not re.fullmatch('[a-f0-9]{64}',source['id']):raise ValueError('source_snapshot_invalid')
        path=self.state_root/'snapshots'/(source['id']+'.json')
        if not path.is_file() or json.loads(path.read_text())!=source:raise ValueError('source_snapshot_not_observed')
        builder=Builder(self.state_root/'workspaces',self.host,
                       self.config.get('provenance_dir') or self.state_root/'provenance')
        builder.observe=self.observe
        result=builder.run(source,payload['patch'],p)
        durable_write(self.artifact_receipt(self.artifact_key(result['artifact'])),result)
        return result

    def released_source(self,profile,image,artifact):
        """The build receipt's exact released tree. Only a hash-identical tree can ground developer tests."""
        directory=self.config.get('provenance_dir') or self.state_root/'provenance'
        path=Path(directory)/(profile['service']+'-'+image.rsplit(':',1)[1]+'.json')
        if path.is_symlink() or not path.is_file():raise ValueError('released_source_unobserved')
        stat=path.stat()
        if stat.st_uid!=os.getuid() or stat.st_mode&0o022:raise ValueError('released_source_untrusted')
        proof=json.loads(path.read_text())
        if (proof.get('provenance_kind')!='local_build_receipt' or proof.get('image')!=image or
                proof.get('profile_id')!=profile['id'] or digest(proof.get('file_sha256'))!=artifact.get('source_tree_sha256')):
            raise ValueError('released_source_mismatch')
        return {'root':proof['source_root'],'file_sha256':proof['file_sha256'],'image':image,'profile_id':profile['id']}

    def dev_identity(self,service,uid,image):
        """Pinned dev Deployment: UID, settled generation, all replicas ready, every owned ready Pod running exactly image."""
        doc=self.kube.read('dev',service)
        observed=summarize(doc,'dev',service,self.registry)
        md,st=doc['metadata'],doc.get('status') or {}
        count=doc['spec'].get('replicas',1);gen=md.get('generation')
        if (md.get('namespace')!='dev' or md.get('name')!=service or md.get('deletionTimestamp') or observed['uid']!=uid
                or observed['image']!=image or type(gen) is not int or st.get('observedGeneration',0)<gen or count<1
                or any(st.get(k)!=count for k in ('updatedReplicas','readyReplicas','availableReplicas'))):
            return None
        pods=self.kube.pods('dev',service)
        if len(pods)!=count or not all(p.get('owned') is True and p.get('ready') is True and p.get('image_id')==image for p in pods):
            return None
        return {'uid':uid,'generation':gen,'template_sha256':observed['template_sha256'],'replicas':count,'image':image}

    def qa(self,job):
        """Real dev UI QA via the QA lane's browser helper. Reads only: dev Deployment/Pods, registered UI, registered reads."""
        from .dev_ui_qa import run_dev_ui_qa
        payload=job['payload'];service=payload['service']
        profile=self.registry.get(service,payload.get('profile_id'))
        if payload.get('stage')!='dev':raise ValueError('qa_target_mismatch')
        image=payload['artifact']['image']
        target=next((t for t in payload.get('targets') or [] if t.get('environment')=='dev' and t.get('service')==service),{})
        # Desired template image is not enough during a rollout: pin UID, generation and actual ready Pod images.
        before=self.dev_identity(service,target.get('uid'),image) if target.get('uid') else None
        configured=dict(profile)
        configured['qa_source']=self.released_source(profile,image,payload['artifact'])
        origin=(self.config.get('qa_ui_origins') or {}).get(service)
        if origin:configured['qa_ui_origin']=origin
        if service=='order':
            def inventory(item):
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,32}',item):raise ValueError('qa_inventory_item_invalid')
                return self.kube.http_get('/api/v1/namespaces/dev/services/http:inventory:8080/proxy/api/stock/'+item)
            configured['qa_inventory_read']=inventory
        evidence=self.state_root/'qa-evidence'/re.sub(r'[^A-Za-z0-9_-]','',job['id'])
        result=run_dev_ui_qa(job={**payload,'observed_image':image if before else None},profile=configured,evidence_dir=evidence)
        after=self.dev_identity(service,target.get('uid'),image) if before else None
        if before and after!=before and result['qa'].get('outcome')!='unverifiable':
            qa={k:v for k,v in result['qa'].items() if k!='receipt_id'}
            qa.update(outcome='unverifiable',reason='dev_deployment_changed_during_qa')
            qa['receipt_id']=digest(qa)
            result={'ok':False,'write_outcome':'not_written','qa':qa,'error':qa['reason']}
        return result
