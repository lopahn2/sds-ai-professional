"""격리 fixture workspace·실제 소스·fake Docker 명령 경계 검증."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from liveprobe.application_runner.profiles import Registry,digest
from liveprobe.application_runner.source import source_files,snapshot

class FixtureTests(unittest.TestCase):
    def test_fixture_copy_patch_build_manifest_and_provenance_match_without_source_mutation(self):
        from liveprobe.application_runner.fixture import FixtureBuilder
        from liveprobe.application_runner.host import DockerHost,ProcessResult
        calls=[];image='liveprobe/catalog@sha256:'+'d'*64
        def run(argv,timeout=0):
            calls.append(argv)
            if 'inspect' in argv:return ProcessResult(0,json.dumps([{'RepoDigests':[image]}]),'')
            return ProcessResult(0,'','')
        registry=Registry(ROOT);profile=registry.get('catalog');original=source_files(ROOT/profile['source_root'])
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'state'
            builder=FixtureBuilder(registry,DockerHost({},run),state)
            dry=builder.run('catalog-title-key','dev',dry_run=True)
            self.assertEqual(dry['writes'],0);self.assertFalse(state.exists());self.assertEqual(calls,[])
            result=builder.run('catalog-title-key','dev')
            self.assertEqual(result['image'],image)
            proof=json.loads(Path(result['proof_file']).read_text())
            workspace=Path(proof['source_root'])
            self.assertTrue(workspace.is_relative_to(state.resolve()));self.assertTrue((workspace/'.application-owner').is_file())
            self.assertIn("name=row['display_name']",(workspace/'app.py').read_text())
            self.assertEqual(proof['provenance_kind'],'local_build_receipt')
            pinned=snapshot(workspace,profile,image,proof)
            self.assertTrue(pinned['provenance_verified']);self.assertIsNone(pinned['base_commit'])
            self.assertEqual(proof['file_sha256'],{p:digest(t.encode()) for p,t in source_files(workspace).items()})
            self.assertEqual(Path(result['proof_file']).name,'catalog-'+'d'*64+'.json')
            self.assertEqual(Path(result['proof_file']).stat().st_mode&0o077,0)
            self.assertTrue(any('build' in c for c in calls));self.assertTrue(any('inspect' in c for c in calls))
            self.assertTrue(all('push' not in c for c in calls))
            self.assertEqual(original,source_files(ROOT/profile['source_root']))
    def test_fixture_business_read_fails_then_exact_reverse_patch_passes_without_network(self):
        from liveprobe.application_runner.fixture import FixtureBuilder
        from liveprobe.application_runner.deploy import validate_business
        from liveprobe.application_runner.source import parse_patch,apply_changes
        class Host:
            def build(self,workspace,profile,tree):return {'image':'liveprobe/catalog@sha256:'+'e'*64,'manifest_observed':True,'source_tree_sha256':tree}
            def observe_manifest(self,image):return image
        with tempfile.TemporaryDirectory() as tmp:
            result=FixtureBuilder(Registry(ROOT),Host(),tmp).run('catalog-title-key','dev')
            proof=json.loads(Path(result['proof_file']).read_text());workspace=Path(proof['source_root'])
            # Load the real isolated service modules, but no HTTP socket/background.
            code="import app; from runtime import Runtime; r=Runtime('catalog','catalog',{}); b=app.Catalog(r); print(b.handle('GET','/api/products',{},{}))"
            broken=subprocess.run([sys.executable,'-B','-c',code],cwd=workspace,capture_output=True,text=True)
            self.assertNotEqual(broken.returncode,0);self.assertIn("KeyError: 'display_name'",broken.stderr)
            (workspace/'app.py').write_text((ROOT/'services/catalog/app.py').read_text())
            fixed=subprocess.run([sys.executable,'-B','-c',code],cwd=workspace,capture_output=True,text=True)
            self.assertEqual(fixed.returncode,0,fixed.stderr);self.assertIn("'price': 79000",fixed.stdout)
    def test_fixture_deploy_uses_same_two_cas_patches_and_keeps_lock_until_confirmation(self):
        from liveprobe.application_runner.fixture import FixtureBuilder
        from test_runner_fencing import KubeHTTP,KEYS
        class Host:
            def build(self,workspace,profile,tree):return {'image':'liveprobe/catalog@sha256:'+'b'*64,'manifest_observed':True,'source_tree_sha256':tree}
            def observe_manifest(self,image):return image
        api=KubeHTTP()
        with tempfile.TemporaryDirectory() as tmp:
            builder=FixtureBuilder(api.registry,Host(),tmp)
            result=builder.run('catalog-title-key','dev')
            deployed=builder.deploy(result,api.transport())
            self.assertTrue(deployed['ok']);self.assertEqual(len(api.patches),2)
            self.assertTrue(all(k in api.doc['metadata']['annotations'] for k in KEYS))
            self.assertTrue(hasattr(builder,'confirm_deployment'),'intentional failing fixture requires read-only confirmation before unlock')
            confirmed=builder.confirm_deployment(api.transport())
            self.assertTrue(confirmed['ok'])
            self.assertEqual(len(api.patches),3)
            self.assertFalse(any(k in api.doc['metadata']['annotations'] for k in KEYS))
            self.assertFalse(any(o['op']=='replace' and o['path'].endswith('/image') for o in api.patches[-1]))

    def test_fixture_non_dev_unknown_and_missing_digest_fail_closed(self):
        from liveprobe.application_runner.fixture import FixtureBuilder
        from liveprobe.application_runner.host import DockerHost,ProcessResult
        calls=[]
        def run(argv,timeout=0):calls.append(argv);return ProcessResult(0,'[{"Id":"sha256:config-only","RepoDigests":[]}]','')
        with tempfile.TemporaryDirectory() as tmp:
            builder=FixtureBuilder(Registry(ROOT),DockerHost({},run),Path(tmp)/'state')
            for env in ('stg','prd'):
                with self.assertRaisesRegex(ValueError,'fixture_dev_only'):builder.run('catalog-title-key',env)
            with self.assertRaisesRegex(ValueError,'fixture_unregistered'):builder.run('../catalog','dev')
            self.assertEqual(calls,[])
            with self.assertRaisesRegex(ValueError,'manifest_digest_not_observed'):builder.run('catalog-title-key','dev')
            self.assertFalse(any((Path(tmp)/'state/provenance').glob('*.json')))

if __name__=='__main__':unittest.main()
