"""C01-C06 preparations come from pregame.cabinet HARNESS_POLICY; C08 stays scripted and labelled."""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = json.loads((ROOT / 'fixtures' / 'workspace-demo.json').read_text(encoding='utf-8'))
RECORDS = {r['client']['client_id']: r for r in DATA['records']}
HARNESS = RECORDS['C03']['prep'].get('engine') == 'pregame-cabinet-harness'


@unittest.skipUnless(HARNESS, 'cache was built without pregame (fallback)')
class HarnessMappingTests(unittest.TestCase):
    def test_c01_to_c06_use_production_harness_with_provenance(self):
        for cid in ('C01', 'C02', 'C03', 'C04', 'C05', 'C06'):
            prep = RECORDS[cid]['prep']
            self.assertEqual(prep['engine'], 'pregame-cabinet-harness')
            self.assertIn('Pregame harness (production rules)', prep['provenance'])
            self.assertEqual([s['id'] for s in prep['sections']], ['key', 'changes', 'holdings', 'disclosures', 'questions'])

    def test_c08_is_scripted_and_labelled(self):
        prep = RECORDS['C08']['prep']
        self.assertEqual(prep['engine'], 'local-rehearsal-rules-v1')
        self.assertIn('Scripted', prep['provenance'])

    def test_c03_briefs_both_holders(self):
        rows = RECORDS['C03']['prep']['sections'][0]['rows']
        dm = next(r for r in rows if r['attribute'] == 'decision_maker')
        self.assertIn('brief both holders', dm['text'])
        self.assertIn('Tom Brennan', dm['text']); self.assertIn('Lisa Brennan', dm['text'])
        self.assertTrue(dm['citations'])

    def test_c02_asks_style_vs_trades(self):
        q = RECORDS['C02']['prep']['sections'][4]['rows']
        self.assertTrue(any(r['attribute'] == 'investing_style' and 'single-stock' in r['text'] for r in q))

    def test_disclosures_are_locked_ids(self):
        for cid in ('C01', 'C02', 'C03', 'C04', 'C05', 'C06'):
            rows = RECORDS[cid]['prep']['sections'][3]['rows']
            self.assertEqual(sorted(c for r in rows for c in r['citations']), ['AS-01', 'AS-02'])


if __name__ == '__main__':
    unittest.main()
