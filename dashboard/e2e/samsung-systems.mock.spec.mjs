import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { capture, errors, layout, mock, openInfraMap } from './p15-helpers.mjs';
const sample = JSON.parse(readFileSync(new URL('./fixtures/samsung-overview.mock.json', import.meta.url), 'utf8'));
const names = ['삼성 트레이드 시스템', '삼성 딜리버리 시스템', '삼성 비즈니스 시스템'];
const captureRoot = `${process.env.LIVEPROBE_EVIDENCE_ROOT ?? 'e2e/artifacts'}/mock/samsung`;
// 이 파일은 mock/preview 전용이다. 실환경 인수 시험과 표본·캡처를 섞지 않는다.
test.skip(!mock, '삼성 합성 inventory의 모의 화면 시험입니다.');
async function load(page, overview = sample) {
  await page.route('**/api/overview', route => route.fulfill({ json: overview }));
  await page.goto('/'); await openInfraMap(page);
  await expect(page.getByRole('combobox', { name: '지도 환경' })).toHaveValue('prd');
}
// 2026-11-13 dev/prd 전환: 표본은 전환 전처럼 stg 환경·자원·계약을 담고 있다. 화면은 활성 dev·prd만 그린다
// (예전 기대: prd·dev·stg 선택, 전체 보기 환경 3·시스템 9·이벤트 계약 9).
test('세 독립 시스템·내부 서비스·공통 Kafka와 발행·소비·복구 계약을 환경별·전체 보기에서 보존한다', async ({ page }, info) => {
  const es = errors(page); await load(page);
  await expect(page.getByRole('combobox', { name: '지도 환경' }).locator('option')).toHaveText(['dev', 'prd', '전체 환경']);
  for (const env of ['prd', 'dev']) {
    await page.getByRole('combobox', { name: '지도 환경' }).selectOption(env);
    await expect(page.locator('.system-box')).toHaveCount(3);
    for (const name of names) await expect(page.locator('.system-box').filter({ hasText: name })).toHaveCount(1);
    const trade = page.locator(`.react-flow__node[data-id="${env}/system/samsung-trade"]`);
    const geometry = await trade.evaluate(el => { const r = el.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom }; });
    for (const name of ['order', 'inventory', 'catalog', 'payment', 'trade-member']) {
      const card = page.locator(`.react-flow__node[data-id="${env}/Deployment/${name}"]`);
      await expect(card).toHaveCount(1);
      const r = await card.boundingBox(); expect(r).not.toBeNull();
      expect(r.x).toBeGreaterThanOrEqual(geometry.left - 1); expect(r.x + r.width).toBeLessThanOrEqual(geometry.right + 1);
      expect(r.y).toBeGreaterThanOrEqual(geometry.top - 1); expect(r.y + r.height).toBeLessThanOrEqual(geometry.bottom + 1);
    }
    await expect(page.locator('.namespace-title').filter({ hasText: '공통 연계 인프라' })).toHaveCount(1);
    await expect(page.locator('.react-flow__edge.declared_event')).toHaveCount(3);
    await expect(page.locator('.react-flow__edge.declared_snapshot_api')).toHaveCount(2);
    const contracts=page.locator('.contract-list');
    if(!await contracts.getAttribute('open') && !await contracts.locator('.disclosure-body').isVisible()) await contracts.getByText('연결·계약 설명',{exact:true}).click();
    await expect(contracts).toContainText('실제 접속 소비자→브로커');
    await layout(page);
  }
  await capture(page, info, 'samsung-selected', captureRoot);
  await page.getByRole('combobox', { name: '지도 환경' }).selectOption('');
  await expect(page.locator('.environment-box')).toHaveCount(2);
  await expect(page.locator('.system-box')).toHaveCount(6);
  for (const name of names) await expect(page.locator('.system-box').filter({ hasText: name })).toHaveCount(2);
  await expect(page.locator('.react-flow__edge.declared_event')).toHaveCount(6);
  expect(await page.locator('.react-flow__node').evaluateAll(es => es.filter(e => (e.dataset.id ?? '').startsWith('stg/') || (e.dataset.id ?? '').startsWith('contract:stg/')).length)).toBe(0);
  await layout(page); await capture(page, info, 'samsung-all', captureRoot);
  expect(es).toEqual([]);
});
test('미배포 브로커는 실제 자원이나 실측 수치가 아닌 계약 끝점으로 설명한다', async ({ page }, info) => {
  const o = structuredClone(sample);
  o.topology.nodes = o.topology.nodes.filter(n => n.namespace !== 'liveprobe-messaging');
  for (const e of o.topology.contract_edges) {
    for (const ref of [e.from_ref, e.to_ref]) if (ref.name === 'kafka') ref.installed = false;
    if (e.type === 'declared_event') e.deployment_state = 'not_deployed';
  }
  await load(page, o);
  await expect(page.getByRole('button', { name: 'kafka 계약 경로 미배포 · 자원 상세 열기', exact: true })).toHaveCount(1);
  await page.getByRole('combobox', { name: '지도 자원 선택' }).selectOption('contract:liveprobe-messaging/Service/kafka');
  const detail = page.getByRole('complementary', { name: '자원 상세', exact: true });
  await expect(detail).toHaveCount(1);
  await expect(detail).toContainText('실제 Kubernetes 자원이 아닙니다');
  await expect(detail).toContainText('계약된 이벤트');
  await expect(detail).toContainText('연결 미관측');
  await expect(detail).not.toContainText('흐름 0');
  await layout(page); await capture(page, info, 'samsung-not-deployed', captureRoot);
});
