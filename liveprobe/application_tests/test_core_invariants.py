"""prd 불변식: 실제 API·worker·durable 큐에서 runnable job 수까지 검사."""
import copy
import unittest
from sqlalchemy import select
from app.application.storage import Job
from test_core_support import HUMAN, MACHINE, PREFIX
import test_core_release


class CoreInvariantTests(test_core_release.CoreReleaseTests):
    def prd_count(self, incident, active=False):
        with self.h.app.state.store.transaction() as session:
            rows = list(session.scalars(select(Job).where(Job.incident_id == incident, Job.kind == 'deploy')))
            return sum(row.payload['stage'] == 'prd' and (not active or row.status in ('queued', 'leased')) for row in rows)

    def test_auto_pass_and_elapsed_time_never_create_human_approval_or_prd_job(self):
        incident = self.awaiting()
        self.h.now += self.h.settings.approval_seconds * 2
        self.assertTrue(self.h.claim(['deploy']) is None)
        self.assertEqual(self.prd_count(incident), 0)
        release = self.release(incident)
        self.assertTrue(release['approval'] is None and release['user_test'] is None)
        events = self.h.client.get(PREFIX + f'/incidents/{incident}/events', headers=HUMAN).json()['events']
        self.assertFalse(any(event['kind'] == 'human_prd_approved' for event in events))

    def test_unapproved_internal_queue_cannot_create_prd_job(self):
        incident = self.awaiting()
        c = self.h.app.state.control
        with c.store.transaction() as session:
            c.queue_deploy(session, c.get(session, incident), 'prd')
        self.assertEqual(self.prd_count(incident), 0)
        self.assertTrue(self.h.claim(['deploy']) is None)

    def test_consumed_approval_cannot_queue_second_prd_after_completion(self):
        incident = self.awaiting()
        body = self.approval_body(incident)
        self.assertEqual(self.h.client.post(PREFIX + f'/incidents/{incident}/approve-prd', headers=HUMAN, json=body).status_code, 200)
        self.deployed('prd')
        self.verified('prd')
        self.assertEqual(self.prd_count(incident), 1)
        c = self.h.app.state.control
        with c.store.transaction() as session:
            c.queue_deploy(session, c.get(session, incident), 'prd')
        self.assertEqual(self.prd_count(incident), 1)
        self.assertEqual(self.prd_count(incident, active=True), 0)

    def test_expired_user_test_other_image_and_rebuild_never_create_prd_job(self):
        incident = self.awaiting()
        body = self.approval_body(incident)
        wrong = dict(body, image='other@sha256:' + 'e' * 64)
        self.assertEqual(self.h.client.post(PREFIX + f'/incidents/{incident}/approve-prd', headers=HUMAN, json=wrong).status_code, 409)
        self.assertEqual(self.prd_count(incident), 0)
        self.h.now += self.h.settings.approval_seconds + 1
        self.assertEqual(self.h.client.post(PREFIX + f'/incidents/{incident}/approve-prd', headers=HUMAN, json=body).status_code, 409)
        self.assertEqual(self.prd_count(incident), 0)
        self.h.app.state.control.invalidate_release(incident, 'artifact_rebuilt')
        self.assertEqual(self.h.client.post(PREFIX + f'/incidents/{incident}/approve-prd', headers=HUMAN, json=body).status_code, 409)
        self.assertEqual(self.prd_count(incident), 0)

    def test_expired_previously_valid_approval_has_zero_runnable_prd_jobs(self):
        incident = self.awaiting()
        body = self.approval_body(incident)
        self.assertEqual(self.h.client.post(PREFIX + f'/incidents/{incident}/approve-prd', headers=HUMAN, json=body).status_code, 200)
        self.h.now += self.h.settings.approval_seconds + 1
        self.assertTrue(self.h.claim(['deploy']) is None)
        self.assertEqual(self.prd_count(incident, active=True), 0)

    def test_user_test_requires_boolean_and_machine_never_becomes_human(self):
        incident = self.awaiting()
        release = self.release(incident)
        body = {key: release[key] for key in ('release_id', 'artifact_sha256', 'plan_sha256')}
        body.update(image=release['artifact']['image'], passed=1, notes_ko='synthetic')
        url = PREFIX + f'/incidents/{incident}/user-test'
        self.assertEqual(self.h.client.post(url, headers=HUMAN, json=body).status_code, 422)
        body['passed'] = True
        self.assertEqual(self.h.client.post(url, headers=MACHINE, json=body).status_code, 403)
        approval = self.approval_body(incident)
        self.assertEqual(self.h.client.post(PREFIX + f'/incidents/{incident}/approve-prd', headers=MACHINE, json=approval).status_code, 403)
        self.assertEqual(self.prd_count(incident), 0)
