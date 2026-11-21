import {object, word, type Data} from './unified.ts';
import {records} from './releaseEvidenceModel.ts';

const numerals = ['Zero','One','Two','Three','Four','Five','Six','Seven','Eight','Nine','Ten'];

// Older JUnit receipts carry class and method names. Translate only the two
// known inventory operations and retain the exact identifier for every row.
export function testReading(id:string, service:string, title?:string|null) {
  if (title) return title;
  const method=id.split(/::|#/).at(-1) || id;
  if (service==='inventory') {
    const reserve=/^reserves(\d+|[A-Z][a-z]+)WhenFaultDisabled$/.exec(method);
    if (reserve) {
      const quantity=/^\d+$/.test(reserve[1])?Number(reserve[1]):numerals.indexOf(reserve[1]);
      if (quantity>=0) return '재고 '+quantity+'개 예약';
    }
    const lookup=/^looksUp([A-Z])WhenFaultDisabled$/.exec(method);
    if (lookup) return '재고 '+lookup[1]+' 조회';
  }
  return method;
}

export function testComparison(patch:Data,release:Data,build:Data,service:string) {
  const tests=object(release.tests ?? build.tests);
  const runs=records(tests.executed_tests);
  const added=Array.isArray(patch.tests_added)?[...new Set(patch.tests_added.filter((id):id is string=>typeof id==='string' && !!id))]:[];
  const ids=added.length?added:[...new Set(runs.map(run=>word(run.id)).filter((id):id is string=>!!id))];
  const rows=ids.map(id=>{
    const matching=runs.filter(run=>run.id===id);
    const status=(phase:string)=>{
      const values=[...new Set(matching.filter(run=>run.phase===phase).map(run=>run.status))];
      return values.length===1 ? ({passed:'통과',failed:'실패',error:'오류',skipped:'건너뜀'}[String(values[0])] ?? '미관측') : '미관측';
    };
    const linked=records(object(release.qa).developer_tests).find(row=>row.test_id===id);
    return {id,label:testReading(id,service,word(linked?.name_ko)),before:status('before'),after:status('after')};
  });
  const total=typeof tests.total==='number'?tests.total:null;
  const failed=typeof tests.failed==='number'?tests.failed:null;
  const after=runs.filter(row=>row.phase==='after');
  const passed=after.length===total && new Set(after.map(row=>row.id)).size===total
    ? after.filter(row=>row.status==='passed').length : null;
  return {rows, added:Array.isArray(patch.tests_added)?added.length:null, total, failed, passed};
}
