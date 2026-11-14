"""사건 최초 신호·탐지 machine 신원·중복 관측의 실제 API 회귀."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import unittest
from test_core_support import Harness, HUMAN, MACHINE, PREFIX, digest


class CoreSignalTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        path = Path(self.h.settings.machine_identity_file)
        data = json.loads(path.read_text())
        data['principals'][0]['scopes'].append('incident:create')
        path.write_text(json.dumps(data))

    def body(self):
        sample = 'ValueError order failed token=synthetic-sensitive-value'
        return {'environment': 'dev', 'service': 'order', 'reason': 'ValueError:order:calculate',
                'signal': {'source': 'pod_log', 'observed_at': '2030-03-17T17:46:40Z',
                           'sample': sample, 'sha256': digest(sample)}}

    def test_machine_create_preserves_scrubbed_first_signal_across_repeated_observation_and_collect(self):
        body = self.body()
        first = self.h.client.post(PREFIX + '/incidents', headers=MACHINE, json=body)
        self.assertEqual(first.status_code, 201)
        incident = first.json()['id']
        saved = self.h.detail(incident)['data']['signal']
        self.assertNotIn('synthetic-sensitive-value', saved['sample'])
        self.assertEqual(saved['sha256'], digest(saved['sample']))
        again = copy.deepcopy(body)
        again['signal']['sample'] = 'a newer observation'
        again['signal']['sha256'] = digest(again['signal']['sample'])
        response = self.h.client.post(PREFIX + '/incidents', headers=MACHINE, json=again)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['id'], incident)
        self.assertEqual(self.h.report(self.h.claim(['collect']), self.h.collect()).status_code, 200)
        self.assertEqual(self.h.detail(incident)['data']['signal'], saved)
        events = self.h.client.get(PREFIX + f'/incidents/{incident}/events', headers=HUMAN).json()['events']
        self.assertEqual(sum(e['kind'] == 'signal_observed' for e in events), 1)
        self.assertNotIn('synthetic-sensitive-value', json.dumps(events))
        self.assertEqual(len(self.h.client.get(PREFIX + '/incidents', headers=HUMAN).json()['incidents']), 1)
        for suffix in ('user-test', 'approve-prd'):
            self.assertEqual(self.h.client.post(PREFIX + f'/incidents/{incident}/' + suffix, headers=MACHINE, json={}).status_code, 403)

    def test_open_signature_dedup_is_atomic_and_environment_service_scoped(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.h.client.post(PREFIX + '/incidents', headers=MACHINE, json=self.body()), range(2)))
        self.assertTrue(all(response.status_code == 201 for response in responses))
        self.assertEqual(len({response.json()['id'] for response in responses}), 1)
        original = responses[0].json()['id']
        for field, value in (('environment', 'prd'), ('service', 'payment'), ('reason', 'other-signature')):
            body = dict(self.body(), **{field: value})
            response = self.h.client.post(PREFIX + '/incidents', headers=MACHINE, json=body)
            self.assertEqual(response.status_code, 201)
            self.assertNotEqual(response.json()['id'], original)

    def test_invalid_signal_hash_timestamp_and_extra_fields_fail_before_persist(self):
        for modifier in ('hash', 'timestamp', 'extra'):
            body = self.body()
            if modifier == 'hash': body['signal']['sha256'] = '0' * 64
            elif modifier == 'timestamp': body['signal']['observed_at'] = 'not-a-time'
            else: body['signal']['opaque'] = 'not-accepted'
            response = self.h.client.post(PREFIX + '/incidents', headers=HUMAN, json=body)
            self.assertEqual(response.status_code, 422)
        self.assertEqual(len(self.h.client.get(PREFIX + '/incidents', headers=HUMAN).json()['incidents']), 0)
