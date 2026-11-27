"""Bind a verified mixed incident to one dev-only, release-scoped infrastructure action."""
from __future__ import annotations

import re
from types import SimpleNamespace

from sqlalchemy import select

from . import actions, proposals
from .application.safety import sha
from .application.storage import Incident as ApplicationIncident, Job
from .models import Action, AgentRun, AuditLog, CodeActionLink, Evidence, Incident
from .runtime_fencing import application_owned


class Unavailable(ValueError):
    pass


def required(incident):
    return ((incident.handoff or {}).get('remediation') or {}).get('kind') == 'both'


def proof(session, incident):
    """Use independently agreeing persisted model messages and their actual collected source."""
    from .unified import verified_code_cause
    from .agent_groups import EvidenceStore, check_citations
    rows = list(session.scalars(select(Evidence).where(Evidence.incident_id == incident.id).order_by(Evidence.id)))
    runs = list(session.scalars(select(AgentRun).where(AgentRun.incident_id == incident.id).order_by(AgentRun.id)))
    if not verified_code_cause(incident, rows, runs):
        raise Unavailable('mixed_code_evidence_missing')
    review = next(r for r in reversed(runs) if r.role in ('result_verifier', 'group_verifier'))
    marker = (incident.handoff or {}).get('code_cause_verification') or {}
    if marker.get('verified') is not True or marker.get('verifier_run_id') != review.id:
        raise Unavailable('mixed_decision_provenance_missing')
    last_review = max((r.id for r in runs if r.id < review.id and r.role in ('result_verifier', 'group_verifier')), default=0)
    prior = next((r for r in reversed(runs) if r.id < review.id and r.role in (
        'signal_analyst', 'coordinator', 'recovery_planner') and r.id > last_review), None)
    proposal = proposals.resolve(review.output, incident.workload)
    if (not prior or prior.status != 'succeeded' or (prior.output or {}).get('_acceptance_rejected') or
            not proposals.same(proposal, proposals.resolve(prior.output, incident.workload)) or
            proposal['name'] != incident.workload or proposal['type'] not in ('set_memory_limit', 'rollout_restart')):
        raise Unavailable('mixed_infrastructure_cause_unverified')
    # An image rollback would discard the repaired image. Code-induced crashes alone do not prove infrastructure need.
    field, value = ('last_terminated_reason', 'OOMKilled') if proposal['type'] == 'set_memory_limit' else (
        'waiting_reason', 'ContainerStatusUnknown')
    source = next((row for row in reversed(rows) if row.source == 'pod_status'), None)
    if (not source or source.error or source.target != f'{incident.namespace}/pods(app={incident.workload})' or
            not isinstance(source.payload, list) or not any(c.get('name') == incident.workload and c.get(field) == value
            for pod in source.payload for c in pod.get('containers', []))):
        raise Unavailable('mixed_infrastructure_evidence_missing')
    store = EvidenceStore({'E2_pods': source.payload})
    for message in (prior.output, review.output):
        cites = [c for c in message.get('evidence', []) if c.get('ref') == 'E2_pods' and value in (c.get('quote') or '')]
        if not cites or check_citations(store, {'evidence': cites}).forged_quotes:
            raise Unavailable('mixed_infrastructure_citation_missing')
    return proposal, {'proposer_id': prior.id, 'verifier_id': review.id, 'evidence_id': source.id}


