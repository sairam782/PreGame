/* Local client workspace. Every marked claim is backed by a resolvable record ID. */
(() => {
  'use strict';
  const $ = s => document.querySelector(s);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const date = (value, year = false) => value ? new Date(String(value).slice(0,10) + 'T12:00:00Z').toLocaleDateString('en-GB', {day:'numeric',month:'short',...(year ? {year:'numeric'} : {}),timeZone:'UTC'}) : '—';
  const money = n => typeof n === 'number' ? new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:0}).format(n) : '—';
  const initials = name => name.split(/[ &]+/).filter(Boolean).slice(0,2).map(x=>x[0]).join('');
  const wait = ms => new Promise(resolve => setTimeout(resolve,ms));
  const example = 'Clara runs the money, Hugo confirmed.\nBond switch: do it this week.\nRetirement still 2027.';
  let data, current, selectedTab = 'notes', epoch = 0, toastTimer, saving = false;
  const sessions = new Map();
  const session = () => sessions.get(current.client.client_id);
  const allRows = record => record.prep.sections.flatMap(section => section.rows);
  const demoMode = new URLSearchParams(location.search).get('demo') === '1';
  const getSession = cid => {
    if (!sessions.has(cid)) sessions.set(cid,{mode:'prepare',prepared:false,shown:0,actions:{},questions:[],text:'',cards:[],busy:false});
    return sessions.get(cid);
  };
  function notify(text) {
    const el = $('#notification'); el.textContent=text; el.classList.add('visible');
    clearTimeout(toastTimer); toastTimer=setTimeout(()=>el.classList.remove('visible'),5500);
  }
  async function request(path, body) {
    const response = await fetch(path,body ? {method:'POST',headers:{'Content-Type':'application/json','X-Workspace-Token':data.csrf_token || ''},body:JSON.stringify(body)} : {});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'The workspace is unavailable. Try again.');
    return result;
  }
  const sourceIndex = record => {
    const refs = new Map();
    record.notes.forEach(n=>refs.set(n.id,'notes'));
    [...record.events,...(record.market||[])].forEach(e=>refs.set(e.id || e.event_id,'activity'));
    record.approved_language.forEach(r=>refs.set(r.claim_id,'documents'));
    record.references.forEach(r=>refs.set(r.ref_id,'documents'));
    (record.client.accounts||[]).forEach(r=>refs.set(r.account_id,'holdings'));
    return refs;
  };
  function citations(ids = []) {
    const refs = sourceIndex(current);
    return ids.filter(id=>refs.has(id)).map(id=>`<button class="citation" data-cite="${escape(id)}" aria-label="Show source ${escape(id)}">${escape(id)}</button>`).join('');
  }
  function renderClients() {
    $('#client-count').textContent=data.records.length;
    $('#clients').innerHTML=data.records.map(r=>`<button class="client-row" data-client="${escape(r.client.client_id)}" aria-current="${r===current}"><span class="client-avatar" aria-hidden="true">${escape(initials(r.client.name))}</span><span class="client-info"><span class="client-name">${escape(r.client.name)}${demoMode && r.demo_has_fault ? '<span class="client-dot" aria-label="Latest baseline has a recorded fault"></span>' : ''}</span><span class="client-meta">${escape(r.client.client_id)}${r.client.tier ? ` · Tier ${escape(r.client.tier)}` : ''}</span>${r.next_call ? `<span class="next-call">Next call ${date(r.next_call)}</span>` : ''}</span></button>`).join('');
    $('#clients [aria-current="true"]')?.scrollIntoView({block:'nearest',behavior:'auto'});
    // Fault dots deliberately absent: this public cache contains no answer_key.
    // A live adapter may add dots only with ?demo=1 and a latest-prep answer-key record.
  }
  function selectClient(cid) {
    const record = data.records.find(r=>r.client.client_id===cid); if (!record) return;
    if(current && session().busy){session().shown=current.prep.sections.length;session().busy=false;}
    epoch++; current=record; const s=getSession(cid); s.busy=false;
    $('#file-id').textContent=cid; $('#file-name').textContent=record.client.name;
    $('#file-detail').textContent=(record.client.accounts||[]).map(a=>a.type).join(' · ') + (record.client.tier ? ` · Tier ${record.client.tier}` : '');
    $('#assistant-client').textContent=record.client.name;
    $('#workspace-date').textContent=date(data.as_of,true);
    renderClients(); selectTab('notes'); renderAssistant();
  }
  function selectTab(tab, focus=false) {
    selectedTab=tab;
    document.querySelectorAll('[data-tab]').forEach(b=>{const active=b.dataset.tab===tab;b.setAttribute('aria-selected',active);b.tabIndex=active?0:-1;if(active&&focus)b.focus();});
    $('#file-content').setAttribute('aria-labelledby',`tab-${tab}`);
    renderFile(); $('#file-content').scrollTop=0;
  }
  function renderFile() {
    const record=current;
    if (selectedTab==='notes') {
      const notes=[...record.notes].sort((a,b)=>b.date.localeCompare(a.date)||Boolean(b.approved)-Boolean(a.approved));
      $('#file-content').innerHTML=`<div class="section-intro"><h3>Notes</h3><span class="muted">${notes.length} entries · newest first</span></div>`+notes.map(n=>`<article class="record ${n.superseded_by?'superseded':''}" id="source-${escape(n.id)}" tabindex="-1"><div class="record-meta"><time>${date(n.date,true)}</time><span class="author">${escape(n.author||'banker')}</span><span class="record-id">${escape(n.id)}</span></div><p>${escape(n.text)}</p>${n.superseded_by?`<span class="superseded-chip">superseded ${date(n.superseded_date)}</span>`:''}${n.approved?`<div class="saved-fields"><span>Banker · call ${date(n.source.date)}</span><span>${escape(n.fact_type)} · ${n.expires_at?`expires ${date(n.expires_at,true)}`:'never expires'}</span><span>Saved locally</span></div>`:''}</article>`).join('');
    } else if (selectedTab==='activity') renderActivity();
    else if (selectedTab==='holdings') $('#file-content').innerHTML=`<div class="section-intro"><h3>Holdings</h3><span class="muted">Client record</span></div>${holdingsTable()}${(record.client.accounts||[]).map(a=>`<div class="reference" id="source-${escape(a.account_id)}" tabindex="-1"><small>${escape(a.account_id)} · ${escape(a.type)}</small>${escape((a.holders||[]).join(' & '))}</div>`).join('')}`;
    else $('#file-content').innerHTML=`<div class="section-intro"><h3>Documents</h3><button disabled title="Attachments are not part of this demo">Attach</button></div>${['Statement Q2 2026','Tax form 2025','Account agreement'].map(name=>`<div class="document"><span aria-hidden="true">▤</span>${name}</div>`).join('')}<h3 class="reference-heading">Approved language</h3>${record.approved_language.map(a=>`<article class="reference" id="source-${escape(a.claim_id)}" tabindex="-1"><small>${escape(a.claim_id)} · Locked · v${escape(a.version||1)}</small><p>${escape(a.text)}</p></article>`).join('')}<h3 class="reference-heading">Fee schedules</h3>${record.references.map(r=>`<article class="reference" id="source-${escape(r.ref_id)}" tabindex="-1"><small>${escape(r.ref_id)} · valid from ${date(r.valid_from,true)}${r.valid_to?` to ${date(r.valid_to,true)}`:''}</small><p>${escape(r.item?.replaceAll('_',' '))}: ${escape(r.value_pct)}%</p></article>`).join('')}`;
  }
  function eventText(e) {
    return String(e.text || [e.type?.replaceAll('_',' '),e.instrument,e.sentiment].filter(Boolean).join(' · ')).replace(/^(trade_buy|trade_sell) instrument=/,(_,type)=>type==='trade_buy'?'Bought ':'Sold ').replace(/, instrument_type=\w+/, '').replace(/, amount=(\d+(?:\.\d+)?)/,(_,amount)=>` · ${money(Number(amount))}`).replace(/^(email_ignored|email_opened) email=/,(_,type)=>type==='email_ignored'?'Email ignored: ':'Email opened: ').replace(/^cash_in counterparty=(.*), amount=(\d+)/,(_,who,amount)=>`Cash in · ${who} · ${money(Number(amount))}`).replace(/^cash_out counterparty=(.*), amount=(\d+)/,(_,who,amount)=>`Cash out · ${who} · ${money(Number(amount))}`);
  }
  function renderActivity() {
    const groups=new Map();
    const events=[...current.events,...(current.market||[]).map(m=>({...m,id:m.event_id,market:true}))];
    events.sort((a,b)=>b.date.localeCompare(a.date));
    for (const e of events) { const month=e.date.slice(0,7);if(!groups.has(month))groups.set(month,[]);groups.get(month).push(e); }
    $('#file-content').innerHTML=[...groups].map(([month,events])=>{
      const logins=events.filter(e=>e.type==='app_login');
      const visible=events.filter(e=>e.type!=='app_login');
      return `<h3 class="month">${new Date(month+'-01T12:00:00Z').toLocaleDateString('en-GB',{month:'long',year:'numeric',timeZone:'UTC'})}</h3>`+visible.map(e=>{
        const life=['beneficiary_changed','retirement','marriage','divorce'].includes(e.type);
        const icon=e.market?'◎':e.type?.includes('email')?'✉':e.type?.includes('trade')?'↗':life?'◇':'·';
        return `<article class="event ${life?'life':''}" id="source-${escape(e.id)}" tabindex="-1"><span class="event-icon" aria-label="${e.market?'Market event':escape(e.type?.replaceAll('_',' '))}">${icon}</span><div><div class="record-meta">${date(e.date)}<span class="record-id">${escape(e.id)}</span></div><p>${escape(eventText(e))}</p></div></article>`;
      }).join('')+(logins.length?`<article class="event"><span class="event-icon" aria-hidden="true">↪</span><div><p class="muted">${logins.length} logins</p>${logins.map(e=>`<span id="source-${escape(e.id)}" tabindex="-1"></span>`).join('')}</div></article>`:'');
    }).join('') || '<p class="muted">No activity in this client file.</p>';
  }
  function holdingsTable() {
    return `<table class="holdings"><thead><tr><th scope="col">Instrument</th><th scope="col">Type</th><th scope="col">Amount</th></tr></thead><tbody>${current.client.holdings.map(h=>`<tr><td>${escape(h.instrument)}</td><td class="instrument-type">${escape(h.type||h.instrument_type||'—')}</td><td>${money(h.amount)}</td></tr>`).join('')}</tbody></table>`;
  }
  function showCitation(id) {
    const tab=sourceIndex(current).get(id);if(!tab)return notify('This source is not in the local file.');
    selectTab(tab);
    const target=document.getElementById('source-'+id);if(!target)return notify('This source is not in the local file.');
    target.classList.remove('flash');void target.offsetWidth;target.classList.add('flash');
    target.focus({preventScroll:true});target.scrollIntoView({block:'center',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'});
  }
  function renderAssistant() {
    const s=session();$('#prepare').disabled=s.busy;
    $('#debrief').setAttribute('aria-pressed',s.mode==='debrief');
    $('#prepare').setAttribute('aria-pressed',s.mode==='prepare' && s.prepared);
    if (s.mode==='debrief') renderDebrief();
    else if (s.prepared) renderBrief();
    else {
      $('#assistant-content').innerHTML=`<div class="empty-prep"><div class="ready-label"><span class="ready-rule" aria-hidden="true"></span>THE NEXT CONVERSATION STARTS HERE</div><h3>Know the client.<br><span>Question the assumptions.</span></h3><p class="hero-description">Turn the client file into a considered conversation. Every finding comes with the evidence behind it.</p><div class="prep-scope"><div class="scope-heading"><span>YOUR CALL PREPARATION</span><span>5 SECTIONS</span></div><ol class="prepare-preview"><li><span class="step-number">01</span><div><strong>The context that matters</strong><p>Key points, recent changes, and holdings.</p></div></li><li><span class="step-number">02</span><div><strong>The evidence behind the words</strong><p>Source checks and approved disclosures.</p></div></li><li><span class="step-number">03</span><div><strong>A better question to ask</strong><p>Resolve assumptions before the next decision.</p></div></li></ol></div><div class="prepare-hint"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 7-7 7 7M12 5v15"/></svg><span>Select <strong>Prepare call</strong> to begin.</span></div><p class="fixture-note">${escape(current.fixture_note)}</p></div>`;
    }
    updateCounters();
  }
  function renderRow(row) {
    const s=session(), applied=s.actions[row.id], cites=citations(row.citations), hasMark=row.mark&&cites;
    if (applied) return `<div class="brief-line mark applied"><p class="replacement">${escape(applied.text)}</p><div class="mark-footer"><div>${cites}</div><span class="applied-label">${escape(applied.label)}</span></div></div>`;
    if (row.severity===1) return `<div class="brief-line"><p class="drift">${escape(row.text)}</p><span class="drift-note">Wording drift · severity 1</span> ${cites}</div>`;
    if (!hasMark) return `<div class="brief-line"><p>${escape(row.text)}</p>${row.badge?`<span class="badge">${escape(row.badge)}</span>`:''} ${cites}</div>`;
    const struck=['contradicted','expired'].includes(row.mark);
    return `<div class="brief-line mark ${row.severity===4?'severity-4':''}" data-mark="${escape(row.id)}"><div class="mark-heading">${row.severity===4?'<span class="severity">Severity 4 · Promise</span>':`<span class="mark-kind">${row.mark==='expired'?'Expired label':row.mark==='feed'?'From the activity feed':row.mark==='disclosure'?'Disclosure check':'Source check'}</span>`}<span class="evidence-label">CITED EVIDENCE</span></div><p class="original">${struck?`<s>${escape(row.text)}</s>`:escape(row.text)}</p>${row.badge?`<span class="badge">${escape(row.badge)}</span>`:''}${row.replacement?`<p class="replacement">${escape(row.replacement)}</p>`:''}<p class="reason">${escape(row.reason)}</p><div class="mark-footer"><div>${cites}</div>${row.action?`<button class="action" data-action="${escape(row.id)}">${escape(row.action)}</button>`:''}</div></div>`;
  }
  function renderSection(section,index) {
    let body=section.rows.map(renderRow).join('');
    if(section.id==='holdings')body=holdingsTable();
    if(section.id==='questions')body+=session().questions.map(q=>`<div class="brief-line"><p>${escape(q.text)}</p>${citations(q.citations)}</div>`).join('');
    if(!body)body=`<p class="muted">${section.id==='questions'?'Use “Ask instead” or “Raise it” to add a question.':'No additional change flagged in this cached preparation.'}</p>`;
    return `<section class="brief-section" id="brief-${section.id}"><div class="section-title"><span class="section-number">${String(index+1).padStart(2,'0')}</span><h3>${section.title}</h3></div>${body}</section>`;
  }
  function renderBrief() {
    const prep=current.prep,s=session();
    $('#assistant-content').innerHTML=`<div class="brief-top"><div class="eyebrow">CALL PREPARATION <span class="brief-date">${date(prep.as_of,true)}</span></div><h3>Your conversation brief.</h3><p class="muted">${escape(current.client.name)} · grounded in the client file</p><div class="brief-reference">${escape(prep.baseline_id)} · cached rehearsal checks</div></div><div id="brief-sections">${prep.sections.slice(0,s.shown).map(renderSection).join('')}</div><p class="fixture-note">${escape(current.fixture_note)}</p>`;
  }
  function updateCounters() {
    const s=session(), rows=current.prep.sections.slice(0,s.shown).flatMap(g=>g.rows);
    const counts=s.prepared?{checked:rows.filter(r=>r.checked).length,flagged:rows.filter(r=>r.checked&&r.mark).length,expired:rows.filter(r=>r.expired).length}:null;
    for(const key of ['checked','flagged','expired'])$('#'+key).textContent=counts?counts[key]:'—';
    $('#prep-status').textContent=s.busy?'Checking sources…':s.prepared?'Cached checks complete':'Ready to prepare';
    $('.assistant-pane').classList.toggle('is-drafting',s.busy);
  }
  async function prepare() {
    if(session().busy)return;
    const s=session(),thisEpoch=++epoch;
    s.mode='prepare';s.shown=0;s.actions={};s.questions=[];s.prepared=true;s.busy=true;
    if(!current.prep.available){s.busy=false;s.prepared=false;renderAssistant();return notify('No baseline preparation is available for this client.');}
    renderAssistant();$('#assistant-content').scrollTop=0;
    for(let i=0;i<current.prep.sections.length;i++){
      await wait(300);if(epoch!==thisEpoch)return;
      s.shown=i+1;$('#brief-sections').insertAdjacentHTML('beforeend',renderSection(current.prep.sections[i],i));updateCounters();
    }
    s.busy=false;$('#prepare').disabled=false;updateCounters();
  }
  function applyAction(id) {
    const row=allRows(current).find(r=>r.id===id);if(!row||!row.action)return;
    const s=session();
    if(row.action==='Replace')s.actions[id]={text:row.replacement,label:'Approved wording applied'};
    else {
      if(!s.questions.some(q=>q.text===row.question))s.questions.push({text:row.question,citations:row.citations});
      s.actions[id]={text:row.action==='Ask instead'?row.question:row.text,label:row.action==='Ask instead'?'Changed to a question':'Added to call questions'};
      // The stock evidence and key point refer to one question, not two competing facts.
      for(const other of allRows(current))if(other.id!==id&&other.question===row.question)s.actions[other.id]={text:['contradicted','expired'].includes(other.mark)?other.question:other.text,label:'Added to call questions'};
    }
    const scroll=$('#assistant-content').scrollTop;renderBrief();$('#assistant-content').scrollTop=scroll;
    notify(row.action==='Replace'?'Locked wording applied to this brief.':'Question added to the call preparation.');
  }
  function renderDebrief() {
    const s=session();
    $('#assistant-content').innerHTML=`<div class="eyebrow">AFTER THE CONVERSATION</div><h3 class="debrief-title">Bring the file up to date.</h3><p class="debrief-description">Review each proposed fact. Approving gives it one current owner.</p><form class="debrief-form" id="debrief-form"><label for="debrief-text">What happened on the call?</label><textarea id="debrief-text" maxlength="6000" placeholder="${escape(current.client.client_id==='C08'?example:'Describe the decision maker, bond switch, or retirement update from the call.')}" ${saving?'disabled':''}>${escape(s.text)}</textarea><div class="form-actions">${current.client.client_id==='C08'?'<button type="button" class="text-button" id="use-example">Use example</button>':'<span></span>'}<button class="primary" type="submit" ${saving?'disabled':''}>Propose notes</button></div></form><p class="fixture-note">Local rehearsal parser · approvals save on this laptop. Behavioural facts expire after 120 days, contact updates after 30; stated facts do not expire.</p><div id="drafts">${s.cards.length?'<p class="draft-status">Proposed notes · review before approving</p>':''}${s.cards.map(renderCard).join('')}</div>`;
  }
  function renderCard(card) {
    return `<article class="note-card" data-draft="${escape(card.draft_id)}"><h3>${escape(card.title)}</h3>${card.editing?`<label class="muted" for="edit-${escape(card.draft_id)}">Note value</label><input id="edit-${escape(card.draft_id)}" data-edit-value="${escape(card.draft_id)}" maxlength="500" value="${escape(card.value)}">`:`<p class="value">${escape(card.value)}</p>`}<p class="note-meta">${date(card.date,true)} · banker, call of ${date(card.source.date)}<br>${escape(card.fact_type)} · ${card.expires_at?`expires ${date(card.expires_at,true)} (${card.expiry_days} days)`:'never expires'}</p>${card.previous.length?`<div class="previous">Supersedes${card.previous.map(p=>`<p><s>${escape(p.text)}</s> ${citations([p.id])}</p>`).join('')}</div>`:'<p class="muted">New fact · no previous owner on file</p>'}<div class="card-actions">${card.saved?'<span class="applied-label">Approved · saved locally</span>':`<button class="primary" data-approve="${escape(card.draft_id)}" ${saving||card.editing?'disabled':''}>Approve</button><button data-edit="${escape(card.draft_id)}" ${saving?'disabled':''}>${card.editing?'Save edit':'Edit'}</button>`}</div></article>`;
  }
  async function propose(event) {
    event.preventDefault();if(saving)return;
    const s=session(),cid=current.client.client_id,text=$('#debrief-text').value;
    s.text=text;if(!text.trim())return notify('Enter the call details or click “Use example”.');
    saving=true;renderDebrief();
    try{const result=await request('/api/workspace/propose',{client_id:cid,text});s.cards=result.cards;if(current.client.client_id===cid)notify(result.message);}
    catch(error){notify(error.message);}finally{saving=false;if(session().mode==='debrief')renderDebrief();}
  }
  function editCard(id) {
    const card=session().cards.find(c=>c.draft_id===id);if(!card||card.saved)return;
    if(card.editing){const value=document.querySelector(`[data-edit-value="${id}"]`).value.trim();if(!value)return notify('Enter a value before saving the edit.');card.value=value;}
    card.editing=!card.editing;const scroll=$('#assistant-content').scrollTop;renderDebrief();$('#assistant-content').scrollTop=scroll;
    if(card.editing)document.querySelector(`[data-edit-value="${id}"]`).focus();
  }
  async function approveCard(id) {
    if(saving)return;const s=session(),cid=current.client.client_id,card=s.cards.find(c=>c.draft_id===id);if(!card||card.saved||card.editing)return;
    saving=true;renderDebrief();
    try{
      const result=await request('/api/workspace/approve',{client_id:cid,draft_id:id,value:card.value});card.saved=true;
      const record=data.records.find(r=>r.client.client_id===cid);record.notes=result.record.notes;
      // Refresh cached checks from the server after a write so a subsequent prep uses the new owner.
      try{const fresh=await request('/api/workspace');data.csrf_token=fresh.csrf_token;const updated=fresh.records.find(r=>r.client.client_id===cid);record.prep=updated.prep;}catch{notify('Note saved. Reload before preparing again to refresh the checks.');s.prepared=false;}
      if(current.client.client_id===cid){selectTab('notes');notify('Approved note saved locally. Previous fact superseded.');}
    }catch(error){notify(error.message);}finally{saving=false;if(session().mode==='debrief')renderDebrief();}
  }
  function renderLedger() {
    $('#ledger-count').textContent=`${data.ledger.length} recorded ${data.ledger.length===1?'change':'changes'}`;
    $('#ledger-content').innerHTML=`<table class="ledger-table"><thead><tr><th>Time</th><th>Proposal / change</th><th>Held-out result</th><th>Tier</th><th>Approver</th><th>Version</th></tr></thead><tbody>${data.ledger.map(row=>`<tr><td>${row.time?escape(new Date(row.time).toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'})):'—'}</td><td>${escape(row.proposal)}</td><td>${escape(row.result)}</td><td>${escape(row.tier)}</td><td>${escape(row.approver)}</td><td>v${escape(row.version)}</td></tr>`).join('')}<tr><td>Rehearsal</td><td>Expire behavioural labels at 120 days</td><td>Not evaluated</td><td>—</td><td>No recorded approval</td><td>Local only</td></tr></tbody></table><p class="ledger-note">${escape(data.ledger_note)}</p>`;
  }
  document.addEventListener('click',event=>{
    const b=event.target.closest('button');if(!b)return;
    if(b.dataset.client)selectClient(b.dataset.client);
    else if(b.dataset.tab)selectTab(b.dataset.tab);
    else if(b.dataset.cite)showCitation(b.dataset.cite);
    else if(b.dataset.action)applyAction(b.dataset.action);
    else if(b.dataset.edit)editCard(b.dataset.edit);
    else if(b.dataset.approve)approveCard(b.dataset.approve);
    else if(b.id==='prepare')prepare();
    else if(b.id==='debrief'){epoch++;if(session().busy)session().shown=current.prep.sections.length;session().busy=false;session().mode='debrief';renderAssistant();$('#assistant-content').scrollTop=0;}
    else if(b.id==='use-example'){session().text=example;$('#debrief-text').value=example;$('#debrief-text').focus();}
  });
  document.addEventListener('input',event=>{if(event.target.id==='debrief-text')session().text=event.target.value;});
  document.addEventListener('submit',event=>{if(event.target.id==='debrief-form')propose(event);});
  $('.file-tabs').addEventListener('keydown',event=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
    event.preventDefault();const tabs=['notes','activity','holdings','documents'],index=tabs.indexOf(selectedTab);
    selectTab(event.key==='Home'?tabs[0]:event.key==='End'?tabs[3]:tabs[(index+(event.key==='ArrowRight'?1:3))%4],true);
  });
  async function start() {
    try{
      try{data=await request('/api/workspace'+(demoMode?'?demo=1':''));}
      catch{data=await request('/fixtures/workspace-demo.json');notify('Cached preview loaded. Restart the viewer server to enable note approvals.');}
      renderLedger();selectClient(data.records.some(r=>r.client.client_id==='C08')?'C08':data.records[0].client.client_id);
    }catch(error){$('#file-name').textContent='Client files unavailable';$('#file-content').innerHTML='<p>Restart the local viewer and reload this page.</p>';notify(error.message);}
  }
  start();
})();
