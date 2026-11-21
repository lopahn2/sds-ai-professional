import { object, type Data } from './unified.ts';

export const records = (v: unknown): Data[] => Array.isArray(v) ? v.filter(row => row != null && typeof row === 'object' && !Array.isArray(row)) : [];
export const observedRecord = (v: unknown) => Object.keys(object(v)).length > 0;
export const statusLabel = (v: unknown) => ({
  passed: '통과', failed: '실패', not_run: '미실행', unverifiable: '확인 불가', tests_missing: '필수 시험 없음',
  observed: '확인됨', recovered: '복구 확인', error_still_observed: '원래 오류 재관측', not_observed: '미관측',
}[typeof v === 'string' ? v : ''] ?? '미관측');
export const boolLabel = (value: unknown, yes = '확인됨', no = '확인 실패') => value === true ? yes : value === false ? no : '미관측';

const stepNames: Record<string,string> = {open:'화면 열기', input:'값 입력', submit:'전송', network:'응답 확인',
  search_dom:'검색 화면 결과', detail_submit:'상품 상세 열기', detail_network:'상세 응답 확인', dom:'화면 결과', independent_read:'독립 조회'};

export function qaFlows(receipt: Data, plan: Data) {
  const actual = records(receipt.flows), planned = records(plan.flows);
  const ids = [...new Set([...planned, ...actual].map(f => f.id).filter((id): id is string => typeof id === 'string' && !!id))];
  return ids.map(id => ({ id, plan: planned.find(f => f.id === id) ?? {}, actual: actual.find(f => f.id === id) ?? {} }));
}

export function qaSteps(plan: Data, actual: Data) {
  const reported = records(actual.steps).filter(s=>typeof s.id==='string' && /^[a-z][a-z0-9_]{0,39}$/.test(s.id));
  const expected: Record<string, boolean> = { open: !!plan.id, input: observedRecord(plan.input), submit: observedRecord(plan.submit),
    network: observedRecord(plan.request), dom: observedRecord(plan.result), independent_read: observedRecord(plan.independent_read) };
  const ids = [...new Set(reported.map(s=>String(s.id)))];
  const order=Object.keys(stepNames);
  // Retain the runner's execution order, then insert any known planned stages
  // missing from a partial receipt before the next planned stage.
  for (const [i,id] of order.entries()) {
    if (!expected[id] || ids.includes(id)) continue;
    const next=ids.findIndex(key=>order.indexOf(key)>i);
    ids.splice(next<0 ? ids.length : next,0,id);
  }
  return ids.map(id=>({id, label:stepNames[id] || id, receipt:reported.find(s=>s.id===id) ?? {}, planned:expected[id] === true}));
}

/** Pair the very same test in the pinned source tree, across its two recorded
 * executions. A suite-level boolean cannot prove an individual journey. */
export function pairedTest(ref: Data, runs: Data[]) {
  const name = ref.test_id, file = ref.file;
  if (typeof name !== 'string' || typeof file !== 'string' || !name || !file) return {before:null, after:null};
  const matching = runs.filter(r=>r.file===file && typeof r.id==='string' &&
    (r.id===name || r.id===file+'::'+name || r.id===file+'#'+name));
  const observed = (phase: string) => {
    const states = [...new Set(matching.filter(r=>r.phase===phase).map(r=>r.status))];
    return states.length===1 && typeof states[0]==='string' ? states[0] : null;
  };
  return {before:observed('before'),after:observed('after')};
}

export function approvedVersion(release: Data) {
  const qa=object(release.qa), artifact=object(release.artifact);
  const dev=object(object(release.stage_results).dev);
  const observedImages=[...new Set(records(dev.verification).map(v=>v.image))];
  const tested=qa.observed_image ?? dev.image ?? (dev.verified===true && observedImages.length===1 ? observedImages[0] : null);
  const target=artifact.image;
  if (typeof tested!=='string' || typeof target!=='string' ||
    ! /@sha256:[a-f0-9]{64}$/.test(tested) || ! /@sha256:[a-f0-9]{64}$/.test(target)) return {tested,target,match:null};
  const sameArtifact = !qa.artifact_sha256 || qa.artifact_sha256===release.artifact_sha256;
  return {tested,target,match:tested===target && sameArtifact};
}