def dev_target(session, application_id, release_id, origin):
    app = session.get(ApplicationIncident, application_id)
    if not app or app.state != 'dev_infra_pending':
        raise Unavailable('mixed_dev_not_pending')
    data = app.document.get('data') or {}
    release = data.get('release') or {}
    stage = (release.get('stage_results') or {}).get('dev') or {}
    artifact = release.get('artifact') or {}
    image, service = artifact.get('image'), app.document.get('service')
    if (data.get('unified_origin') != origin or release.get('infra_gate') not in ('verified', 'decision') or release.get('valid') is not True or
            release.get('release_id') != release_id or not re.fullmatch(r'[^\s@]+@sha256:[0-9a-f]{64}', image or '') or
            stage.get('verified') is not True or stage.get('deployed') is not True or stage.get('write_outcome') != 'written'):
        raise Unavailable('mixed_dev_receipt_missing')
    for kind, field in (('deploy', 'deploy_job_id'), ('verify', 'verify_job_id')):
        job = session.get(Job, stage.get(field)) if stage.get(field) else None
        if (not job or job.status not in ('completed', 'reconciled') or job.incident_id != application_id or job.kind != kind or
                job.payload.get('stage') != 'dev' or job.payload.get('release_id') != release_id or
                (job.payload.get('artifact') or {}).get('image') != image or not job.fence or
                not job.result_hash or (job.receipt or {}).get('result_sha256') != job.result_hash):
            raise Unavailable('mixed_dev_job_unobserved')
    for job in session.scalars(select(Job).where(Job.kind.in_(['deploy', 'reconcile']),
            Job.status.in_(['queued', 'leased', 'reconciliation_required']))):
        payload = (job.payload or {}).get('original_payload') if job.kind == 'reconcile' else job.payload
        if not isinstance(payload, dict) or not isinstance(payload.get('targets'), list):
            raise Unavailable('mixed_competing_write_target_unobserved')
        if payload.get('stage') == 'dev' and any(t.get('service') == service for t in payload['targets']):
            raise Unavailable('mixed_competing_application_write')
    target = next((r for r in stage.get('targets', []) if r.get('environment') == 'dev' and r.get('service') == service), {})
    verified = any(r.get('environment') == 'dev' and r.get('service') == service and r.get('image') == image and
                   (r.get('business') or {}).get('ok') is True and r['business'].get('status_code') == 200 and
                   re.fullmatch('[0-9a-f]{64}', r['business'].get('body_sha256') or '')
                   for r in stage.get('verification', []))
    if not verified or not target.get('uid') or target.get('image') != image:
        raise Unavailable('mixed_dev_business_unverified')
    return {'uid': target['uid'], 'service': service, 'image': image, 'release_id': release_id,
            'application_id': application_id, 'verify_job_id': stage['verify_job_id']}


class DevTarget:
    """Narrow the existing executor to exactly the verified dev Deployment and released ownership fence."""
    def __init__(self, executor, control, origin, binding):
        self.executor, self.control, self.origin, self.binding = executor, control, origin, binding
        self.action_id = None

    @property
    def typed_actions_enabled(self):
        return self.executor is not None and self.executor.typed_actions_enabled

    def get_deployment(self, namespace, name):
        if namespace != 'dev' or name != self.binding['service']:
            raise actions.Denied('mixed_target_mismatch')
        dep = self.executor.get_deployment(namespace, name) or {}
        md = dep.get('metadata') or {}
        containers = (((dep.get('spec') or {}).get('template') or {}).get('spec') or {}).get('containers') or []
        if (md.get('uid') != self.binding['uid'] or md.get('namespace') != 'dev' or md.get('name') != name or
                not md.get('resourceVersion') or application_owned(dep) or len(containers) != 1 or
                containers[0].get('name') != name or containers[0].get('image') != self.binding['image']):
            raise actions.Denied('mixed_target_changed_or_owned')
        return dep

    def patch_deployment_json(self, namespace, name, ops):
        # Application's mutex makes release invalidation and this single CAS mutually exclusive.
        with self.control.store.transaction() as session:
            try:
                live = dev_target(session, self.binding['application_id'], self.binding['release_id'], self.origin)
            except Unavailable as exc:
                raise actions.Denied(str(exc)) from None
            if live != self.binding:
                raise actions.Denied('mixed_release_changed')
            dep = self.get_deployment(namespace, name)
            conditions = [{'op': 'test', 'path': '/metadata/uid', 'value': self.binding['uid']},
                          {'op': 'test', 'path': '/metadata/resourceVersion', 'value': dep['metadata']['resourceVersion']},
                          {'op': 'test', 'path': '/spec/template/spec/containers/0/image', 'value': self.binding['image']}]
            return self.executor.patch_deployment_json(namespace, name, conditions + ops)

    def list_pods(self, namespace, *args, **kwargs):
        if namespace != 'dev':
            raise actions.Denied('mixed_target_mismatch')
        return self.executor.list_pods(namespace, *args, **kwargs)


def result(state, release_id, code=None, action_id=None):
    return {'state': state, 'release_id': release_id, 'reason': code, 'action_id': str(action_id) if action_id else None}


