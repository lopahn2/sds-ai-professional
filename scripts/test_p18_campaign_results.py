"""Cost-report integrity: unknown usage stays unknown, dedupe real run IDs, no fabricated currency bills."""
import importlib.util
from pathlib import Path
import unittest

SCRIPT=Path(__file__).with_name('p18-campaign-results.py')


def load():
    spec=importlib.util.spec_from_file_location('results',SCRIPT)
    assert spec and spec.loader
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


class ResultsContract(unittest.TestCase):
    def test_aggregator_exists(self):
        self.assertTrue(SCRIPT.exists(),'token aggregation is missing')

    @unittest.skipUnless(SCRIPT.exists(),'module not implemented')
    def test_missing_usage_is_not_free(self):
        m=load(); r=m.usage([{'id':1,'input_tokens':20,'output_tokens':5,'estimated_cost_usd':None},
                           {'id':2,'input_tokens':None,'output_tokens':None,'estimated_cost_usd':None}])
        self.assertEqual(r['known_tokens'],25)
        self.assertIsNone(r['total_tokens'])
        self.assertEqual(r['missing_usage_calls'],1)
        self.assertIsNone(r['estimated_cost_usd'])
        self.assertIsNone(r['actual_billed_usd'])

    @unittest.skipUnless(SCRIPT.exists(),'module not implemented')
    def test_duplicate_run_ids_count_once(self):
        m=load(); call={'id':12,'input_tokens':20,'output_tokens':5,'estimated_cost_usd':0.2}
        r=m.usage([call,dict(call)])
        self.assertEqual(r['calls'],1)
        self.assertEqual(r['total_tokens'],25)
        self.assertEqual(r['estimated_cost_usd'],0.2)

    @unittest.skipUnless(SCRIPT.exists(),'module not implemented')
    def test_observed_group_does_not_replace_planned_group(self):
        m=load(); rows=[{'id':'a','system':'payment','planned_group':'SIMPLE','state':'completed',
            'restoration':{'verified':True},'incidents':[{'incident':{'id':4,'status':'handed_off'},
            'routing':[{'level':'COMPLEX'}],'agent_runs':[{'id':12,'input_tokens':20,'output_tokens':5,'estimated_cost_usd':0.2}]}]}]
        r=m.summarize(rows)
        self.assertEqual(r['planned_groups']['SIMPLE']['cases'],1)
        self.assertEqual(r['observed_groups']['COMPLEX']['incidents'],1)
        self.assertEqual(r['case_rows'][0]['planned_group'],'SIMPLE')
        self.assertEqual(r['case_rows'][0]['observed_groups'],['COMPLEX'])

    @unittest.skipUnless(SCRIPT.exists(),'module not implemented')
    def test_report_compares_groups_states_matrix_and_cost(self):
        import tempfile
        m=load()
        def rec(i,planned,state,level,tokens,cost):
            inc=[] if level is None else [{'incident':{'id':i,'status':'closed'},'routing':[{'level':level}],
                 'agent_runs':[{'id':100+i,'input_tokens':tokens,'output_tokens':0,'estimated_cost_usd':cost}]}]
            return {'id':f'payment-{i:02d}','system':'payment','planned_group':planned,'state':state,
                    'restoration':{'verified':True},'fault_observation':{'verified':state!='fault_unobserved'},'incidents':inc}
        rows=[rec(1,'SIMPLE','completed','NORMAL',100,0.1),rec(2,'SIMPLE','fault_unobserved',None,0,0),
              rec(3,'COMPLEX','completed','COMPLEX',900,0.9),rec(4,'COMPLEX','analysis_incomplete','COMPLEX',300,None)]
        r=m.summarize(rows,planned_total=180)
        self.assertEqual(r['planned_groups']['SIMPLE']['states'],{'completed':1,'fault_unobserved':1})
        self.assertEqual(r['planned_groups']['SIMPLE']['known_estimated_cost_usd'],0.1)
        self.assertIsNone(r['planned_groups']['COMPLEX']['estimated_cost_usd'])
        self.assertEqual(r['planned_vs_observed']['SIMPLE'],{'NORMAL':1,'NO_INCIDENT':1})
        self.assertEqual(r['coverage'],{'planned':180,'recorded':4,'not_run':176})
        with tempfile.TemporaryDirectory(dir=str(Path.home()/'.hermes/cache/scratch')) as d:
            m.write_report(rows,Path(d),planned_total=180,attempts=[{'id':'payment-02','state':'indeterminate','path':'runs/payment-02'}])
            text=(Path(d)/'report.md').read_text()
        for needle in ('추정 비용(USD)','설계 그룹 → 실제 라우팅','시스템별','미실행 176','payment-02','indeterminate'):
            self.assertIn(needle,text)

    def test_cli_reports_progress_only_case_without_counting_it_as_final_or_free(self):
        import json
        import subprocess
        import sys
        import tempfile
        with tempfile.TemporaryDirectory(dir=str(Path.home()/'.hermes/cache/scratch')) as d:
            root=Path(d);records=root/'records';output=root/'report'
            (records/'member-01').mkdir(parents=True)
            (records/'member-01'/'result.json').write_text(json.dumps({
                'id':'member-01','system':'member','planned_group':'SIMPLE','state':'completed',
                'restoration':{'verified':True},'incidents':[]}))
            (records/'member-02').mkdir()
            (records/'member-02'/'progress.json').write_text(json.dumps({
                'id':'member-02','system':'member','planned_group':'SIMPLE','state':'injected'}))
            (records/'attempts'/'member-02-old').mkdir(parents=True)
            (records/'attempts'/'member-02-old'/'result.json').write_text(json.dumps({
                'id':'member-02','state':'indeterminate','incidents':[]}))
            run=subprocess.run([sys.executable,str(SCRIPT),'--records',str(records),
                '--output',str(output)],text=True,capture_output=True,check=True)
            observed=json.loads(run.stdout);summary=json.loads((output/'summary.json').read_text())
            self.assertEqual(observed['in_progress'],['member-02'])
            self.assertEqual(observed['cases'],1)
            self.assertEqual(summary['in_progress'],['member-02'])
            self.assertEqual(summary['coverage'],{'planned':180,'recorded':1,'in_progress':1,'not_run':178})
            self.assertEqual([r['id'] for r in summary['case_rows']],['member-01'])
            self.assertEqual(len(summary['excluded_attempts']),1)
            text=(output/'report.md').read_text()
            for needle in ('진행 중 1건','member-02','미실행 178건','완료 전 토큰·비용은 집계하지 않음'):
                self.assertIn(needle,text)

    def test_cli_excludes_interim_failed_result_until_usage_snapshot_is_final(self):
        import json
        import subprocess
        import sys
        import tempfile
        with tempfile.TemporaryDirectory(dir=str(Path.home()/'.hermes/cache/scratch')) as d:
            root=Path(d);records=root/'records';output=root/'report'
            folder=records/'member-03';folder.mkdir(parents=True)
            row={'id':'member-03','system':'member','planned_group':'SIMPLE','state':'failed',
                 'injection_started_at':'2026-10-26T00:00:00+00:00',
                 'drain':{'verified':True},'restoration':{'verified':True},'incidents':[]}
            for finalized in (False,True):
                with self.subTest(finalized=finalized):
                    if finalized:
                        row.update(recorded_at='2026-10-26T00:05:00+00:00',
                                   usage_snapshot_at='2026-10-26T00:05:00+00:00')
                    (folder/'result.json').write_text(json.dumps(row))
                    subprocess.run([sys.executable,str(SCRIPT),'--records',str(records),
                        '--output',str(output)],text=True,capture_output=True,check=True)
                    summary=json.loads((output/'summary.json').read_text())
                    self.assertEqual(summary['executed_case_records'],int(finalized))
                    self.assertEqual(summary['in_progress'],[] if finalized else ['member-03'])

    def test_cli_labels_archived_zero_as_attributed_only_with_excluded_calls(self):
        import json
        import subprocess
        import sys
        import tempfile
        with tempfile.TemporaryDirectory(dir=str(Path.home()/'.hermes/cache/scratch')) as d:
            root=Path(d);records=root/'records';records.mkdir();output=root/'report'
            call={'id':91,'input_tokens':20,'output_tokens':5,'estimated_cost_usd':0.2}
            attempt={'id':'member-02','state':'indeterminate','incidents':[],
                     'excluded_incidents':[{'incident':{'id':81},'agent_runs':[call,dict(call)]}]}
            source=root/'archived-result.json';source.write_text(json.dumps(attempt))
            subprocess.run([sys.executable,str(SCRIPT),'--records',str(records),'--output',str(output),
                '--extra-attempt',str(source)],text=True,capture_output=True,check=True)
            summary=json.loads((output/'summary.json').read_text());row=summary['excluded_attempts'][0]
            self.assertEqual(row.get('usage_scope'),'attributed_incidents_only')
            self.assertEqual(row['known_tokens'],0)
            self.assertEqual(row['estimated_cost_usd'],0)
            self.assertEqual(row['excluded_incidents_count'],1)
            self.assertEqual(row['excluded_observed_calls'],1)
            self.assertEqual(summary['overall']['known_tokens'],0)
            self.assertIsNone(row['actual_billed_usd'])
            text=(output/'report.md').read_text()
            for needle in ('귀속 토큰','귀속 추정 비용','제외 사건','제외 관측 호출',
                           '귀속된 사건만','시도 전체 비용이 0이라는 뜻이 아님'):
                self.assertIn(needle,text)

    def test_cli_rejects_one_run_owned_by_different_cases_or_incidents(self):
        import copy
        import json
        import subprocess
        import sys
        import tempfile
        call={'id':900001,'input_tokens':20,'output_tokens':5,'estimated_cost_usd':0.2,
              'model':'probe-model','reasoning_effort':'probe','role':'probe'}
        first={'id':'probe-a','system':'probe','planned_group':'SIMPLE','state':'completed',
               'restoration':{'verified':True},'incidents':[{'incident':{'id':800001},
               'routing':[{'level':'NORMAL'}],'agent_runs':[call]}]}
        for ownership in ('different_cases','different_incidents'):
            with self.subTest(ownership=ownership), tempfile.TemporaryDirectory(
                    dir=str(Path.home()/'.hermes/cache/scratch')) as d:
                root=Path(d);records=root/'records';output=root/'report'
                second=copy.deepcopy(first);second['id']='probe-b'
                second['incidents'][0]['incident']['id']=800002
                inputs=[first,second]
                if ownership=='different_incidents':
                    combined=copy.deepcopy(first);combined['incidents']+=second['incidents'];inputs=[combined]
                for record in inputs:
                    folder=records/record['id'];folder.mkdir(parents=True)
                    (folder/'result.json').write_text(json.dumps(record))
                run=subprocess.run([sys.executable,str(SCRIPT),'--records',str(records),
                    '--output',str(output)],text=True,capture_output=True)
                self.assertNotEqual(run.returncode,0)
                self.assertIn('agent run 900001 attributed to multiple cases or incidents',run.stderr)
                self.assertFalse((output/'summary.json').exists())
        same=copy.deepcopy(first);same['incidents'][0]['agent_runs'].append(dict(call))
        summary=load().summarize([same])
        self.assertEqual(summary['overall']['calls'],1)
        self.assertEqual(summary['overall']['known_tokens'],25)
        self.assertEqual(summary['planned_groups']['SIMPLE']['calls'],1)
        self.assertEqual(summary['observed_groups']['NORMAL']['calls'],1)


if __name__=='__main__':unittest.main()
