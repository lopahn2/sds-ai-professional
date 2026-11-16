"""등록 저장소를 읽어서만 확인하는 사람 병합 gate. PR 생성·병합·Git 쓰기 권한 없음."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field


class MergeReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    release_id: str
    artifact_sha256: str
    plan_sha256: str
    image: str
    pr_number: int = Field(strict=True, gt=0, lt=2**31)

from .safety import sha, safe_source_path


class MergeBlocked(ValueError):
    """메시지는 고정 코드만 포함한다. HTTP 응답·자격·소스 본문은 포함하지 않는다."""


class GitHubMergeObserver:
    def __init__(self, repositories_file, token_file=None, *, get_json=None):
        self.repositories_file = repositories_file
        self.token_file = token_file
        self.get_json = get_json or self._get

    def _get(self, path):
        # 주소는 아래 코드가 생성한다. 사용자 URL/redirect/proxy 환경은 사용하지 않는다.
        headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
        if self.token_file:
            token = Path(self.token_file).read_text().strip()
            if not token or any(c.isspace() for c in token):
                raise MergeBlocked('merge_identity_unconfigured')
            headers['Authorization'] = 'Bearer ' + token
        try:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=8) as client:
                with client.stream('GET', 'https://api.github.com' + path, headers=headers) as response:
                    if response.status_code != 200:
                        raise MergeBlocked('merge_observation_unavailable')
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 2_000_000:
                            raise MergeBlocked('merge_observation_too_large')
                    return json.loads(raw)
        except MergeBlocked:
            raise
        except Exception:
            raise MergeBlocked('merge_observation_unavailable') from None

    def registration(self, service):
        try:
            data = json.loads(Path(self.repositories_file).read_text())
            policy = data['pull_request_policy']
            if (policy['auto_merge'] is not False or policy['human_merge_required_before_prd'] is not True or
                    policy['each_promotion_requires_confirmation'] is not True):
                raise ValueError()
            matches = [v for v in data['systems'].values() if service in v.get('services', {})]
            if len(matches) != 1:
                raise ValueError()
            cfg = matches[0]
            repo, base = cfg['repository'], cfg.get('base_branch', 'main')
            subdir = cfg['services'][service]['source_subdir']
            if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or
                    not re.fullmatch(r'[A-Za-z0-9_./-]+', base) or '..' in base or
                    (subdir != '.' and (PurePosixPath(subdir).is_absolute() or
                     any(x in ('', '.', '..') for x in subdir.split('/'))))):
                raise ValueError()
            return repo, base, subdir
        except Exception:
            raise MergeBlocked('repository_policy_unconfigured') from None

    def _source_tree(self, repo, commit, subdir):
        import base64
        from .safety import DENIED_PATH as DENIED, SAFE_EXTENSIONS as ALLOWED
        result = self.get_json(f'/repos/{repo}/git/trees/{commit}?recursive=1')
        if result.get('truncated') is not False or not isinstance(result.get('tree'), list):
            raise MergeBlocked('merge_tree_incomplete')
        prefix = '' if subdir == '.' else subdir + '/'
        hashes = {}
        total = 0
        for entry in result['tree']:
            full = entry.get('path', '')
            if not full.startswith(prefix) or entry.get('type') == 'tree':
                continue
            name = full[len(prefix):]
            if entry.get('mode') in ('120000', '160000') or entry.get('type') != 'blob':
                raise MergeBlocked('merge_tree_unsafe')
            parts = name.split('/')
            # source_files와 같은 제외 규칙을 적용한다. 숨긴 파일을 시험 소스에 섞지 않는다.
            if (any(p.startswith('.') or p in ('node_modules', '__pycache__') or DENIED.search(p) for p in parts)
                    or parts[0] == 'target'):
                continue
            if PurePosixPath(name).suffix.lower() not in ALLOWED and parts[-1] != 'Dockerfile':
                continue
            if not safe_source_path(name) or name in hashes or len(hashes) >= 200:
                raise MergeBlocked('merge_tree_unsafe')
            blob_sha = entry.get('sha', '')
            if not re.fullmatch('[a-f0-9]{40}', blob_sha) or entry.get('size', 512001) > 512000:
                raise MergeBlocked('merge_tree_unsafe')
            blob = self.get_json(f'/repos/{repo}/git/blobs/{blob_sha}')
            if blob.get('encoding') != 'base64':
                raise MergeBlocked('merge_tree_incomplete')
            try:
                body = base64.b64decode(''.join(blob['content'].split()), validate=True)
                body.decode('utf-8')
            except Exception:
                raise MergeBlocked('merge_tree_incomplete') from None
            total += len(body)
            if len(body) > 512000 or total > 4_000_000 or blob.get('size') != len(body):
                raise MergeBlocked('merge_tree_unsafe')
            # GitHub 응답과 Git blob 지문도 대조한다.
            if hashlib.sha1(b'blob ' + str(len(body)).encode() + b'\0' + body).hexdigest() != blob_sha:
                raise MergeBlocked('merge_tree_incomplete')
            hashes[name] = hashlib.sha256(body).hexdigest()
        if not hashes:
            raise MergeBlocked('merge_tree_incomplete')
        return sha(hashes)

    def observe(self, *, service, pr_number, source_tree_sha256):
        if type(pr_number) is not int or not 0 < pr_number < 2**31:
            raise MergeBlocked('pull_request_invalid')
        if not re.fullmatch('[a-f0-9]{64}', source_tree_sha256 or ''):
            raise MergeBlocked('tested_source_tree_required')
        repo, base, subdir = self.registration(service)
        pr = self.get_json(f'/repos/{repo}/pulls/{pr_number}')
        actor = pr.get('merged_by') or {}
        head, target = pr.get('head') or {}, pr.get('base') or {}
        commit = pr.get('merge_commit_sha')
        if (pr.get('merged') is not True or pr.get('state') != 'closed' or not pr.get('merged_at') or
                actor.get('type') != 'User' or not re.fullmatch(r'[A-Za-z0-9-]{1,39}', actor.get('login', '')) or
                target.get('ref') != base or (target.get('repo') or {}).get('full_name') != repo or
                (head.get('repo') or {}).get('full_name') != repo or
                not re.fullmatch('[a-f0-9]{40}', commit or '')):
            raise MergeBlocked('human_merge_required')
        # 병합 후 main이 더 진행했다면 현재 운영 승인 대상으로 간주하지 않는다.
        refpath = f'/repos/{repo}/git/ref/heads/{quote(base, safe="")}'
        if (self.get_json(refpath).get('object') or {}).get('sha') != commit:
            raise MergeBlocked('merged_main_changed')
        if self._source_tree(repo, commit, subdir) != source_tree_sha256:
            raise MergeBlocked('merged_source_differs_from_tested')
        # 여러 blob 조회 동안 branch가 바뀐 경우도 거부한다.
        if (self.get_json(refpath).get('object') or {}).get('sha') != commit:
            raise MergeBlocked('merged_main_changed')
        return {'repository': repo, 'pr_number': pr_number,
                'url': f'https://github.com/{repo}/pull/{pr_number}', 'merge_commit': commit,
                'merged_by': actor['login'], 'source_tree_sha256': source_tree_sha256,
                'human_merge_observed': True}


def install_merge_policy(app, control, identities, settings):
    """권한이 있는 명시 조회만 proof를 저장한다. gate 재검사는 읽기만 수행한다."""
    from fastapi import Header
    from .safety import fail

    def observer():
        path = getattr(settings, 'repositories_file', None)
        if not path:
            raise MergeBlocked('repository_policy_unconfigured')
        return GitHubMergeObserver(path, getattr(settings, 'github_read_token_file', None))

    def check(document):
        release = document.get('data', {}).get('release') or {}
        proof = document.get('data', {}).get('merge_review') or {}
        if (not proof.get('human_merge_observed') or proof.get('release_id') != release.get('release_id') or
                proof.get('artifact_sha256') != release.get('artifact_sha256') or
                proof.get('plan_sha256') != release.get('plan_sha256') or
                proof.get('image') != release.get('artifact', {}).get('image')):
            return False
        try:
            fresh = observer().observe(service=document['service'], pr_number=proof['pr_number'],
                         source_tree_sha256=release['artifact']['source_tree_sha256'])
            return all(proof.get(k) == v for k, v in fresh.items())
        except Exception:
            return False

    control.merge_authorized = check

    def review(incident_id, body, actor):
        if not isinstance(body, MergeReview):
            body = MergeReview.model_validate(body)
        control.require_store()
        # 원격 조회 동안 공용 DB 쓰기 잠금을 유지하지 않는다.
        with control.store.read() as session:
            incident = control.get(session, incident_id)
            if incident.state not in ('awaiting_user_test', 'awaiting_prd_approval'):
                fail(409, 'merge_review_state_conflict', 'dev 검증이 끝난 버전만 병합을 확인할 수 있습니다.')
            release = control.matching_release(incident, body)
            service = incident.document['service']
            tree = release['artifact']['source_tree_sha256']
        try:
            proof = observer().observe(service=service, pr_number=body.pr_number, source_tree_sha256=tree)
        except MergeBlocked as exc:
            fail(409, str(exc), '등록 저장소에서 시험한 소스의 사람 병합을 확인하지 못했습니다.')
        except Exception:
            fail(503, 'merge_observation_unavailable', '병합 상태 조회를 완료하지 못했습니다.')
        with control.store.transaction() as session:
            incident = control.get(session, incident_id)
            if incident.state not in ('awaiting_user_test', 'awaiting_prd_approval'):
                fail(409, 'merge_review_state_conflict')
            current = control.matching_release(incident, body)
            if current['artifact']['source_tree_sha256'] != tree:
                fail(409, 'release_fingerprint_conflict')
            proof.update(release_id=body.release_id, artifact_sha256=body.artifact_sha256,
                         plan_sha256=body.plan_sha256, image=body.image, observed_at=control.clock(),
                         expires_at=control.clock() + 60)
            control.data(incident, 'merge_review', proof)
            control.event(session, incident, 'human_merge_observed', {
                'actor': actor['subject'], 'pr_number': body.pr_number, 'merge_commit': proof['merge_commit']})
            return {'incident_id': incident_id, 'state': incident.state, 'merge_review': proof}

    control.review_merge = review
    # FastAPI resolves annotation using globals; use the actual type on the function below.
    def endpoint(incident_id: str, body: MergeReview, authorization: str | None = Header(default=None)):
        actor = identities.authenticate(authorization, 'prd-approver')
        return review(incident_id, body, actor)
    endpoint.__annotations__['body'] = MergeReview
    app.post('/api/application/v1/incidents/{incident_id}/review-merge')(endpoint)
