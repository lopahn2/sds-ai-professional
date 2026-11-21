import {expect, test} from '@playwright/test';
import {mockLocalOperator} from './fixtures/local-operator.mjs';

test('로컬 운영자가 개인 권한 조회 없이 시험할 버전을 확인한다', async ({page}) => {
  await mockLocalOperator(page);
  const id='old-v2-review', base='/api/application/v1/incidents/'+id;
  const release={protocol:'dev-prd/v2',valid:true,release_id:id,
    artifact:{image:'registry.local/order@sha256:'+'c'.repeat(64)},artifact_sha256:'a'.repeat(64),
    plan_sha256:'b'.repeat(64),stage_results:{dev:{verified:true}}};
  const requests=[];
  await page.route('**/api/**', route => {
    const path=new URL(route.request().url()).pathname;
    requests.push(path);
    if(path===base+'/summary')return route.fulfill({json:{id,service:'order',state:'awaiting_user_test',protocol:'dev-prd/v2',environment:'dev',reason:'주문 dev 시험'}});
    if(path===base+'/release')return route.fulfill({json:{incident_id:id,state:'awaiting_user_test',release}});
    if(path===base)return route.fulfill({json:{id,state:'awaiting_user_test',data:{test_report:'열람 허용된 시험 결과'}}});
    if(path.endsWith('/stream'))return route.fulfill({contentType:'text/event-stream',body:': fixture\n\n'});
    return route.fulfill({json:{items:[],accounts:[],incidents:[],total:0}});
  });
  await page.goto('/application?incident='+id);
  await expect(page).toHaveURL(new RegExp('/incidents/application/'+id+'$'));
  await page.getByRole('navigation',{name:'사건 처리 단계'}).getByRole('button',{name:/dev QA/}).click();
  await expect(page.getByRole('form',{name:'dev 사용자 시험 기록'})).toBeVisible();
  expect(requests.some(path=>path.includes('/human-sessions'))).toBe(false);
  await expect(page.getByRole('button',{name:/역할 확인|운영자로 확인/})).toHaveCount(0);
});
