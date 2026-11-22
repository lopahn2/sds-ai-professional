// 단계 카드 브리핑 스택(시안 A): 머리 → (결정) → 이어받음 → 브리프 → 데이터, 한글 정리 규칙과 근거 합침.
import { mkdirSync } from 'node:fs';
import { expect, test } from '@playwright/test';
import { layout, mock } from './p15-helpers.mjs';
import { mockAppInvestigation, mockSummary } from './fixtures/app-investigation.mjs';
import { mockUnifiedRoom } from './fixtures/unified-room.mjs';

test.skip(!mock, '격리된 관제실 모의 서버 전용');

const card = page => page.locator('.semantic-stage');
const LEAK = /한글 해석|해석 (실패|미관측|미기록)/;
async function select(page, stage) {
  const full = page.getByRole('button', { name: '전체 흐름', exact: true });
  if (await full.isVisible()) await full.click();
  const node = page.locator(`.react-flow__node[data-id="stage:${stage}"] button`).first();
  await node.focus(); await page.keyboard.press('Enter');
  await expect(card(page)).toHaveAttribute('data-selected-step', `stage:${stage}`);
}
async function keep(page, info, name) {
  const root = process.env.LIVEPROBE_EVIDENCE_ROOT;
  if (!root) return;
  mkdirSync(`${root}/stage-card`, { recursive: true });
  await card(page).screenshot({ path: `${root}/stage-card/${info.project.name}-${name}.png` });
}
/** No text in the card is smaller than 12px; the conclusion is at least 14px. */
async function textSizes(page) {
  const sizes = await card(page).evaluate(el => [...el.querySelectorAll('*')].filter(e => [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim()) && e.getBoundingClientRect().width > 0)
    .map(e => parseFloat(getComputedStyle(e).fontSize)));
  expect(Math.min(...sizes)).toBeGreaterThanOrEqual(12);
  expect(await card(page).locator('.step-lead').evaluate(e => parseFloat(getComputedStyle(e).fontSize))).toBeGreaterThanOrEqual(14);
}

test('원인 조사 카드는 결론·목표·남은 것을 먼저 보이고 근거를 출처별로 합친다', async ({ page }, info) => {
  await mockAppInvestigation(page, { variant: 'current' });
  await page.goto('/incidents/application/native-a');
  await select(page, 'investigation');
  const c = card(page);
  await expect(c.locator('.step-lead')).toHaveCount(1);
  await expect(c.locator('.step-lead')).toContainText('입력 전용 분기 2곳');
  await expect(c.locator('.stage-models .model-tag')).toHaveText(['gpt-6.1-sol · 중간', 'gpt-6.2-astra · 높음']);
  await expect(c.locator('.stage-badge')).toHaveClass(/is-(ok|info|warn|danger|muted)/);
  await expect(c.locator('.stage-lineage-row').first()).toContainText('대응 경로 결정');
  await expect(c.locator('.stage-lineage-row').first()).toContainText('심층 조사팀');
  await expect(c.locator('.stage-goals li')).toHaveCount(4);
  await expect(c.locator('.stage-goals li.is-ok')).toHaveCount(4);
  await expect(c.locator('.stage-goals')).toContainText('재시도 후 통과');
  await expect(c.locator('.stage-remaining')).toContainText('quantity=7');
  await expect(c.locator('.stage-facts')).toContainText('근거 9');
  // Raw source refs and excerpts stay out of view until a row is opened.
  await expect(c).not.toContainText('source:');
  await expect(c).not.toContainText(LEAK);
  await textSizes(page);
  await layout(page);
  await keep(page, info, 'app-investigation');

  const calls = c.getByRole('button', { name: /판단 기록 4/ });
  await calls.click();
  await expect(c.locator('.call-row')).toHaveCount(4);
  await expect(c.locator('.call-row .model-tag').first()).toBeVisible();
  const evidence = c.getByRole('button', { name: /^근거 9/ });
  await evidence.click();
  const code = c.locator('.evidence-group').filter({ hasText: '소스 코드' });
  await expect(code.locator('.evidence-row').first()).toContainText('AppFaultBusiness.java');
  await expect(code.locator('.evidence-row').first()).toContainText('인용 7');
  await expect(code.locator('.evidence-row').first()).toContainText('지문 인용');
  await expect(c.locator('.evidence-row').filter({ hasText: '배포 변경' })).toContainText('정리 중');
  await expect(c).not.toContainText('IllegalStateException');
  await expect(c.locator('.evidence-group').last()).toContainText('출처 미확인');
  await code.locator('.evidence-toggle').first().click();
  await expect(code.locator('pre.raw-text')).toHaveCount(2);
  await expect(c).not.toContainText(LEAK);
  await textSizes(page);
  await layout(page);
  await keep(page, info, 'app-investigation-open');
});

