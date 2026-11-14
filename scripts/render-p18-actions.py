#!/usr/bin/env python3
"""Generate the narrowly extended P18 action RBAC/admission declarations; never apply automatically.

The existing single-container restriction remains. prd Istio multi-container actions fail closed and are not claimed supported.
"""
import copy
import json
from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parents[1]
NAMES=('inventory','order','catalog','member','payment','notification','delivery')
# 2026-11-13: 활성 환경 dev/prd만 실행자 쓰기를 허용한다. 퇴역한 stg는 Role/RoleBinding·admission 허용 목록에서 뺀다
# (권한 축소). 이미 클러스터에 있는 stg Role/RoleBinding은 이 선언으로 지워지지 않으며 부모가 이름·UID 확인 후 개별 퇴역한다.
NAMESPACES=('dev','prd')
NS_CEL="['"+"','".join(NAMESPACES)+"']"
TARGET_NS_CEL="['"+"','".join(('liveprobe-target',)+NAMESPACES)+"']"


def render():
    base=list(yaml.safe_load_all((ROOT/'deploy/optional/safe-action-executor/safe-action-executor.yaml').read_text()))
    docs=[]
    for ns in NAMESPACES:
        for d in base:
            if not d or d['kind'] not in ('Role','RoleBinding'):continue
            item=copy.deepcopy(d);item['metadata']['namespace']=ns
            if item['kind']=='Role':
                # No dynamic approval verifier exists at admission: keep ConfigMap reads only.
                item['rules'] = [r for r in item['rules'] if not (r['resources'] == ['configmaps'] and 'patch' in r['verbs'])]
                for r in item['rules']:
                    if r['resources']==['deployments']:r['resourceNames']=list(NAMES)
                    elif r['resources']==['configmaps']:
                        r['resourceNames']=[n+'-config' for n in NAMES]
                        if 'get' in r['verbs']:r['resourceNames'] += [n+'-config-v1' for n in NAMES]
            docs.append(item)
    policy=copy.deepcopy(next(d for d in base if d and d['kind']=='ValidatingAdmissionPolicy'))
    # 실행자 허용 조건(참이어야 통과)이므로 stg를 빼면 클러스터에 남은 옛 stg Role이 있어도 admission에서 거부된다.
    policy['spec']['validations'][0]={'expression':"request.namespace in "+TARGET_NS_CEL,
                                     'message':'executor is limited to the explicit target namespaces'}
    policy['spec']['validations'][1]['expression']="""request.resource.resource != 'pods' ||
      (has(oldObject.metadata.labels) &&
       ((request.namespace == 'liveprobe-target' && oldObject.metadata.labels[?'app.kubernetes.io/part-of'].orValue('') == 'liveprobe-target') ||
        (request.namespace in """+NS_CEL+""" && oldObject.metadata.labels[?'liveprobe.io/managed'].orValue('') == 'true' &&
         oldObject.metadata.labels[?'app.kubernetes.io/part-of'].orValue('') in ['order','payment','member','catalog','delivery','notification'])) &&
       has(oldObject.metadata.ownerReferences) && oldObject.metadata.ownerReferences.exists(o, o.kind == 'ReplicaSet' && o.controller == true))"""
    # Restrict all new target deployments at admission in addition to name-bound Roles and backend UID ownership checks.
    policy['spec']['validations'].append({'expression':"""request.namespace == 'liveprobe-target' || request.resource.resource != 'deployments' ||
      (has(oldObject.metadata.labels) && oldObject.metadata.labels[?'liveprobe.io/managed'].orValue('') == 'true' &&
       oldObject.metadata.labels[?'app.kubernetes.io/part-of'].orValue('') in ['order','payment','member','catalog','delivery','notification'] &&
       object.metadata.labels == oldObject.metadata.labels)""", 'message':'only managed P18 system deployments may be tuned'})
    docs.append(policy)
    # Existing binding names the same policy; preserve it rather than recreating identities/tokens.
    docs.append(copy.deepcopy(next(d for d in base if d and d['kind']=='ValidatingAdmissionPolicyBinding')))
    reader=next(yaml.safe_load_all((ROOT/'deploy/base/liveprobe/liveprobe-write-guard.yaml').read_text()))
    # First resource is the policy; the old write guard remains effective in the historical namespace as well.
    assert reader['kind']=='ValidatingAdmissionPolicy'
    # 이 정책은 LiveProbe API 신원의 쓰기를 '거부'하는 방어선이다. stg를 빼면 퇴역(보존) namespace에서 거부가 사라져
    # 오히려 권한이 넓어지므로, 퇴역한 stg는 거부 대상 목록에 그대로 남긴다(실행자 허용 목록과 반대 방향).
    reader['spec']['matchConditions'][0]['expression']="request.namespace in ['liveprobe-target','dev','stg','prd']"
    docs.append(reader)
    return docs


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps({'apiVersion':'v1','kind':'List','items':render()},indent=2)+'\n')
    print('명시적 활성2환경(dev/prd) Role/기존 제한 유지 VAP 선언 생성(stg 실행자 권한 제외, API 쓰기 거부는 stg 유지); 아직 적용하지 않음')
