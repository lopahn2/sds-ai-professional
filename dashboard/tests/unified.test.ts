import test from 'node:test';
import assert from 'node:assert/strict';
import { canAttest, continuationActive, fingerprint, latestAnalysisSummary, mergeObserved, observed, releaseKey, releaseProof, validUserTest, type CodeActionDetail, type CodeActionSummary } from '../src/unified.ts';
const now = 1000000;
const f = { release_id: 'release-a', artifact_sha256: 'a'.repeat(64), plan_sha256: 'b'.repeat(64), image: `registry/service@sha256:${'c'.repeat(64)}` };
const detail = (): CodeActionDetail => ({ summary: { incident_id: 971 } as CodeActionSummary, application: { id: 'native-a', state: 'awaiting_prd_approval', data: { merge_review: { ...f, human_merge_observed: true, expires_at: now / 1000 + 60 } } } as CodeActionDetail['application'], release: { incident_id: 'native-a', state: 'awaiting_prd_approval', release: { ...f, artifact: { image: f.image }, valid: true, protocol: 'dev-prd/v2', stage_results: { dev: { verified: true } }, user_test: { ...f, id: 'test-a', kind: 'human', passed: true, invalidated: false, expires_at: now / 1000 + 1800 } } }, events: [] });
test('missing, tagged or incomplete fingerprints never enable an attestation', () => {
  assert.equal(fingerprint({}), null); const d = detail(); d.release.release!.artifact = { image: 'registry/service:latest' }; assert.equal(canAttest(d, 'approve-prd', now), false);
  d.release.release!.artifact = { image: f.image }; d.release.release!.plan_sha256 = null; assert.equal(fingerprint(d.release.release!), null);
});
test('user test requires observed dev success and current dev/prd protocol', () => {
  const d = detail(); d.application.state=d.release.state='awaiting_user_test'; assert.equal(canAttest(d, 'user-test', now), true);
  d.release.release!.stage_results = {}; assert.equal(canAttest(d, 'user-test', now), false);
  d.release.release!.stage_results = { dev: { verified: true }, stg: { verified: true } }; d.release.release!.protocol = 'dev-stg-prd/v1'; assert.equal(canAttest(d, 'user-test', now), false);
});
test('expired, nonhuman, failed, invalidated or different-release user tests block approval', () => {
  const patches = [{ expires_at: now / 1000 }, { kind: 'runner' }, { passed: false }, { invalidated: true }, { plan_sha256: 'd'.repeat(64) }];
  for (const patch of patches) { const d = detail(); Object.assign(d.release.release!.user_test as object, patch); assert.equal(validUserTest(d.release.release!, now), false); assert.equal(canAttest(d, 'approve-prd', now), false); }
});
test('exact, fresh human merge observation is mandatory for prd approval', () => {
  const d = detail(); assert.equal(canAttest(d, 'approve-prd', now), true);
  for (const patch of [{ human_merge_observed: false }, { expires_at: now / 1000 }, { image: `registry/other@sha256:${'c'.repeat(64)}` }]) { const x = detail(); Object.assign(x.application.data.merge_review as object, patch); assert.equal(mergeObserved(x, now), false); assert.equal(canAttest(x, 'approve-prd', now), false); }
});
test('merge verification request is possible with a valid user test before merge observation', () => {
  const d = detail(); d.application.data.merge_review = null; assert.equal(canAttest(d, 'review-merge', now), true); assert.equal(canAttest(d, 'approve-prd', now), false);
});
test('release or proof changes invalidate the confirmation key', () => {
  const d = detail(), before = releaseKey(d); d.release.release!.plan_sha256 = 'd'.repeat(64); assert.notEqual(releaseKey(d), before);
});
test('server write lock overrides a terminal-looking child state', () => {
  assert.equal(continuationActive({ linked: true, state: 'handoff', blocks_cluster: true } as CodeActionSummary), true);
  assert.equal(continuationActive({ linked: true, state: 'reconciliation_required' } as CodeActionSummary), true);
  assert.equal(continuationActive({ linked: true, state: 'completed', blocks_cluster: false } as CodeActionSummary), false);
});
test('missing observations remain unknown rather than false or passed', () => { assert.equal(observed(null), '미관측'); assert.equal(observed(undefined), '미관측'); assert.equal(observed(false), '미확인'); });

test('v3 승인은 현재 QA·위험·이미지·사람 병합 지문이 일치해야 하고, 보완 대기 중에는 보류한다',()=>{
  const d=detail(),r=d.release.release!;
  Object.assign(r,{protocol:'dev-qa-prd/v3',user_test:null,
    qa:{...f,observed_image:f.image,accepted:true,outcome:'passed',receipt_id:'e'.repeat(64)},
    risk:{assessment:'complete',approval_blocked:false,destructive:false},risk_sha256:'d'.repeat(64)});
  assert.deepEqual(releaseProof(r),{qa_receipt_id:'e'.repeat(64),risk_sha256:'d'.repeat(64)});
  assert.equal(canAttest(d,'approve-prd',now),true);
  assert.equal(canAttest(d,'user-test',now),false);
  const key=releaseKey(d);
  d.application.data.supplements=[{status:'pending'}];
  assert.notEqual(releaseKey(d),key);assert.equal(canAttest(d,'approve-prd',now),false);
  d.application.data.supplements=[];
  for (const overrides of [{risk:{assessment:'incomplete'}},{qa:{...r.qa as object,receipt_id:null}},{infra_gate:'verified',infra:{state:'not_required'}},{valid:false}]){
    assert.equal(releaseProof({...r,...overrides}),null);
  }
});

test('actual role conclusion is read without treating patch output as root cause', () => {
  assert.equal(latestAnalysisSummary({}), null);
  assert.equal(latestAnalysisSummary({ roles: [{ role: 'analyst', result: { summary_ko: '연결 반환이 빠졌습니다.' } }, { role: 'patch', result: { summary_ko: '수정했습니다.' } }] }), '연결 반환이 빠졌습니다.');
});
