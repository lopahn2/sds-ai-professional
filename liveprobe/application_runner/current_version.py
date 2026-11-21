"""Observe a registered business operation on the currently serving image. Never deploy a release."""
import time

from .deploy import summarize
from .profiles import digest
from .recovery_probe import GET_OPERATIONS, Missing, business_evidence, settled_target


def check_current_version(registry, kube, job, clock=time.time):
    payload = job['payload']
    env, service = payload['environment'], payload['service']
    proof = {'protocol': 'current-version/v1', 'job_id': job['id'], 'attempt': job['fencing_token'], 'request_id': payload['request_id'],
             'incident_id': job['incident_id'], 'contract_sha256': digest(payload), 'service': service,
             'environment': env, 'image': None, 'outcome': 'unverifiable'}
    try:
        profile = registry.get(service, payload['profile_id'])
        path = profile['verification']['path']
        operation = payload['operation']
        if (env not in ('dev', 'prd') or operation['method'] != 'GET'
                or operation['path'] != path or GET_OPERATIONS.get(profile['verification']['kind']) != path):
            raise Missing('operation_unmatched')
        # Resolve the live image at execution time without accepting an image or target supplied by the browser.
        target = summarize(kube.read(env, service), env, service, registry)
        image = target['image']
        proof.update(image=image, path=path)
        before = settled_target(registry, kube, target, image, env)
        proof['before'] = {'uid': target['uid'], 'generation': before[0], 'template_sha256': before[1],
                           'replicas': before[2], 'image': image}
        response = kube.business(env, service, path)
        status = response.get('status')
        if type(status) is int and 100 <= status <= 599:
            proof['status_code'] = status
        if status == 200:
            try:
                evidence = business_evidence(profile, response)
                proof.update(outcome='passed', body_sha256=evidence['body_sha256'])
            except (AttributeError, KeyError, TypeError, ValueError):
                proof.update(outcome='failed', reason='business_mismatch')
        elif type(status) is int and 400 <= status <= 599:
            proof.update(outcome='failed', reason='request_failed')
        else:
            raise Missing('business_unobserved')
        try:
            after = settled_target(registry, kube, target, image, env)
        except Missing:
            raise Missing('deployment_changed_during_probe') from None
        if before != after:
            raise Missing('deployment_changed_during_probe')
        proof['after'] = dict(proof['before'])
    except Missing as exc:
        proof.update(outcome='unverifiable', reason=str(exc))
    except Exception:
        # Do not persist HTTP bodies, credentials, or arbitrary exception text.
        proof.update(outcome='unverifiable', reason='observation_unavailable')
    proof['observed_at'] = clock()
    proof['receipt_id'] = digest(proof)
    return {'ok': proof['outcome'] == 'passed', 'write_outcome': 'not_written', 'check': proof}
