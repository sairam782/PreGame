"""Build a portable rehearsal cache without modifying recorded cabinet fixtures."""
import copy
import json
import os
import sys
from pathlib import Path
from demo_harness import compile_prep, latest_prep

ROOT = Path(__file__).resolve().parent
TODAY = "2026-09-26"
HARNESS_CLIENTS = ("C01", "C02", "C03", "C04", "C05", "C06")
HARNESS_PROVENANCE = ("Pregame harness (production rules), code-written claims; "
                      "scored separately against the answer key")
SCRIPTED_PROVENANCE = "Scripted rehearsal client: local rehearsal rules, not the production harness"


def _text_block(text, heading):
    """The '- ' lines under one heading of a harness client-file text."""
    out, inside = [], False
    for line in text.splitlines():
        if line.strip() == heading:
            inside = True
            continue
        if inside:
            if not line.strip():
                break
            if line.startswith("- ") and line.strip() not in ("- (none)", "- (nothing on file)"):
                out.append(line[2:].strip())
    return out


def harness_preps():
    """Latest prep per client from pregame.cabinet's HARNESS_POLICY, or None if pregame cannot be loaded.
    Visible data only: Atlas `cabinet` when MONGODB_URI is set (falls back to files), else the visible files."""
    try:
        sys.path.insert(0, str(ROOT.parent))
        from pregame import cabinet
        client = None
        if os.environ.get("MONGODB_URI"):
            try:
                from pymongo import MongoClient
                client = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=5000)
            except ImportError:
                print("pymongo not installed; reading the visible files")
        data = cabinet.load_data(client)
        latest = {}
        for prep in cabinet.write_preps(data, cabinet.HARNESS_POLICY):
            if prep["client_id"] not in latest or prep["date"] >= latest[prep["client_id"]]["date"]:
                latest[prep["client_id"]] = prep
        print(f"Harness preps from {data['source']}")
        return latest
    except Exception as exc:  # fallback: the rehearsal adapter, as before
        print(f"pregame harness unavailable ({type(exc).__name__}: {exc}); using the rehearsal adapter")
        return None


def harness_to_prep(record, prep):
    """Map a harness prep's claims into the front end's sections/rows/source-chip shape."""
    ids = {n["id"] for n in record["notes"]} | {e["id"] for e in record["events"]}
    ids |= {a["claim_id"] for a in record["approved_language"]} | {r["ref_id"] for r in record["references"]}
    cite = lambda basis: [b for b in dict.fromkeys(basis or []) if b in ids]
    text = prep["text"]
    lines = {"observation": _text_block(text, "Client file (current view; each line dated and sourced):"),
             "question": _text_block(text, "Call questions:"), "flag": _text_block(text, "Watch-outs:")}
    used = {k: 0 for k in lines}
    key_rows, changes, disclosures, questions, other = [], [], [], [], []
    for i, claim in enumerate(prep["claims"]):
        kind, attr = claim["kind"], claim["attribute"]
        row = {"id": f"h-{kind}-{i}", "attribute": attr, "checked": True, "citations": cite(claim.get("basis"))}
        if kind in lines and used[kind] < len(lines[kind]):
            row["text"] = lines[kind][used[kind]]
            used[kind] += 1
        if kind == "observation":
            if attr == "decision_maker" and isinstance(claim["value"], list):
                row["badge"] = "Brief both holders"
            key_rows.append(row)
        elif kind == "question":
            if "older than" in row.get("text", ""):
                row["expired"] = True
            questions.append(row)
        elif kind == "flag" and row["citations"]:
            row.update(mark="feed", reason="Harness watch-out: a 'No changes' note while the client's state moved.")
            changes.append(row)
        elif kind == "disclosure":
            row.update(text=claim.get("text", ""), citations=cite([claim["value"]]),
                       badge=f"Locked wording {claim['value']}, written by code")
            disclosures.append(row)
        else:  # unclear mapping: a plain bullet with its source chips
            row.setdefault("text", f"{attr}: {claim.get('value')} ({kind})")
            other.append(row)
    groups = [{"id": "key", "title": "Key points", "rows": key_rows + other},
              {"id": "changes", "title": "What changed since last call", "rows": changes},
              {"id": "holdings", "title": "Holdings", "rows": []},
              {"id": "disclosures", "title": "Disclosures", "rows": disclosures},
              {"id": "questions", "title": "Questions to ask", "rows": questions}]
    rows = [r for g in groups for r in g["rows"]]
    return {"available": True, "engine": "pregame-cabinet-harness", "policy": "harness",
            "provenance": HARNESS_PROVENANCE, "as_of": prep["date"], "baseline_id": prep["prep_id"],
            "harness_text": text, "sections": groups,
            "counters": {"checked": sum(bool(r.get("checked")) for r in rows),
                         "flagged": sum(bool(r.get("mark")) and r.get("checked", False) for r in rows),
                         "expired": sum(bool(r.get("expired")) for r in rows)}}

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
    harness = harness_preps()
    for record in records:
        cid = record['client']['client_id']
        if harness and cid in HARNESS_CLIENTS and cid in harness:
            record['prep'] = harness_to_prep(record, harness[cid])
        else:
            record['prep'] = compile_prep(record,TODAY)
            if cid not in HARNESS_CLIENTS:
                record['prep']['provenance'] = SCRIPTED_PROVENANCE
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
          'provenance':('C01–C06 preparations: ' + HARNESS_PROVENANCE + '. ' if harness else '') + 'C08 and the marked C02/C04 rehearsal additions are scripted fixtures from Fran’s brief; their checks are deterministic rehearsal output, not production evaluation results.'}
    target=ROOT/'fixtures'/'workspace-demo.json'
    target.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    print(f'Built {len(records)} clients; cached checks with resolvable source IDs.')

if __name__=='__main__': build()