def advance(service, application_id, release_id, engine):
    from .unified import active_link, target_lock
    from .safety import has_network_risk
    with service.sessions() as s:
        link = s.scalar(select(CodeActionLink).where(CodeActionLink.application_id == application_id))
        if not link:
            return result('waiting', release_id, 'mixed_root_unobserved')
        iid = link.incident_id
        inc = s.get(Incident, iid)
        if not inc or link.state != 'linked':
            return result('waiting', release_id, 'mixed_root_unobserved')
        if service.control.environment_paused(inc.namespace):
            return result('waiting', release_id, 'environment_paused')
        target_lock(s, 'dev', inc.workload)
        inc = s.scalar(select(Incident).where(Incident.id == iid).with_for_update())
        origin = {'installation_id': service.instance, 'incident_id': iid, 'action_id': link.action_id,
                  'request_sha256': link.request_sha256}
        try:
            with service.control.store.read() as app_db:
                binding = dev_target(app_db, application_id, release_id, origin)
        except Unavailable as exc:
            return result('waiting', release_id, str(exc))
        if binding['service'] != inc.workload:
            return result('blocked', release_id, 'mixed_target_mismatch')
        scoped = SimpleNamespace(id=iid, namespace='dev', workload=inc.workload, system=inc.system)
        if service._unknown_target(s, inc) or (inc.namespace != 'dev' and service._unknown_target(s, scoped)):
            return result('blocked', release_id, 'mixed_prior_write_unknown')
        if not required(inc):
            return result('not_required', release_id)
        if (not inc.system or has_network_risk(inc) or inc.severity_level not in ('SIMPLE', 'NORMAL', 'COMPLEX') or
                inc.namespace not in ('prd', 'dev') or inc.status not in ('handed_off', 'resolved') or
                link.state != 'linked' or active_link(s, 'dev', inc.workload, except_id=iid)):
            return result('blocked', release_id, 'mixed_ownership_uncertain')
        try:
            plan, source = proof(s, inc)
        except Unavailable as exc:
            return result('blocked', release_id, str(exc))
        if not engine or not engine.exec_kube:
            return result('waiting', release_id, 'mixed_executor_unconfigured')
        kube = DevTarget(engine.exec_kube, service.control, origin, binding)
        held = s.scalar(select(Action).where(Action.incident_id == iid, Action.params['mixed']['release_id'].as_string() == release_id))
        if held is not None:
            aid = held.id
            if (held.params.get('mixed') != binding or held.params.get('mixed_source') != source or
                    held.type != plan['type'] or held.params.get('args') != plan['params'] or
                    held.target != f'dev/Deployment/{inc.workload}'):
                return result('blocked', release_id, 'mixed_proof_changed', aid)
            if held.status == 'succeeded' and held.executed and (held.after_state or {}).get('checks') and all(
                    valid is True for valid in held.after_state['checks'].values()) and s.scalar(select(AuditLog.id).where(AuditLog.incident_id == iid,
                    AuditLog.event == 'action_verified', AuditLog.detail['action_id'].as_integer() == aid).limit(1)):
                try:
                    current = actions._dep_snapshot(kube.get_deployment('dev', inc.workload))
                except actions.Denied as exc:
                    return result('blocked', release_id, str(exc), aid)
                applied = held.params.get('applied') or {}
                field = 'memory_limit' if held.type == 'set_memory_limit' else 'restarted_at'
                if not applied.get(field) or current.get(field) != applied[field]:
                    return result('blocked', release_id, 'mixed_verified_effect_changed', aid)
                receipt = {'binding': binding, 'source': source, 'action_id': aid, 'after': held.after_state}
                return {**result('verified', release_id, action_id=aid), 'receipt_sha256': sha(receipt),
                        'verified_at': held.updated_at.timestamp(), 'dev_image': binding['image']}
            if held.status != 'proposed':
                return result('waiting' if held.status in ('executing', 'pending_approval') else 'blocked',
                              release_id, held.status, aid)
        if service._busy(s, scoped, exclude_action_id=held.id if held else None):
            return result('waiting', release_id, 'mixed_other_write_active')
        if held is None:
            if plan['type'] == 'set_memory_limit':
                try:
                    current = actions._dep_snapshot(kube.get_deployment('dev', inc.workload))
                    wanted = plan['params'].get('memory_limit_mi')
                    if type(wanted) is not int or wanted <= actions.parse_mem_mi(current['memory_limit']):
                        return result('blocked', release_id, 'mixed_limit_increase_unproven')
                except actions.Denied as exc:
                    return result('blocked', release_id, str(exc))
            held = actions.propose_typed(s, engine.cfg, kube, scoped, inc.severity_level, plan['type'], 'Deployment',
                                         inc.workload, plan['params'], 'apps', 'unified-coordinator')
            held.params = {**held.params, 'mixed': binding, 'mixed_source': source}
            aid = held.id
        if held.status != 'proposed':
            return result('waiting' if held.status == 'pending_approval' else 'blocked', release_id, held.status, aid)
        kube.action_id = aid
    execution = actions.execute_typed(service.sessions, engine.cfg, kube, aid, 'unified-coordinator')
    with service.sessions() as session:
        current = session.get(Action, aid)
        durable = current.status if current else execution.status
    if durable == 'succeeded':
        # Read back the committed action, verification receipt and current dev identity.
        return advance(service, application_id, release_id, engine)
    return result('waiting' if durable in ('proposed', 'executing', 'pending_approval') else 'blocked', release_id, durable, aid)
