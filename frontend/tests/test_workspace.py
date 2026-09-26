"""Rehearsal data integrity, approval ownership, persistence and request boundaries."""
import copy
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from demo_workspace import Workspace, WorkspaceError
from demo_harness import compile_prep, latest_prep

EXAMPLE='Clara runs the money, Hugo confirmed. Bond switch: do it this week. Retirement still 2027.'

class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Workspace(ROOT/'fixtures',Path(self.tmp.name))
        self.data=self.store.data()
    def tearDown(self): self.tmp.cleanup()
    def record(self,cid): return next(r for r in self.data['records'] if r['client']['client_id']==cid)
    def test_cache_matches_recomputed_harness_and_has_no_truth(self):
        for r in self.data['records']:
            if r['prep'].get('engine')=='pregame-cabinet-harness':continue  # C01-C06: production harness output
            self.assertEqual({k:v for k,v in r['prep'].items() if k!='provenance'},compile_prep(r,self.data['as_of']))
        self.assertNotIn('answer_key',json.dumps(self.data))
    def test_every_citation_and_mark_resolves_to_visible_record(self):
        for r in self.data['records']:
            ids={n['id'] for n in r['notes']}|{e['id'] for e in r['events']}|{x['claim_id'] for x in r['approved_language']}|{x['ref_id'] for x in r['references']}
            for s in r['prep']['sections']:
                for row in s['rows']:
                    self.assertTrue(set(row.get('citations',[]))<=ids)
                    if row.get('mark'):self.assertTrue(row['citations'])
    def test_c08_conflict_promise_and_expiry(self):
        r=self.record('C08');rows=[x for s in r['prep']['sections'] for x in s['rows']]
        conflict=next(x for x in rows if x.get('attribute')=='decision_maker')
        self.assertEqual(conflict['mark'],'contradicted')
        self.assertTrue(conflict['expired'])
        self.assertIn('128 days',conflict['badge'])
        self.assertEqual(len([x for x in conflict['citations'] if x.startswith('E-')]),2)
        promise=next(x for x in rows if x.get('severity')==4)
        self.assertEqual(promise['citations'],['AS-01'])
        self.assertEqual(promise['action'],'Replace')
    def test_c02_style_vs_trades_question_from_harness(self):
        r=self.record('C02');q=r['prep']['sections'][4]['rows']
        if r['prep'].get('engine')!='pregame-cabinet-harness':self.skipTest('pregame harness unavailable at build time')
        style=next(x for x in q if x['attribute']=='investing_style')
        self.assertIn('single-stock buy',style['text'])
        self.assertTrue(any(c.startswith('E-') for c in style['citations']))
    def test_c04_harness_prep_and_scripted_event_kept(self):
        r=self.record('C04')
        self.assertTrue(any(e['id']=='E-DEMO-C04-0016' for e in r['events']))
        if r['prep'].get('engine')=='pregame-cabinet-harness':
            self.assertTrue(r['prep']['sections'][0]['rows'])
    def test_three_cards_correct_types_and_expiry(self):
        cards=self.store.propose('C08',EXAMPLE)['cards']
        self.assertEqual([c['attribute'] for c in cards],['decision_maker','bond_switch','retirement_date'])
        self.assertEqual([c['expiry_days'] for c in cards],[120,30,None])
        self.assertTrue(all(c['source']['author']=='banker' for c in cards))
        self.assertEqual(cards[0]['supersedes'],['N-0078','N-0081'])
    def test_approve_is_durable_idempotent_and_replaces_owner(self):
        cards=self.store.propose('C08',EXAMPLE)['cards']
        for card in cards:
            result=self.store.approve('C08',card['draft_id'],card['value'])
            self.assertEqual(self.store.approve('C08',card['draft_id'],card['value'])['note']['id'],result['note']['id'])
        reopened=Workspace(ROOT/'fixtures',Path(self.tmp.name)).data()
        r=next(r for r in reopened['records'] if r['client']['client_id']=='C08')
        self.assertEqual(len([n for n in r['notes'] if n.get('approved')]),3)
        for attr in ['decision_maker','bond_switch','retirement_date']:
            self.assertEqual(len(Workspace._active(r,attr)),1)
        self.assertTrue(next(n for n in r['notes'] if n['id']=='N-0081').get('superseded_by'))
        decision=next(x for x in r['prep']['sections'][0]['rows'] if x.get('attribute')=='decision_maker')
        self.assertNotIn('mark',decision)
        self.assertIn('Clara Adeyemi',decision['text'])
    def test_stale_concurrent_draft_is_rejected(self):
        a=self.store.propose('C08',EXAMPLE)['cards'][0]
        b=self.store.propose('C08',EXAMPLE)['cards'][0]
        self.store.approve('C08',a['draft_id'],a['value'])
        with self.assertRaises(WorkspaceError) as ctx:self.store.approve('C08',b['draft_id'],b['value'])
        self.assertEqual(ctx.exception.status,409)
    def test_edit_is_preserved_and_cannot_cross_clients(self):
        card=self.store.propose('C08',EXAMPLE)['cards'][0]
        with self.assertRaises(WorkspaceError):self.store.approve('C02',card['draft_id'],'Changed')
        saved=self.store.approve('C08',card['draft_id'],'Clara leads; consult both holders')['note']
        self.assertEqual(saved['value'],'Clara leads; consult both holders')
    def test_failed_disk_write_does_not_report_approval(self):
        card=self.store.propose('C08',EXAMPLE)['cards'][0]
        with patch('demo_workspace.os.replace',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.store.approve('C08',card['draft_id'],card['value'])
        self.assertFalse(self.store.state_path.exists())
        self.assertTrue(self.store.approve('C08',card['draft_id'],card['value'])['note']['approved'])
    def test_corrupt_store_fails_closed(self):
        self.store.state_path.write_text('{broken')
        with self.assertRaises(ValueError):self.store.data()
        self.assertEqual(self.store.state_path.read_text(),'{broken')
    def test_invalid_input_does_not_create_notes(self):
        for value in ['',None,'x'*6001,'Nothing supported here']:
            with self.assertRaises(WorkspaceError):self.store.propose('C08',value)
        self.assertFalse(self.store.state_path.exists())
    def test_no_invented_heldout_results(self):
        self.assertTrue(all(x['provenance']=='Recorded' for x in self.data['ledger']))
        self.assertNotIn('22 → 14',json.dumps(self.data['ledger']))
    def test_future_prep_selection_uses_next_not_last(self):
        r={'preps':[{'id':'a','date':'2026-09-25'},{'id':'b','date':'2026-09-28'},{'id':'c','date':'2026-10-10'}]}
        self.assertEqual(latest_prep(r,'2026-09-26')['id'],'b')

if __name__=='__main__': unittest.main()

class RequestBoundaryTests(unittest.TestCase):
    def setUp(self):
        import server
        from types import SimpleNamespace
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Workspace(ROOT/'fixtures',Path(self.tmp.name))
        self.handler=server.Handler.__new__(server.Handler)
        self.handler.app=SimpleNamespace(workspace=self.store)
        self.handler.client_address=('127.0.0.1',43210)
        self.handler.path='/api/workspace/propose'
        self.response=None
        self.handler.send_json=lambda status,body,*args:setattr(self,'response',(status,body))
    def tearDown(self):self.tmp.cleanup()
    def post(self,body,headers=None,path='/api/workspace/propose'):
        import io
        raw=json.dumps(body).encode()
        self.handler.path=path
        self.handler.headers={'Host':'127.0.0.1:8877','Origin':'http://127.0.0.1:8877',
                              'Content-Type':'application/json','Content-Length':str(len(raw)),
                              'X-Workspace-Token':self.store.token}
        self.handler.headers.update(headers or {})
        self.handler.rfile=io.BytesIO(raw)
        self.handler.do_POST()
        return self.response
    def test_local_propose_then_approve(self):
        status,body=self.post({'client_id':'C08','text':EXAMPLE})
        self.assertEqual(status,200)
        c=body['cards'][0]
        status,body=self.post({'client_id':'C08','draft_id':c['draft_id'],'value':c['value']},path='/api/workspace/approve')
        self.assertEqual(status,200)
        self.assertEqual(body['note']['storage'],'local_file')
    def test_cross_origin_missing_token_wrong_host_and_type_are_rejected(self):
        for headers,status in [({'Origin':'https://untrusted.example'},403),({'X-Workspace-Token':''},403),
                               ({'Host':'untrusted.example'},403),({'Content-Type':'text/plain'},415),
                               ({'Content-Length':'17000'},413)]:
            actual,_=self.post({'client_id':'C08','text':EXAMPLE},headers)
            self.assertEqual(actual,status)
        self.assertFalse(self.store.state_path.exists())
    def test_non_local_writes_are_rejected(self):
        self.handler.client_address=('192.0.2.1',43210)
        status,_=self.post({'client_id':'C08','text':EXAMPLE})
        self.assertEqual(status,403)
    def test_unrelated_posts_remain_read_only(self):
        status,_=self.post({},path='/api/db/cabinet/notes')
        self.assertEqual(status,405)
    def test_bad_json_shape_and_approval_ids(self):
        for body in [[],{'client_id':[]},{'client_id':'C08','draft_id':[]}]:
            status,_=self.post(body,path='/api/workspace/approve')
            self.assertEqual(status,400)
