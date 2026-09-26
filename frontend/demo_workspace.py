"""Local-only rehearsal note store. Never opens a MongoDB write connection."""
from __future__ import annotations
import copy
import datetime as dt
import json
import os
import re
import secrets
import threading
import uuid
from pathlib import Path
from demo_harness import POLICY, TITLES, attribute, compile_prep


class WorkspaceError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Workspace:
    def __init__(self, fixtures_dir, state_dir):
        self.fixtures_dir = Path(fixtures_dir)
        self.state_path = Path(state_dir) / 'approved-notes.json'
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.drafts = {}

    def _cache(self):
        return json.loads((self.fixtures_dir / 'workspace-demo.json').read_text())

    def _state(self):
        if not self.state_path.exists(): return {"notes": []}
        # Fail closed if the note store is corrupt; never replace it with an empty store.
        return json.loads(self.state_path.read_text())

    @staticmethod
    def _merge(record, state):
        saved = [copy.deepcopy(n) for n in state['notes'] if n['client_id'] == record['client']['client_id']]
        record['notes'] += saved
        for new in saved:
            for old in record['notes']:
                if old['id'] in new['supersedes']:
                    old.update(superseded_by=new['id'], superseded_date=new['date'])
        return record

    def data(self, demo=False):
        with self.lock:
            data, state = self._cache(), self._state()
            for record in data['records']:
                self._merge(record, state)
                if any(n.get('approved') for n in record['notes']):
                    record['prep'] = compile_prep(record, data['as_of'])
            # Private snapshots may carry truth; reveal only a boolean for the selected
            # baseline and only when demo=1. Public snapshots never carry demo dots.
            meta_path = self.fixtures_dir / '_meta.json'
            meta = json.loads(meta_path.read_text()) if meta_path.exists() else {'public': True}
            truth_path = self.fixtures_dir / 'db_cabinet_truth.answer_key.json'
            if demo and not meta.get('public', True) and truth_path.exists():
                truth = json.loads(truth_path.read_text()).get('docs', [])
                for record in data['records']:
                    record['demo_has_fault'] = any(f.get('client_id') == record['client']['client_id'] and
                        f.get('doc_id') == record['prep'].get('baseline_id') for f in truth)
            data['csrf_token'] = self.token
            data['storage'] = 'local_file'
            return data

    def _record(self, cid):
        record = next((r for r in self._cache()['records'] if r['client']['client_id'] == cid), None)
        if record is None: raise WorkspaceError(404, 'Client not found.')
        return self._merge(record, self._state())

    @staticmethod
    def _active(record, attr):
        return [n for n in record['notes'] if not n.get('superseded_by') and
                (n.get('attribute') or attribute(n['text'])) == attr]

    def propose(self, cid, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 6000:
            raise WorkspaceError(400, 'Enter a call debrief of 1–6,000 characters.')
        with self.lock:
            record = self._record(cid)
            values = []
            names = [h['name'] for h in record['client'].get('household', [])]
            for name in names:
                first = re.escape(name.split()[0])
                if re.search(rf'\b{first}\s+(?:runs the money|runs the household finances|makes (?:the )?decisions)', text, re.I):
                    values.append(('decision_maker',name,'behavioural'))
                    break
            match = re.search(r'bond switch\s*:\s*([^.!?\n]+)', text, re.I)
            if match: values.append(('bond_switch',match.group(1).strip(),'contact'))
            match = re.search(r'retirement\s*(?::|still|is|in)?\s*((?:20\d\d)(?:-\d\d)?)\b', text, re.I)
            if match: values.append(('retirement_date',match.group(1),'stated'))
            if not values:
                raise WorkspaceError(422, 'The local rehearsal parser supports decision maker, “Bond switch: …”, and “Retirement …”. Review the example or use those labels.')
            today = dt.date.today()
            cards = []
            batch_id = uuid.uuid4().hex
            for attr, value, kind in values:
                prior = self._active(record,attr)
                days = POLICY[kind + '_days']
                card = {'draft_id':uuid.uuid4().hex,'batch_id':batch_id,'client_id':cid,'attribute':attr,
                        'title':TITLES[attr],'value':value,'date':today.isoformat(),'source':{'author':'banker','kind':'call','date':today.isoformat()},
                        'fact_type':kind,'expires_at':(today+dt.timedelta(days=days)).isoformat() if days else None,
                        'expiry_days':days,'supersedes':[n['id'] for n in prior],
                        'previous':[{'id':n['id'],'text':n['text']} for n in sorted(prior,key=lambda n:n['date'],reverse=True)],
                        'call_text':text}
                self.drafts[card['draft_id']] = card
                cards.append(copy.deepcopy(card))
            # Bound local rehearsal memory without expiring this batch.
            if len(self.drafts) > 1000:
                self.drafts = {c['draft_id']:c for c in cards}
            return {'cards':cards,'mode':'local_rehearsal','message':'Drafts from the local rehearsal parser. Review each value before approving.'}

    def approve(self, cid, draft_id, value):
        if not isinstance(value,str) or not value.strip() or len(value) > 500:
            raise WorkspaceError(400, 'A note value must contain 1–500 characters.')
        with self.lock:
            state = self._state()
            previous = next((n for n in state['notes'] if n.get('draft_id') == draft_id and n['client_id'] == cid),None)
            if previous:
                return {'note':previous,'already_saved':True,'record':self._record(cid)}
            draft = self.drafts.get(draft_id)
            if not draft or draft['client_id'] != cid:
                raise WorkspaceError(404, 'This draft is no longer available. Propose notes again.')
            record = self._record(cid)
            active = {n['id'] for n in self._active(record,draft['attribute'])}
            if active != set(draft['supersedes']):
                raise WorkspaceError(409, 'This fact changed after the draft was created. Propose notes again to review its current owner.')
            note = {k:copy.deepcopy(draft[k]) for k in ('client_id','attribute','date','source','fact_type','expires_at','expiry_days','supersedes','draft_id')}
            note.update(id='N-CALL-'+uuid.uuid4().hex[:12],author='banker',kind='note',approved=True,
                        value=value.strip(),text=f"{draft['title']}: {value.strip()}",storage='local_file')
            state['notes'].append(note)
            self.state_path.parent.mkdir(parents=True,exist_ok=True)
            tmp = self.state_path.with_suffix('.tmp')
            try:
                with tmp.open('w',encoding='utf-8') as f:
                    json.dump(state,f,ensure_ascii=False,indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp,self.state_path)
            finally:
                if tmp.exists(): tmp.unlink()
            return {'note':note,'already_saved':False,'record':self._record(cid)}