test('작업 중인 카드는 지금 하는 일과 진행 중인 목표를 먼저 보인다', async ({ page }, info) => {
  await mockAppInvestigation(page, { variant: 'running' });
  await page.goto('/incidents/application/native-a');
  await select(page, 'investigation');
  const c = card(page);
  await expect(c.locator('.stage-working')).toContainText('공개 답변 수신');
  await expect(c.locator('.stage-goals li.is-working').first()).toBeVisible();
  await expect(c.locator('.data-section').first().locator('.data-toggle')).toHaveAttribute('aria-expanded', 'true');
  await expect(c.locator('.call-row.is-running')).toHaveCount(1);
  await keep(page, info, 'app-investigation-running');
});

test('새 정리 도입 이전 기록은 해석 상태 대신 과거 데이터 없음만 보인다', async ({ page }, info) => {
  await mockAppInvestigation(page, { variant: 'legacy' });
  await page.goto('/incidents/application/native-a');
  await select(page, 'investigation');
  const c = card(page);
  await c.getByRole('button', { name: /^근거/ }).click();
  await expect(c.locator('.evidence-row').filter({ hasText: '애플리케이션 로그' }).first()).toContainText('과거 데이터 없음');
  await expect(c).not.toContainText('정리 중');
  await expect(c).not.toContainText(LEAK);
  await keep(page, info, 'app-investigation-legacy');
});

test('인용 불일치로 멈춘 조사는 해당 목표를 미충족으로 표시한다', async ({ page }) => {
  await mockAppInvestigation(page, { variant: 'blocked' });
  await page.goto('/incidents/application/native-a');
  await select(page, 'investigation');
  await expect(card(page).locator('.stage-goals li.is-fail')).toContainText(['인용이 수집 근거와 일치']);
});

test('운영 승인 카드는 보류 사유와 결정을 브리프보다 먼저 보인다', async ({ page }, info) => {
  const room = await mockUnifiedRoom(page, { state: 'awaiting_prd_approval' });
  await mockSummary(page, room.application);
  await page.goto('/incidents/application/native-a');
  await select(page, 'approval');
  const c = card(page);
  await expect(c.locator('.stage-decision .stage-hold')).toBeVisible();
  const first = await c.locator('.stage-decision').evaluate(d => !!(d.compareDocumentPosition(d.parentElement.querySelector('.stage-brief')) & Node.DOCUMENT_POSITION_FOLLOWING));
  expect(first).toBe(true);
  await expect(c.locator('.stage-facts')).toContainText('PR 병합 확인');
  await expect(c).not.toContainText(LEAK);
  await layout(page);
  await keep(page, info, 'app-approval');
});

test('Kubernetes 원인 조사 카드도 한 줄 결론과 모델 태그, 합친 근거를 보인다', async ({ page }, info) => {
  await page.goto('/incidents/971');
  await select(page, 'investigation');
  const c = card(page);
  await expect(c.locator('.step-lead')).toHaveCount(1);
  await expect(c.locator('.stage-models .model-tag').first()).toBeVisible();
  await expect(c).not.toContainText(LEAK);
  await c.getByRole('button', { name: /^근거/ }).click();
  await expect(c.locator('.evidence-group').first()).toBeVisible();
  await textSizes(page);
  await layout(page);
  await keep(page, info, 'k8s-investigation');
});
