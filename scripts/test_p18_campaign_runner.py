"""Operator harness guards (unit fixtures only; never live evidence)."""
import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

SCRIPT=Path(__file__).with_name('p18-campaign-run.py')


def module():
    spec=importlib.util.spec_from_file_location('runner',SCRIPT);assert spec and spec.loader
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def deployment():
    return {'metadata':{'name':'payment','namespace':'dev','uid':'expected','resourceVersion':'9'},
            'spec':{'replicas':1,'template':{'metadata':{'annotations':{'scrape':'yes'}},
                    'spec':{'containers':[{'name':'payment','image':'normal@sha256:'+'a'*64,
                                          'resources':{'limits':{'memory':'256Mi','cpu':'500m'}}}]}}}}


class RunnerGuards(unittest.TestCase):
    def test_runner_exists(self):self.assertTrue(SCRIPT.exists(),'fault runner is not implemented')

    @unittest.skipUnless(SCRIPT.exists(),'runner missing')
    def test_replaced_deployment_refuses_restore(self):
        m=module();base=deployment();current=copy.deepcopy(base);current['metadata']['uid']='replacement'
        with self.assertRaisesRegex(RuntimeError,'UID'):m.restore_ops(base,current)

    @unittest.skipUnless(SCRIPT.exists(),'runner missing')
    def test_unchanged_deployment_needs_no_write(self):
        m=module();base=deployment();self.assertEqual(m.restore_ops(base,copy.deepcopy(base)),[])

    @unittest.skipUnless(SCRIPT.exists(),'runner missing')
    def test_restore_is_cas_and_recovers_every_mutable_field(self):
        m=module();base=deployment();current=copy.deepcopy(base);current['spec']['replicas']=2
        current['spec']['template']['metadata']['annotations']['kubectl.kubernetes.io/restartedAt']='injected'
        current['spec']['template']['spec']['containers'][0]['resources']['limits']['memory']='768Mi'
        current['spec']['template']['spec']['containers'][0]['image']='fault@sha256:'+'b'*64
        ops=m.restore_ops(base,current)
        self.assertEqual(ops[:2],[{'op':'test','path':'/metadata/uid','value':'expected'},
                                {'op':'test','path':'/metadata/resourceVersion','value':'9'}])
        paths={o['path'] for o in ops[2:]}
        self.assertIn('/spec/replicas',paths)
        self.assertIn('/spec/template/spec/containers/0/image',paths)
        self.assertIn('/spec/template/spec/containers/0/resources',paths)
        self.assertIn('/spec/template/metadata/annotations/kubectl.kubernetes.io~1restartedAt',paths)

    @unittest.skipUnless(SCRIPT.exists(),'runner missing')
    def test_restore_refuses_foreign_cpu_change(self):
        m=module();base=deployment();current=copy.deepcopy(base)
        current['spec']['template']['spec']['containers'][0]['resources']['limits']['cpu']='4'
        with self.assertRaisesRegex(RuntimeError,'drift'):m.restore_ops(base,current)

    @unittest.skipUnless(SCRIPT.exists(),'runner missing')
    def test_pod_owner_chain_must_match_baseline(self):
        m=module();pod={'metadata':{'ownerReferences':[{'kind':'ReplicaSet','name':'r','uid':'rs','controller':True}]}}
        rs={'metadata':{'name':'r','uid':'rs','ownerReferences':[{'kind':'Deployment','uid':'expected','controller':True}]}}
        self.assertTrue(m.owned_pod(pod,rs,'expected'))
        rs['metadata']['ownerReferences'][0]['uid']='foreign'
        self.assertFalse(m.owned_pod(pod,rs,'expected'))

    @unittest.skipUnless(SCRIPT.exists(),'runner missing')
    def test_restore_refuses_unapproved_executable_drift(self):
        m=module();base=deployment();current=copy.deepcopy(base)
        current['spec']['template']['spec']['containers'][0]['command']=['malicious']
        with self.assertRaisesRegex(RuntimeError,'drift'):m.restore_ops(base,current)


    def test_build_manifest_requires_success_and_exact_deployed_images(self):
        m=module();base=deployment()
        deps={n:copy.deepcopy(base) for n in m.SERVICES}
        rows=[{'service':n,'status':'passed','test':{'exit':0},'build':{'exit':0},
               'image':base['spec']['template']['spec']['containers'][0]['image'],
               'source_sha256':{'app.py':'tested'}} for n in m.SERVICES]
        for n,d in deps.items():d['spec']['template']['spec']['containers'][0]['name']=n
        with patch.object(m,'current_source_hashes',return_value={'app.py':'tested'}):
            m.verify_deployed_builds(rows,deps)
            rows[0]['status']='failed'
            with self.assertRaisesRegex(RuntimeError,'build'):m.verify_deployed_builds(rows,deps)
            rows[0]['status']='passed'
            deps[m.SERVICES[0]]['spec']['template']['spec']['containers'][0]['image']='old:image'
            with self.assertRaisesRegex(RuntimeError,'image'):m.verify_deployed_builds(rows,deps)

    def test_build_manifest_refuses_unverified_source_change(self):
        m=module();base=deployment();deps={n:copy.deepcopy(base) for n in m.SERVICES}
        rows=[{'service':n,'status':'passed','test':{'exit':0},'build':{'exit':0},
               'image':base['spec']['template']['spec']['containers'][0]['image'],
               'source_sha256':{'app.py':'tested'}} for n in m.SERVICES]
        with patch.object(m,'current_source_hashes',return_value={'app.py':'changed'}):
            with self.assertRaisesRegex(RuntimeError,'source'):m.verify_deployed_builds(rows,deps)

    def test_completion_requires_an_observed_fault(self):
        m=module();details=[{'incident':{'status':'resolved'}}]
        self.assertEqual(m.case_state({'verified':False},details),'fault_unobserved')
        self.assertEqual(m.case_state({'verified':True},details),'completed')
        self.assertEqual(m.case_state({'verified':True},[]),'analysis_incomplete')


    def test_old_restart_count_is_not_a_new_fault(self):
        m=module();before=[{'uid':'old','name':'payment','restartCount':3}]
        self.assertFalse(m.new_restart(before,[{'uid':'old','name':'payment','restartCount':3}]))
        self.assertTrue(m.new_restart(before,[{'uid':'old','name':'payment','restartCount':4}]))
        self.assertFalse(m.new_restart(before,[{'uid':'new','name':'payment','restartCount':0}]))
        self.assertTrue(m.new_restart(before,[{'uid':'new','name':'payment','restartCount':1}]))


if __name__=='__main__':unittest.main()
