// 실제 프로덕션 정적 코드와 실제 HTTP API의 무토큰 요약·세션 진입을 검증한다.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {resolve} from 'node:path';
const [url, id] = process.argv.slice(2);
const {chromium,expect}=createRequire(resolve('dashboard/package.json'))('@playwright/test');
const browser=await chromium.launch({headless:true});
try {
  const page=await browser.newPage({viewport:{width:1440,height:960},reducedMotion:'reduce'});
  const errors=[]; page.on('pageerror', e=>errors.push(e.message));
  let summaries=0;
  page.on('response', r=>{ if(r.url().includes('/incidents?')) summaries++; });
  await page.addInitScript(()=>{ const interval=window.setInterval; window.setInterval=(fn,ms)=>interval(fn,Math.min(ms,300)); });
  await page.goto(url);
  await expect(page.locator('#incident-list [data-id="'+id+'"]').first()).toBeVisible();
  await expect(page.locator('#incident-list [data-id="'+id+'"]').first()).toHaveAttribute('aria-pressed','true');

  assert.equal(await page.locator('#workspace').evaluate(e=>getComputedStyle(e).animationName),'none');
  await expect(page.locator('#detail-sections')).toBeVisible();
  await page.locator('#list-filter button[type=submit]').click();
  await expect.poll(()=>summaries).toBeGreaterThan(2);
  assert.equal(await page.locator('#session-start').count(),0);
  assert.equal(await page.locator('input[type=password]').count(),0);
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.locator('html').evaluate(e=>e.scrollWidth>e.clientWidth+1),false);
  assert.deepEqual(errors,[]);
  console.log('실제 API/Chromium: 무토큰 목록·상세·필터·poll·로컬 운영자 자동 진입·390px·JS 오류 0 통과');
} finally {await browser.close();}
