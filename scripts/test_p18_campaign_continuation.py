"""장기 순차 캠페인 계속/중단·quota 대기·재개 정책 회귀(실제 주입 없음)."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('continuation_runner',ROOT/'scripts/p18-campaign-run.py')
assert spec and spec.loader
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)

OK={'drain':{'verified':True},'restoration':{'verified':True}}


class Continuation(unittest.TestCase):
    def test_recorded_failures_continue_only_after_verified_restore(self):
        for state in ('completed','fault_unobserved','analysis_incomplete','failed'):
            self.assertEqual(r.after_case({**OK,'state':state}),'continue',state)
        self.assertEqual(r.after_case({**OK,'state':'indeterminate'}),'stop')
        self.assertEqual(r.after_case({**OK,'state':'interrupted'}),'stop')
        self.assertEqual(r.after_case({'state':'fault_unobserved','drain':{'verified':True},'restoration':{'verified':False}}),'stop')
        self.assertEqual(r.after_case({**OK,'state':'failed','quota_wait':[{'state':'cooldown'}]}),'retry')

    def test_resume_skips_recorded_retries_quota_and_blocks_unknown(self):
        self.assertEqual(r.resume_action({**OK,'state':'completed'}),'skip')
        self.assertEqual(r.resume_action({**OK,'state':'fault_unobserved'}),'skip')
        self.assertEqual(r.resume_action({**OK,'state':'failed','quota_wait':[{}]}),'retry')
        self.assertEqual(r.resume_action({**OK,'state':'interrupted'}),'retry')
        self.assertEqual(r.resume_action({**OK,'state':'indeterminate'}),'block')
        self.assertEqual(r.resume_action({'state':'completed','drain':{'verified':True},'restoration':{'verified':False}}),'block')
        self.assertEqual(r.resume_action({'state':'preparing'}),'block')

    def test_quota_gate_waits_and_records_without_inventing_availability(self):
        h=r.Harness.__new__(r.Harness);h.args=NS(quota_wait_limit=100,quota_poll_seconds=10)
        answers=[{'accounts':[{'state':'cooldown'}]},{'accounts':[{'state':'cooldown'},{'state':'available'}]}]
        h.api=lambda path,*a,**k:answers.pop(0)
        waits=[]
        with patch.object(r.time,'monotonic',side_effect=[0,0,10]),patch.object(r.time,'sleep'):
            self.assertTrue(h.wait_for_quota(waits.append))
        self.assertEqual(len(waits),1)
        self.assertEqual(waits[0]['accounts'],['cooldown'])
        h.api=lambda path,*a,**k:{'accounts':[]}
        with patch.object(r.time,'monotonic',side_effect=[0,0,200]),patch.object(r.time,'sleep'):
            self.assertFalse(h.wait_for_quota(waits.append))

    def test_existing_analysis_waits_instead_of_injecting(self):
        h=r.Harness.__new__(r.Harness);h.ns='dev';h.args=NS(preexisting_wait=50)
        lists=[[{'id':9,'namespace':'dev','status':'analyzing'}],[{'id':9,'namespace':'dev','status':'handed_off'}]]
        h.incident_listing=lambda:lists.pop(0)
        with patch.object(r.time,'monotonic',side_effect=[0,0,5]),patch.object(r.time,'sleep'):
            listing,waited=h.wait_no_active_incidents()
        self.assertEqual(waited,[9]);self.assertEqual(listing[0]['status'],'handed_off')
        h.incident_listing=lambda:[{'id':9,'namespace':'dev','status':'analyzing'}]
        with patch.object(r.time,'monotonic',side_effect=[0,0,60]),patch.object(r.time,'sleep'):
            with self.assertRaises(RuntimeError):h.wait_no_active_incidents()


    def test_listing_retries_snapshot_when_new_incident_shifts_pages(self):
        # 목록 조회 중 새 사건이 생겨 total이 바뀌면 처음부터 다시 읽는다(일관된 snapshot). 계속 흔들리면 실패.
        h=r.Harness.__new__(r.Harness);calls=[]
        def api(path,*a,**k):
            calls.append(path);offset=int(path.rsplit('=',1)[1])
            total=600 if len(calls)<=1 else 601
            start=offset
            return {'total':total,'items':[{'id':total-i} for i in range(start,min(start+500,total))]}
        h.api=api
        with patch.object(r.time,'sleep'):
            items=h.incident_listing()
        self.assertEqual(len(items),601)
        h.api=lambda path,*a,**k:{'total':600+len(calls)+(calls.append(path) or 0),'items':[{'id':i} for i in range(500)]}
        with patch.object(r.time,'sleep'):
            with self.assertRaisesRegex(RuntimeError,'pagination'):h.incident_listing()


if __name__=='__main__':unittest.main()
