// 실제 Application API·폐기 SQLite에서 실패 시험 영수증이 브라우저에 보이는지 검증한다.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync,mkdirSync,writeFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {createHash} from 'node:crypto';
const [url,incidentId,out] = process.argv.slice(2);
assert.ok(url && incidentId && out, '로컬 시험 서버·사건·증거 경로가 필요합니다.');
assert.equal(new URL(url).hostname,'127.0.0.1');
const root=process.cwd();
const require=createRequire(resolve(root,'dashboard/package.json'));
const {chromium,expect}=require('@playwright/test');
const browser=await chromium.launch({headless:true});
mkdirSync(out,{recursive:true});
const observations=[];
try {
  for(const width of [390,1440]) {
    const page=await browser.newPage({viewport:{width,height:950}});
    const errors=[],requests=[];
    page.on('pageerror',e=>errors.push(e.message));
    page.on('request',r=>requests.push({method:r.method(),origin:new URL(r.url()).origin,path:new URL(r.url()).pathname}));
    await page.goto(url);
    await page.locator('#incident-list [data-id="'+incidentId+'"]').click();
    await expect(page.locator('#test-results')).toContainText('수정 후 시험이 통과하지 못했습니다');
    await expect(page.locator('#test-results')).not.toContainText('tests/test_main.py::test_run');
    await expect(page.locator('#test-results')).not.toContainText('e'.repeat(64));
    await expect(page.locator('#test-details')).toBeEmpty();
    await page.locator('#raw-tests > summary').click();
    await expect(page.locator('#test-details')).toContainText('tests/test_main.py::test_run');
    await expect(page.locator('#test-details')).toContainText('수정 후');
    await expect(page.locator('#test-details')).toContainText('실패');
    await expect(page.locator('#test-details')).toContainText('e'.repeat(64));
    await expect(page.locator('#test-details')).toContainText('after_not_passed');
    await expect(page.locator('#approve-submit')).toBeDisabled();
    await expect(page.locator('#user-test-submit')).toBeDisabled();
    const overflow=await page.locator('html').evaluate(e=>e.scrollWidth>e.clientWidth+1);
    assert.equal(overflow,false);
    assert.deepEqual(errors,[]);
    assert.ok(requests.every(r=>r.origin===new URL(url).origin),'외부 요청 없음');
    assert.deepEqual(requests.filter(r=>!['GET','HEAD'].includes(r.method)).map(r=>[r.method,r.path]),[],'브라우저는 조회만 수행한다.');
    await page.locator('#raw-tests').screenshot({path:resolve(out,`failed-tests-${width}.png`)});
    observations.push({width,requests,page_errors:errors,overflow,approval_disabled:true});
    await page.close();
  }
  const sources={};
  for(const name of ['index.html','app.js','styles.css']) {
    const path=`liveprobe/app/application/static/${name}`;
    sources[path]=createHash('sha256').update(readFileSync(path)).digest('hex');
  }
  writeFileSync(resolve(out,'browser.json'),JSON.stringify({scope:'실제 로컬 API·합성 모델 전송·폐기 SQLite, 운영 배포 아님',observations,sources},null,2));
  console.log(`실제 API 실패 시험 표시: ${observations.length}개 화면 폭 통과`);
} finally {await browser.close();}
