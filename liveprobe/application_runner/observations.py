"""관측 완료 시각과 정제된 제한 본문. 예상 관측을 생성하지 않는다."""
from datetime import datetime,timezone
import json
from .worker import redact


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')


def bounded(text):
    clean=redact(text)
    return clean[:2000], 'truncated' if len(clean)>2000 else 'observed'


def deployment_text(doc,target):
    # Environment/Secret/annotation values are deliberately not serialized.
    safe={k:target[k] for k in ('uid','resource_version','container','image','template_sha256')}
    if 'generation' in doc.get('metadata',{}):safe['generation']=doc['metadata']['generation']
    return json.dumps(safe,ensure_ascii=False,sort_keys=True)


def rollout_text(doc):
    status=doc.get('status') or {}
    safe={k:status[k] for k in ('observedGeneration','replicas','updatedReplicas','readyReplicas','availableReplicas','unavailableReplicas') if k in status}
    conditions=[{k:c[k] for k in ('type','status','reason','message','lastTransitionTime') if k in c} for c in status.get('conditions',[])]
    if conditions:safe['conditions']=conditions
    return json.dumps(safe,ensure_ascii=False,sort_keys=True) if safe else None
