"""인증 접근 실패를 이유로 비root·공유 인증 읽기 전용 경계를 완화하지 않는다."""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]


class AuthBoundaryTests(unittest.TestCase):
    def test_auth_access_problem_does_not_relax_security_context(self):
        spec = importlib.util.spec_from_file_location('application_renderer_guard', ROOT / 'scripts/render-application-deploy.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        docs = [json.loads(value) for value in module.render('local/application@sha256:' + '1' * 64).values()]
        pod = next(row for row in docs if row['kind'] == 'Deployment')['spec']['template']['spec']
        api = pod['containers'][0]
        self.assertTrue(api['securityContext'].get('runAsNonRoot'), '인증 준비 실패는 실행 권한 완화 근거가 아니다')
        self.assertNotEqual(api['securityContext']['runAsUser'], 0)
        # 기존 관제의 공유 인증 PVC는 마운트하지 않는다(읽기·쓰기 모두). 새 서비스 전용 PVC만 사용한다.
        claims = [row['persistentVolumeClaim']['claimName'] for row in pod['volumes'] if 'persistentVolumeClaim' in row]
        self.assertEqual(claims, ['liveprobe-application-codex'])
        self.assertNotIn('liveprobe-codex-spare', json.dumps(docs))
        self.assertFalse(api['securityContext']['allowPrivilegeEscalation'])
        self.assertTrue(api['securityContext']['readOnlyRootFilesystem'])
        self.assertNotIn('initContainers', pod)
        self.assertNotIn('fsGroup', pod.get('securityContext', {}))


if __name__ == '__main__':
    unittest.main()
