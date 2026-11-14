import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {registerHooks} from 'node:module';
import {fileURLToPath} from 'node:url';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import ts from 'typescript';
import type {EvidenceCard, FlowNode, GroupCard, Narrative} from '../src/api.ts';
// 기존 p18의 TSX 로더 패턴. 추가 런타임·서버·패키지 없음.
const src=new URL('../src/',import.meta.url).href;
registerHooks({
  resolve(specifier,context,next){
    if(specifier.startsWith('.')&&context.parentURL?.startsWith(src)&&! /\.[a-z]+$/i.test(specifier)){
      for(const ext of ['.tsx','.ts']){const url=new URL(`${specifier}${ext}`,context.parentURL);try{readFileSync(url);return {url:url.href,shortCircuit:true};}catch{}}
    }
    return next(specifier,context);
  },
  load(url,context,next){
    if(url.endsWith('.css'))return {format:'module',source:'export {};',shortCircuit:true};
    if(url.startsWith(src)&&/\.tsx?$/.test(url))return {format:'module',source:ts.transpileModule(readFileSync(fileURLToPath(url),'utf8'),{compilerOptions:{module:ts.ModuleKind.ESNext,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText,shortCircuit:true};
    return next(url,context);
  }
});
const {DetailPanel}=await import('../src/DetailPanel.tsx');
const fixture=()=>JSON.parse(readFileSync(new URL('../e2e/fixtures/narrative-990.json',import.meta.url),'utf8')) as Narrative;
const render=(d:Narrative,id:string)=>renderToStaticMarkup(createElement(DetailPanel,{target:{kind:'node',nodeId:id},data:d,handoff:null,onClose(){}}));
const escape=(s:string)=>s.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#x27;');
function evidence(){
  const d=fixture(),n=d.flow.nodes.find(x=>x.type==='evidence')!;
  assert.equal(n.card?.kind,'evidence');
  return {d,n,c:n.card as EvidenceCard,raw:d.raw.find(r=>r.ref==='E1_deployment')!};
}

test('동일 입력 ref가 반복돼도 단계 전체 상세의 입력 묶음은 한 번만 표시한다',()=>{
  const d=fixture(),n=d.flow.nodes.find(x=>x.type==='agent')!;
  const ref=d.raw[0].ref;n.inputs=[ref,ref,ref];d.flow.nodes=[n];
  const html=render(d,n.id);
  assert.equal((html.match(/<details[^>]*class="detail-input"/g)||[]).length,1);
});

test('단계 전체 수집 상세는 같은 ref의 서로 다른 당시 스냅샷을 각각 보존한다',()=>{
  const {d,n,c,raw}=evidence();
  c.items=[0,1].map(i=>({...c.items[0],id:800+i,ref:raw.ref,summary_ko:`행 ${i} 당시 요약`,raw_items:[{...raw,id:`snapshot-${i}`,raw:`행 ${i} 당시 원문 <태그> &`,ko:`행 ${i} 당시 해석`}]}));
  c.total=2;c.ok=2;c.failed=0;n.inputs=[];d.raw=[{...raw,raw:'공통 최신값 — 대신 표시하면 안 됨'}];
  const html=render(d,n.id);
  for(const i of [0,1]) {assert.ok(html.includes(`행 ${i} 당시 요약`));assert.ok(html.includes(escape(`행 ${i} 당시 원문 <태그> &`)));assert.ok(html.includes(`snapshot-${i}`));}
  assert.ok(!html.includes('공통 최신값'));
});

test('단계 전체 상세의 중복 ref 구형 행은 최신 원문으로 추측 보충하지 않는다',()=>{
  const {d,n,c,raw}=evidence();
  const {raw_items:_ignored,...base}=c.items[0];
  c.items=[{...base,id:801,ref:raw.ref},{...base,id:802,ref:raw.ref}];n.inputs=[];
  d.raw=[{...raw,raw:'최신 정상 원문은 과거 행에 사용 금지'}];
  const html=render(d,n.id);
  assert.ok(!html.includes('최신 정상 원문은 과거 행에 사용 금지'));
  assert.equal((html.match(/이 항목의 원문은 현재 응답에 없습니다/g)||[]).length,2);
});

test('빈 raw_items는 ref가 유일해도 공통 최신 묶음으로 보충하지 않는다',()=>{
  const {d,n,c,raw}=evidence();c.items=[{...c.items[0],ref:raw.ref,raw_items:[]}];n.inputs=[];
  d.raw=[{...raw,raw:'빈 스냅샷 대신 최신 원문 금지'}];
  const html=render(d,n.id);
  assert.ok(html.includes('원문 0줄'));assert.ok(html.includes('현재 응답에 없습니다'));
  assert.ok(!html.includes('빈 스냅샷 대신 최신 원문 금지'));
});

test('실패한 수집 상세는 원문·ref가 없어도 실패 사유와 행 요약을 보존한다',()=>{
  const {d,n,c}=evidence();const error='HTTP 503 <unavailable> & 원문 수집 실패';
  c.items=[{...c.items[0],id:803,ok:false,ref:null,raw_items:[],error,summary_ko:'당시 수집 연결이 끊겼습니다.'}];n.inputs=[];d.raw=[];
  const html=render(d,n.id);
  for(const s of ['수집 실패','실패 사유 원문','당시 수집 연결이 끊겼습니다.',escape(error),'이 항목의 원문은 현재 응답에 없습니다.'])assert.ok(html.includes(s),s);
});

test('유일 ref만 공통 원문을 사용하며 E1과 E10은 서로 섞지 않는다',()=>{
  const {d,n,c,raw}=evidence();const {raw_items:_ignored,...base}=c.items[0];
  c.items=[{...base,id:804,ref:'E1'}];n.inputs=[];
  d.raw=[{...raw,id:'one',ref:'E1',raw:'정확한 첫 ref'}, {...raw,id:'ten',ref:'E10',raw:'비슷하지만 다른 열번째 ref'}];
  const html=render(d,n.id);assert.ok(html.includes('정확한 첫 ref'));assert.ok(!html.includes('비슷하지만 다른 열번째 ref'));
});

test('Group 판단·전체 단계·역할·예산·진행은 긴 특수문자 본문도 생략하거나 HTML로 해석하지 않는다',()=>{
  const d=fixture(),n=d.flow.nodes.find(x=>x.type==='group')!,c=n.card as GroupCard;
  assert.equal(c.kind,'group');
  const long='<script>alert("x")</script> & 특수문자'.repeat(80);
  c.group_ko='검토 그룹';c.decision.final_reason_ko='선택 이유 '+long;
  c.decision.steps=[{key:'final',title_ko:'판단 과정 제목',detail_ko:'판단 상세 '+long,items_ko:['하위 판단 '+long],source:'policy'}];
  c.roles=[{...c.roles[0],role_ko:'분석 역할',model:'synthetic-model',job_ko:'역할 업무 '+long}];
  c.budget={max_agent_calls:0,max_cost_usd:0,deadline_s:0};c.progress_ko=['진행 기록 '+long];
  const html=render(d,n.id);
  for(const text of [c.decision.final_reason_ko,c.decision.steps[0].detail_ko,c.decision.steps[0].items_ko![0],c.roles[0].job_ko!,c.progress_ko[0],'synthetic-model','Agent 호출 최대 0회','비용 상한 0 USD','제한 시간 0초'])assert.ok(html.includes(escape(text)),text.slice(0,40));
  assert.ok(!html.includes('<script>'));
});

test('한글 해석 상세는 긴 원문과 해석의 정확한 쌍을 보존하고 비LLM·빈 해석을 섞지 않는다',()=>{
  const d=fixture(),base=d.flow.nodes[0];
  const n={...base,id:'interpret-regression',type:'interpret',card:null,inputs:[],conclusion_raw:null,conclusion_ko:null} as FlowNode;d.flow.nodes=[n];
  const origin=d.raw[0],raw='RAW <tag> & "quote"\n'.repeat(90),ko='한글 해석 <태그> & 특수문자\n'.repeat(90);
  d.raw=[{...origin,id:'translated-1',raw,ko,ko_source:'llm'}, {...origin,id:'not-llm',raw:'NOT_LLM_RAW',ko:'다른 출처의 해석',ko_source:'rules'}, {...origin,id:'empty-ko',raw:'EMPTY_KO_RAW',ko:'',ko_source:'llm'}];
  const html=render(d,n.id);
  assert.ok(html.includes('해석 1건'));assert.ok(html.includes(escape(raw)));assert.ok(html.includes(escape(ko)));
  assert.ok(!html.includes('NOT_LLM_RAW'));assert.ok(!html.includes('EMPTY_KO_RAW'));
});

test('최신 응답에서 사라진 단계와 완전히 빈 수집 목록은 추측 없이 표시한다',()=>{
  const {d,n,c}=evidence();
  const missing=render(d,'없어진-실행-id');assert.ok(missing.includes('이 단계는 최신 기록에 없습니다.'));
  c.items=[];c.total=0;c.ok=0;c.failed=0;n.inputs=[];d.raw=[];
  const html=render(d,n.id);assert.ok(html.includes('수집 0건 · 성공 0 · 실패 0'));assert.ok(html.includes('이 단계에 연결된 원문 기록이 없습니다.'));
});
