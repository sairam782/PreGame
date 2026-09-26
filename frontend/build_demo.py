"""Build a portable rehearsal cache without modifying recorded cabinet fixtures."""
import copy
import json
from pathlib import Path
from demo_harness import compile_prep, latest_prep

ROOT = Path(__file__).resolve().parent
TODAY = "2026-09-26"

def load(name):
    return json.loads((ROOT / 'fixtures' / f'{name}.json').read_text())

def build():
    approved = load('db_cabinet.approved_language')['docs']
    refs = load('db_cabinet.reference_data')['docs']
    market = load('db_cabinet.market')['docs']
    book = load('db_cabinet.book_summary')['docs']
    records = []
    for item in load('cabinet_clients'):
        data = load('cabinet_client_' + item['client_id'])
        record = {'client': data['client'], 'notes': [x for x in data['timeline'] if x['kind']=='note'],
                  'events':[x for x in data['timeline'] if x['kind']=='feed'],
                  'preps':[x for x in data['timeline'] if x['kind']=='prep'], 'approved_language':approved,
                  'references':refs, 'market':market, 'fixture_note':'Recorded synthetic cabinet snapshot.', 'harness_questions':[]}
        # book_summary is preferred where it has a matching tier line.
        import re
        for doc in book:
            for line in doc.get('lines', []):
                if item['name'].split()[-1] in line.get('text',''):
                    match = re.search(r'Tier ([A-Z])', line['text'])
                    if match: record['client']['tier'] = match.group(1)
        records.append(record)
    overlay = load('rehearsal_overlay')
    for record in records:
        cid = record['client']['client_id']
        additions = overlay.get('existing_clients',{}).get(cid)
        if additions:
            record['events'] += additions.get('events',[])
            if additions.get('next_prep'):
                prep = copy.deepcopy(max(record['preps'], key=lambda p:p['date']))
                prep.update(id=f'P-DEMO-{cid}-0926',date=TODAY)
                prep['text'] = re.sub(r'\(2026-\d\d-\d\d\)',f'({TODAY})',prep['text'],count=1)
                record['preps'].append(prep)
            record['fixture_note'] = additions['label']
    for record in overlay['new_clients']:
        record.update(approved_language=approved,references=refs,market=[],harness_questions=[])
        records.append(record)
    for record in records:
        record['prep'] = compile_prep(record,TODAY)
        future = [p for p in record['preps'] if p['date'][:10] >= TODAY]
        record['next_call'] = min((p['date'] for p in future), default=None)
    versions = load('versions').get('versions',[])
    proposals = {p['id']:p for p in load('proposals')}
    ledger=[]
    for version in versions:
        proposal = proposals.get(version.get('proposal_id'))
        if not proposal: continue
        candidate, champion = proposal.get('heldout_candidate') or {}, proposal.get('heldout_champion') or {}
        result = 'Not recorded'
        if candidate.get('mean_accuracy') is not None and champion.get('mean_accuracy') is not None:
            result = f"Held-out accuracy {champion['mean_accuracy']:.0%} → {candidate['mean_accuracy']:.0%}"
        ledger.append({'time':version.get('created_at') or version.get('created_sim'),
                       'proposal':'; '.join(proposal.get('diff',[])) or proposal.get('rationale',''),
                       'result':result,'tier':proposal.get('tier','—'),'approver':version.get('approved_by','—'),
                       'version':version.get('version'), 'id':version.get('id'), 'provenance':'Recorded'})
    data={'schema_version':1,'mode':'local_rehearsal','as_of':TODAY,'records':records,'ledger':sorted(ledger,key=lambda x:x['time'] or '',reverse=True),
          'policy':{'behavioural_days':120,'contact_days':30,'stated_days':None},
          'ledger_note':'The rehearsal uses a 120-day behavioural-label policy. No recorded approval or held-out result for that policy is present in this snapshot.',
          'provenance':'C08 and the marked C02/C04 rehearsal additions are scripted fixtures from Fran’s brief. Checks are deterministic rehearsal output, not production evaluation results.'}
    target=ROOT/'fixtures'/'workspace-demo.json'
    target.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    print(f'Built {len(records)} clients; cached checks with resolvable source IDs.')

if __name__=='__main__': build()
