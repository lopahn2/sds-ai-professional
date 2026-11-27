"""Environment render contract for dev and prd with bounded observation permissions."""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/render-p18-environments.py'


class RenderContract(unittest.TestCase):
    def test_renderer_exists(self):
        self.assertTrue(SCRIPT.exists(), 'reproducible environment renderer is not implemented')

    def test_environment_boundaries(self):
        if not SCRIPT.exists():
            self.skipTest('renderer missing; existence test is RED')
        spec = importlib.util.spec_from_file_location('render_envs', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        images = {n: f'liveprobe/{n}@sha256:' + 'a'*64 for n in module.SERVICES}
        docs = module.render(images)
        deployments = [d for d in docs if d['kind'] == 'Deployment']
        self.assertEqual(len(deployments), 14)
        for ns in ('dev', 'prd'):
            ds = [d for d in deployments if d['metadata']['namespace'] == ns]
            self.assertEqual({d['metadata']['name'] for d in ds}, set(module.SERVICES))
            self.assertEqual({d['metadata']['labels']['app.kubernetes.io/part-of'] for d in ds},
                             {'order', 'payment', 'member', 'catalog', 'delivery', 'notification'})
            for d in ds:
                p = d['spec']['template']['spec']
                self.assertFalse(p['automountServiceAccountToken'])
                if d['metadata']['name'] in ('member', 'delivery'):
                    self.assertEqual(p['containers'][0]['securityContext'].get('runAsUser'), 1000,
                                     'kubelet cannot resolve the named image user node with runAsNonRoot')
                self.assertFalse(any('hostPath' in v for v in p.get('volumes', [])))
                c = p['containers'][0]
                self.assertIn('@sha256:', c['image'])
                self.assertEqual(c['resources']['limits']['memory'], '512Mi' if d['metadata']['name'] in ('order', 'inventory') else '256Mi')
        ingresses = [d for d in docs if d['kind'] == 'Ingress']
        self.assertEqual(len(ingresses), 12)
        self.assertNotIn('loadgen', str(docs))
        self.assertFalse(any(d['kind'] == 'Secret' for d in docs))
        self.assertFalse(any(d['kind'] in ('ClusterRole', 'ClusterRoleBinding', 'ValidatingAdmissionPolicy') for d in docs))

        def config_readers(resources):
            return {item['metadata']['namespace']: item['rules'] for item in resources
                    if item['kind'] == 'Role' and item['metadata']['name'] == 'liveprobe-config-reader'}

        generated = json.loads((ROOT / 'deploy/environments/apps.json').read_text())['items']
        roles = config_readers(docs)
        self.assertEqual(set(roles), {'dev', 'prd'})
        self.assertEqual(roles, config_readers(generated))
        for rules in roles.values():
            self.assertEqual([rule for rule in rules if 'persistentvolumeclaims' in rule['resources']],
                             [{'apiGroups': [''], 'resources': ['persistentvolumeclaims'], 'verbs': ['get']}])
            self.assertEqual([rule for rule in rules if 'horizontalpodautoscalers' in rule['resources']],
                             [{'apiGroups': ['autoscaling'], 'resources': ['horizontalpodautoscalers'], 'verbs': ['list']}])


if __name__ == '__main__':
    unittest.main()
