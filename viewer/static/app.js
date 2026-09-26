/* Pregame mission control: read-only viewer page. Plain JavaScript, no build step, no external requests.
   Contract: ../CONTRACT.md. Data comes from /api/... or, with ?fixtures=1 (or when opened as a file), from
   fixture files: /fixtures/<name>.json when served, ../fixtures/<name>.json when opened from disk. */
(function () {
  'use strict';

  // ---------------------------------------------------------------- basics

  var POLL_MS = 2000;
  var OVERVIEW_EVERY = 10;            // also refresh the overview every 10th poll (20 s) when nothing new arrives
  var NEW_HIGHLIGHT_MS = 6000;
  var FEED_MAX = 600;                 // render at most this many feed rows (newest first)
  var TABS = ['activity', 'runs', 'proposals', 'versions', 'briefs', 'findings', 'cabinet', 'databases'];
  var KNOWN_ACTORS = ['world', 'drafter', 'improver', 'gate'];
  var LS_TAB = 'pregame-viewer.tab';
  var LS_THEME = 'pregame-viewer.theme';
  var LS_DB = 'pregame-viewer.db';

  var params = new URLSearchParams(location.search);
  var IS_FILE = location.protocol === 'file:';
  // A single-file copy carries its snapshot inside the page, in <script type="application/json" id="pregame-data">
  // as { "<fixture name>": data }. A published copy with its snapshot files beside it sets PREGAME_STATIC_SITE.
  var EMBEDDED = (function () {
    var el = document.getElementById('pregame-data');
    if (!el) return null;
    try { var d = JSON.parse(el.textContent); return d && typeof d === 'object' ? d : null; } catch (e) { return null; }
  })();
  var STATIC_SITE = window.PREGAME_STATIC_SITE === true || !!EMBEDDED;
  var FIXTURES = STATIC_SITE || IS_FILE || params.get('fixtures') === '1';
  var FIXTURE_BASE = STATIC_SITE ? 'fixtures/' : (IS_FILE ? '../fixtures/' : '/fixtures/');

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function isObj(v) { return v !== null && typeof v === 'object' && !Array.isArray(v); }
  function arr(v) { return Array.isArray(v) ? v : []; }
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function has(v) { return v !== null && v !== undefined && v !== ''; }

  function humanize(key) {
    var s = String(key == null ? '' : key).replace(/[_]+/g, ' ').replace(/\s+/g, ' ').trim();
    return s ? s.charAt(0).toUpperCase() + s.slice(1) : '';
  }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : (many || one + 's')); }
  function fmtNum(n, digits) {
    if (!isNum(n)) return '–';
    if (Number.isInteger(n)) return n.toLocaleString('en-US');
    return n.toFixed(digits == null ? 2 : digits);
  }
  function fmtPct(r) { return isNum(r) ? Math.round(r * 100) + '%' : '–'; }
  function pad2(n) { return (n < 10 ? '0' : '') + n; }
  function clock(d) { return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds()); }

  // Sim times are naive ISO strings ("2026-03-17T00:00:00"): show them as written, date only at midnight.
  function fmtSim(iso) {
    if (!has(iso)) return '–';
    var s = String(iso);
    var m = /^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?)?/.exec(s);
    if (!m) return s;
    if (!m[2] || m[2] === '00:00') return m[1];
    return m[1] + ' ' + m[2];
  }
  // Wall-clock stamps: as written, without fractions; "UTC" when the string says so.
  function fmtStamp(iso) {
    if (!has(iso)) return '–';
    var s = String(iso);
    var m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)(?:\.\d+)?(Z|[+-]00:?00)?/.exec(s);
    if (!m) return s;
    return m[1] + ' ' + m[2] + (m[3] ? ' UTC' : '');
  }
  // "2026-09-26T17:15:11Z" -> "09-26 17:15" for dense tables (the full stamp goes in a title).
  function shortStamp(iso) {
    var m = /^\d{4}-(\d{2}-\d{2})[T ](\d{2}:\d{2})/.exec(String(iso || ''));
    return m ? m[1] + ' ' + m[2] : fmtStamp(iso);
  }
  function shortHash(h) { return has(h) ? String(h).slice(0, 10) : '–'; }
  function json(v) {
    try { return JSON.stringify(v, null, 2); } catch (e) { return String(v); }
  }

  function lsGet(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } }
  function lsSet(key, val) { try { window.localStorage.setItem(key, val); } catch (e) { /* storage unavailable */ } }

  function el(html) {
    var t = document.createElement('template');
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
  }

  // ---------------------------------------------------------------- data loading

  function LoadError(kind, message, detail) {
    this.name = 'LoadError';
    this.kind = kind;          // network | http | parse | blocked
    this.message = message;
    this.detail = detail || {};
  }
  LoadError.prototype = Object.create(Error.prototype);

  function fixtureName(parts) {
    var p = parts.map(String);
    if (p[0] === 'cabinet' && p[1] === 'clients') return 'cabinet_clients';
    if (p[0] === 'cabinet' && p[1] === 'client') return 'cabinet_client_' + p[2];
    if (p[0] === 'db') return 'db_' + p[1] + '.' + p[2];     // server.py --snapshot writes db_<db>.<collection>.json
    return p.join('_');
  }

  function xhrJSON(url) {
    return new Promise(function (resolve, reject) {
      var x = new XMLHttpRequest();
      try { x.open('GET', url); } catch (e) { reject(e); return; }
      x.onload = function () {
        if (x.status === 200 || x.status === 0) {
          try { resolve(JSON.parse(x.responseText)); } catch (e) { reject(new LoadError('parse', 'not valid JSON', { url: url })); }
        } else {
          reject(new LoadError('http', 'HTTP ' + x.status, { url: url, status: x.status }));
        }
      };
      x.onerror = function () { reject(new LoadError('blocked', 'blocked', { url: url })); };
      x.send();
    });
  }

  // Pregame resources belong to one Pregame database (the "Run" switcher); the generic browser and the
  // cross-run summary do not.
  function isPregame(parts) { return parts[0] !== 'dbs' && parts[0] !== 'db' && parts[0] !== 'runs'; }

  // Fixture mode: the default database's files have plain names (overview.json); another database's carry
  // @<db> (overview@pregame_run_a.json). overview.json lists which files each database has; data that is shared
  // (the cabinet, for a run without harness preps) falls back to the plain file.
  var fixtureIndexP = null;
  function fixtureIndex() {
    if (!fixtureIndexP) {
      fixtureIndexP = fetchJSON(FIXTURE_BASE + 'overview.json', { url: FIXTURE_BASE + 'overview.json', fixture: 'overview' }).then(function (o) {
        var dbs = {};
        arr(o && o.pregame_dbs).forEach(function (d) { if (isObj(d) && d.name) dbs[d.name] = d; });
        return { def: o && (o.default_db || o.active_db || o.pregame_db) || null, dbs: dbs };
      }, function () { return { def: null, dbs: {} }; });
    }
    return fixtureIndexP;
  }
  function fixtureFile(parts, idx) {
    var name = fixtureName(parts);
    var db = isPregame(parts) ? state.pdb : null;
    if (!db || db === idx.def) return name;
    var info = idx.dbs[db];
    var generic = /^cabinet_client_/.test(name) ? 'cabinet_client_<cid>' : name;
    if (info && Array.isArray(info.fixtures) && info.fixtures.indexOf(generic) < 0 && /^cabinet_/.test(name)) return name;
    return name + '@' + db;
  }

  function getJSON(parts, query) {
    if (FIXTURES) {
      return fixtureIndex().then(function (idx) {
        var file = fixtureFile(parts, idx);
        return fetchJSON(FIXTURE_BASE + encodeURIComponent(file) + '.json', { url: FIXTURE_BASE + file + '.json', fixture: file });
      });
    }
    var q = {};
    if (query) Object.keys(query).forEach(function (k) { q[k] = query[k]; });
    if (isPregame(parts) && state.pdb) q.db = state.pdb;
    var url = '/api/' + parts.map(function (x) { return encodeURIComponent(x); }).join('/');
    var qs = new URLSearchParams(q).toString();
    if (qs) url += '?' + qs;
    return fetchJSON(url, { url: url, fixture: null });
  }

  function fetchJSON(url, detail) {
    if (EMBEDDED && url.indexOf(FIXTURE_BASE) === 0) {
      var name = decodeURIComponent(url.slice(FIXTURE_BASE.length)).replace(/\.json$/, '');
      if (Object.prototype.hasOwnProperty.call(EMBEDDED, name)) {
        return Promise.resolve(JSON.parse(JSON.stringify(EMBEDDED[name])));
      }
      detail.status = 404;
      return Promise.reject(new LoadError('http', 'HTTP 404', detail));
    }
    return fetch(url, { cache: 'no-store', headers: { Accept: 'application/json' } }).then(function (res) {
      if (!res.ok) {
        return res.text().then(function (text) {
          var msg = '', more = '', hint = '';
          try {
            var j = JSON.parse(text);
            msg = j.error || j.message || '';
            more = j.detail || '';
            hint = j.hint || '';
          } catch (e) { msg = ''; }
          detail.status = res.status;
          detail.serverMessage = typeof msg === 'string' ? msg : json(msg);
          detail.serverDetail = typeof more === 'string' ? more : json(more);
          detail.serverHint = typeof hint === 'string' ? hint : json(hint);
          throw new LoadError('http', 'HTTP ' + res.status, detail);
        });
      }
      return res.text().then(function (text) {
        try { return JSON.parse(text); } catch (e) { throw new LoadError('parse', 'not valid JSON', detail); }
      });
    }, function () {
      if (IS_FILE) {
        return xhrJSON(url).catch(function () { throw new LoadError('blocked', 'blocked', detail); });
      }
      throw new LoadError('network', 'no answer', detail);
    });
  }

  // A small cache: one entry per resource, marked stale when new ledger entries suggest it changed. Pregame
  // resources are keyed by the selected database, so a late answer for the old one never lands in the new view.
  var cache = {};
  function scoped(key) { return /^(dbs$|db:|runs$)/.test(key) ? key : (state.pdb || '') + '|' + key; }
  function entry(key) { return cache[scoped(key)]; }
  function need(key, parts, query, force) {
    key = scoped(key);
    var c = cache[key] || (cache[key] = { data: undefined, stale: true, promise: null, error: null });
    if (!force && !c.stale && c.data !== undefined) return Promise.resolve(c.data);
    if (c.promise) return c.promise;
    c.promise = getJSON(parts, query).then(function (d) {
      c.data = d; c.stale = false; c.error = null; c.promise = null; c.at = Date.now(); return d;
    }, function (e) {
      c.error = e; c.promise = null; throw e;
    });
    return c.promise;
  }
  function cached(key) { var c = entry(key); return c ? c.data : undefined; }
  function markStale(keys) { keys.forEach(function (k) { var c = entry(k); if (c) c.stale = true; }); }

  function describeError(e, what) {
    var d = (e && e.detail) || {};
    var where = d.fixture ? 'fixtures/' + d.fixture + '.json' : (d.url || '');
    if (e && e.kind === 'blocked') {
      return {
        title: 'The browser blocked the fixture files',
        text: 'Pages opened straight from disk cannot read other files. From the viewer folder run ' +
          '<code>python -m http.server 8080</code> and open <code>http://127.0.0.1:8080/static/index.html?fixtures=1</code>.'
      };
    }
    if (FIXTURES && e && e.kind === 'http' && d.status === 404) {
      return {
        title: 'No snapshot file for ' + what,
        text: 'Missing <code>' + esc(where) + '</code>. Run <code>python server.py --snapshot</code> against the database to write it.'
      };
    }
    if (e && e.kind === 'network') {
      return {
        title: 'The viewer server did not answer',
        text: 'Could not reach <code>' + esc(where) + '</code>. Check that <code>server.py</code> is still running, then try again.'
      };
    }
    if (e && e.kind === 'parse') {
      return { title: 'Unreadable data for ' + what, text: '<code>' + esc(where) + '</code> did not return valid JSON.' };
    }
    if (e && e.kind === 'http') {
      var s = d.status;
      var msg = (d.serverMessage ? ' The server said: “' + esc(d.serverMessage) + '”.' : '') +
        (d.serverDetail ? ' ' + esc(d.serverDetail) : '') +
        (d.serverHint ? ' <strong>Next step:</strong> ' + esc(d.serverHint) : '');
      if (s === 503) {
        return {
          title: 'The database is not reachable',
          text: 'The server could not read ' + esc(what) + ' (503).' + msg +
            (d.serverHint ? '' : ' Check the network or run <code>start --offline</code> to serve the snapshot.')
        };
      }
      if (s === 400) return { title: 'Bad request', text: 'The server rejected <code>' + esc(where) + '</code> (400).' + msg };
      if (s === 403) return { title: 'Not allowed', text: 'The server refused <code>' + esc(where) + '</code> (403).' + msg };
      if (s === 404) return { title: 'Not found', text: 'The server has nothing at <code>' + esc(where) + '</code> (404).' + msg };
      return {
        title: 'The server could not load ' + what,
        text: 'Request <code>' + esc(where) + '</code> failed with ' + esc(s) + '.' + msg + ' Check the server window, then try again.'
      };
    }
    return { title: 'Could not load ' + what, text: esc(e && e.message ? e.message : String(e)) };
  }

  function errorPanel(e, what, retry) {
    var info = describeError(e, what);
    var node = el(
      '<div class="notice notice-error" role="alert">' +
        '<p class="notice-title">' + esc(info.title) + '</p>' +
        '<p class="notice-text">' + info.text + '</p>' +
        (retry ? '<button type="button" class="btn btn-primary" data-retry>Try again</button>' : '') +
      '</div>');
    if (retry) node.querySelector('[data-retry]').addEventListener('click', retry);
    return node;
  }

  function emptyState(title, text) {
    return '<div class="empty"><p class="empty-title">' + esc(title) + '</p>' +
      (text ? '<p class="empty-text">' + text + '</p>' : '') + '</div>';
  }
  function loadingState(what) {
    return '<p class="loading" role="status">Loading ' + esc(what) + '…</p>';
  }

  // ---------------------------------------------------------------- shared renderers

  // ---------------------------------------------------------------- plain words
  // Everything a judge reads uses plain words; the technical original goes in a title attribute. Stored text (the
  // gate's decision sentences, rationales, diffs) is never reworded: it is shown as recorded, next to a plain summary.

  var ACTOR_WORDS = { world: 'simulated world', drafter: 'Sonnet (writer)', improver: 'Opus (proposer)', gate: 'test gate (code)' };
  var KIND_WORDS = {
    policy: 'what goes into the brief', rules: 'writing instructions', tools: 'data sources', guardrails: 'safety checks',
    scenarios: 'the unseen test meetings’ questions'
  };
  var STATUS_WORDS = {
    committed: 'adopted', approved: 'signed off', rejected: 'rejected', refused: 'refused', frozen: 'never allowed',
    pending: 'not tested yet', evaluating: 'being tested', awaiting_owner: 'waiting for a person’s sign-off',
    awaiting_approval: 'waiting for a person’s sign-off', stale: 'set aside (the settings moved on)'
  };
  var TIER_WORDS = {
    G: 'adopted automatically if it wins', H: 'needs a person’s sign-off',
    X: 'never allowed: it would change how the system is graded'
  };
  var SPLIT_WORDS = { heldout: 'unseen test meetings', tuning: 'practice meetings' };
  var ENTRY_WORDS = {
    seed: 'set up', event: 'world event', feedback: 'advisor feedback', brief: 'brief written', proposal: 'change proposed',
    eval: 'tested', commit: 'adopted', reject: 'rejected', refused: 'refused', approve: 'signed off', rollback: 'rolled back'
  };
  var FIELD_WORDS = { business_owners: 'business owners', global: 'all client groups' };
  var METRIC_WORDS = {
    mean_accuracy: 'questions answered correctly',
    worst_accuracy: 'worst single meeting',
    pass_k: 'consistency across repeat runs',
    missed_changes: 'missed changes per run',
    false_alarms: 'false alarms per run (flagging news that doesn’t affect the client)',
    guardrail_violations: 'safety-check failures per run',
    stale_claims: 'out-of-date claims per run',
    uncited_claims: 'claims without a source per run',
    context_tokens: 'brief size'
  };

  // The server names each actor for the selected run (actor_label: "Haiku (proposer)" in run B); use that when an
  // audit-log entry has it, else the default words.
  function actorWord(a) {
    var s = String(a || '');
    var items = state.activity.items;
    for (var i = items.length - 1; i >= 0; i--) {
      if (items[i].actor === s && has(items[i].actor_label)) return String(items[i].actor_label);
    }
    if (s === 'guardrails') return 'safety checks (code)';
    if (ACTOR_WORDS[s]) return ACTOR_WORDS[s];
    var m = /^owner:(.+)$/.exec(s);
    if (m) return m[1] + ' (person)';
    return s || 'unknown';
  }
  function kindWord(k) { return KIND_WORDS[k] || humanize(k).toLowerCase(); }
  function statusWord(s) { return STATUS_WORDS[s] || humanize(s).toLowerCase(); }
  function fieldWord(f) { return FIELD_WORDS[f] || humanize(f).toLowerCase(); }
  function splitWord(s) { return SPLIT_WORDS[s] || humanize(s).toLowerCase(); }
  // "policy", "retirement" -> "what goes into the brief (retirement)"; "scenarios", "retirement:heldout" -> "the test meetings (retirement)"
  function surfaceWord(kind, key) {
    var k = String(key || '').split(':')[0];
    return kindWord(kind) + (k && k !== 'global' ? ' (' + fieldWord(k) + ')' : '');
  }
  function configWord(label) {
    var s = String(label || '');
    if (s === 'champion') return 'current version';
    var m = /^candidate:(.+)$/.exec(s);
    if (m) return 'proposed version (' + m[1] + ')';
    if (s === 'candidate') return 'proposed version';
    return s;
  }
  function pct(v) { return isNum(v) ? Math.round(v * 100) + '%' : '–'; }
  // Plain text on screen, the technical original on hover.
  function term(plain, tech) {
    return tech && tech !== plain ? '<span class="term" title="' + esc(tech) + '">' + esc(plain) + '</span>' : esc(plain);
  }
  function joinAnd(list) {
    if (list.length < 2) return list.join('');
    return list.slice(0, -1).join(', ') + ' and ' + list[list.length - 1];
  }

  // Why the gate said no, in plain words, read from its recorded decision sentence (the same patterns as the
  // server's plain_reasons, so both pages say the same thing). The recorded sentence itself is shown unchanged.
  var REASON_PATTERNS = [
    [/guardrail violations rose/i, 'it failed more safety checks'],
    [/context grew[^;]*?(\d+)\s*->\s*(\d+) tokens(?: \((\+\d+%))?/i, 'brief size'],
    [/context grew/i, 'the brief got too long'],
    [/false alarms rose/i, 'it raised more false alarms (flagging news that doesn’t affect the client)'],
    [/worst scenario fell/i, 'its worst unseen test meeting got worse'],
    [/pass\^k fell/i, 'it was less consistent across repeat runs'],
    [/missed changes rose/i, 'it missed more changes the client would ask about'],
    [/stale claims rose/i, 'it repeated more out-of-date facts'],
    [/uncited claims rose/i, 'it made more statements without a cited fact']
  ];
  function gainTooSmall(decision) { return /inside the [\d.]+ margin|needs at least \+/i.test(String(decision || '')); }
  function rejectReasons(decision) {
    var d = typeof decision === 'string' ? decision : '';
    var out = [];
    REASON_PATTERNS.forEach(function (r) {
      var m = r[0].exec(d);
      if (!m) return;
      if (r[1] === 'brief size') {
        out.push('the brief size grew too much (' + m[1] + ' → ' + m[2] + (m[3] ? ', ' + m[3] : '') + '; the limit is +50%)');
      } else if (!(r[1] === 'the brief got too long' && out.some(function (x) { return x.indexOf('the brief size') === 0; }))) {
        out.push(r[1]);
      }
    });
    return out;
  }
  function reasonText(decision) {
    var bits = (gainTooSmall(decision) ? ['a gain too small to trust'] : []).concat(rejectReasons(decision));
    return joinAnd(bits);
  }

  // Diff lines in plain words (server.py's plain_change); the recorded line goes on hover.
  var FACT_KIND_WORDS = {
    price: 'rate and price facts', regulation: 'tax and rule facts', competitor: 'product-change facts',
    disruption: 'market-shock facts', demand: 'economy facts', account: 'the client’s own notes'
  };
  var SOURCE_WORDS = { market_feed: 'the verified market feed', account_notes: 'the advisor’s client notes', analyst_notes: 'analyst notes (unverified)' };
  var SETTING_WORDS = {
    include_kinds: 'fact types', max_facts: 'most facts', prefer_exposed: 'client-relevant facts first',
    recency_days: 'how recent the facts are', likely_questions: 'likely questions listed', section_order: 'section order'
  };
  function plainChange(line) {
    var l = String(line || '').trim(), m;
    if ((m = /^include_kinds:\s*(.+)$/.exec(l))) {
      var added = [], dropped = [];
      m[1].split(',').forEach(function (part) {
        part = part.trim();
        var kind = /^[+-]/.test(part) ? part.slice(1).trim() : part;
        (part.charAt(0) === '-' ? dropped : added).push(FACT_KIND_WORDS[kind] || kind);
      });
      var bits = [];
      if (added.length) bits.push('also include ' + joinAnd(added));
      if (dropped.length) bits.push('leave out ' + joinAnd(dropped));
      return bits.join('; ') || l;
    }
    if ((m = /^max_facts:\s*(\d+)\s*->\s*(\d+)$/.exec(l))) return 'use up to ' + m[2] + ' facts (was ' + m[1] + ')';
    if ((m = /^prefer_exposed:\s*(\w+)\s*->\s*(\w+)$/.exec(l))) {
      return m[2].toLowerCase() === 'true' ? 'put the facts that affect this client first' : 'stop putting the facts that affect this client first';
    }
    if ((m = /^recency_days:\s*(\d+)\s*->\s*(\d+)$/.exec(l))) return 'only use facts from the last ' + m[2] + ' days (was ' + m[1] + ')';
    if ((m = /^likely_questions:\s*(\d+)\s*->\s*(\d+)$/.exec(l))) return 'list ' + m[2] + ' likely client questions (was ' + m[1] + ')';
    if (/^section_order/.test(l)) return 'reorder the brief’s sections';
    if ((m = /^(market_feed|account_notes|analyst_notes):\s*(\w+)\s*->\s*(\w+)$/.exec(l))) {
      return (m[3].toLowerCase() === 'true' ? 'turn on ' : 'turn off ') + SOURCE_WORDS[m[1]];
    }
    if ((m = /^heldout questions:\s*(.+)$/.exec(l))) return m[1].replace('rewrite them', 'rewrite the unseen test meetings’ questions');
    return l.replace(/^rule\b/, 'writing instruction').replace(/^guardrail\b/, 'safety check');
  }
  // A setting's value in words where the value is a list of fact types or a yes/no.
  function plainValue(key, v) {
    if (key === 'include_kinds' && Array.isArray(v)) return v.map(function (k) { return FACT_KIND_WORDS[k] || k; }).join(', ');
    if (typeof v === 'boolean') return v ? 'yes' : 'no';
    return compact(v);
  }

  // A plain-language outcome built from the numbers: {status, tier, champ, cand (EvalSummary or number), decision}.
  function plainOutcome(o) {
    var a = scoreOf(o.champ), b = scoreOf(o.cand);
    var st = String(o.status || '');
    var tier = String(o.tier || '').toUpperCase();
    var dec = typeof o.decision === 'string' ? o.decision : '';
    if (st === 'refused' || tier === 'X' || /^Refused/.test(dec)) {
      return 'Refused: it is never allowed, because it would change how the system is graded.';
    }
    if (st === 'stale') return 'Set aside: the settings it was tested against have changed since.';
    var scores = isNum(a) && isNum(b)
      ? 'the proposed version answered ' + pct(b) + ' of unseen test meetings’ questions correctly vs ' + pct(a) + ' for the current version'
      : '';
    if (st === 'committed' || st === 'approved' || /^Committed/.test(dec)) return scores ? 'Adopted: ' + scores + '.' : 'Adopted.';
    if (st === 'awaiting_owner' || st === 'awaiting_approval') return 'Passed the test, waiting for a person’s sign-off' + (scores ? ': ' + scores : '') + '.';
    if (st === 'rejected' || /^Rejected/.test(dec)) {
      var small = gainTooSmall(dec), reasons = rejectReasons(dec);
      if (scores) {
        var worse = b < a, tail;
        if (small) tail = 'a gain too small to trust' + (reasons.length ? ', and ' + joinAnd(reasons) : '');
        else if (worse) tail = 'worse than the current version' + (reasons.length ? ', and ' + joinAnd(reasons) : '');
        else tail = reasons.length ? 'but ' + joinAnd(reasons) : '';
        return 'Rejected: ' + scores + (tail ? ', ' + tail : '') + '.';
      }
      var why = reasonText(dec);
      return 'Rejected' + (why ? ': ' + why : '') + '.';
    }
    if (st === 'pending' || st === 'evaluating') return 'Not tested yet.';
    return scores ? humanize(scores) + '.' : '';
  }
  // The plain outcome, then the recorded sentence, smaller, exactly as stored.
  function decisionBlock(o) {
    var plain = plainOutcome(o);
    var dec = o.decision;
    var exact = has(dec) ? (typeof dec === 'string' ? dec : json(dec)) : '';
    if (!plain && !exact) return '<p class="muted">No decision yet.</p>';
    return (plain ? '<p class="plain-outcome">' + esc(plain) + '</p>' : '') +
      (exact ? '<p class="exact-line"><span class="exact-label">Exact decision (as recorded):</span> ' + esc(exact) + '</p>' : '');
  }

  // One plain sentence per audit-log entry: the server's (v2 `summary`, with `summary_technical` beside it), or for
  // older servers one built here from the payload, with the server's sentence on hover.
  function plainSummary(it) {
    if (has(it.summary_technical) && has(it.summary)) return String(it.summary);
    var p = isObj(it.payload) ? it.payload : {};
    var id = p.proposal_id || (it.ref && it.ref.type === 'proposal' ? it.ref.id : '');
    try {
      switch (it.kind) {
        case 'seed':
          if (arr(p.seeded).length) return 'Set up version 1 of ' + plural(p.seeded.length, 'setting') + '.';
          break;
        case 'event':
          if (p.action === 'load_world' && isNum(p.base_facts)) {
            return 'Loaded the simulated world: ' + p.base_facts + ' facts and ' + (isNum(p.events) ? p.events + ' scripted events' : 'its events') +
              (arr(p.fields).length ? ' across ' + plural(p.fields.length, 'client group') : '') + '.';
          }
          break;
        case 'brief':
          if (has(p.account_id)) {
            return 'Wrote a brief for ' + p.account_id + (has(p.as_of) ? ' as of ' + fmtSim(p.as_of) : '') +
              (Array.isArray(p.fact_ids) ? ', from ' + plural(p.fact_ids.length, 'fact') : '') + '.';
          }
          break;
        case 'proposal':
          if (has(p.kind)) {
            return 'Proposed ' + (id ? id + ', ' : '') + 'a change to ' + surfaceWord(p.kind, p.key) +
              (arr(p.diff).length ? ': ' + p.diff.map(plainChange).join('; ') : '') + '.';
          }
          break;
        case 'eval': {
          var h = isObj(p.heldout) ? p.heldout : null;
          var c = h ? scoreOf(h.candidate) : null, ch = h ? scoreOf(h.champion) : null;
          if (isNum(c) && isNum(ch)) {
            return 'Tested ' + (id || 'the change') + ' on unseen test meetings: proposed version ' + pct(c) + ' correct, current version ' + pct(ch) +
              (has(it.status) ? '; ' + statusWord(it.status) : '') + '.';
          }
          break;
        }
        case 'commit':
          if (has(p.kind) && isNum(p.version)) {
            return 'Adopted ' + (id || 'a change') + ': ' + surfaceWord(p.kind, p.key) + ' is now version ' + p.version + '.';
          }
          break;
        case 'reject': {
          var why = reasonText(p.decision);
          if (id) return 'Rejected ' + id + (why ? ': ' + why : '') + '.';
          break;
        }
        case 'refused':
          if (id && it.actor === 'gate') return 'Refused ' + id + ': it is never allowed, because it would change how the system is graded.';
          break;
      }
    } catch (e) { /* fall back to the server's sentence */ }
    return it.summary || (actorWord(it.actor) + ' ' + (ENTRY_WORDS[it.kind] || it.kind || ''));
  }

  function actorKey(actor) {
    var a = String(actor || '').toLowerCase();
    return KNOWN_ACTORS.indexOf(a) >= 0 ? a : 'other';
  }
  function actorChip(actor) {
    var k = actorKey(actor);
    return '<span class="actor-chip actor-' + k + '" title="' + esc('actor: ' + (actor || 'unknown')) + '"><span class="actor-mark" aria-hidden="true"></span>' +
      esc(actorWord(actor)) + '</span>';
  }

  var STATUS_TONE = {
    committed: 'positive', approved: 'positive',
    rejected: 'danger',
    refused: 'locked', frozen: 'locked',
    pending: 'warning', evaluating: 'warning', awaiting_owner: 'warning', awaiting_approval: 'warning',
    stale: 'neutral'
  };
  function statusBadge(status) {
    if (!has(status)) return '';
    var tone = STATUS_TONE[String(status)] || 'neutral';
    return '<span class="badge badge-' + tone + '" title="' + esc('status: ' + status) + '">' + esc(statusWord(status)) + '</span>';
  }

  function tierBadge(tier) {
    if (!has(tier)) return '<span class="badge badge-neutral" title="tier: not set">not classified yet</span>';
    var t = String(tier).toUpperCase();
    var tone = t === 'H' ? 'warning' : t === 'X' ? 'locked' : 'neutral';
    return '<span class="badge badge-' + tone + ' badge-tier" title="' + esc('tier ' + t) + '">' + esc(TIER_WORDS[t] || 'tier ' + t) + '</span>';
  }

  function jsonBlock(v, label) {
    return '<pre class="json" tabindex="0"' + (label ? ' aria-label="' + esc(label) + '"' : '') + '>' + esc(json(v)) + '</pre>';
  }

  function factChips(ids) {
    var list = arr(ids);
    if (!list.length) return '<span class="muted">no source</span>';
    return list.map(function (id) { return '<span class="fact-chip">' + esc(id) + '</span>'; }).join(' ');
  }

  // Safe markdown: escape everything first, then add only a fixed set of tags.
  function mdInline(text) {
    var parts = esc(text).split(/`([^`]+)`/);
    return parts.map(function (seg, i) {
      if (i % 2 === 1) return '<code>' + seg + '</code>';
      return seg
        .replace(/\(cites: ([^)]*)\)/g, function (_, ids) {
          var chips = ids.split(/,\s*/).filter(Boolean).map(function (id) { return '<span class="fact-chip">' + id + '</span>'; }).join(' ');
          return '<span class="cites"><span class="visually-hidden">cites </span>' + chips + '</span>';
        })
        .replace(/\*\*([^*]+?)\*\*/g, '<strong>$1</strong>')
        .replace(/__([^_]+?)__/g, '<strong>$1</strong>')
        .replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?![*\w])/g, '$1<em>$2</em>')
        .replace(/(^|[^\w])_([^_\s][^_]*?)_(?![\w])/g, '$1<em>$2</em>')
        .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    }).join('');
  }
  function renderMarkdown(src) {
    var lines = String(src == null ? '' : src).replace(/\r\n?/g, '\n').split('\n');
    var out = [], para = [], list = null, code = null;
    function flushPara() { if (para.length) { out.push('<p>' + mdInline(para.join(' ')) + '</p>'); para = []; } }
    function flushList() { if (list) { out.push('<' + list.type + '>' + list.items.map(function (it) { return '<li>' + mdInline(it) + '</li>'; }).join('') + '</' + list.type + '>'); list = null; } }
    lines.forEach(function (line) {
      if (code) {
        if (/^\s*```/.test(line)) { out.push('<pre class="md-code"><code>' + esc(code.join('\n')) + '</code></pre>'); code = null; }
        else code.push(line);
        return;
      }
      if (/^\s*```/.test(line)) { flushPara(); flushList(); code = []; return; }
      if (/^\s*$/.test(line)) { flushPara(); flushList(); return; }
      var m = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line);
      if (m) {
        flushPara(); flushList();
        var level = Math.min(6, m[1].length + 2);   // page owns h1/h2; brief headings start at h3
        out.push('<h' + level + ' class="md-h md-h' + m[1].length + '">' + mdInline(m[2]) + '</h' + level + '>');
        return;
      }
      if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) { flushPara(); flushList(); out.push('<hr>'); return; }
      m = /^\s*[-*+]\s+(.*)$/.exec(line);
      if (m) { flushPara(); if (!list || list.type !== 'ul') { flushList(); list = { type: 'ul', items: [] }; } list.items.push(m[1]); return; }
      m = /^\s*\d+[.)]\s+(.*)$/.exec(line);
      if (m) { flushPara(); if (!list || list.type !== 'ol') { flushList(); list = { type: 'ol', items: [] }; } list.items.push(m[1]); return; }
      if (list && /^\s{2,}\S/.test(line)) { list.items[list.items.length - 1] += ' ' + line.trim(); return; }
      m = /^\s*>\s?(.*)$/.exec(line);
      if (m) { flushPara(); flushList(); out.push('<blockquote>' + mdInline(m[1]) + '</blockquote>'); return; }
      flushList();
      para.push(line.trim());
    });
    if (code) out.push('<pre class="md-code"><code>' + esc(code.join('\n')) + '</code></pre>');
    flushPara(); flushList();
    return out.join('\n');
  }

  // Horizontal bars. rows: [{label, value, series: 'base'|'focus', text}], max: scale top.
  function barRow(label, value, max, series, text) {
    var pct = isNum(value) && max > 0 ? Math.max(0, Math.min(100, (value / max) * 100)) : 0;
    var shown = text != null ? text : (isNum(value) ? fmtNum(value) : 'no result');
    return '<div class="bar-row">' +
      '<span class="bar-label">' + esc(label) + '</span>' +
      '<span class="bar-area" title="' + esc(label + ': ' + shown) + '">' +
        (isNum(value) ? '<span class="bar-fill series-' + series + (value > 0 ? '' : ' is-zero') + '" style="--pct:' + pct.toFixed(2) + '"></span>' : '') +
        '<span class="bar-value' + (isNum(value) ? '' : ' muted') + '">' + esc(shown) + '</span>' +
      '</span>' +
    '</div>';
  }
  function legend(items) {
    return '<p class="legend">' + items.map(function (it) {
      return '<span class="legend-item"><span class="legend-swatch series-' + it.series + '" aria-hidden="true"></span>' + esc(it.label) + '</span>';
    }).join('') + '</p>';
  }

  // ---------------------------------------------------------------- state

  var state = {
    tab: 'activity',
    overview: null,
    overviewError: null,
    lastOk: null,
    conn: 'idle',
    activity: {
      items: [], seqs: {}, lastSeq: 0, loaded: false, error: null,
      fresh: {}, expanded: {}, actorsOff: {}, field: '', actors: [], fields: []
    },
    pollTimer: null, pollCount: 0, polling: false,
    briefSel: null,
    cabinet: { selected: null, showAll: false, answerKey: false, vocab: null },
    db: { db: null, coll: null, limit: 20 },
    pendingFocus: null,
    pdb: null            // selected Pregame database; null = the server's default
  };

  function pollingEnabled() {
    return !FIXTURES && !(state.overview && state.overview.offline);
  }

  // ---------------------------------------------------------------- header

  function setConn(kind) {
    state.conn = kind;
    var box = $('#hdr-conn');
    var label = $('#hdr-updated');
    var when = state.lastOk ? clock(state.lastOk) : null;
    var text;
    if (kind === 'ok') text = 'updated ' + when;
    else if (kind === 'error') text = when ? 'no answer since ' + when : 'server not reachable';
    else if (kind === 'paused') text = (FIXTURES ? 'saved files, loaded ' : 'snapshot, loaded ') + (when || '–');
    else text = 'connecting';
    box.setAttribute('data-state', kind);
    label.textContent = text;
    box.title = kind === 'paused' ? 'Not polling: the data is a fixed snapshot' : kind === 'ok' ? 'Checking for new audit-log entries every 2 seconds (polling /api/activity)' : '';
  }

  var MODE_BADGE = {
    fake: { text: 'STAND-IN', tone: 'warning', title: 'LLM mode fake: deterministic stand-in answers, no model calls' },
    cassette: { text: 'REPLAY', tone: 'accent', title: 'LLM mode cassette: replaying recorded model answers' },
    replay: { text: 'REPLAY', tone: 'accent', title: 'LLM mode replay: answers served from a recorded cassette' },
    live: { text: 'LIVE', tone: 'positive', title: 'LLM mode live: real model calls' },
    record: { text: 'LIVE', tone: 'positive', title: 'LLM mode record: real model calls, recorded to a cassette for replay' }
  };

  function renderHeader() {
    var o = state.overview;
    $('#hdr-sim').textContent = o ? fmtSim(o.sim_time) : (state.overviewError ? 'unavailable' : 'not loaded');
    renderDbSwitch();
    var mode = $('#hdr-mode');
    var key = o && has(o.llm_mode) ? String(o.llm_mode).toLowerCase() : null;
    var def = key ? MODE_BADGE[key] : null;
    mode.className = 'badge badge-' + (def ? def.tone : 'neutral') + (key === 'live' || key === 'record' ? ' badge-live' : '');
    mode.textContent = def ? def.text : key ? key.toUpperCase() : (o || state.overviewError ? 'MODE UNKNOWN' : 'MODE');
    mode.title = def ? def.title + (o.provider ? ' (provider ' + o.provider + ')' : '') : 'LLM mode not reported';
    var off = $('#hdr-offline');
    if (o && o.offline) {
      off.hidden = false;
      var stamp = o.snapshot_at || o.generated_at;
      off.textContent = 'OFFLINE SNAPSHOT · ' + shortStamp(stamp) + (/Z$|[+-]00:?00$/.test(String(stamp || '')) ? ' UTC' : '');
      off.title = 'Served from fixtures/ by server.py --offline; snapshot taken ' + fmtStamp(o.snapshot_at || o.generated_at);
    } else {
      off.hidden = true;
    }
    var fx = $('#hdr-fixtures');
    fx.hidden = !FIXTURES || STATIC_SITE;
    if (FIXTURES && o) fx.title = 'Loaded from the fixtures folder; generated ' + fmtStamp(o.snapshot_at || o.generated_at);
  }

  // ---------------------------------------------------------------- Pregame database ("Run") switcher

  // What each Pregame database is, in plain words. A label from the server (runs[].label) wins over these.
  var DB_NOTES = {
    pregame_demo: 'demo (stage, recorded from run C)',
    pregame_run_a: 'run A (earlier live run, not replayable)',
    pregame_run_b: 'run B (earlier live run, not replayable)',
    pregame_run_c: 'run C (live run, the stage recording)'
  };
  function dbLabel(name) {
    var n = String(name || '');
    if (DB_NOTES[n]) return DB_NOTES[n];
    var m = /^pregame_run_([a-z0-9]+)$/i.exec(n);
    if (m) return 'run ' + m[1].toUpperCase() + ' (live run)';
    m = /^pregame_regrade_([a-z0-9]+)$/i.exec(n);
    if (m) return 'regrade ' + m[1].toUpperCase();
    return n;
  }
  // "run A (earlier live run, not replayable)" -> "run A"; "demo (stage, ...)" -> "demo"
  function dbShort(name) { return dbLabel(name).replace(/\s*\(.*\)$/, ''); }
  function effectiveDb() {
    var o = state.overview;
    return state.pdb || (o && (o.active_db || o.pregame_db)) || null;
  }
  function renderDbSwitch() {
    var o = state.overview;
    var sel = $('#db-select'), txt = $('#hdr-db'), label = $('#hdr-db-label');
    var list = o && Array.isArray(o.pregame_dbs) ? o.pregame_dbs.filter(function (d) { return d && has(d.name || d); }) : [];
    var cur = effectiveDb();
    if (list.length) {
      var names = list.map(function (d) { return typeof d === 'string' ? d : d.name; });
      if (cur && names.indexOf(cur) < 0) names.unshift(cur);
      var sig = names.join('|') + '#' + cur;
      if (sel.getAttribute('data-sig') !== sig) {
        var byName = {};
        list.forEach(function (d) { if (isObj(d)) byName[d.name] = d; });
        var offline = FIXTURES || (o && o.offline);
        sel.innerHTML = names.map(function (n) {
          var missing = offline && isObj(byName[n]) && byName[n].in_snapshot === false;
          return '<option value="' + esc(n) + '"' + (n === cur ? ' selected' : '') + (missing ? ' disabled' : '') + '>' +
            esc(dbLabel(n) + (missing ? ' (not in the snapshot)' : '')) + '</option>';
        }).join('');
        sel.setAttribute('data-sig', sig);
      }
      var info = list.filter(function (d) { return (d.name || d) === cur; })[0];
      var bits = [];
      if (isObj(info)) {
        if (isNum(info.ledger)) bits.push(plural(info.ledger, 'audit log entry', 'audit log entries'));
        if (isNum(info.proposals)) bits.push(plural(info.proposals, 'proposed change'));
        if (isNum(info.briefs)) bits.push(plural(info.briefs, 'brief'));
        if (isNum(info.cabinet_preps)) bits.push(plural(info.cabinet_preps, 'harness prep'));
      }
      sel.title = (cur || '') + (bits.length ? ': ' + bits.join(', ') : '');
      sel.hidden = false;
      txt.hidden = true;
      label.textContent = 'Run';
    } else {
      sel.hidden = true;
      txt.hidden = false;
      label.textContent = 'Database';
      txt.textContent = cur || (state.overviewError ? 'unavailable' : 'not loaded');
    }
  }

  function setupDbSwitch() {
    state.pdb = lsGet(LS_DB) || null;
    $('#db-select').addEventListener('change', function (ev) { switchDb(ev.target.value); });
  }

  // Switching runs: forget the feed and every Pregame view, then load the new database's.
  function switchDb(name) {
    if (!name || name === effectiveDb()) return;
    state.pdb = name;
    lsSet(LS_DB, name);
    clearTimeout(state.pollTimer);
    resetActivity();
    state.activity.error = null;
    state.overview = null;
    state.overviewError = null;
    state.briefSel = null;
    state.versionFilter = '';
    ['#runs-body', '#proposals-body', '#versions-body', '#briefs-body', '#findings-body', '#cabinet-body'].forEach(function (sel) {
      var b = $(sel); if (b) { b.removeAttribute('data-ready'); b.innerHTML = ''; }
    });
    renderHeader();
    setConn('idle');
    renderActivity();
    loadRun();
    if (state.tab !== 'activity') loadTab(state.tab, {});
  }

  // Overview first (it says whether we are offline), then the ledger, then start polling.
  function loadRun() {
    var db = state.pdb;
    return refreshOverview(true).catch(function (e) {
      // A remembered run the server no longer has: drop it and fall back to the server's default.
      if (state.pdb && e && e.kind === 'http' && (e.detail.status === 400 || e.detail.status === 403 || e.detail.status === 404)) {
        state.pdb = null;
        lsSet(LS_DB, '');
        return refreshOverview(true).catch(noop);
      }
    }).then(function () {
      if (db !== state.pdb && state.pdb !== null) return;
      return loadActivityFull().then(function () {
        setConn(pollingEnabled() ? 'ok' : 'paused');
      }, function () {
        setConn(pollingEnabled() ? 'error' : 'paused');
      });
    }).then(function () {
      if (state.tab === 'activity') renderActivity();
      else updateFilterControls();
      schedulePoll();
    });
  }

  function refreshOverview(force) {
    return need('overview', ['overview'], null, force).then(function (o) {
      state.overview = o;
      state.overviewError = null;
      renderHeader();
      if (state.tab === 'activity') renderSide();
      return o;
    }, function (e) {
      state.overviewError = e;
      renderHeader();
      if (state.tab === 'activity') renderSide();
      throw e;
    });
  }

  // ---------------------------------------------------------------- theme

  var THEMES = ['auto', 'light', 'dark'];
  function applyTheme(t) {
    if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t);
    else document.documentElement.removeAttribute('data-theme');
    var b = $('#theme-toggle');
    var name = t === 'light' || t === 'dark' ? t : 'auto';
    b.textContent = 'Theme: ' + name;
    b.setAttribute('aria-label', 'Colour theme: ' + (name === 'auto' ? 'follow the system' : name) + '. Change theme');
  }
  function setupTheme() {
    var t = lsGet(LS_THEME) || 'auto';
    applyTheme(t);
    $('#theme-toggle').addEventListener('click', function () {
      var cur = document.documentElement.getAttribute('data-theme') || 'auto';
      var next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
      applyTheme(next);
      lsSet(LS_THEME, next);
    });
  }

  // ---------------------------------------------------------------- tabs

  function setupTabs() {
    var tabs = $$('[role="tab"]');
    tabs.forEach(function (t) {
      t.addEventListener('click', function () { selectTab(t.getAttribute('data-tab')); });
      t.addEventListener('keydown', function (ev) {
        var i = tabs.indexOf(t), j = null;
        if (ev.key === 'ArrowRight') j = (i + 1) % tabs.length;
        else if (ev.key === 'ArrowLeft') j = (i - 1 + tabs.length) % tabs.length;
        else if (ev.key === 'Home') j = 0;
        else if (ev.key === 'End') j = tabs.length - 1;
        if (j === null) return;
        ev.preventDefault();
        tabs[j].focus();
        selectTab(tabs[j].getAttribute('data-tab'));
      });
    });
  }

  function selectTab(name, opts) {
    if (TABS.indexOf(name) < 0) name = 'activity';
    var changed = state.tab !== name;
    state.tab = name;
    if (changed && !state.pendingFocus) window.scrollTo(0, 0);
    lsSet(LS_TAB, name);
    $$('[role="tab"]').forEach(function (t) {
      var on = t.getAttribute('data-tab') === name;
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      t.tabIndex = on ? 0 : -1;
    });
    $$('[role="tabpanel"]').forEach(function (p) { p.hidden = p.id !== 'panel-' + name; });
    loadTab(name, opts || {});
  }

  function loadTab(name, opts) {
    var fn = { activity: showActivity, runs: showRuns, proposals: showProposals, versions: showVersions, briefs: showBriefs,
      findings: showFindings, cabinet: showCabinet, databases: showDatabases }[name];
    fn(opts || {});
  }

  // Run a tab's loader; show loading only when the tab has nothing yet, errors in the tab only.
  function runTab(bodySel, what, loader, render, opts) {
    var body = $(bodySel);
    var hasContent = body.getAttribute('data-ready') === '1';
    var db = state.pdb;
    if (!hasContent && !opts.quiet) body.innerHTML = loadingState(what);
    return loader().then(function (data) {
      if (db !== state.pdb) return;   // the run was switched; the new run's load renders instead
      render(data);
      body.setAttribute('data-ready', '1');
      afterRender();
    }, function (e) {
      if (db !== state.pdb) return;
      if (opts.quiet && hasContent) return;
      body.removeAttribute('data-ready');
      body.innerHTML = '';
      body.appendChild(errorPanel(e, what, function () { loadTab(state.tab, {}); }));
    });
  }

  function afterRender() {
    var f = state.pendingFocus;
    if (!f) return;
    state.pendingFocus = null;
    var target = document.getElementById(f);
    if (target) {
      target.scrollIntoView({ block: 'center' });
      flash(target);
      var focusable = target.matches('button, [tabindex]') ? target : target.querySelector('button, summary, [tabindex]');
      if (focusable) focusable.focus({ preventScroll: true });
    }
  }
  function flash(node) {
    node.classList.remove('is-flash');
    void node.offsetWidth;
    node.classList.add('is-flash');
    setTimeout(function () { node.classList.remove('is-flash'); }, 2400);
  }

  function openRef(ref) {
    if (!ref || !ref.type) return;
    var id = ref.id;
    if (ref.type === 'proposal') { state.pendingFocus = 'proposal-' + id; selectTab('proposals'); }
    else if (ref.type === 'brief') { state.briefSel = id; state.pendingFocus = 'brief-detail'; selectTab('briefs'); }
    else if (ref.type === 'version' || ref.type === 'config') { state.pendingFocus = 'version-' + id; selectTab('versions'); }
  }
  function refLabel(ref) {
    if (!ref || !ref.type) return '';
    var link = ref.type === 'proposal' || ref.type === 'brief' || ref.type === 'version' || ref.type === 'config';
    var REF_WORDS = { proposal: 'Proposed change', brief: 'Brief', version: 'Version', config: 'Version', event: 'World event', feedback: 'Advisor feedback' };
    var text = (REF_WORDS[ref.type] || humanize(ref.type)) + ' ' + (ref.id != null ? ref.id : '');
    if (!link) return '<span class="mono">' + esc(text) + '</span>';
    return '<button type="button" class="link-btn" data-ref-type="' + esc(ref.type) + '" data-ref-id="' + esc(ref.id) + '">' +
      esc(text) + '</button>';
  }
  document.addEventListener('click', function (ev) {
    var b = ev.target.closest && ev.target.closest('[data-ref-type]');
    if (b) { ev.preventDefault(); openRef({ type: b.getAttribute('data-ref-type'), id: b.getAttribute('data-ref-id') }); }
  });

  // ---------------------------------------------------------------- activity: data

  function itemsOf(data) {
    if (Array.isArray(data)) return data;
    if (isObj(data)) return arr(data.items);
    return [];
  }

  function mergeActivity(data) {
    var a = state.activity;
    var incoming = itemsOf(data);
    var added = [];
    incoming.forEach(function (it) {
      if (!it || !isNum(it.seq) || a.seqs[it.seq]) return;
      a.seqs[it.seq] = it;
      a.items.push(it);
      added.push(it);
    });
    if (added.length) a.items.sort(function (x, y) { return x.seq - y.seq; });
    // Page forward from the newest entry we hold (a limited response may stop short of the server's last_seq).
    var maxSeq = a.items.length ? a.items[a.items.length - 1].seq : 0;
    a.lastSeq = Math.max(a.lastSeq, maxSeq);
    added.forEach(function (it) {
      if (it.actor && a.actors.indexOf(it.actor) < 0) a.actors.push(it.actor);
      if (has(it.field) && a.fields.indexOf(it.field) < 0) a.fields.push(it.field);
    });
    return added;
  }

  function resetActivity() {
    var a = state.activity;
    a.items = []; a.seqs = {}; a.lastSeq = 0; a.loaded = false; a.fresh = {}; a.expanded = {};
    a.actors = []; a.fields = [];
  }

  function loadActivityFull() {
    var db = state.pdb;
    return getJSON(['activity'], { after_seq: 0, limit: 300 }).then(function (d) {
      if (db !== state.pdb) return d;   // the run was switched while this was in flight
      resetActivity();
      mergeActivity(d);
      state.activity.loaded = true;
      state.activity.error = null;
      state.lastOk = new Date();
      return d;
    }, function (e) {
      state.activity.error = e;
      throw e;
    });
  }

  var STALE_BY_KIND = {
    proposal: ['proposals'], eval: ['proposals', 'findings'], commit: ['proposals', 'versions'],
    reject: ['proposals'], refused: ['proposals'], approve: ['proposals', 'versions'], rollback: ['proposals', 'versions'],
    brief: ['briefs'], event: ['world'], feedback: ['world'], seed: ['proposals', 'versions', 'briefs', 'world', 'findings']
  };
  var TAB_OF_KEY = { proposals: 'proposals', versions: 'versions', briefs: 'briefs', findings: 'findings', world: 'activity' };

  function onNewActivity(added) {
    var keys = {};
    added.forEach(function (it) {
      (STALE_BY_KIND[it.kind] || ['proposals', 'versions', 'briefs', 'world', 'findings']).forEach(function (k) { keys[k] = 1; });
    });
    var list = Object.keys(keys);
    markStale(list);
    var a = state.activity;
    added.forEach(function (it) { a.fresh[it.seq] = Date.now(); });
    setTimeout(clearFresh, NEW_HIGHLIGHT_MS + 50);
    if (state.tab === 'activity') {
      updateFilterControls();
      prependFeed(added);
      if (keys.world) need('world', ['world'], null, true).then(renderSide, renderSide);
    } else if (list.some(function (k) { return TAB_OF_KEY[k] === state.tab; })) {
      loadTab(state.tab, { quiet: true });
    }
  }

  function clearFresh() {
    var a = state.activity, now = Date.now();
    Object.keys(a.fresh).forEach(function (seq) {
      if (now - a.fresh[seq] >= NEW_HIGHLIGHT_MS) {
        delete a.fresh[seq];
        var node = document.getElementById('act-' + seq);
        if (node) {
          node.classList.remove('is-new');
          var tag = node.querySelector('.new-tag');
          if (tag) tag.remove();
        }
      }
    });
  }

  function schedulePoll() {
    clearTimeout(state.pollTimer);
    if (pollingEnabled()) state.pollTimer = setTimeout(pollOnce, POLL_MS);
  }

  function noop() {}

  function reloadActivity() {
    return loadActivityFull().then(function () {
      setConn('ok');
      renderActivity();
      markStale(['proposals', 'versions', 'briefs', 'world', 'findings']);
      return refreshOverview(true).catch(noop);
    });
  }

  function pollOnce() {
    if (!pollingEnabled()) { setConn('paused'); return; }
    if (document.hidden || state.polling) { schedulePoll(); return; }
    state.polling = true;
    var a = state.activity;
    var work;
    if (!a.loaded) {
      work = reloadActivity();
    } else {
      var db = state.pdb;
      work = getJSON(['activity'], { after_seq: a.lastSeq, limit: 300 }).then(function (d) {
        if (db !== state.pdb) return;   // switched runs meanwhile
        state.lastOk = new Date();
        state.pollCount += 1;
        if (isObj(d) && isNum(d.last_seq) && d.last_seq < a.lastSeq) return reloadActivity();   // ledger reset: start over
        var added = mergeActivity(d);
        setConn('ok');
        if (added.length) onNewActivity(added);
        if (added.length || state.pollCount % OVERVIEW_EVERY === 0) return refreshOverview(true).catch(noop);
      });
    }
    work.catch(function () { setConn('error'); }).then(function () {
      state.polling = false;
      schedulePoll();
    });
  }

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && pollingEnabled()) { clearTimeout(state.pollTimer); pollOnce(); }
  });

  // ---------------------------------------------------------------- activity: render

  function filteredItems() {
    var a = state.activity;
    return a.items.filter(function (it) {
      if (a.actorsOff[it.actor]) return false;
      if (a.field && it.field !== a.field) return false;
      return true;
    });
  }

  function feedItemHtml(it) {
    var a = state.activity;
    var open = !!a.expanded[it.seq];
    var isNew = !!a.fresh[it.seq];
    var id = 'act-' + it.seq;
    return '<article class="feed-item' + (isNew ? ' is-new' : '') + (open ? ' is-open' : '') + '" id="' + id + '" data-seq="' + esc(it.seq) + '">' +
      '<button type="button" class="feed-row" aria-expanded="' + open + '" aria-controls="' + id + '-detail">' +
        '<span class="feed-seq mono">#' + esc(it.seq) + '</span>' +
        '<span class="feed-actor">' + actorChip(it.actor) + '</span>' +
        '<span class="feed-main">' +
          '<span class="feed-top">' +
            '<span class="feed-kind" title="' + esc('kind: ' + (it.kind || '')) + '">' + esc(ENTRY_WORDS[it.kind] || it.kind || 'entry') + (isNew ? ' <span class="new-tag">new</span>' : '') + '</span>' +
            '<span class="feed-meta">' +
              (has(it.field) ? '<span class="field-tag" title="' + esc('field: ' + it.field) + '">' + esc(fieldWord(it.field)) + '</span>' : '') +
              (has(it.status) && statusWord(it.status) !== ENTRY_WORDS[it.kind] ? statusBadge(it.status) : '') +
              '<span class="feed-time mono" title="Simulated date (sim_time)">' + esc(fmtSim(it.sim_time)) + '</span>' +
            '</span>' +
          '</span>' +
          '<span class="feed-summary" title="' + esc(it.summary_technical || it.summary || '') + '">' + esc(plainSummary(it)) + '</span>' +
        '</span>' +
        '<span class="feed-caret" aria-hidden="true"></span>' +
      '</button>' +
      '<div class="feed-detail" id="' + id + '-detail"' + (open ? '' : ' hidden') + '>' +
        (open ? feedDetailHtml(it) : '') +
      '</div>' +
    '</article>';
  }

  function feedDetailHtml(it) {
    var p = isObj(it.payload) ? it.payload : {};
    return '<dl class="kv kv-inline">' +
      '<div><dt>Simulated date</dt><dd class="mono">' + esc(has(it.sim_time) ? it.sim_time : '–') + '</dd></div>' +
      '<div><dt>Recorded</dt><dd class="mono">' + esc(fmtStamp(it.recorded_at)) + '</dd></div>' +
      (it.ref ? '<div><dt>Refers to</dt><dd>' + refLabel(it.ref) + '</dd></div>' : '') +
      '<div><dt title="hash">Entry fingerprint</dt><dd class="mono" title="' + esc(it.hash || '') + '">' + esc(shortHash(it.hash)) + '</dd></div>' +
      '<div><dt title="prev_hash">Previous entry’s fingerprint</dt><dd class="mono" title="' + esc(it.prev_hash || '') + '">' + esc(shortHash(it.prev_hash)) + '</dd></div>' +
    '</dl>' +
    (has(p.decision) ? '<p class="exact-line"><span class="exact-label">Exact decision (as recorded):</span> ' + esc(textOf(p.decision)) + '</p>' : '') +
    (has(it.summary_technical) || has(it.summary) ? '<p class="exact-line"><span class="exact-label">Engineering summary:</span> ' + esc(it.summary_technical || it.summary) + '</p>' : '') +
    '<p class="detail-label" title="payload">The recorded entry, as stored</p>' +
    (it.payload === undefined ? '<p class="muted">Nothing recorded.</p>' : jsonBlock(it.payload, 'Recorded entry ' + it.seq));
  }

  function updateFilterControls() {
    var a = state.activity;
    var box = $('#actor-filters');
    var actors = KNOWN_ACTORS.slice();
    a.actors.forEach(function (x) { if (actors.indexOf(x) < 0) actors.push(x); });
    var counts = {};
    a.items.forEach(function (it) { counts[it.actor] = (counts[it.actor] || 0) + 1; });
    var sig = actors.map(function (x) { return x + ':' + (counts[x] || 0) + ':' + (a.actorsOff[x] ? 0 : 1); }).join('|');
    if (box.getAttribute('data-sig') !== sig) {
      var focused = document.activeElement && box.contains(document.activeElement) ? document.activeElement.getAttribute('data-actor') : null;
      box.innerHTML = actors.map(function (x) {
        var on = !a.actorsOff[x];
        return '<button type="button" class="filter-chip actor-' + actorKey(x) + '" data-actor="' + esc(x) + '" aria-pressed="' + on + '" title="' + esc('actor: ' + x + (on ? '' : ' (hidden)')) + '">' +
          '<span class="actor-mark" aria-hidden="true"></span>' + esc(actorWord(x)) +
          ' <span class="filter-count">' + (counts[x] || 0) + '</span></button>';
      }).join('');
      box.setAttribute('data-sig', sig);
      if (focused) { var f = box.querySelector('[data-actor="' + CSS.escape(focused) + '"]'); if (f) f.focus(); }
    }
    var sel = $('#field-filter');
    var fields = a.fields.slice().sort();
    var fsig = fields.join('|');
    if (sel.getAttribute('data-sig') !== fsig) {
      sel.innerHTML = '<option value="">All client groups</option>' + fields.map(function (f) {
        return '<option value="' + esc(f) + '"' + (a.field === f ? ' selected' : '') + '>' + esc(fieldWord(f)) + '</option>';
      }).join('');
      sel.setAttribute('data-sig', fsig);
    }
  }

  function updateCount() {
    var a = state.activity;
    var shown = filteredItems().length;
    $('#activity-count').textContent = a.loaded
      ? (shown === a.items.length ? plural(a.items.length, 'entry', 'entries') : shown + ' of ' + plural(a.items.length, 'entry', 'entries'))
      : '';
  }

  function renderFeed() {
    var a = state.activity;
    var feed = $('#activity-feed');
    if (!a.loaded) {
      if (a.error) { feed.innerHTML = ''; feed.appendChild(errorPanel(a.error, 'the audit log', retryActivity)); }
      else feed.innerHTML = loadingState('the audit log');
      updateCount();
      return;
    }
    var items = filteredItems();
    if (!a.items.length) {
      feed.innerHTML = emptyState('The audit log is empty', 'Nothing has happened yet. Entries appear here as the simulated world, the writer, the proposer and the test gate act.');
    } else if (!items.length) {
      feed.innerHTML = emptyState('No entries match these filters', 'Turn one of the “Who” buttons back on, or pick “All client groups”.');
    } else {
      var list = items.slice(-FEED_MAX).reverse();
      feed.innerHTML = list.map(feedItemHtml).join('') +
        (items.length > FEED_MAX ? '<p class="muted small">Showing the newest ' + FEED_MAX + ' of ' + items.length + '.</p>' : '');
    }
    updateCount();
  }

  function prependFeed(added) {
    var a = state.activity;
    var feed = $('#activity-feed');
    if (!feed.querySelector('.feed-item')) { renderFeed(); return; }
    var visible = added.filter(function (it) {
      if (a.actorsOff[it.actor]) return false;
      if (a.field && it.field !== a.field) return false;
      return true;
    }).sort(function (x, y) { return y.seq - x.seq; });
    var html = visible.map(feedItemHtml).join('');
    if (html) feed.insertAdjacentHTML('afterbegin', html);
    updateCount();
  }

  function retryActivity() {
    state.activity.error = null;
    renderFeed();
    loadActivityFull().then(function () {
      setConn(pollingEnabled() ? 'ok' : 'paused');
      renderActivity();
      schedulePoll();
    }, function () { renderFeed(); setConn(pollingEnabled() ? 'error' : 'paused'); });
  }

  function setupActivityControls() {
    $('#actor-filters').addEventListener('click', function (ev) {
      var b = ev.target.closest('[data-actor]');
      if (!b) return;
      var x = b.getAttribute('data-actor');
      var a = state.activity;
      if (a.actorsOff[x]) delete a.actorsOff[x]; else a.actorsOff[x] = true;
      updateFilterControls();
      renderFeed();
    });
    $('#field-filter').addEventListener('change', function (ev) {
      state.activity.field = ev.target.value;
      renderFeed();
    });
    $('#activity-feed').addEventListener('click', function (ev) {
      if (ev.target.closest('[data-ref-type]')) return;
      var row = ev.target.closest('.feed-row');
      if (!row) return;
      var art = row.closest('.feed-item');
      var seq = Number(art.getAttribute('data-seq'));
      toggleFeedItem(art, seq);
    });
    $('#activity-side').addEventListener('click', function (ev) {
      var b = ev.target.closest('[data-show-seq]');
      if (!b) return;
      var seq = Number(b.getAttribute('data-show-seq'));
      var a = state.activity;
      var it = a.seqs[seq];
      if (!it) return;
      // Make sure the entry is visible under the current filters, then open it.
      if (a.actorsOff[it.actor] || (a.field && it.field !== a.field)) { a.actorsOff = {}; a.field = ''; $('#field-filter').value = ''; updateFilterControls(); renderFeed(); }
      var art = document.getElementById('act-' + seq);
      if (!art) return;
      if (!a.expanded[seq]) toggleFeedItem(art, seq);
      art.scrollIntoView({ block: 'center' });
      flash(art);
      art.querySelector('.feed-row').focus({ preventScroll: true });
    });
  }

  function toggleFeedItem(art, seq) {
    var a = state.activity;
    var open = !a.expanded[seq];
    if (open) a.expanded[seq] = true; else delete a.expanded[seq];
    var row = art.querySelector('.feed-row');
    var detail = art.querySelector('.feed-detail');
    row.setAttribute('aria-expanded', String(open));
    art.classList.toggle('is-open', open);
    if (open) { detail.innerHTML = feedDetailHtml(a.seqs[seq]); detail.hidden = false; }
    else { detail.hidden = true; detail.innerHTML = ''; }
  }

  function latestCard(title, item, emptyText) {
    if (!item) return '<section class="side-card"><h2 class="side-title">' + esc(title) + '</h2><p class="muted">' + esc(emptyText) + '</p></section>';
    return '<section class="side-card"><h2 class="side-title">' + esc(title) + '</h2>' +
      '<p class="side-line">' + actorChip(item.actor) + ' <span class="feed-kind" title="' + esc('kind: ' + (item.kind || '')) + '">' + esc(ENTRY_WORDS[item.kind] || item.kind || '') + '</span>' +
        (has(item.status) && statusWord(item.status) !== ENTRY_WORDS[item.kind] ? ' ' + statusBadge(item.status) : '') + '</p>' +
      '<p class="side-summary" title="' + esc(item.summary_technical || item.summary || '') + '">' + esc(plainSummary(item)) + '</p>' +
      '<p class="side-foot"><span class="mono">#' + esc(item.seq) + '</span> · ' + esc(fmtSim(item.sim_time)) +
        (has(item.field) ? ' · ' + esc(fieldWord(item.field)) : '') +
        (isNum(item.seq) ? ' · <button type="button" class="link-btn" data-show-seq="' + esc(item.seq) + '">show in the log</button>' : '') +
      '</p></section>';
  }

  function renderSide() {
    var side = $('#activity-side');
    var o = state.overview;
    if (!o) {
      if (state.overviewError) {
        side.innerHTML = '';
        side.appendChild(errorPanel(state.overviewError, 'the overview', function () { refreshOverview(true).catch(function () {}); }));
      } else side.innerHTML = loadingState('the overview');
      return;
    }
    var html = '';
    var ch = o.chain || {};
    var ok = ch.links_ok;
    html += '<section class="side-card"><h2 class="side-title" title="ledger hash chain">Audit log</h2>' +
      '<p class="chain-state" title="' + esc('chain.links_ok = ' + ok) + '">' +
        (ok === true ? '<span class="badge badge-positive">intact</span> <span class="chain-text">every entry chained, no edits</span>' :
          ok === false ? '<span class="badge badge-danger">broken</span> <span class="chain-text">the chain breaks at entry #' + esc(ch.first_break_seq != null ? ch.first_break_seq : '?') + '</span>' :
          '<span class="badge badge-neutral">not checked</span>') +
      '</p>' +
      '<dl class="kv kv-tight">' +
        '<div><dt>Entries</dt><dd>' + esc(fmtNum(ch.entries)) + '</dd></div>' +
        '<div><dt title="last_seq">Latest entry</dt><dd class="mono">' + esc(has(ch.last_seq) ? '#' + ch.last_seq : '–') + '</dd></div>' +
        '<div><dt title="last_hash">Latest fingerprint</dt><dd class="mono" title="' + esc(ch.last_hash || '') + '">' + esc(shortHash(ch.last_hash)) + '</dd></div>' +
      '</dl>' +
      '<p class="side-note" title="each entry\u2019s prev_hash equals the previous entry\u2019s hash">Each entry carries the fingerprint of the one before it, so an edit would show.</p></section>';

    var latest = o.latest || {};
    html += latestCard('Latest adopted change', latest.commit, 'Nothing adopted yet.');
    var rej = latest.reject, ref = latest.refused, pick = null;
    if (rej && ref) pick = (ref.seq || 0) > (rej.seq || 0) ? ref : rej; else pick = rej || ref || null;
    html += latestCard('Latest rejected or refused change', pick, 'Nothing rejected or refused yet.');

    var pbs = o.proposals_by_status || {};
    var keys = Object.keys(pbs);
    var total = keys.reduce(function (s, k) { return s + (isNum(pbs[k]) ? pbs[k] : 0); }, 0);
    var nonzero = keys.filter(function (k) { return pbs[k]; });
    var zero = keys.filter(function (k) { return !pbs[k]; });
    html += '<section class="side-card"><h2 class="side-title" title="proposals_by_status">Proposed changes by outcome</h2>' +
      (total ? '<ul class="status-list">' + nonzero.map(function (k) {
        return '<li><span>' + statusBadge(k) + '</span><span class="status-count">' + esc(fmtNum(pbs[k])) + '</span></li>';
      }).join('') + '</ul>' +
      '<p class="side-foot">' + plural(total, 'proposed change') + ' in total.</p>'
      : '<p class="muted">No proposed changes yet.</p>') +
      '</section>';

    var counts = o.counts || {};
    var ckeys = Object.keys(counts);
    if (ckeys.length) {
      var COUNT_WORDS = { ledger: 'audit log entries', proposals: 'proposed changes', briefs: 'briefs', feedback: 'advisor feedback',
        facts: 'facts', events_fired: 'world events so far', eval_runs: 'test runs' };
      html += '<section class="side-card"><h2 class="side-title">In the database</h2><dl class="kv kv-grid">' +
        ckeys.map(function (k) { return '<div><dt title="' + esc(k) + '">' + esc(humanize(COUNT_WORDS[k] || k)) + '</dt><dd>' + esc(fmtNum(counts[k])) + '</dd></div>'; }).join('') +
        '</dl></section>';
    }

    var w = cached('world');
    var we = entry('world') && entry('world').error;
    if (w && isObj(w)) {
      var ev = arr(w.events);
      var firedList = ev.filter(function (e) { return e.fired; });
      var waiting = ev.filter(function (e) { return !e.fired; }).sort(function (x, y) { return String(x.at || '') < String(y.at || '') ? -1 : 1; });
      var eventLi = function (e) {
        return '<li>' +
          (e.fired ? '<span class="badge badge-positive">happened</span>' : '<span class="badge badge-neutral">next</span>') +
          '<span class="event-title">' + esc(e.title || e.id) + '</span>' +
          '<span class="event-meta muted">' + esc([has(e.field) ? fieldWord(e.field) : '', fmtSim(e.fired ? e.fired_at || e.at : e.at)].filter(has).join(' · ')) + '</span>' +
        '</li>';
      };
      html += '<section class="side-card"><h2 class="side-title">World events</h2>' +
        (ev.length ? '<p class="side-foot">' + firedList.length + ' of ' + ev.length + ' scripted events so far · ' + plural(arr(w.feedback).length, 'advisor feedback note') + '</p>' +
          '<ol class="event-list">' + firedList.map(eventLi).join('') + waiting.slice(0, 3).map(eventLi).join('') + '</ol>' +
          (waiting.length > 3 ? '<p class="side-note">' + (waiting.length - 3) + ' more waiting.</p>' : '')
          : '<p class="muted">No scripted events.</p>') +
        '</section>';
    } else if (we) {
      html += '<section class="side-card"><h2 class="side-title">World events</h2><p class="muted">Could not load the world events.</p></section>';
    }
    side.innerHTML = html;
  }

  function renderActivity() {
    updateFilterControls();
    renderFeed();
    renderSide();
  }

  function showActivity() {
    renderActivity();
    if (!state.overview && !entry('overview')) refreshOverview(false).catch(function () {});
    need('world', ['world'], null, false).then(renderSide, renderSide);
  }


  // ---------------------------------------------------------------- runs (the same harness, several live runs)

  var RUNS_MAX_AGE_MS = 15000;   // the runs summary spans databases we do not poll: refetch when older than this

  function showRuns(opts) {
    runTab('#runs-body', 'the runs', function () {
      var c = entry('runs');
      var old = !c || !c.at || Date.now() - c.at > RUNS_MAX_AGE_MS;
      return need('runs', ['runs'], null, old && !FIXTURES);
    }, renderRuns, opts);
  }

  function runsOf(d) {
    if (Array.isArray(d)) return d;
    return isObj(d) ? arr(d.runs) : [];
  }
  function runName(r) { return has(r.label) ? String(r.label) : dbLabel(r.db); }
  function shortRunName(r) { return has(r.label) ? String(r.label) : dbShort(r.db); }
  function localClock(iso) {
    var d = has(iso) ? new Date(iso) : null;
    return d && !isNaN(d) ? pad2(d.getHours()) + ':' + pad2(d.getMinutes()) : '';
  }

  // The try-again gap: each run's first champion score is the seeded config, before anything was committed, so the
  // spread of those scores across runs is what re-running alone does to the number.
  function noiseBand(runs) {
    var vals = [];
    // A replay (the stage copy) repeats another run's numbers, so it is not an independent try.
    runs.filter(function (r) { return !r.stage && !has(r.recorded_from); }).forEach(function (r) {
      var first = arr(r.champion_mean_accuracy).filter(isNum)[0];
      if (isNum(first)) vals.push({ run: r, v: first });
    });
    if (!vals.length) return null;
    var nums = vals.map(function (x) { return x.v; });
    return { lo: Math.min.apply(null, nums), hi: Math.max.apply(null, nums), vals: vals };
  }

  // A bar row with the champion's across-run range drawn behind it as a shaded band (bars run 0 to 1).
  function bandBarRow(label, value, series, band) {
    var w = isNum(value) ? Math.max(0, Math.min(100, value * 100)) : 0;
    return '<div class="bar-row">' +
      '<span class="bar-label">' + esc(label) + '</span>' +
      '<span class="bar-area has-band" title="' + esc(label + ': ' + (isNum(value) ? fmtNum(value, 3) : 'no result')) + '">' +
        (band ? '<span class="bar-band" style="--lo:' + (band.lo * 100).toFixed(2) + ';--hi:' + (band.hi * 100).toFixed(2) + '" aria-hidden="true"></span>' : '') +
        (isNum(value) ? '<span class="bar-fill series-' + series + '" style="--pct:' + w.toFixed(2) + '"></span>' : '') +
        '<span class="bar-value' + (isNum(value) ? '' : ' muted') + '">' + esc(isNum(value) ? pct(value) : 'no result') + '</span>' +
      '</span></div>';
  }

  function textOf(v) { return typeof v === 'string' ? v : json(v); }
  function moveText(a, b) {
    if (!isNum(a) || !isNum(b)) return '';
    return pct(a) + ' → ' + pct(b);
  }

  function renderRuns(d) {
    var body = $('#runs-body');
    var all = runsOf(d);
    var runs = all.filter(function (r) { return !(isObj(r.chain) && r.chain.entries === 0); });
    var empty = all.length - runs.length;
    var html = sectionHead('Runs', 'The same system, run again against live models: what each run adopted, rejected and refused.');
    if (!runs.length) {
      body.innerHTML = html + emptyState('No runs yet', 'Runs appear here once a Pregame run database has a ledger.');
      return;
    }
    var band = noiseBand(runs);
    html += '<div class="noise-strip card" role="note">';
    if (band) {
      var same = Math.abs(band.hi - band.lo) < 0.005;
      html += '<p class="noise-main"><span class="noise-label" title="champion held-out mean_accuracy, first evaluation in each run">Questions the current version answered correctly on unseen test meetings, across runs:</span> ' +
        '<strong class="noise-range" title="' + esc(fmtNum(band.lo, 3) + '–' + fmtNum(band.hi, 3)) + '">' + (same ? pct(band.lo) : pct(band.lo) + '–' + pct(band.hi)) + '</strong> ' +
        '<span class="muted">(the try-again gap)</span></p>' +
        '<p class="noise-sub">The same starting version, tested in ' + plural(band.vals.length, 'run') + ' with nothing changed. ' +
        (same ? 'The scores agree.' : 'Read any gain against this spread of ' + Math.round((band.hi - band.lo) * 100) + ' points; a smaller gain is noise.') + '</p>' +
        '<p class="noise-runs">' + band.vals.map(function (x) {
          return '<span class="noise-run"><span class="muted">' + esc(shortRunName(x.run)) + '</span> <span title="' + esc(fmtNum(x.v, 3)) + '">' + esc(pct(x.v)) + '</span></span>';
        }).join('') + '</p>';
    } else {
      html += '<p class="noise-main">No test scores for the current version yet, so there is no try-again gap to show.</p>';
    }
    html += '</div>';

    html += sameChangeTable(runs);

    html += '<div class="run-grid">' + runs.map(function (r) { return runCard(r, band); }).join('') + '</div>';
    html += '<p class="muted small">' + (band ? 'Shaded band behind each bar: the current version’s range across runs (the try-again gap). Bars run from 0% to 100%. ' : '') +
      (empty ? plural(empty, 'empty database') + ' not shown.' : '') + '</p>';
    body.innerHTML = html;
  }

  // One row per proposed change (same diff), one column per live run: did each run commit, reject or refuse it?
  function sameChangeTable(runs) {
    var live = runs.filter(function (r) { return /^pregame_run_/.test(String(r.db || '')); });
    var cols = live.length >= 2 ? live : runs;
    if (cols.length < 2) return '';
    var rows = [], byKey = {};
    cols.forEach(function (r, ci) {
      arr(r.committed).map(function (x) { return { x: x, outcome: 'committed' }; })
        .concat(arr(r.rejected).map(function (x) { return { x: x, outcome: x.status === 'stale' ? 'stale' : 'rejected' }; }))
        .forEach(function (o) {
          var diff = arr(o.x.diff);
          if (!diff.length) return;
          var key = [o.x.field, o.x.kind, diff.join(' | ')].join(' / ');
          if (!byKey[key]) { byKey[key] = { diff: diff, field: o.x.field, kind: o.x.kind, cells: {} }; rows.push(byKey[key]); }
          var champ = o.outcome === 'committed' ? scoreOf(o.x.heldout_champion) : o.x.heldout_champion_mean;
          var cand = o.outcome === 'committed' ? scoreOf(o.x.heldout_candidate) : o.x.heldout_candidate_mean;
          (byKey[key].cells[ci] = byKey[key].cells[ci] || []).push({ outcome: o.outcome, champ: champ, cand: cand, decision: o.x.decision, id: o.x.id });
        });
    });
    if (!rows.length) return '';
    var refusedRow = cols.some(function (r) { return isNum(r.refused); });
    return '<section class="section-gap same-change"><h3 class="block-title">Same proposed change, each run</h3>' +
      '<div class="table-wrap"><table class="table same-table"><thead><tr><th scope="col">Proposed change</th>' +
      cols.map(function (r) {
        return '<th scope="col">' + esc(shortRunName(r)) + (has(r.proposer_model) || has(r.proposer) ? '<span class="score-note">proposer ' + esc(r.proposer_model || r.proposer) + '</span>' : '') + '</th>';
      }).join('') + '</tr></thead><tbody>' +
      rows.map(function (row) {
        return '<tr><th scope="row"><span class="muted small" title="' + esc([row.kind, row.field].filter(has).join(':')) + '">' + esc(surfaceWord(row.kind, row.field)) + '</span>' +
          '<ul class="diff">' + row.diff.map(function (l) { return '<li title="' + esc('as recorded: ' + l) + '">' + esc(plainChange(l)) + '</li>'; }).join('') + '</ul></th>' +
          cols.map(function (r, ci) {
            var cell = row.cells[ci];
            if (!cell) return '<td class="muted small">not proposed</td>';
            return '<td>' + cell.map(function (c) {
              return '<div class="same-cell">' + statusBadge(c.outcome) +
                (moveText(c.champ, c.cand) ? ' <span class="small" title="' + esc('held-out mean_accuracy ' + fmtNum(c.champ, 3) + ' -> ' + fmtNum(c.cand, 3)) + '">' + esc(moveText(c.champ, c.cand)) + '</span>' : '') +
                (c.outcome !== 'committed' && reasonText(c.decision) ? '<p class="same-why small">' + esc(humanize(reasonText(c.decision))) + '</p>' : '') +
                (has(c.decision) ? '<p class="exact-line" title="exact decision, as recorded">' + esc(textOf(c.decision)) + '</p>' : '') +
              '</div>';
            }).join('') + '</td>';
          }).join('') + '</tr>';
      }).join('') +
      (refusedRow ? '<tr><th scope="row"><span class="score-measure">Change how the system is graded</span><span class="score-note" title="tier X: frozen surfaces">never allowed; refused by the test gate</span></th>' +
        cols.map(function (r) {
          return '<td>' + (isNum(r.refused) ? (r.refused ? statusBadge('refused') + ' <span class="small">' + esc(r.refused === 1 ? 'once' : r.refused + ' times') + '</span>' : '<span class="muted small">none</span>') : '–') + '</td>';
        }).join('') + '</tr>' : '') +
      '</tbody></table></div>' +
      '<p class="muted small">Questions answered correctly on unseen test meetings: current version → proposed version.</p></section>';
  }

  function runCard(r, band) {
    var ch = isObj(r.chain) ? r.chain : {};
    var committed = arr(r.committed), rejected = arr(r.rejected);
    var refusedN = isNum(r.refused) ? r.refused : Array.isArray(r.refused) ? r.refused.length : null;
    var pbs = isObj(r.proposals_by_status) ? r.proposals_by_status : {};
    var nCommitted = committed.length || (isNum(pbs.committed) ? pbs.committed : 0);
    var nRejected = rejected.length || (isNum(pbs.rejected) ? pbs.rejected : 0);
    var models = arr(r.models);
    var span = has(r.first_at) ? localClock(r.first_at) + (has(r.last_at) ? '–' + localClock(r.last_at) : '') : '';
    var tags = [];
    if (r.stage) tags.push('<span class="badge badge-accent" title="stage: true">on stage</span>');
    if (r.replayable === false) tags.push('<span class="badge badge-neutral" title="replayable: false">not replayable</span>');
    if (r.replayable === true) tags.push('<span class="badge badge-positive" title="replayable: true">replayable</span>');
    var who = [];
    if (has(r.proposer_model) || has(r.proposer)) who.push('<span class="run-who"><span class="muted">Proposer</span> <span class="key-chip">' + esc(r.proposer_model || r.proposer) + '</span></span>');
    if (models.length) who.push('<span class="run-who"><span class="muted">Writer</span> ' + models.map(function (m) { return '<span class="key-chip" title="briefs.model">' + esc(m) + '</span>'; }).join(' ') + '</span>');
    var html = '<article class="card run-card' + (r.stage ? ' is-stage' : '') + '">' +
      '<header class="run-head">' +
        '<h3 class="run-title">' + esc(runName(r)) + ' <span class="muted mono small">' + esc(r.db || '') + '</span></h3>' +
        (tags.length ? '<p class="run-tags">' + tags.join(' ') + '</p>' : '') +
        (has(r.note) ? '<p class="small muted">' + esc(textOf(r.note)) + '</p>' : '') +
        '<p class="run-models">' + (who.length ? who.join('') : '<span class="muted small">models not recorded</span>') + '</p>' +
        '<p class="run-chain small">' +
          (ch.links_ok === true ? '<span class="badge badge-positive" title="chain.links_ok">audit log intact</span>' : ch.links_ok === false ? '<span class="badge badge-danger">audit log broken</span>' : '<span class="badge badge-neutral">audit log not checked</span>') +
          ' <span class="muted">' + esc([isNum(ch.entries) ? plural(ch.entries, 'entry', 'entries') : '', span].filter(has).join(' · ')) + '</span>' +
        '</p>' +
      '</header>' +
      '<dl class="run-counts">' +
        '<div class="run-count"><dt title="committed">Adopted</dt><dd>' + nCommitted + '</dd></div>' +
        '<div class="run-count"><dt>Rejected</dt><dd>' + nRejected + '</dd></div>' +
        '<div class="run-count"><dt>Refused</dt><dd>' + (refusedN === null ? '–' : refusedN) + '</dd></div>' +
      '</dl>';

    html += '<h4 class="sub-title">Adopted changes</h4>';
    if (!committed.length) html += '<p class="muted small">Nothing adopted in this run.</p>';
    committed.forEach(function (c) {
      var champ = scoreOf(c.heldout_champion), cand = scoreOf(c.heldout_candidate);
      html += '<div class="run-change">' +
        '<p class="run-change-head"><span class="mono small">' + esc(c.id || '') + '</span> <span class="muted small" title="' + esc([c.kind, c.field].filter(has).join(':')) + '">' + esc(surfaceWord(c.kind, c.field)) + '</span></p>' +
        (arr(c.diff).length ? diffHtml(c.diff) : '') +
        '<div class="bars">' + bandBarRow('Current version', champ, 'base', band) + bandBarRow('Proposed version', cand, 'focus', band) + '</div>' +
        (isNum(champ) && isNum(cand) ? '<p class="bar-foot">Questions answered correctly on unseen test meetings: ' + moveText(champ, cand) + ' (' + (cand - champ >= 0 ? '+' : '−') + Math.round(Math.abs(cand - champ) * 100) + ' points)</p>' : '') +
        decisionBlock({ status: 'committed', champ: champ, cand: cand, decision: c.decision }) +
      '</div>';
    });

    html += '<h4 class="sub-title">Rejected</h4>';
    html += rejected.length ? '<ul class="run-list">' + rejected.map(function (x) {
      return '<li><span class="mono small">' + esc(x.id || '') + '</span> <span class="muted small">' + esc(surfaceWord(x.kind, x.field)) + '</span>' +
        (arr(x.diff).length ? '<p class="small" title="' + esc('as recorded: ' + arr(x.diff).join('; ')) + '">' + esc(arr(x.diff).map(plainChange).join('; ')) + '</p>' : '') +
        decisionBlock({ status: x.status || 'rejected', tier: x.tier, champ: x.heldout_champion_mean, cand: x.heldout_candidate_mean, decision: x.decision }) + '</li>';
    }).join('') + '</ul>' : '<p class="muted small">Nothing rejected in this run.</p>';

    if (refusedN !== null) {
      html += '<p class="small run-refused"><span class="badge badge-locked">' + refusedN + ' refused</span> ' +
        '<span class="muted" title="tier X: frozen surfaces">attempts to change how the system is graded</span></p>';
    }
    return html + '</article>';
  }

  // ---------------------------------------------------------------- proposals

  function scoreOf(v) {
    if (isNum(v)) return v;
    if (!isObj(v)) return null;
    var keys = ['mean_accuracy', 'accuracy', 'score', 'value', 'mean'];
    for (var i = 0; i < keys.length; i++) if (isNum(v[keys[i]])) return v[keys[i]];
    return null;
  }
  function evalNote(v) {
    if (!isObj(v)) return '';
    var bits = [];
    if (isNum(v.n_scenarios)) bits.push(plural(v.n_scenarios, 'meeting'));
    if (isNum(v.k)) bits.push(plural(v.k, 'run') + ' each');
    return bits.join(', ');
  }

  function heldoutHtml(p) {
    var cand = scoreOf(p.heldout_candidate), champ = scoreOf(p.heldout_champion);
    if (cand === null && champ === null) {
      var why = String(p.tier || '').toUpperCase() === 'X'
        ? 'Never tested: it would change how the system is graded, so the test gate refused it outright.'
        : 'Not tested on unseen test meetings yet.';
      return '<p class="muted">' + esc(why) + '</p>';
    }
    var delta = cand !== null && champ !== null ? cand - champ : null;
    return '<div class="bars">' +
      barRow('Current version', champ, 1, 'base', champ === null ? 'no result' : pct(champ)) +
      barRow('Proposed version', cand, 1, 'focus', cand === null ? 'no result' : pct(cand)) +
    '</div>' +
    '<p class="bar-foot" title="held-out mean_accuracy: champion ' + esc(fmtNum(champ, 3)) + ', candidate ' + esc(fmtNum(cand, 3)) + '">Questions answered correctly' +
      (delta !== null ? ' · proposed version ' + (delta >= 0 ? '+' : '−') + Math.round(Math.abs(delta) * 100) + ' points' : '') +
      (evalNote(p.heldout_candidate) ? ' · ' + esc(evalNote(p.heldout_candidate)) : '') + '</p>' +
      heldoutMetrics(p.heldout_champion, p.heldout_candidate);
  }

  var GATE_METRICS = ['worst_accuracy', 'missed_changes', 'false_alarms', 'guardrail_violations', 'stale_claims', 'context_tokens'];
  function heldoutMetrics(champ, cand) {
    if (!isObj(champ) && !isObj(cand)) return '';
    var rows = GATE_METRICS.filter(function (m) {
      return (isObj(champ) && isNum(champ[m])) || (isObj(cand) && isNum(cand[m]));
    });
    if (!rows.length) return '';
    function cell(s, k) {
      var v = isObj(s) ? s[k] : null;
      if (!isNum(v)) return '–';
      if (k === 'context_tokens') return fmtNum(Math.round(v));
      if (k === 'worst_accuracy') return pct(v);
      return v.toFixed(2);
    }
    return '<table class="table table-compact metrics-table"><caption class="visually-hidden">What else the test gate checks</caption>' +
      '<thead><tr><th scope="col">What else the test gate checks</th><th scope="col" class="num">Current</th><th scope="col" class="num">Proposed</th></tr></thead><tbody>' +
      rows.map(function (m) {
        return '<tr><th scope="row" title="' + esc(m) + '">' + esc(humanize(METRIC_WORDS[m] || m)) + '</th><td class="num">' + esc(cell(champ, m)) + '</td><td class="num">' + esc(cell(cand, m)) + '</td></tr>';
      }).join('') + '</tbody></table>';
  }

  function diffHtml(diff) {
    var lines = arr(diff);
    if (!lines.length) return '<p class="muted">No change recorded.</p>';
    return '<ul class="diff">' + lines.map(function (l) {
      var raw = typeof l === 'string' ? l : json(l);
      var plain = plainChange(raw);
      var cls = /^\+/.test(raw) ? ' diff-add' : /^-(?!>)/.test(raw) ? ' diff-del' : '';
      return '<li class="' + cls.trim() + '" title="' + esc('as recorded: ' + raw) + '">' + esc(plain) + '</li>';
    }).join('') + '</ul>';
  }

  function proposalCard(p) {
    var id = p.id != null ? String(p.id) : '';
    var target = [p.kind, p.key].filter(has).join(':');
    var ev = arr(p.evidence);
    return '<article class="card proposal" id="proposal-' + esc(id) + '" tabindex="-1">' +
      '<header class="card-head">' +
        '<div class="card-titles">' +
          '<h3 class="card-title mono" title="proposal id">' + esc(id || 'proposed change') + '</h3>' +
          '<p class="card-sub" title="' + esc(target) + '">' + esc(surfaceWord(p.kind, p.key || p.field)) +
            (has(p.base_version) ? ' · from version ' + esc(p.base_version) : '') +
            (has(p.committed_version) ? ' → <strong>version ' + esc(p.committed_version) + '</strong>' : '') + '</p>' +
        '</div>' +
        '<div class="card-badges">' + tierBadge(p.tier) + statusBadge(p.status) + '</div>' +
      '</header>' +
      '<p class="card-meta muted">Proposed by ' + esc(actorWord(p.filed_by)) + ' · simulated date ' + esc(fmtSim(p.created_sim)) + '</p>' +
      '<h4 class="sub-title">The change</h4>' + diffHtml(p.diff) +
      '<h4 class="sub-title">Why, in the proposer’s words</h4>' + (has(p.rationale) ? '<p>' + esc(p.rationale) + '</p>' : '<p class="muted">No reason given.</p>') +
      '<h4 class="sub-title" title="held-out: candidate vs champion">Unseen test meetings: current vs proposed version</h4>' + heldoutHtml(p) +
      '<h4 class="sub-title">Test gate decision</h4>' +
        decisionBlock({ status: p.status, tier: p.tier, champ: p.heldout_champion, cand: p.heldout_candidate, decision: p.decision }) +
      '<div class="card-more">' +
        (ev.length ? '<details><summary title="evidence">What it cites (' + ev.length + ')</summary><p class="chip-wrap">' +
          ev.map(function (x) { return '<span class="fact-chip">' + esc(typeof x === 'string' ? x : json(x)) + '</span>'; }).join(' ') + '</p></details>' : '') +
        (p.tuning ? '<details><summary title="tuning result">Result on practice meetings' + (isNum(scoreOf(p.tuning)) ? ' (' + pct(scoreOf(p.tuning)) + ' correct)' : '') + '</summary>' + jsonBlock(p.tuning, 'Practice meetings result') + '</details>' : '') +
        (p.body !== undefined && p.body !== null ? '<details><summary title="proposed body">The proposed settings, as recorded</summary>' + jsonBlock(p.body, 'Proposed settings') + '</details>' : '') +
      '</div>' +
    '</article>';
  }

  function showProposals(opts) {
    runTab('#proposals-body', 'the proposed changes', function () { return need('proposals', ['proposals'], null, false); }, function (data) {
      var list = Array.isArray(data) ? data : arr(data && (data.items || data.proposals));
      var body = $('#proposals-body');
      var title = 'Proposed changes and the test gate';
      if (!list.length) {
        body.innerHTML = sectionHead(title, '') +
          emptyState('No proposed changes yet', 'Opus proposes a change after advisor feedback arrives; the test gate then tries it on unseen test meetings.');
        return;
      }
      var by = {};
      list.forEach(function (p) { var s = p.status || 'unknown'; by[s] = (by[s] || 0) + 1; });
      var summary = plural(list.length, 'proposed change') + ': ' + Object.keys(by).map(function (s) { return by[s] + ' ' + statusWord(s); }).join(', ');
      body.innerHTML = sectionHead(title, esc(summary)) +
        '<p class="tier-key"><span class="muted">Kinds of change:</span> ' + tierBadge('G') + ' ' + tierBadge('H') + ' ' + tierBadge('X') + '</p>' +
        '<div class="proposal-grid">' + list.map(proposalCard).join('') + '</div>';
    }, opts);
  }

  function sectionHead(title, lede) {
    return '<div class="section-head"><h2 class="section-title">' + esc(title) + '</h2>' +
      (lede ? '<p class="section-lede">' + lede + '</p>' : '') + '</div>';
  }

  // ---------------------------------------------------------------- versions

  var KIND_ORDER = ['policy', 'rules', 'tools', 'guardrails'];
  function orderIndex(list, v) { var i = list.indexOf(v); return i < 0 ? list.length : i; }

  function showVersions(opts) {
    runTab('#versions-body', 'the versions', function () { return need('versions', ['versions'], null, false); }, function (data) {
      var heads = arr(data && data.heads), versions = arr(data && data.versions);
      var body = $('#versions-body');
      var html = sectionHead('Versions of the settings', 'What the writer runs on now, and every change the test gate or a person adopted.');

      // Heads as a kind × key grid.
      if (!heads.length) {
        html += emptyState('No settings yet', 'Setting up the system creates version 1 of each setting.');
      } else {
        var kinds = [], keys = [], cell = {};
        heads.forEach(function (h) {
          var kind = h.kind || String(h.id || '').split(':')[0];
          var key = h.key || String(h.id || '').split(':')[1] || '';
          if (kinds.indexOf(kind) < 0) kinds.push(kind);
          if (keys.indexOf(key) < 0) keys.push(key);
          cell[kind + '\u0000' + key] = h;
        });
        kinds.sort(function (a, b) { return orderIndex(KIND_ORDER, a) - orderIndex(KIND_ORDER, b) || (a < b ? -1 : 1); });
        keys.sort(function (a, b) { return (a === 'global') - (b === 'global') || (a < b ? -1 : a > b ? 1 : 0); });
        html += '<h3 class="block-title" title="config heads">In use now</h3>' +
          '<div class="table-wrap table-wrap-fit"><table class="table heads-table"><thead><tr><th scope="col">Setting</th>' +
          keys.map(function (k) { return '<th scope="col" title="' + esc(k) + '">' + esc(fieldWord(k)) + '</th>'; }).join('') + '</tr></thead><tbody>' +
          kinds.map(function (kind) {
            return '<tr><th scope="row" title="' + esc(kind) + '">' + esc(humanize(kindWord(kind))) + '</th>' + keys.map(function (k) {
              var h = cell[kind + '\u0000' + k];
              if (!h) return '<td class="muted" aria-label="none">–</td>';
              var v = h.version;
              return '<td><button type="button" class="head-cell' + (isNum(v) && v > 1 ? ' is-changed' : '') + '" data-version-filter="' + esc(kind + ':' + k) + '" title="Show the history of ' + esc(kind + ':' + k) + '">v' + esc(v) + '</button></td>';
            }).join('') + '</tr>';
          }).join('') + '</tbody></table></div>' +
          '<p class="muted small">Bold: changed since version 1. Pick a cell to see its history.</p>';
      }

      // History.
      var ids = [];
      versions.forEach(function (v) { var k = (v.kind || '') + ':' + (v.key || ''); if (ids.indexOf(k) < 0) ids.push(k); });
      ids.sort();
      var sel = state.versionFilter && ids.indexOf(state.versionFilter) >= 0 ? state.versionFilter : '';
      var shown = versions.filter(function (v) { return !sel || (v.kind + ':' + v.key) === sel; }).slice().sort(function (a, b) {
        var ta = String(a.created_at || ''), tb = String(b.created_at || '');
        if (ta !== tb) return ta < tb ? 1 : -1;
        return (b.version || 0) - (a.version || 0);
      });
      html += '<div class="block-head"><h3 class="block-title">History</h3>' +
        '<label class="inline-field">Setting <select class="select" id="version-filter"><option value="">All settings</option>' +
        ids.map(function (k) { var kk = k.split(':'); return '<option value="' + esc(k) + '"' + (k === sel ? ' selected' : '') + '>' + esc(surfaceWord(kk[0], kk[1])) + '</option>'; }).join('') +
        '</select></label><span class="muted small">' + shown.length + ' of ' + plural(versions.length, 'version') + '</span></div>';
      var byId = {};
      versions.forEach(function (v) { byId[(v.kind || '') + ':' + (v.key || '') + '@' + v.version] = v; });
      var isSeed = function (v) { return v.version === 1 && !has(v.proposal_id) && !has(v.supersedes); };
      var changes = shown.filter(function (v) { return !isSeed(v); });
      var seeds = shown.filter(isSeed);
      if (!versions.length) {
        html += emptyState('No versions recorded', '');
      } else {
        html += changes.length
          ? '<ol class="version-list">' + changes.map(function (v) {
              var prev = has(v.supersedes) ? byId[(v.kind || '') + ':' + (v.key || '') + '@' + v.supersedes] : null;
              return versionRow(v, prev);
            }).join('') + '</ol>'
          : '<p class="muted">No changes since the first version yet.</p>';
        if (seeds.length) {
          html += '<details class="seed-list"><summary title="seeded v1">First versions of ' + plural(seeds.length, 'setting') + '</summary><ul class="plain-list">' +
            seeds.map(function (v) {
              return '<li id="version-' + esc(v.id || '') + '" tabindex="-1"><span title="' + esc((v.kind || '') + ':' + (v.key || '')) + '">' + esc(surfaceWord(v.kind, v.key)) + '</span> version 1 ' +
                '<span class="muted small">simulated date ' + esc(fmtSim(v.created_sim)) + '</span>' +
                (v.body !== undefined ? '<details><summary title="body">The settings, as recorded</summary>' + jsonBlock(v.body, 'Settings ' + (v.id || '')) + '</details>' : '') + '</li>';
            }).join('') + '</ul></details>';
        }
      }
      body.innerHTML = html;
      var fsel = $('#version-filter');
      if (fsel) fsel.addEventListener('change', function () { state.versionFilter = fsel.value; showVersions({ quiet: true }); });
      $$('[data-version-filter]', body).forEach(function (b) {
        b.addEventListener('click', function () {
          state.versionFilter = b.getAttribute('data-version-filter');
          showVersions({ quiet: true });
          var s = $('#version-filter'); if (s) s.focus();
        });
      });
    }, opts);
  }

  function compact(v) {
    if (v === undefined) return '–';
    var s = typeof v === 'string' ? v : JSON.stringify(v);
    return s.length > 140 ? s.slice(0, 137) + '…' : s;
  }

  function versionRow(v, prev) {
    var id = v.id != null ? String(v.id) : (v.kind + ':' + v.key + '@v' + v.version);
    var ck = v.changed_keys;
    var changed;
    if (Array.isArray(ck) && ck.length) {
      var canShow = prev && isObj(prev.body) && isObj(v.body);
      changed = canShow
        ? '<ul class="change-list">' + ck.map(function (k) {
            return '<li><span class="key-chip" title="' + esc(k) + '">' + esc(SETTING_WORDS[k] || humanize(k).toLowerCase()) + '</span> <span class="small" title="' + esc(compact(prev.body[k]) + ' -> ' + compact(v.body[k])) + '">' + esc(plainValue(k, prev.body[k])) +
              ' <span aria-hidden="true">→</span><span class="visually-hidden"> becomes </span> ' + esc(plainValue(k, v.body[k])) + '</span></li>';
          }).join('') + '</ul>'
        : ck.map(function (k) { return '<span class="key-chip" title="' + esc(k) + '">' + esc(SETTING_WORDS[k] || humanize(k).toLowerCase()) + '</span>'; }).join(' ');
    }
    else if (Array.isArray(ck)) changed = '<span class="muted">' + (v.version === 1 || !has(v.supersedes) ? 'first version' : 'nothing changed') + '</span>';
    else changed = '<span class="muted">' + (v.version === 1 ? 'first version' : 'not compared') + '</span>';
    var who = v.approved_by || (v.proposal_id ? 'unknown' : 'seed');
    var whoText = who === 'gate' ? 'adopted by the test gate (code)' : /^owner:/.test(who) ? 'signed off by ' + who.slice(6) : who === 'seed' ? 'first version' : who;
    return '<li class="version-item card" id="version-' + esc(id) + '" tabindex="-1">' +
      '<div class="version-head">' +
        '<span class="version-num">v' + esc(v.version) + '</span>' +
        '<span class="version-id" title="' + esc((v.kind || '') + ':' + (v.key || '')) + '">' + esc(surfaceWord(v.kind, v.key)) + '</span>' +
        '<span class="badge badge-neutral" title="' + esc('approved_by: ' + who) + '">' + esc(whoText) + '</span>' +
        (has(v.restores) ? '<span class="badge badge-warning" title="restores">brings back version ' + esc(v.restores) + '</span>' : '') +
        '<span class="version-when muted">simulated date ' + esc(fmtSim(v.created_sim)) + '</span>' +
      '</div>' +
      '<div class="version-changed"><span class="muted" title="changed_keys">What changed:</span> ' + changed + '</div>' +
      (has(v.rationale) ? '<p class="version-rationale">' + esc(v.rationale) + '</p>' : '') +
      '<p class="version-links muted small">' +
        (has(v.proposal_id) ? 'From ' + refLabel({ type: 'proposal', id: v.proposal_id }) : 'First version, not a proposed change') +
        (has(v.supersedes) ? ' · replaces version ' + esc(v.supersedes) : '') +
        ' · recorded ' + esc(fmtStamp(v.created_at)) +
      '</p>' +
      (v.body !== undefined ? '<details><summary title="body">The settings, as recorded</summary>' + jsonBlock(v.body, 'Settings ' + id) + '</details>' : '') +
    '</li>';
  }

  // ---------------------------------------------------------------- briefs

  var SECTION_ORDER = ['what_changed', 'why_it_matters', 'likely_questions', 'talking_points', 'watch_outs'];

  function showBriefs(opts) {
    runTab('#briefs-body', 'the briefs', function () {
      var world = need('world', ['world'], null, false).catch(function () { return null; });   // optional: feedback per brief
      return Promise.all([need('briefs', ['briefs'], { limit: 20 }, false), world]).then(function (r) { return r[0]; });
    }, function (data) {
      var list = Array.isArray(data) ? data : arr(data && (data.items || data.briefs));
      var body = $('#briefs-body');
      if (!list.length) {
        body.innerHTML = sectionHead('Briefs', '') + emptyState('No briefs yet', 'Sonnet (the writer) writes a brief for each review meeting; they appear here newest first.');
        return;
      }
      if (!state.briefSel || !list.some(function (b) { return String(b.id) === String(state.briefSel); })) state.briefSel = list[0].id;
      var cur = list.filter(function (b) { return String(b.id) === String(state.briefSel); })[0];
      body.innerHTML = sectionHead('Briefs', plural(list.length, 'brief') + ', newest first. Pick one to read it with its receipt: what went into it.') +
        '<div class="split">' +
          '<nav class="pick-list" aria-label="Briefs"><ul>' + list.map(function (b) {
            var on = String(b.id) === String(state.briefSel);
            var r = isObj(b.receipt) ? b.receipt : {};
            var vers = isObj(r.versions) && has(r.versions.policy) ? 'what goes into the brief v' + r.versions.policy : '';
            return '<li><button type="button" class="pick' + (on ? ' is-on' : '') + '" data-brief="' + esc(b.id) + '"' + (on ? ' aria-current="true"' : '') + '>' +
              '<span class="pick-title">' + esc(b.account_id || 'account') + '</span>' +
              '<span class="pick-sub">' + esc(['as of ' + fmtSim(b.as_of), has(b.field) ? fieldWord(b.field) : ''].filter(has).join(' · ')) + '</span>' +
              '<span class="pick-sub" title="' + esc('seq ' + (b.seq != null ? b.seq : '') + ' · policy v · config_label ' + (b.config_label || '')) + '">' + esc([has(b.seq) ? '#' + b.seq : '', vers, configWord(b.config_label)].filter(has).join(' · ')) + '</span>' +
            '</button></li>';
          }).join('') + '</ul></nav>' +
          '<div class="split-main" id="brief-detail" tabindex="-1"></div>' +
        '</div>';
      renderBriefDetail(cur);
      $$('[data-brief]', body).forEach(function (b) {
        b.addEventListener('click', function () {
          state.briefSel = b.getAttribute('data-brief');
          $$('[data-brief]', body).forEach(function (x) {
            var on = x === b;
            x.classList.toggle('is-on', on);
            if (on) x.setAttribute('aria-current', 'true'); else x.removeAttribute('aria-current');
          });
          renderBriefDetail(list.filter(function (x) { return String(x.id) === String(state.briefSel); })[0]);
        });
      });
    }, opts);
  }

  function renderBriefDetail(b) {
    var box = $('#brief-detail');
    if (!box) return;
    box.innerHTML = briefDetail(b);
    $$('[data-brief-view]', box).forEach(function (btn) {
      btn.addEventListener('click', function () {
        state.briefView = btn.getAttribute('data-brief-view');
        renderBriefDetail(b);
        var again = $('[data-brief-view="' + state.briefView + '"]', box);
        if (again) again.focus();
      });
    });
  }

  function briefDetail(b) {
    if (!b) return emptyState('No brief selected', '');
    var r = isObj(b.receipt) ? b.receipt : null;
    var html = '<header class="brief-head">' +
      '<h3 class="block-title">' + esc(b.account_id || 'Brief') + '</h3>' +
      '<p class="muted">' + esc([has(b.field) ? fieldWord(b.field) : '', 'as of ' + fmtSim(b.as_of), b.model ? 'written by ' + b.model : '', configWord(b.config_label)].filter(has).join(' · ')) + '</p>' +
      '<p class="muted small mono">' + esc(b.id || '') + '</p>' +
    '</header>';
    var view = state.briefView === 'claims' ? 'claims' : 'rendered';
    html += '<div class="brief-grid">';
    html += '<section class="card brief-doc" aria-label="Brief">' +
      '<div class="seg" role="group" aria-label="Brief view">' +
        '<button type="button" class="seg-btn" data-brief-view="rendered" aria-pressed="' + (view === 'rendered') + '" title="rendered markdown">The brief</button>' +
        '<button type="button" class="seg-btn" data-brief-view="claims" aria-pressed="' + (view === 'claims') + '" title="claims with fact-id citations">Claims and their sources</button>' +
      '</div>' +
      (view === 'rendered'
        ? (has(b.markdown) ? '<div class="md">' + renderMarkdown(b.markdown) + '</div>' : emptyState('No text on this brief', ''))
        : claimsHtml(b.sections)) +
    '</section>';
    html += '<div class="brief-aside">';

    // Receipt.
    html += '<section class="card pad"><h4 class="sub-title" title="receipt">Receipt: what went into it</h4>';
    if (!r) {
      html += '<p class="muted">No receipt recorded.</p>';
    } else {
      var versions = isObj(r.versions) ? Object.keys(r.versions).map(function (k) { return kindWord(k) + ' v' + r.versions[k]; }).join(', ') : '';
      var facts = arr(r.fact_ids);
      html += '<dl class="kv kv-tight">' +
        (versions ? '<div><dt title="config versions">Settings used</dt><dd>' + esc(versions) + '</dd></div>' : '') +
        (has(r.config_hash) ? '<div><dt title="config_hash">Settings fingerprint</dt><dd class="mono" title="' + esc(r.config_hash) + '">' + esc(shortHash(r.config_hash)) + '</dd></div>' : '') +
        (Array.isArray(r.fact_ids) ? '<div><dt title="facts in context">Facts given to the writer</dt><dd>' + facts.length + '</dd></div>' : '') +
        (isNum(r.excluded_superseded) ? '<div><dt title="excluded_superseded">Out-of-date facts left out</dt><dd>' + r.excluded_superseded + '</dd></div>' : '') +
        (isNum(r.context_tokens) ? '<div><dt title="context_tokens">Brief size</dt><dd title="tokens">' + fmtNum(r.context_tokens) + '</dd></div>' : '') +
        (has(r.as_of) ? '<div><dt>As of</dt><dd>' + esc(fmtSim(r.as_of)) + '</dd></div>' : '') +
      '</dl>' +
      (facts.length ? '<details><summary title="fact ids in context">The facts given to the writer (' + facts.length + ')</summary><p class="chip-wrap">' + factChips(facts) + '</p></details>' : '') +
      '<details><summary>The full receipt, as recorded</summary>' + jsonBlock(r, 'Receipt') + '</details>';
    }
    html += '</section>';

    // Dropped claims.
    var dc = b.dropped_claims;
    html += '<section class="card pad"><h4 class="sub-title" title="dropped_claims">Claims dropped</h4>';
    if (isNum(dc)) {
      html += '<p><strong class="big-num">' + dc + '</strong> ' + (dc === 1 ? 'claim' : 'claims') + ' dropped because they cited no fact the writer was given.</p>';
    } else if (Array.isArray(dc)) {
      html += dc.length ? '<ul class="plain-list">' + dc.map(function (c) {
        return '<li>' + esc(isObj(c) ? (c.text || c.claim || json(c)) : c) + (isObj(c) && has(c.reason) ? ' <span class="muted">(' + esc(c.reason) + ')</span>' : '') + '</li>';
      }).join('') + '</ul>' : '<p><strong class="big-num">0</strong> claims dropped because they cited no fact the writer was given.</p>';
    } else {
      html += '<p class="muted">Not recorded on this brief.</p>';
    }
    html += '</section>';

    // Feedback linked to this brief.
    var w = cached('world');
    var fb = w && isObj(w) ? arr(w.feedback).filter(function (f) { return f.brief_id != null && String(f.brief_id) === String(b.id); }) : [];
    if (fb.length) {
      html += '<section class="card pad"><h4 class="sub-title">Advisor feedback on this brief</h4><ul class="plain-list">' +
        fb.map(function (f) { return '<li>' + esc(f.text) + ' <span class="muted small">simulated date ' + esc(fmtSim(f.sim_time)) + '</span></li>'; }).join('') + '</ul></section>';
    }
    html += '</div></div>';
    return html;
  }

  function claimsHtml(sections) {
    if (!sections || (isObj(sections) && !Object.keys(sections).length) || (Array.isArray(sections) && !sections.length)) {
      return '<p class="muted">No separate claims recorded on this brief.</p>';
    }
    var groups = [];
    if (Array.isArray(sections)) {
      sections.forEach(function (s, i) { groups.push({ name: s.title || s.name || ('Section ' + (i + 1)), claims: arr(s.claims || s.items) }); });
    } else {
      var names = Object.keys(sections).sort(function (a, b) { return orderIndex(SECTION_ORDER, a) - orderIndex(SECTION_ORDER, b); });
      names.forEach(function (n) { groups.push({ name: humanize(n), claims: arr(sections[n]) }); });
    }
    return '<div class="claims-grid">' + groups.map(function (g) {
      return '<div class="claims-group"><h5 class="claims-title">' + esc(g.name) + ' <span class="muted small">' + g.claims.length + '</span></h5>' +
        (g.claims.length ? '<ul class="claim-list">' + g.claims.map(function (c) {
          var text = isObj(c) ? (c.text || c.claim || '') : String(c);
          var ids = isObj(c) ? (c.fact_ids || c.facts || c.citations || []) : [];
          return '<li><p class="claim-text">' + esc(text) + '</p><p class="chip-wrap">' + factChips(ids) + '</p></li>';
        }).join('') + '</ul>' : '<p class="muted small">No claims.</p>') +
      '</div>';
    }).join('') + '</div>';
  }

  // ---------------------------------------------------------------- findings

  function summaryOf(x) {
    if (!isObj(x)) return null;
    return isObj(x.summary) ? x.summary : x;
  }
  function parseFrac(s) {
    if (isObj(s) && isNum(s.faulty) && isNum(s.preps)) return { a: s.faulty, b: s.preps };
    var m = /^\s*(\d+)\s*\/\s*(\d+)\s*$/.exec(String(s == null ? '' : s));
    return m ? { a: Number(m[1]), b: Number(m[2]) } : null;
  }

  // The cabinet scoreboard has three writers, none of them an AI model: the data teammate's scripted assistant (written
  // with deliberately careless habits), and Pregame's own code with its fixes off and on.
  var SOURCE_DEFS = [
    { key: 'baseline', label: 'Scripted assistant', series: 'base', note: 'a script with deliberately careless habits, not an AI model; no harness' },
    { key: 'naive', label: 'Fixes off', series: 'naive', note: 'Pregame’s code with every fix switched off' },
    { key: 'harness', label: 'Fixes on', series: 'focus', note: 'Pregame’s code with its fixes switched on' }
  ];
  // Said beside the scoreboard. Agreed wording with the build's page and README (commit 20322a8, 26 Sep 15:26).
  var CABINET_CAVEAT = 'How to read this: the first column is the data teammate’s scripted assistant, written with ' +
    'deliberately careless habits; it is not an AI model. The other two columns are Pregame’s own code with its fixes ' +
    'off and on, designed from the same fault list and scored on the same 24 preps. When Claude Sonnet 5 wrote these ' +
    '24 preps (below), it made no mistakes with or without the harness. On this small book the harness adds guarantees ' +
    '(compliance wording inserted by code, every fact dated and sourced) and focus, not accuracy.';
  // Claude Sonnet 5 writing the same 24 preps from the raw records, 2 runs each way. Read from the database
  // pregame_cabinet_live (cabinet_runs CR-20260926T192359458982Z to ...985Z; recording cassettes/cabinet-live.jsonl).
  var MODEL_CHECK = {
    runs: ['Run 1', 'Run 2'],
    rows: [
      { label: 'Preps with a mistake', note: 'out of 24, by the answer key', without: ['0 of 24', '0 of 24'], with: ['0 of 24', '0 of 24'] },
      { label: 'Forbidden promises', note: 'wording compliance never allows', without: ['0', '0'], with: ['0', '0'] },
      { label: 'Expected actions taken', note: 'questions to ask, bad notes to flag, briefing both holders', without: ['6 of 10', '7 of 10'], with: ['7 of 10', '8 of 10'] },
      { label: 'Unneeded questions', note: 'questions the file did not call for', without: ['27', '26'], with: ['2', '3'] }
    ]
  };
  function modelCheckHtml() {
    var head = '<tr><th scope="col">Measure</th>' +
      MODEL_CHECK.runs.map(function (r) { return '<th scope="col" class="num">Without harness, ' + esc(r.toLowerCase()) + '</th>'; }).join('') +
      MODEL_CHECK.runs.map(function (r) { return '<th scope="col" class="num">With harness, ' + esc(r.toLowerCase()) + '</th>'; }).join('') + '</tr>';
    var body = MODEL_CHECK.rows.map(function (row) {
      return '<tr><th scope="row"><span class="score-measure">' + esc(row.label) + '</span><span class="score-note">' + esc(row.note) + '</span></th>' +
        row.without.concat(row.with).map(function (v) { return '<td class="num"><strong>' + esc(v) + '</strong></td>'; }).join('') + '</tr>';
    }).join('');
    return '<section class="card pad section-gap"><div class="block-head"><h3 class="block-title">Claude Sonnet 5 writing the same 24 preps</h3>' +
      '<span class="muted small">2 runs each, from the raw records</span></div>' +
      '<div class="table-wrap"><table class="table scoreboard"><thead>' + head + '</thead><tbody>' + body + '</tbody></table></div>' +
      '<p class="muted small">A strong model alone made none of the scripted assistant’s mistakes on this small book. With the ' +
      'harness it stayed focused (2 or 3 unneeded questions instead of 26 or 27), largely because the harness’s code chooses ' +
      'the questions. The other differences are within the noise of 2 runs. The harness’s value on this data is its ' +
      'guarantees: compliance wording inserted by code, and every fact dated and sourced.</p></section>';
  }

  // findings.cabinet_runs: the pregame database's cabinet_runs documents, newest first. A run document carries
  // `scores` {baseline, naive, harness} (scorer summaries) and the `policy` it ran with; older shapes (one document per
  // policy) are read by their policy name.
  function cabinetRuns(data, cab) {
    var cr = data && data.cabinet_runs !== undefined ? data.cabinet_runs : cab.cabinet_runs;
    var list = Array.isArray(cr) ? cr : isObj(cr) ? [cr] : [];
    var out = {}, meta = null;
    list.forEach(function (doc) {
      if (!isObj(doc)) return;
      if (isObj(doc.scores)) {
        if (!meta) {
          meta = doc;
          Object.keys(doc.scores).forEach(function (k) { if (isObj(doc.scores[k])) out[k.toLowerCase()] = doc.scores[k]; });
        }
        return;
      }
      var name = String(isObj(doc.policy) ? doc.policy.name : doc.policy || doc.label || doc.name || '').toLowerCase();
      if (name && !out[name]) out[name] = doc;
    });
    return { present: list.length > 0, runs: out, meta: meta };
  }

  // The settings the scored cabinet run used, for the "what the harness does" note.
  function cabinetRunNote(meta) {
    if (!isObj(meta)) return '';
    var pol = isObj(meta.policy) ? meta.policy : null;
    var bits = [];
    if (has(meta.id)) bits.push('<span class="mono" title="cabinet run id">' + esc(meta.id) + '</span>');
    if (has(meta.created_at)) bits.push(esc(fmtStamp(meta.created_at)));
    if (isNum(meta.prep_count)) bits.push(esc(plural(meta.prep_count, 'prep')));
    var settings = pol ? Object.keys(pol).filter(function (k) { return k !== 'name'; }).map(function (k) {
      return '<li><span class="mono">' + esc(k) + '</span> ' + esc(textOf(pol[k])) + '</li>';
    }).join('') : '';
    return '<p class="muted small">Scored run: ' + bits.join(' · ') + '</p>' +
      (settings ? '<details class="small"><summary>The harness settings used in this run</summary><ul class="plain-list">' + settings + '</ul></details>' : '');
  }

  function findingSources(data) {
    var cab = isObj(data && data.cabinet) ? data.cabinet : {};
    var cr = cabinetRuns(data, cab);
    var runs = cr.runs;
    var harnessRun = runs.harness || null;
    return {
      cab: cab,
      meta: cr.meta,
      runsPresent: cr.present && Object.keys(runs).length > 0,
      list: SOURCE_DEFS.map(function (def) {
        var raw = def.key === 'baseline' ? (cab.baseline || runs.baseline) : def.key === 'naive' ? runs.naive : (harnessRun || cab.harness);
        var s = summaryOf(raw);
        var byClient = def.key === 'baseline' ? (isObj(cab.baseline_faults_by_client) ? cab.baseline_faults_by_client : null)
          : def.key === 'harness' && !harnessRun && isObj(cab.harness_faults_by_client) ? cab.harness_faults_by_client : null;
        if (!byClient && s && isObj(s.faulty_preps_by_client)) byClient = s.faulty_preps_by_client;
        var preps = def.key === 'baseline' ? arr(cab.baseline_preps)
          : def.key === 'harness' && !harnessRun ? arr(cab.harness_preps)
          : isObj(raw) ? arr(raw.preps_rows || raw.prep_rows || raw.preps_list) : [];
        return { key: def.key, label: def.label, series: def.series, note: def.note, s: s, raw: raw, byClient: byClient || {}, preps: preps };
      })
    };
  }

  function outOf(a, b) { return isNum(a) && isNum(b) ? a + ' of ' + b : ''; }

  // Scoreboard rows: each returns {big, small} for one summary, or null when the scorer did not report it.
  var SCORE_ROWS = [
    ['Trusted prep rate', 'Preps with no fault at all (strict)', function (s) {
      var n = s.preps, f = s.preps_with_a_fault;
      var rate = isNum(s.trusted_prep_rate) ? s.trusted_prep_rate : (isNum(n) && isNum(f) && n ? (n - f) / n : null);
      if (!isNum(rate)) return null;
      return { big: fmtPct(rate), small: isNum(n) && isNum(f) ? outOf(n - f, n) + ' preps' : '' };
    }],
    ['Trusted, ignoring wording warnings', 'Severity-1 compliance wording counted as a warning, not a fault', function (s) {
      var n = s.preps, f = s.preps_with_a_fault_ignoring_warnings;
      var rate = isNum(s.trusted_prep_rate_ignoring_warnings) ? s.trusted_prep_rate_ignoring_warnings : (isNum(n) && isNum(f) && n ? (n - f) / n : null);
      if (!isNum(rate)) return null;
      return { big: fmtPct(rate), small: isNum(n) && isNum(f) ? outOf(n - f, n) + ' preps' : '' };
    }],
    ['Preps with a fault', 'At least one fault from the answer key', function (s) {
      if (!isNum(s.preps_with_a_fault)) return null;
      return {
        big: outOf(s.preps_with_a_fault, s.preps) || String(s.preps_with_a_fault),
        small: (isNum(s.faults_total) ? plural(s.faults_total, 'fault') + ' in all' : '') +
          (isNum(s.warnings_total) && isNum(s.faults_total) ? ', ' + s.warnings_total + ' of them wording warnings' : '')
      };
    }],
    ['Forbidden promises', 'Promises compliance never allows', function (s) {
      if (!isNum(s.forbidden_promises)) return null;
      return { big: String(s.forbidden_promises), small: isNum(s.preps) ? 'in ' + s.preps + ' preps' : '' };
    }],
    ['Question recall', 'Questions the call needed that the prep asked', function (s) {
      var e = s.questions_expected, h = s.questions_hit;
      if (isNum(e) && e === 0) return { big: '–', small: 'no questions expected' };
      var r = isNum(s.question_recall) ? s.question_recall : (isNum(e) && isNum(h) && e ? h / e : null);
      if (!isNum(r)) return null;
      return { big: fmtPct(r), small: isNum(e) && isNum(h) ? outOf(h, e) + ' expected' : '' };
    }],
    ['Expected actions taken', 'Ask, flag or brief both, where the answer key expects it', function (s) {
      var e = s.expected_actions, h = s.actions_hit;
      if (isNum(e) && e === 0) return { big: '–', small: 'no actions expected' };
      var r = isNum(s.action_recall) ? s.action_recall : (isNum(e) && isNum(h) && e ? h / e : null);
      if (!isNum(r)) return null;
      return { big: fmtPct(r), small: isNum(e) && isNum(h) ? outOf(h, e) + ' expected' : '' };
    }]
  ];

  var ACTION_TEXT = {
    ask: ['Ask', 'check a changed fact with the client'],
    flag: ['Flag', 'a “No changes” note that conflicts with activity'],
    brief_both: ['Brief both', 'brief both holders of a joint account']
  };

  function showFindings(opts) {
    runTab('#findings-body', 'findings', function () {
      var names = need('cabinet_clients', ['cabinet', 'clients'], null, false).catch(function () { return null; });   // optional: client names
      return Promise.all([need('findings', ['findings'], null, false), names]).then(function (r) { return r[0]; });
    }, renderFindings, opts);
  }

  function renderFindings(data) {
    var body = $('#findings-body');
    var src = findingSources(data);
    var cab = src.cab;
    var sources = src.list;
    var scored = sources.filter(function (x) { return x.s; });
    var base = sources[0].s;
    var html = sectionHead('Findings', 'The banker’s client files, scored against the data teammate’s answer key: his scripted assistant (not an AI model), Pregame’s code with its fixes off and on, and Claude Sonnet 5 with and without the harness.');

    var errs = arr(data && data.errors);
    if (errs.length) {
      html += '<div class="notice notice-warning" role="status"><p class="notice-title">Some findings could not be read</p><ul class="plain-list">' +
        errs.map(function (e) { return '<li>' + esc(typeof e === 'string' ? e : json(e)) + '</li>'; }).join('') + '</ul></div>';
    }

    // Scoreboard: one row per measure, one column per writer.
    html += '<div class="block-head"><h3 class="block-title">Cabinet scoreboard</h3>' +
      (has(cab.baseline_source) ? '<span class="muted small">Baseline: ' + esc(cab.baseline_source) + '</span>' : '') + '</div>';
    if (!scored.length) {
      html += emptyState('No cabinet scores yet', 'Copy the scorer output to <code>data/cabinet_baseline.json</code> in the viewer folder.');
    } else {
      var missing = sources.filter(function (x) { return !x.s; });
      if (missing.length) {
        html += '<div class="notice notice-info"><p class="notice-title">' + esc(missing.map(function (x) { return x.label; }).join(' and ')) + ': not run yet</p>' +
          '<p class="notice-text">Their columns fill in once the naive bot and the harness have written and scored their preps for this run (<code>cabinet_runs</code>). Switch the Run to one that has them, or wait for the next run.</p></div>';
      }
      html += cabinetRunNote(src.meta) +
        (sources.some(function (x) { return x.key === 'harness' && x.s; }) ?
          '<div class="notice notice-info"><p class="notice-text">' + esc(CABINET_CAVEAT) + '</p></div>' : '') +
        '<div class="table-wrap"><table class="table scoreboard"><thead><tr><th scope="col">Measure</th>' +
        sources.map(function (x) {
          return '<th scope="col" class="score-col is-' + x.key + '"><span class="legend-swatch series-' + x.series + '" aria-hidden="true"></span> ' + esc(x.label) +
            '<span class="score-note">' + esc(x.note) + '</span></th>';
        }).join('') + '</tr></thead><tbody>' +
        SCORE_ROWS.map(function (row, ri) {
          return '<tr><th scope="row"><span class="score-measure">' + esc(row[0]) + '</span><span class="score-note">' + esc(row[1]) + '</span></th>' +
            sources.map(function (x) {
              if (!x.s) {
                return ri === 0 ? '<td class="score-none" rowspan="' + SCORE_ROWS.length + '">Not run yet</td>' : '';
              }
              var v = row[2](x.s);
              if (!v) return '<td class="score-cell is-' + x.key + '"><span class="muted">not reported</span></td>';
              return '<td class="score-cell is-' + x.key + '"><span class="score-big">' + esc(v.big) + '</span>' +
                (v.small ? '<span class="score-small">' + esc(v.small) + '</span>' : '') + '</td>';
            }).join('') + '</tr>';
        }).join('') + '</tbody></table></div>';
    }

    // Expected actions by type.
    var types = [];
    scored.forEach(function (x) {
      Object.keys(isObj(x.s.expected_actions_by_type) ? x.s.expected_actions_by_type : {}).forEach(function (t) { if (types.indexOf(t) < 0) types.push(t); });
    });
    if (types.length) {
      types.sort(function (a, b) { return orderIndex(['ask', 'flag', 'brief_both'], a) - orderIndex(['ask', 'flag', 'brief_both'], b); });
      html += '<section class="section-gap"><h3 class="block-title">Expected actions by type</h3>' +
        '<div class="table-wrap table-wrap-fit"><table class="table"><thead><tr><th scope="col">Action</th>' +
        sources.map(function (x) { return '<th scope="col" class="num">' + esc(x.label) + '</th>'; }).join('') + '</tr></thead><tbody>' +
        types.map(function (t) {
          var txt = ACTION_TEXT[t] || [humanize(t), ''];
          return '<tr><th scope="row"><span class="score-measure">' + esc(txt[0]) + '</span>' + (txt[1] ? '<span class="score-note">' + esc(txt[1]) + '</span>' : '') + '</th>' +
            sources.map(function (x) {
              if (!x.s) return '<td class="num muted">not run yet</td>';
              var exp = isObj(x.s.expected_actions_by_type) ? x.s.expected_actions_by_type[t] : undefined;
              var hit = isObj(x.s.actions_hit_by_type) ? x.s.actions_hit_by_type[t] : undefined;
              if (!isNum(exp)) return '<td class="num muted">–</td>';
              return '<td class="num"><strong>' + esc(outOf(isNum(hit) ? hit : 0, exp)) + '</strong></td>';
            }).join('') + '</tr>';
        }).join('') + '</tbody></table></div></section>';
    }

    // What an AI model does on the same preps, with and without the harness.
    html += modelCheckHtml();

    // Faults by type: one bar per scored writer.
    var ft = {};
    scored.forEach(function (x) { ft[x.key] = isObj(x.s.faults_by_type) ? x.s.faults_by_type : {}; });
    var ftypes = [];
    scored.forEach(function (x) { Object.keys(ft[x.key]).forEach(function (t) { if (ftypes.indexOf(t) < 0) ftypes.push(t); }); });
    if (ftypes.length) {
      var bft = ft.baseline || {};
      ftypes.sort(function (a, b) { return (bft[b] || 0) - (bft[a] || 0) || (a < b ? -1 : 1); });
      var max = 0;
      scored.forEach(function (x) { ftypes.forEach(function (t) { max = Math.max(max, ft[x.key][t] || 0); }); });
      html += '<section class="card pad section-gap"><div class="block-head"><h3 class="block-title">Faults by type</h3>' +
        scored.map(function (x) {
          return isNum(x.s.faults_total) ? '<span class="muted small">' + esc(x.label) + ': ' + x.s.faults_total + ' faults in ' + (isNum(x.s.preps) ? x.s.preps + ' preps' : 'all preps') + '</span>' : '';
        }).join('') + '</div>' +
        (scored.length > 1 ? legend(scored.map(function (x) { return { series: x.series, label: x.label }; })) : '<p class="muted small">Naive and harness bars appear once they are run.</p>') +
        '<div class="pairs">' + ftypes.map(function (t) {
          return '<div class="pair"><p class="pair-label">' + esc(humanize(t)) + '</p><div class="pair-bars">' +
            scored.map(function (x) { return barRow(x.label, isNum(ft[x.key][t]) ? ft[x.key][t] : 0, max, x.series); }).join('') +
          '</div></div>';
        }).join('') + '</div>';
      var sev = base && isObj(base.compliance_drift_by_severity) ? base.compliance_drift_by_severity : null;
      if (sev && Object.keys(sev).length) {
        html += '<p class="muted small">Baseline compliance drift by severity (1 is a wording warning, 4 a forbidden promise): ' +
          Object.keys(sev).sort().map(function (k) { return 'level ' + esc(k) + ': ' + esc(sev[k]); }).join(' · ') + '</p>';
      }
      html += '</section>';
    }

    // Per client: preps with a fault for each writer, and the baseline's preps with their fault types.
    var cids = [];
    sources.forEach(function (x) {
      Object.keys(x.byClient).forEach(function (c) { if (cids.indexOf(c) < 0) cids.push(c); });
      x.preps.forEach(function (p) { if (p.client_id && cids.indexOf(p.client_id) < 0) cids.push(p.client_id); });
    });
    cids.sort();
    if (cids.length) {
      var clients = cached('cabinet_clients');
      var names = {};
      arr(Array.isArray(clients) ? clients : clients && clients.clients).forEach(function (c) { names[c.client_id] = c.name; });
      var prepList = function (preps, cid) {
        var mine = preps.filter(function (p) { return p.client_id === cid; });
        if (!mine.length) return '';
        return '<ul class="prep-cells">' + mine.map(function (p) {
          var fts = arr(p.fault_types);
          return '<li class="prep-cell' + (fts.length ? ' has-fault' : '') + '">' +
            '<span class="mono small">' + esc(p.prep_id || '') + '</span>' +
            '<span class="muted small">' + esc(p.date || '') + '</span>' +
            '<span class="prep-cell-faults">' + (fts.length ? esc(countTypes(fts)) : '<span class="muted">no fault</span>') + '</span>' +
          '</li>';
        }).join('') + '</ul>';
      };
      html += '<section class="section-gap"><h3 class="block-title">Preps with a fault, per client</h3><div class="client-grid">' +
        cids.map(function (cid) {
          return '<div class="card client-tile"><p class="client-tile-name">' + esc(names[cid] || cid) + ' <span class="muted mono small">' + esc(cid) + '</span></p>' +
            sources.map(function (x) {
              var f = parseFrac(x.byClient[cid]);
              var line = '<p class="client-tile-line"><span class="tile-who">' + esc(x.label) + '</span> ' +
                (!x.s ? '<span class="muted">not run yet</span>'
                  : f ? '<strong>' + f.a + ' of ' + f.b + '</strong> preps with a fault'
                  : '<span class="muted">' + esc(x.byClient[cid] != null ? x.byClient[cid] : 'not reported') + '</span>') + '</p>';
              return line + (x.s ? prepList(x.preps, cid) : '');
            }).join('') +
          '</div>';
        }).join('') + '</div></section>';
    }

    // Research table.
    var rs = data && data.research;
    html += '<section class="section-gap"><h3 class="block-title">' + esc(isObj(rs) && rs.title ? rs.title : 'Research: static bot vs time-aware design') + '</h3>';
    if (!isObj(rs) || !arr(rs.rows).length) {
      html += emptyState('No research findings loaded', 'Add <code>data/research_findings.json</code> to the viewer folder.');
    } else {
      if (rs.note) html += '<p class="section-lede">' + esc(rs.note) + '</p>';
      html += '<div class="table-wrap"><table class="table research-table"><thead><tr>' +
        '<th scope="col">Failure</th><th scope="col">Static bot</th><th scope="col">Time-aware design</th><th scope="col">Caveat</th><th scope="col">Source</th>' +
        '</tr></thead><tbody>' + arr(rs.rows).map(function (r) {
          return '<tr><th scope="row">' + esc(r.failure) + '</th>' +
            '<td>' + (has(r.static) ? esc(r.static) : '<span class="muted">Not reported</span>') + '</td>' +
            '<td>' + (has(r.designed) ? esc(r.designed) : '<span class="muted">No design result reported</span>') + '</td>' +
            '<td class="muted">' + (has(r.caveat) ? esc(r.caveat) : '–') + '</td>' +
            '<td>' + (has(r.url) && /^https?:\/\//.test(r.url) ? '<a href="' + esc(r.url) + '" target="_blank" rel="noopener noreferrer">' + esc(hostOf(r.url)) + '<span class="visually-hidden"> (opens in a new tab)</span></a>' : '<span class="muted">–</span>') + '</td>' +
          '</tr>';
        }).join('') + '</tbody></table></div>';
      if (rs.self_improvement) html += '<p class="callout">' + esc(rs.self_improvement) + '</p>';
      var srcNote = (rs.note ? '' : 'Published benchmarks, not Pregame’s own numbers. ') +
        (has(rs.source) ? 'Compiled in ' + esc(String(rs.source).split(/[\\/]/).pop()) + '.' : '');
      if (srcNote) html += '<p class="muted small">' + srcNote + '</p>';
    }
    html += '</section>';

    // Pregame eval rows: held-out by default, all runs on request.
    var rows = arr(data && data.pregame_eval);
    var splitOf = function (r) { return r.split || (isObj(r.summary) ? r.summary.split : '') || ''; };
    var heldRows = rows.filter(function (r) { return splitOf(r) === 'heldout'; });
    var evalView = state.evalView === 'all' ? 'all' : 'heldout';
    var shownRows = evalView === 'all' ? rows : heldRows;
    html += '<section class="section-gap"><div class="block-head"><h3 class="block-title" title="Pregame eval runs (eval_runs summary rows)">Pregame test runs</h3>' +
      (rows.length ? '<div class="seg seg-inline" role="group" aria-label="Which test runs">' +
        '<button type="button" class="seg-btn" data-eval-view="heldout" aria-pressed="' + (evalView === 'heldout') + '" title="split = heldout">Unseen test meetings (' + heldRows.length + ')</button>' +
        '<button type="button" class="seg-btn" data-eval-view="all" aria-pressed="' + (evalView === 'all') + '">All test runs (' + rows.length + ')</button>' +
      '</div>' : '') + '</div>';
    if (!rows.length) {
      html += data && has(data.pregame_eval_error)
        ? emptyState('Test runs unavailable', 'The database could not be read: ' + esc(textOf(data.pregame_eval_error)))
        : emptyState('No test runs yet', 'The test gate records a row each time it tests a version.');
    } else if (!shownRows.length) {
      html += emptyState('No runs on unseen test meetings yet', 'Only practice meetings so far. The test gate tries a proposed version on unseen test meetings before it adopts anything.');
    } else {
      var two = function (v) { return isNum(v) ? v.toFixed(2) : '–'; };
      html += '<div class="table-wrap"><table class="table table-compact eval-table"><thead><tr>' +
        '<th scope="col" title="field">Client group</th><th scope="col" title="split">Meetings</th><th scope="col" title="config_label">Version</th>' +
        '<th scope="col" class="num" title="k">Runs each</th><th scope="col" class="num" title="n_scenarios">Meetings tested</th>' +
        '<th scope="col" class="num" title="mean_accuracy">Questions answered correctly</th><th scope="col" class="num" title="worst_accuracy">Worst single meeting</th>' +
        '<th scope="col" class="num" title="pass_k">Meetings passed every run</th><th scope="col" class="num" title="stale_claims">Out-of-date claims</th>' +
        '<th scope="col" class="num" title="uncited_claims">Claims without a source</th><th scope="col">Recorded</th>' +
        '</tr></thead><tbody>' + shownRows.map(function (r) {
          var s = isObj(r.summary) ? r.summary : {};
          var k = has(r.k) ? r.k : s.k;
          var split = splitOf(r);
          var cfg = r.config_label || s.config_label || '';
          return '<tr><td>' + esc(fieldWord(r.field || s.field) || '–') + '</td>' +
            '<td class="nowrap" title="' + esc(split) + '">' + (split === 'heldout' ? '<strong>' + esc(splitWord(split)) + '</strong>' : esc(split ? splitWord(split) : '–')) + '</td>' +
            '<td class="nowrap" title="' + esc(cfg) + '">' + esc(configWord(cfg) || '–') + '</td>' +
            '<td class="num">' + esc(has(k) ? k : '–') + '</td>' +
            '<td class="num">' + esc(fmtNum(s.n_scenarios)) + '</td>' +
            '<td class="num" title="' + esc(two(s.mean_accuracy)) + '"><strong>' + esc(pct(s.mean_accuracy)) + '</strong></td>' +
            '<td class="num" title="' + esc(two(s.worst_accuracy)) + '">' + esc(pct(s.worst_accuracy)) + '</td>' +
            '<td class="num" title="' + esc(two(s.pass_k)) + '">' + esc(pct(s.pass_k)) + '</td>' +
            '<td class="num">' + esc(two(s.stale_claims)) + '</td>' +
            '<td class="num">' + esc(two(s.uncited_claims)) + '</td>' +
            '<td class="mono small nowrap" title="' + esc(fmtStamp(r.created_at)) + '">' + esc(shortStamp(r.created_at)) + '</td></tr>';
        }).join('') + '</tbody></table></div>' +
        '<p class="muted small">Questions answered correctly: the share of the client’s questions a reader could answer from the brief alone, averaged over the runs. Out-of-date claims and claims without a source are averages per run.</p>';
    }
    html += '</section>';
    body.innerHTML = html;
    $$('[data-eval-view]', body).forEach(function (b) {
      b.addEventListener('click', function () {
        state.evalView = b.getAttribute('data-eval-view');
        renderFindings(data);
        var again = $('[data-eval-view="' + state.evalView + '"]', body);
        if (again) again.focus();
      });
    });
  }

  function countTypes(types) {
    var order = [], n = {};
    types.forEach(function (t) { if (!n[t]) { n[t] = 0; order.push(t); } n[t] += 1; });
    return order.map(function (t) { return humanize(t).toLowerCase() + (n[t] > 1 ? ' ×' + n[t] : ''); }).join(', ');
  }

  function hostOf(url) {
    try { return new URL(url).hostname.replace(/^www\./, ''); } catch (e) { return 'link'; }
  }

  // ---------------------------------------------------------------- cabinet

  // /api/cabinet/clients is a list (or {clients, vocabulary}); the vocabulary may also come from
  // /api/cabinet/vocabulary. Both are optional extras: without them the page simply shows no meanings.
  function clientsOf(d) { return arr(Array.isArray(d) ? d : d && d.clients); }
  function vocabOf(d) {
    if (isObj(d) && isObj(d.vocabulary)) return d.vocabulary;
    return isObj(d) && !Array.isArray(d) && !('clients' in d) ? d : null;
  }

  function showCabinet(opts) {
    runTab('#cabinet-body', 'the cabinet', function () {
      return need('cabinet_clients', ['cabinet', 'clients'], null, false).then(function (cl) {
        if (isObj(cl) && isObj(cl.vocabulary)) return { clients: clientsOf(cl), vocab: cl.vocabulary };
        return need('cabinet_vocabulary', ['cabinet', 'vocabulary'], null, false).then(function (v) {
          return { clients: clientsOf(cl), vocab: vocabOf(v) };
        }, function () { return { clients: clientsOf(cl), vocab: null }; });
      });
    }, function (r) {
      state.cabinet.vocab = r.vocab;
      renderCabinetShell(r.clients);
    }, opts);
  }

  function vocabMeaning(attr, value) {
    var v = state.cabinet.vocab;
    if (!v || !has(attr)) return '';
    var a = v[attr];
    if (isObj(a) && has(value) && typeof a[value] === 'string') return a[value];
    return '';
  }
  // A value with its plain meaning on hover (dotted underline when a meaning exists).
  function vocabTerm(attr, value) {
    var s = has(value) ? String(value) : '–';
    var m = vocabMeaning(attr, value);
    return m ? '<span class="vocab-term" title="' + esc(m) + '">' + esc(s) + '</span>' : esc(s);
  }
  function attrText(attr) { return humanize(attr).toLowerCase(); }

  function vocabLegend() {
    var v = state.cabinet.vocab;
    if (!v || !Object.keys(v).length) return '';
    var rows = Object.keys(v).filter(function (k) { return k.charAt(0) !== '_'; }).map(function (k) {
      var a = v[k];
      var vals = isObj(a)
        ? '<ul class="vocab-values">' + Object.keys(a).map(function (val) {
            return '<li><span class="mono">' + esc(val) + '</span> <span class="muted">' + esc(a[val]) + '</span></li>';
          }).join('') + '</ul>'
        : '<p class="muted">' + esc(textOf(a)) + '</p>';
      return '<div class="vocab-attr"><dt>' + esc(attrText(k)) + '</dt><dd>' + vals + '</dd></div>';
    }).join('');
    var rules = isObj(v._rules) ? Object.keys(v._rules).map(function (k) {
      return '<li><strong>' + esc(attrText(k)) + ':</strong> ' + esc(textOf(v._rules[k])) + '</li>';
    }).join('') : '';
    return '<details class="vocab-legend"><summary>What the labels mean</summary>' +
      '<dl class="vocab-list">' + rows + '</dl>' + (rules ? '<ul class="plain-list small">' + rules + '</ul>' : '') + '</details>';
  }

  function renderCabinetShell(clients) {
    var body = $('#cabinet-body');
    var cs = state.cabinet;
    if (!clients.length) {
      body.innerHTML = sectionHead('Cabinet (demo data)', '') + emptyState('No clients in the cabinet', 'Load the synthetic banker data into the <code>cabinet</code> database.');
      return;
    }
    if (!cs.selected || !clients.some(function (c) { return c.client_id === cs.selected; })) cs.selected = clients[0].client_id;
    body.innerHTML = sectionHead('Cabinet (demo data)', 'A synthetic banker’s book: notes, call preps and the client feed. The answer key marks what each prep got wrong and what it should have done.') +
      '<div class="cabinet-bar">' +
        '<label class="switch"><input type="checkbox" role="switch" id="answer-key"' + (cs.answerKey ? ' checked' : '') + '>' +
          '<span class="switch-track" aria-hidden="true"></span><span>Show answer key <span class="muted">(presenter)</span></span></label>' +
        vocabLegend() +
      '</div>' +
      '<div class="split">' +
        '<nav class="pick-list" aria-label="Clients"><ul>' + clients.map(function (c) {
          var on = c.client_id === cs.selected;
          var n = c.counts || {};
          var bits = [];
          if (isNum(n.notes)) bits.push(plural(n.notes, 'note'));
          if (isNum(n.preps)) bits.push(plural(n.preps, 'prep'));
          if (isNum(n.events)) bits.push(plural(n.events, 'event'));
          return '<li><button type="button" class="pick' + (on ? ' is-on' : '') + '" data-client="' + esc(c.client_id) + '"' + (on ? ' aria-current="true"' : '') + '>' +
            '<span class="pick-title">' + esc(c.name || c.client_id) + '</span>' +
            '<span class="pick-sub">' + esc([c.client_id, has(c.tier) ? 'tier ' + c.tier : '', has(c.age) ? 'age ' + c.age : ''].filter(has).join(' · ')) + '</span>' +
            '<span class="pick-sub">' + esc(bits.join(' · ')) + '</span>' +
            (cs.answerKey && isNum(n.faults) ? '<span class="pick-faults">' + plural(n.faults, 'fault') + '</span>' : '') +
          '</button></li>';
        }).join('') + '</ul></nav>' +
        '<div class="split-main" id="client-detail" tabindex="-1"></div>' +
      '</div>';
    $$('[data-client]', body).forEach(function (b) {
      b.addEventListener('click', function () {
        cs.selected = b.getAttribute('data-client');
        $$('[data-client]', body).forEach(function (x) {
          var on = x === b;
          x.classList.toggle('is-on', on);
          if (on) x.setAttribute('aria-current', 'true'); else x.removeAttribute('aria-current');
        });
        loadClient();
      });
    });
    $('#answer-key').addEventListener('change', function (ev) {
      cs.answerKey = ev.target.checked;
      renderCabinetShell(clients);
      var t = $('#answer-key'); if (t) t.focus();
    });
    loadClient();
  }

  function loadClient() {
    var cs = state.cabinet;
    var cid = cs.selected;
    var db = state.pdb;
    var box = $('#client-detail');
    if (!box || !cid) return;
    var faults = cs.answerKey ? 1 : 0;
    var key = 'client:' + cid + ':' + (FIXTURES ? 1 : faults);
    if (cached(key) === undefined) box.innerHTML = loadingState('client ' + cid);
    need(key, ['cabinet', 'client', cid], { faults: faults }, false).then(function (d) {
      if (state.cabinet.selected !== cid || db !== state.pdb || !document.getElementById('client-detail')) return;
      renderClient(d);
      afterRender();
    }, function (e) {
      if (state.cabinet.selected !== cid || db !== state.pdb) return;
      var b = $('#client-detail');
      if (!b) return;
      b.innerHTML = '';
      b.appendChild(errorPanel(e, 'client ' + cid, loadClient));
    });
  }

  function isHarnessPrep(it) { return it && (it.kind === 'harness_prep' || it.kind === 'prep_harness'); }
  function isWarning(f) { return f && String(f.level || '').toLowerCase() === 'warning'; }

  function renderClient(d) {
    var cs = state.cabinet;
    var box = $('#client-detail');
    var c = isObj(d && d.client) ? d.client : {};
    var tl = arr(d && d.timeline);
    // The public copy ships without the answer key: say so instead of showing empty fault badges.
    var keyMissing = cs.answerKey && d && d.answer_key_included === false;
    var showKey = cs.answerKey && !keyMissing;
    var idsInTimeline = {};
    tl.forEach(function (it) { if (has(it.id)) idsInTimeline[it.id] = it; });
    var feedAll = tl.filter(function (it) { return it.kind === 'feed'; });
    var feedNotable = feedAll.filter(function (it) { return it.notable; });
    var harnessPreps = tl.filter(isHarnessPrep);

    // Answer-key counts over the documents shown here (baseline notes and preps; harness preps counted apart).
    var faultCount = 0, warnCount = 0, expCount = 0;
    if (showKey) {
      tl.forEach(function (it) {
        if (isHarnessPrep(it)) return;
        arr(it.faults).forEach(function (f) { if (isWarning(f)) warnCount += 1; else faultCount += 1; });
        expCount += arr(it.expected).length;
      });
    }
    var listed = clientsOf(cached('cabinet_clients')).filter(function (x) { return x.client_id === (c.client_id || cs.selected); })[0];
    var totalFaults = isObj(c.counts) && isNum(c.counts.faults) ? c.counts.faults : (listed && isObj(listed.counts) && isNum(listed.counts.faults) ? listed.counts.faults : null);
    var outside = totalFaults !== null && totalFaults > faultCount + warnCount ? totalFaults - faultCount - warnCount : 0;

    var hh = arr(c.household).map(function (p) { return esc(p.name || '') + (p.role ? ' <span class="muted">(' + esc(p.role) + ')</span>' : ''); }).join(', ');
    var ac = arr(c.accounts).map(function (a) {
      return esc(a.type || a.account_id || 'account') + (arr(a.holders).length > 1 ? ' <span class="muted">(' + esc(arr(a.holders).join(' and ')) + ')</span>' : '') +
        (a.advisory ? ' <span class="badge badge-neutral">advisory</span>' : '');
    }).join('; ');
    var holdings = arr(c.holdings).map(function (h) {
      return esc(h.instrument || '') + (has(h.instrument_type) ? ' ' + instrumentChip(h.instrument_type) : '') +
        (isNum(h.amount) ? ' <span class="muted mono">' + esc(fmtNum(h.amount)) + '</span>' : '');
    }).join('; ');

    var html = '<header class="client-head">' +
      '<h3 class="block-title">' + esc(c.name || cs.selected) + ' <span class="muted mono small">' + esc(c.client_id || cs.selected) + '</span></h3>' +
      '<dl class="kv kv-inline">' +
        (has(c.age) ? '<div><dt>Age</dt><dd>' + esc(c.age) + '</dd></div>' : '') +
        (has(c.tier) ? '<div><dt>Tier</dt><dd>' + esc(c.tier) + '</dd></div>' : '') +
        (hh ? '<div><dt>Household</dt><dd>' + hh + '</dd></div>' : '') +
        (ac ? '<div><dt>Accounts</dt><dd>' + ac + '</dd></div>' : '') +
        (holdings ? '<div><dt>Holdings</dt><dd>' + holdings + '</dd></div>' : '') +
      '</dl></header>';

    html += '<div class="block-head timeline-head"><h4 class="block-title">Timeline</h4>' +
      '<label class="switch switch-small"><input type="checkbox" role="switch" id="show-all-feed"' + (cs.showAll ? ' checked' : '') + '>' +
        '<span class="switch-track" aria-hidden="true"></span><span>Show all feed events</span></label>' +
      '<span class="muted small">' + (cs.showAll ? 'All ' + feedAll.length + ' feed events shown' : feedNotable.length + ' notable of ' + feedAll.length + ' feed events shown') +
        (harnessPreps.length ? ' · ' + plural(harnessPreps.length, 'Pregame prep') + ' beside the originals' : '') + '</span>' +
      (showKey ? '<span class="badge badge-danger">' + plural(faultCount, 'fault') + '</span>' +
        (warnCount ? '<span class="badge badge-warning">' + plural(warnCount, 'wording warning') + '</span>' : '') +
        (expCount ? '<span class="badge badge-accent">' + plural(expCount, 'expected action') + '</span>' : '') +
        (outside ? '<span class="muted small">' + outside + ' more on documents outside this timeline, such as the book summary</span>' : '') : '') +
    '</div>' +
    (keyMissing ? '<div class="notice notice-info" role="status"><p class="notice-title">The answer key isn’t in this copy</p>' +
      '<p class="notice-text">This is the public snapshot, which leaves out the data seat’s answer key so nobody can wonder whether the harness saw it. ' +
      'Run the viewer online, with a database connection, to show each prep’s faults and the actions it should have taken.</p></div>' : '');

    // Harness preps sit next to the baseline prep of the same date; unmatched ones keep their own row.
    var paired = {};
    var rows = [];
    tl.forEach(function (it) {
      if (it.kind === 'prep') {
        var mates = harnessPreps.filter(function (h) {
          if (paired[h.id || h.date + h.policy]) return false;
          return (has(h.prep_id) && h.prep_id === it.id) || (has(h.baseline_prep_id) && h.baseline_prep_id === it.id) ||
            (!has(h.prep_id) && !has(h.baseline_prep_id) && h.date === it.date);
        });
        mates.forEach(function (h) { paired[h.id || h.date + h.policy] = true; });
        rows.push({ it: it, mates: mates });
      }
    });
    var visible = tl.filter(function (it) {
      if (isHarnessPrep(it)) return !paired[it.id || it.date + it.policy];
      return it.kind !== 'feed' || cs.showAll || it.notable;
    });
    var matesOf = {};
    rows.forEach(function (r) { matesOf[r.it.id] = r.mates; });

    if (!tl.length) {
      html += emptyState('No documents for this client', '');
    } else if (!visible.length) {
      html += emptyState('Only routine feed events', 'Turn on “Show all feed events” to see them.');
    } else {
      html += '<ol class="timeline">' + visible.map(function (it) {
        return timelineItem(it, showKey, idsInTimeline, it.kind === 'prep' ? matesOf[it.id] : null);
      }).join('') + '</ol>';
    }
    box.innerHTML = html;

    $('#show-all-feed').addEventListener('change', function (ev) {
      cs.showAll = ev.target.checked;
      renderClient(d);
      var t = $('#show-all-feed'); if (t) t.focus();
    });
    $$('[data-evidence]', box).forEach(function (b) {
      b.addEventListener('click', function () {
        var id = b.getAttribute('data-evidence');
        var target = document.getElementById('tl-' + id);
        if (!target && !cs.showAll && idsInTimeline[id]) {
          cs.showAll = true;
          state.pendingFocus = 'tl-' + id;
          renderClient(d);
          afterRender();
          return;
        }
        if (target) { state.pendingFocus = 'tl-' + id; afterRender(); }
      });
    });
  }

  var INSTRUMENT_TEXT = { single_stock: 'single stock', index_fund: 'index fund', fund: 'fund' };
  function instrumentChip(t) {
    var key = String(t);
    var text = INSTRUMENT_TEXT[key] || humanize(key).toLowerCase();
    return '<span class="inst-chip inst-' + esc(key.replace(/[^a-z_]/gi, '')) + '" title="Instrument type">' + esc(text) + '</span>';
  }

  function policyBadge(p) {
    if (!has(p)) return '';
    var up = String(p).toUpperCase();
    return '<span class="badge ' + (up === 'HARNESS' ? 'badge-accent' : 'badge-neutral') + ' policy-badge">' + esc(up) + '</span>';
  }

  // The answer-key overlay for one document: faults (warnings softer) and the actions the prep should have taken.
  function answerKeyHtml(it, showKey, ids, noExpected) {
    if (!showKey) return '';
    var faults = arr(it.faults), expected = noExpected ? [] : arr(it.expected);
    var out = '';
    if (faults.length) {
      out += '<div class="faults" aria-label="Answer key: faults in ' + esc(it.id || 'this document') + '">' +
        faults.map(function (f) { return faultHtml(f, ids); }).join('') + '</div>';
    }
    if (expected.length) {
      out += '<div class="expected" aria-label="Answer key: what this prep should do">' +
        expected.map(function (x) { return expectedHtml(x, ids); }).join('') + '</div>';
    }
    return out;
  }

  function expectedHtml(x, ids) {
    var action = String(x.action || '').toLowerCase();
    var text;
    if (action === 'ask') text = 'Should ask about ' + vocabAttr(x.attribute);
    else if (action === 'flag') text = 'Should flag ' + (has(x.note_id) ? idChip(x.note_id, ids) : 'the note') + ': “No changes” conflicts with activity';
    else if (action === 'brief_both') text = 'Should brief both holders';
    else text = esc(humanize(action || 'action')) + (has(x.attribute) ? ' · ' + vocabAttr(x.attribute) : '');
    var ev = arr(x.evidence);
    return '<div class="expect">' +
      '<p class="expect-head"><span class="badge badge-accent">expected</span> <span class="expect-text">' + text + '</span>' +
        (action === 'brief_both' && has(x.attribute) ? ' <span class="muted small">(' + esc(attrText(x.attribute)) + ')</span>' : '') + '</p>' +
      (has(x.reason) ? '<p class="expect-reason muted">' + esc(x.reason) + '</p>' : '') +
      (ev.length ? '<p class="fault-evidence"><span class="fault-label">Evidence</span> ' + ev.map(function (id) { return idChip(id, ids); }).join(' ') + '</p>' : '') +
    '</div>';
  }
  function vocabAttr(attr) {
    if (!has(attr)) return 'this';
    var v = state.cabinet.vocab && state.cabinet.vocab[attr];
    var tip = isObj(v) ? 'Possible values: ' + Object.keys(v).join(', ') : typeof v === 'string' ? v : '';
    return tip ? '<span class="vocab-term" title="' + esc(tip) + '">' + esc(attrText(attr)) + '</span>' : esc(attrText(attr));
  }

  function prepColumn(it, showKey, ids, label, noExpected) {
    var used = arr(it.used_note_ids);
    var harness = isHarnessPrep(it);
    return '<div class="prep-col' + (harness ? ' is-harness' : '') + '"' + (harness && has(it.id) ? ' id="tl-' + esc(it.id) + '" tabindex="-1"' : '') + '>' +
      '<p class="tl-head"><span class="tl-kind tl-kind-' + (harness ? 'harness' : 'prep') + '">' + esc(label) + '</span>' +
        (has(it.id) ? '<span class="mono small"' + (harness ? ' title="' + esc(it.id) + '"' : '') + '>' + esc(harness && has(it.prep_id) ? 'for ' + it.prep_id : it.id) + '</span>' : '') +
        (harness ? policyBadge(it.policy || it.policy_label || it.label) : '') +
        (harness && has(it.model) ? '<span class="muted small">' + esc(it.model) + '</span>' : '') + '</p>' +
      '<pre class="prep-block" tabindex="0" aria-label="' + esc(label + ' ' + (it.id || '')) + '">' + esc(it.text || it.markdown || '') + '</pre>' +
      (used.length ? '<p class="tl-used"><span class="fault-label">Built from</span> ' + used.map(function (id) { return idChip(id, ids); }).join(' ') + '</p>' : '') +
      answerKeyHtml(it, showKey, ids, noExpected) +
    '</div>';
  }

  function timelineItem(it, showKey, ids, mates) {
    var kind = String(it.kind || 'item');
    var faults = showKey ? arr(it.faults) : [];
    var onlyWarnings = faults.length && faults.every(isWarning);
    var cls = 'tl-item tl-' + esc(kind) + (faults.length ? (onlyWarnings ? ' has-warnings' : ' has-faults') : '') +
      (kind === 'feed' && !it.notable ? ' is-routine' : '');
    var idAttr = has(it.id) && !isHarnessPrep(it) ? ' id="tl-' + esc(it.id) + '" tabindex="-1"' : '';

    if (kind === 'prep' || isHarnessPrep(it)) {
      var compare = arr(mates).length > 0;
      var cols = [prepColumn(it, showKey, ids, isHarnessPrep(it) ? 'Pregame prep' : (compare ? 'original prep' : 'prep'), compare)];
      arr(mates).forEach(function (m) { cols.push(prepColumn(m, showKey, ids, 'Pregame prep', true)); });
      // Side by side, the expected actions apply to both versions of the prep: show them once, under both.
      var exp = compare && showKey ? arr(it.expected) : [];
      return '<li class="' + cls + (compare ? ' is-compare' : '') + '"' + idAttr + '>' +
        '<span class="tl-date mono">' + esc(it.date || '') + '</span>' +
        '<div class="tl-body">' + (compare ? '<div class="prep-compare">' + cols.join('') + '</div>' : cols[0]) +
          (exp.length ? '<div class="expected" aria-label="Answer key: what this prep should do"><p class="fault-label">What this prep should do (both versions)</p>' +
            exp.map(function (x) { return expectedHtml(x, ids); }).join('') + '</div>' : '') +
        '</div></li>';
    }

    var head = '<span class="tl-kind tl-kind-' + esc(kind) + '">' + esc(kind) + '</span>' +
      (has(it.id) ? '<span class="mono small">' + esc(it.id) + '</span>' : '') +
      (has(it.author) ? '<span class="muted small">by ' + esc(it.author) + '</span>' : '') +
      (kind === 'feed' && has(it.type) ? '<span class="field-tag">' + esc(humanize(it.type).toLowerCase()) + '</span>' : '') +
      (has(it.instrument_type) ? instrumentChip(it.instrument_type) : '');
    var content = kind === 'feed' ? '<p class="tl-feed mono">' + esc(it.text || '') + '</p>' : '<p class="tl-text">' + esc(it.text || '') + '</p>';
    return '<li class="' + cls + '"' + idAttr + '>' +
      '<span class="tl-date mono">' + esc(it.date || '') + '</span>' +
      '<div class="tl-body"><p class="tl-head">' + head + '</p>' + content + answerKeyHtml(it, showKey, ids) + '</div></li>';
  }

  function faultHtml(f, ids) {
    var ev = arr(f.evidence);
    var warn = isWarning(f);
    return '<div class="fault' + (warn ? ' is-warning' : '') + '">' +
      '<p class="fault-head"><span class="badge ' + (warn ? 'badge-warning' : 'badge-danger') + '">' +
          esc(humanize(f.fault_type || 'fault').toLowerCase()) + (warn ? ' · wording warning' : '') + '</span>' +
        (has(f.attribute) ? ' <span class="mono small">' + esc(f.attribute) + '</span>' : '') +
        (isNum(f.severity) ? ' <span class="muted small">severity ' + esc(f.severity) + '</span>' : '') +
        (has(f.fault_id) ? ' <span class="muted mono small">' + esc(f.fault_id) + '</span>' : '') + '</p>' +
      '<dl class="fault-grid">' +
        '<div><dt>Claimed</dt><dd>' + vocabTerm(f.attribute, f.claimed) + (has(f.claimed_text) && f.claimed_text !== f.claimed ? '<br><span class="muted">“' + esc(f.claimed_text) + '”</span>' : '') + '</dd></div>' +
        '<div><dt>Truth</dt><dd>' + vocabTerm(f.attribute, f.truth) + '</dd></div>' +
      '</dl>' +
      (has(f.fix) ? '<p class="fault-fix"><span class="fault-label">Fix</span> ' + esc(f.fix) + '</p>' : '') +
      (ev.length ? '<p class="fault-evidence"><span class="fault-label">Evidence</span> ' + ev.map(function (id) { return idChip(id, ids); }).join(' ') + '</p>' : '') +
    '</div>';
  }

  function idChip(id, ids) {
    return ids[id]
      ? '<button type="button" class="fact-chip is-link" data-evidence="' + esc(id) + '" title="Jump to ' + esc(id) + ' in the timeline">' + esc(id) + '</button>'
      : '<span class="fact-chip" title="Not in this client’s timeline">' + esc(id) + '</span>';
  }

  // ---------------------------------------------------------------- databases

  function normalizeDbs(d) {
    // Accepts {dbs:[...]}, {databases:[...]}, [...] or {name: {...}}; returns [{name, collections:[{name,count}]}].
    if (isObj(d) && (Array.isArray(d.dbs) || Array.isArray(d.databases))) return normalizeDbs(d.dbs || d.databases);
    if (isObj(d) && isObj(d.dbs)) return normalizeDbs(d.dbs);
    var out = [];
    function colls(c) {
      if (Array.isArray(c)) return c.map(function (x) {
        if (typeof x === 'string') return { name: x, count: null };
        return { name: x.name || x.collection || '?', count: isNum(x.count) ? x.count : isNum(x.documents) ? x.documents : null };
      });
      if (isObj(c)) return Object.keys(c).map(function (k) { return { name: k, count: isNum(c[k]) ? c[k] : (isObj(c[k]) && isNum(c[k].count) ? c[k].count : null) }; });
      return [];
    }
    if (Array.isArray(d)) {
      d.forEach(function (x) {
        if (typeof x === 'string') out.push({ name: x, collections: [], note: null });
        else if (isObj(x)) out.push({ name: x.name || x.db || '?', collections: colls(x.collections), note: x.error || x.note || null, role: x.role || null });
      });
    } else if (isObj(d)) {
      Object.keys(d).forEach(function (k) {
        var v = d[k];
        if (isObj(v) && ('collections' in v)) out.push({ name: k, collections: colls(v.collections), note: v.error || v.note || null, role: v.role || null });
        else out.push({ name: k, collections: colls(v), note: null });
      });
    }
    out.forEach(function (db) { db.collections.sort(function (a, b) { return a.name < b.name ? -1 : 1; }); });
    return out;
  }

  function showDatabases(opts) {
    runTab('#databases-body', 'the database list', function () { return need('dbs', ['dbs'], null, false); }, function (d) {
      var dbs = normalizeDbs(d);
      var body = $('#databases-body');
      var sel = state.db;
      if (!dbs.length) {
        body.innerHTML = sectionHead('Databases', '') + emptyState('No databases reported', 'The server lists only the databases it is allowed to read.');
        return;
      }
      if (!sel.db || !dbs.some(function (x) { return x.name === sel.db && x.collections.some(function (c) { return c.name === sel.coll; }); })) {
        var first = dbs.filter(function (x) { return x.collections.length; })[0];
        sel.db = first ? first.name : null;
        sel.coll = first ? first.collections[0].name : null;
      }
      body.innerHTML = sectionHead('Databases', 'Read-only. The latest documents in each collection, newest first.') +
        '<div class="split">' +
          '<nav class="pick-list db-list" aria-label="Collections">' + dbs.map(function (db) {
            return '<div class="db-group"><p class="db-name">' + esc(db.name) + (db.role && db.role !== db.name ? ' <span class="muted small">' + esc(db.role) + '</span>' : '') + '</p>' +
              (db.note ? '<p class="muted small">' + esc(db.note) + '</p>' : '') +
              (db.collections.length ? '<ul>' + db.collections.map(function (c) {
                var on = db.name === sel.db && c.name === sel.coll;
                return '<li><button type="button" class="pick pick-row' + (on ? ' is-on' : '') + '" data-db="' + esc(db.name) + '" data-coll="' + esc(c.name) + '"' + (on ? ' aria-current="true"' : '') + '>' +
                  '<span class="pick-title mono">' + esc(c.name) + '</span><span class="pick-count">' + esc(c.count == null ? '' : fmtNum(c.count)) + '</span></button></li>';
              }).join('') + '</ul>' : '<p class="muted small">Empty</p>') +
            '</div>';
          }).join('') + '</nav>' +
          '<div class="split-main" id="db-docs"></div>' +
        '</div>';
      $$('[data-db]', body).forEach(function (b) {
        b.addEventListener('click', function () {
          sel.db = b.getAttribute('data-db');
          sel.coll = b.getAttribute('data-coll');
          $$('[data-db]', body).forEach(function (x) {
            var on = x === b;
            x.classList.toggle('is-on', on);
            if (on) x.setAttribute('aria-current', 'true'); else x.removeAttribute('aria-current');
          });
          loadDocs();
        });
      });
      loadDocs();
    }, opts);
  }

  function docsOf(d) {
    if (Array.isArray(d)) return { docs: d, count: null };
    if (!isObj(d)) return { docs: [], count: null };
    var docs = d.docs || d.documents || d.items || d.rows || d.latest;
    if (!Array.isArray(docs)) {
      for (var k in d) if (Array.isArray(d[k])) { docs = d[k]; break; }
    }
    var count = isNum(d.count) ? d.count : isNum(d.total) ? d.total : null;
    return { docs: arr(docs), count: count };
  }

  function docSummary(doc) {
    if (!isObj(doc)) return esc(String(doc));
    var id = doc.id != null ? doc.id : doc._id;
    var bits = [];
    Object.keys(doc).forEach(function (k) {
      if (k === 'id' || k === '_id' || bits.length >= 3) return;
      var v = doc[k];
      if (typeof v === 'string' && v.length <= 60) bits.push(k + ': ' + v);
      else if (isNum(v) || typeof v === 'boolean') bits.push(k + ': ' + v);
    });
    return '<span class="mono">' + esc(id != null ? (typeof id === 'object' ? json(id) : id) : '(no id)') + '</span>' +
      (bits.length ? ' <span class="muted small">' + esc(bits.join(' · ')) + '</span>' : '');
  }

  function loadDocs() {
    var sel = state.db;
    var box = $('#db-docs');
    if (!box) return;
    if (!sel.db || !sel.coll) { box.innerHTML = emptyState('Pick a collection', ''); return; }
    var db = sel.db, coll = sel.coll, limit = sel.limit;
    var key = 'db:' + db + '/' + coll + ':' + limit;
    if (cached(key) === undefined) box.innerHTML = loadingState(db + '.' + coll);
    need(key, ['db', db, coll], { limit: limit }, false).then(function (d) {
      if (sel.db !== db || sel.coll !== coll || sel.limit !== limit) return;
      var r = docsOf(d);
      box.innerHTML = '<div class="block-head"><h3 class="block-title mono">' + esc(db + '.' + coll) + '</h3>' +
        '<span class="muted small">' + (r.count != null ? 'latest ' + r.docs.length + ' of ' + fmtNum(r.count) : r.docs.length + ' shown') + '</span>' +
        (FIXTURES ? '' : '<label class="inline-field">Show <select class="select" id="db-limit">' + [20, 50, 100].map(function (n) {
          return '<option value="' + n + '"' + (n === limit ? ' selected' : '') + '>' + n + '</option>';
        }).join('') + '</select></label>') +
      '</div>' +
      (r.docs.length ? '<ol class="doc-list">' + r.docs.map(function (doc, i) {
        return '<li><details' + (i === 0 ? ' open' : '') + '><summary>' + docSummary(doc) + '</summary>' + jsonBlock(doc, 'Document') + '</details></li>';
      }).join('') + '</ol>' : emptyState('This collection is empty', ''));
      var ls = $('#db-limit');
      if (ls) ls.addEventListener('change', function () { sel.limit = Number(ls.value); loadDocs(); });
    }, function (e) {
      if (sel.db !== db || sel.coll !== coll) return;
      box.innerHTML = '';
      box.appendChild(errorPanel(e, db + '.' + coll, loadDocs));
    });
  }

  // ---------------------------------------------------------------- start

  function start() {
    setupTheme();
    setupDbSwitch();
    setupTabs();
    setupActivityControls();
    renderHeader();
    setConn('idle');
    var saved = lsGet(LS_TAB);
    selectTab(TABS.indexOf(saved) >= 0 ? saved : 'activity');
    loadRun();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
