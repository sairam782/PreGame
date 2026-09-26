#!/usr/bin/env python3
"""
Banker cabinet SHOWCASE: a controlled test of whether the harness's self-improvement loop can learn this
book's regularities from the tuning clients, and whether what it learns holds on clients it never saw.

Built from the data seat's v3 generator (same schema, same writers, same answer-key rules), with these
deliberate changes. They are the experiment, and we say so whenever a number from this set is shown:

  1. PLANTED regularities, one per tunable harness setting, identical in the tune and test clients:
       - a panic seller's cautious stance lasts REVERT_DAYS (+/- REVERT_JITTER) days after the sell-off, then
         quietly returns to their usual stance. (v3 gave each client its own arbitrary lifetime, so there was
         nothing consistent to learn: its own sweep picked settings on tune that did worse on test.)
       - "one_off_stock" clients buy exactly one single stock, once, and do NOT change how they invest; real
         style changers ("said_vs_did") buy STYLE_CHANGE_BUYS single stocks in their first week and keep
         buying. So "one single-stock buy means a style change" is wrong in this book.
     The planted values go to hidden/planted.json. The harness never reads them; we publish them after
     scoring so anyone can check whether the loop found them.
  2. Story mix weighted toward the planted stories (PATTERN below), so each split has enough cases.
  3. Left out on purpose: life events, contact preferences and the product-specific disclosures (AS-03 to
     AS-10). The harness does not handle them yet; v3 measures that gap. This set tests learning, not coverage.
  4. Plain wording (PLAIN_WORDING): each note uses one phrasing (the v2 phrasing), so a note the harness's
     reader can't parse is not mistaken for a setting the loop failed to learn. v3's varied wording tests the
     reader.

Everything else (the scripted assistant's careless habits, the junior's "No changes" notes, the fee change,
the joint account, the retirement plan, compliance drift, the answer-key rules) is v3's.

Run:  python gen.py                    (SEED=26, N_CLIENTS=20 by default: 10 tune, 10 test)
      N_CLIENTS=40 OUT=out40 python gen.py
"""
import json, os, random
from datetime import date, timedelta

SEED = int(os.environ.get("SEED", 26))
N_CLIENTS = int(os.environ.get("N_CLIENTS", 20))
OUT = os.environ.get("OUT", "out")
PLAIN_WORDING = os.environ.get("PLAIN_WORDING", "1") != "0"
# ---- the planted regularities (hidden; written to hidden/planted.json)
REVERT_DAYS, REVERT_JITTER = 60, 5   # a panic seller's cautious stance lasts 55 to 65 days after the sell-off
STYLE_CHANGE_BUYS = 2                # a real style change starts with 2 single-stock buys within its first week
rng = random.Random(SEED)
D = date.fromisoformat
FEED_START, SIM_START, END = date(2026, 2, 1), date(2026, 5, 1), date(2026, 8, 31)
MONTHS = [(2026, 5), (2026, 6), (2026, 7), (2026, 8)]
T0 = "2019-01-01"
WID = 4 if N_CLIENTS <= 30 else 5            # id width: E-0001 at 20 clients, E-00001 when larger
CW = 2 if N_CLIENTS <= 99 else 3
LIFE_WINDOW_DAYS = 90                       # a prep should raise a life event from the last 90 days
EXPIRY_RULE = "A label is expired when MORE than N days have passed since the note that states it; day N itself is still valid."

# ---------------------------------------------------------------- reference data (unchanged from v2)
REFERENCE = [
    {"ref_id": "FEE-v1", "kind": "fee_schedule", "item": "advisory_fee", "value_pct": 0.85, "valid_from": "2025-01-01", "valid_to": "2026-05-31"},
    {"ref_id": "FEE-v2", "kind": "fee_schedule", "item": "advisory_fee", "value_pct": 0.75, "valid_from": "2026-06-01", "valid_to": None},
]
def fee_at(day):
    for r in REFERENCE:
        if D(r["valid_from"]) <= day and (r["valid_to"] is None or day <= D(r["valid_to"])):
            return r["value_pct"], r["ref_id"]
    return None, None

# ---------------------------------------------------------------- approved language (AS-01, AS-02 unchanged)
APPROVED = [
    {"claim_id": "AS-01", "locked": True, "version": 1, "owner": "Compliance", "applies_to": ["all"],
     "text": "Past performance is not indicative of future results."},
    {"claim_id": "AS-02", "locked": True, "version": 1, "owner": "Compliance", "applies_to": ["all"],
     "text": "The value of investments can fall as well as rise, and you may get back less than you invested."},
]
for _a in APPROVED:
    _a["required_when"] = "always"
APPROVED_V3_ONLY = [   # v3's product-specific sentences: not used in the showcase (see the module docstring)
    {"claim_id": "AS-03", "locked": True, "version": 1, "owner": "Compliance", "applies_to": ["fund", "index_fund"],
     "text": "Equity funds invest in shares, and their value can fall sharply in a falling market."},
    {"claim_id": "AS-04", "locked": True, "version": 1, "owner": "Compliance", "applies_to": ["bond_fund"],
     "text": "Bond fund values fall when interest rates rise, and income from them is not guaranteed."},
    {"claim_id": "AS-05", "locked": True, "version": 1, "owner": "Compliance", "applies_to": ["single_stock"],
     "text": "Holding individual shares concentrates risk; a single company can lose most or all of its value."},
    {"claim_id": "AS-06", "locked": True, "version": 1, "owner": "Compliance", "applies_to": ["managed"],
     "text": "A managed portfolio can fall in value; professional management does not protect your capital."},
    {"claim_id": "AS-07", "locked": True, "version": 1, "owner": "Compliance", "applies_to": ["advisory"],
     "text": "Advisory fees are charged whether the portfolio rises or falls, and they reduce your overall return."},
    {"claim_id": "AS-08", "locked": True, "version": 1, "owner": "Compliance", "applies_to": ["multi_asset"],
     "text": "Diversification does not guarantee a profit or protect against a loss."},
    {"claim_id": "AS-09", "locked": True, "version": 1, "owner": "Compliance", "applies_to": ["cash"],
     "text": "Cash in the account earns interest at a variable rate that can change at any time."},
    {"claim_id": "AS-10", "locked": True, "version": 1, "owner": "Compliance", "applies_to": ["life_event"],
     "text": "Tax treatment depends on your individual circumstances and may change in the future."},
]
APPROVED_TEXT = {a["claim_id"]: a["text"] for a in APPROVED + APPROVED_V3_ONLY}
# drift ladders: step 0 = exact, 4 = forbidden promise. Sentences without a ladder are copied exactly.
LADDER = {
    "AS-01": [APPROVED_TEXT["AS-01"], "Past performance is no guarantee of future results.",
              "Past returns don't tell you much about future returns.", "Past performance has been a good guide for this fund.",
              "This fund has consistently outperformed."],
    "AS-02": [APPROVED_TEXT["AS-02"], "Investments can go down as well as up, and you could get back less.",
              "Your portfolio may dip from time to time.", "Short-term dips aside, this portfolio grows over time.",
              "Your money is safe in this portfolio."],
    "AS-05": [APPROVED_TEXT["AS-05"], "Individual shares carry more risk than a fund.", "Single stocks can be bumpy.",
              "These are solid companies with limited downside.", "These stocks are a safe way to grow your money."],
    "AS-06": [APPROVED_TEXT["AS-06"], "A managed portfolio can still go down in value.", "Managed portfolios smooth out most of the bumps.",
              "Our management keeps losses small.", "This portfolio is built to protect your capital."],
    "AS-07": [APPROVED_TEXT["AS-07"], "Advisory fees apply whatever the market does and reduce your return.",
              "There's an advisory fee, but it's small next to the returns.", "The fee pays for itself through better returns.",
              "Our fee is more than covered by the returns we deliver."],
}
DRIFT_P = 0.15            # rarer and steeper than v2: when a rewording happens it jumps 2 or 3 steps

# ---------------------------------------------------------------- instruments
INSTR = {"Balanced Growth Fund": "fund", "US Total Market Index Fund": "index_fund", "International Index Fund": "index_fund",
         "Short-Term Bond Fund": "bond_fund", "Income Fund": "bond_fund", "Managed Advisory Portfolio": "managed", "Cash": "cash"}
STOCKS = ["Lumen Arc Semiconductors (LASC)", "Cobalt Cloud Inc (CBCL)", "Northgate AI (NGAI)", "Quillfeather Systems (QLFS)"]

# ---------------------------------------------------------------- story types (client behaviour is scripted; errors are not)
# Showcase mix: per 20 clients, 4 quiet reverts and 2 lasting sellers (label expiry), 3 real style changers and
# 3 one-off stock buyers (the buy threshold), plus v3's other stories. Life events and contact preferences are out.
PATTERN = ["sticky_revert", "said_vs_did", "one_off_stock", "sticky_lasting", "advisory_fee",
           "sticky_revert", "wrong_decision_maker", "one_off_stock", "control", "said_vs_did",
           "sticky_revert", "stale_overwrite", "sticky_evidence", "one_off_stock", "sticky_lasting",
           "said_vs_did", "sticky_revert", "advisory_fee", "wrong_decision_maker", "control"]
LIFE_TYPES = ["beneficiary_changed", "spouse_removed", "employer_changed", "large_transfer_in"]
DROPS = {"2026-02-24": -3.6, "2026-03-19": -4.4, "2026-04-15": -3.1, "2026-05-12": -4.1, "2026-06-17": -3.3}
# the sell-off each quiet revert follows (cycled); every one lasts REVERT_DAYS +/- REVERT_JITTER
REVERT_SELLS = ["2026-04-15", "2026-05-12", "2026-06-17", "2026-03-19", "2026-05-12", "2026-04-15", "2026-06-17", "2026-02-24"]
LASTING_PLAN = ["2026-05-12", "2026-03-19", "2026-06-17", "2026-02-24"]
CONTACT_LIFETIMES = [38, 45, 31, 52]

FIRST_M = ["Daniel", "Robert", "Samuel", "Marcus", "Tomas", "Victor", "Owen", "Felix", "Hugo", "Arjun", "Mateo", "Ellis", "Rafael", "Dev", "Colin", "Jonah"]
FIRST_F = ["Priya", "Helen", "Lisa", "Nora", "Grace", "Amara", "Leah", "Ines", "Maya", "Clara", "Sofia", "Ruth", "Hana", "Talia", "June", "Eva"]
LAST = ["Reyes", "Okafor", "Brennan", "Nair", "Ortiz", "Park", "Lindqvist", "Adeyemi", "Castell", "Moreau", "Haddad", "Novak",
        "Ferreira", "Iyer", "Kowalski", "Ashworth", "Mbeki", "Tanaka", "Quinlan", "Varga", "Delacroix", "Osei", "Brandt", "Salas"]

# ---------------------------------------------------------------- vocabulary (visible: possible values only)
VOCABULARY = {
    "risk_attitude": {
        "moderate": "balanced appetite for risk; the client's usual long-run stance",
        "cautious": "after a sell-off or stated nervousness; wants low volatility for now",
        "growth": "open to more equity exposure (for example a growth result on the risk questionnaire)",
        "conservative": "income and capital preservation first"},
    "investing_style": {
        "funds": "holds mixed or active funds",
        "index_only": "index funds only; does not pick single stocks",
        "index_plus_single_stocks": "index funds plus some single stocks bought by the client",
        "managed": "discretionary advisory account; the bank manages the portfolio"},
    "contact_preference": {
        "phone_ok": "calls are fine (the default when nothing else is recorded)",
        "email_only": "the client asked not to be called for now; email only"},
    "decision_maker": "one holder's name from clients.json, or a list of both holders when briefing both",
    "retirement_date": "YYYY-MM, or null when there is no plan",
    "fee_rate": "percent, from reference_data by date",
    "disclosure": "an approved_language claim_id (AS-01 or AS-02)",
    "life_event": "a feed event_id of type beneficiary_changed, spouse_removed, employer_changed or large_transfer_in",
    "_rules": {
        "as_of": "A claim may carry as_of (ISO date). It is then history and is scored against the truth on as_of. A claim without as_of is the document's current view, scored against the truth on the document's date.",
        "instrument_type": "trades, holdings and news carry instrument_type: fund, index_fund, bond_fund, single_stock, managed or cash",
        "expiry": EXPIRY_RULE,
        "life_events": f"A prep should raise any life event from the {LIFE_WINDOW_DAYS} days before it."},
}

# ---------------------------------------------------------------- ids and containers
_counters = {}
def nid(prefix, width=None):
    _counters[prefix] = _counters.get(prefix, 0) + 1
    return f"{prefix}-{_counters[prefix]:0{width or WID}d}"

CLIENTS, CL, TRUTH, START_HOLDINGS, STORY, EVENTS, MARKET, NOTES, PREPS, CLAIMS = [], {}, {}, {}, {}, [], [], [], [], []
LABEL_ATTRS = ["risk_attitude", "investing_style", "decision_maker", "contact_preference"]
FAULT_BY_ATTR = {"risk_attitude": "sticky_label", "investing_style": "said_vs_did", "decision_maker": "wrong_decision_maker",
                 "contact_preference": "sticky_label", "retirement_date": "stale_overwrite"}

def truth_at(cid, attr, day):
    val = None
    for since, v in TRUTH[cid][attr]:
        if D(since) <= day:
            val = v
    return val

def truth_changes(cid, a, b):
    out = []
    for attr, tl in TRUTH[cid].items():
        if attr == "life_events":
            out += [("life_event", e["date"]) for e in tl if a < D(e["date"]) <= b]
            continue
        for since, _ in tl[1:]:
            if a < D(since) <= b:
                out.append((attr, since))
    return out

def ev(cid, day, etype, **f):
    e = {"event_id": nid("E"), "client_id": cid, "date": day if isinstance(day, str) else day.isoformat(), "type": etype}
    e.update(f)
    EVENTS.append(e)
    return e

def rand_day(y, m, lo=1, hi=28):
    return date(y, m, rng.randint(lo, hi))

def pick(lst):
    return lst[rng.randrange(len(lst))]

def say(options):
    """A note's wording: the first (v2) phrasing when PLAIN_WORDING, else a random one as in v3."""
    return options[0] if PLAIN_WORDING else pick(options)

# ---------------------------------------------------------------- build the client book
def build_clients():
    counts, used_last = {}, set()
    split_toggle = 0
    for i in range(N_CLIENTS):
        cid = f"C{i + 1:0{CW}d}"
        story = PATTERN[i % len(PATTERN)]
        k = counts.get(story, 0); counts[story] = k + 1
        # split by client so test clients are unseen; alternate within each story type
        split = "tune" if k % 2 == 0 else "test"
        if PATTERN.count(story) == 1 and N_CLIENTS <= len(PATTERN):
            split = "tune" if split_toggle % 2 == 0 else "test"; split_toggle += 1
        last = next(l for l in (LAST[(i + j) % len(LAST)] for j in range(len(LAST))) if l not in used_last or len(used_last) >= len(LAST))
        used_last.add(last)
        female = rng.random() < 0.5
        first = pick(FIRST_F if female else FIRST_M)
        joint = story in ("wrong_decision_maker",) or (story == "life_event" and LIFE_TYPES[k % 4] == "spouse_removed")
        if joint:
            a, b = f"{pick(FIRST_M)} {last}", f"{pick(FIRST_F)} {last}"
            household = [{"name": a, "role": "joint holder"}, {"name": b, "role": "joint holder"}]
            holders, name, pron = [a, b], f"{a.split()[0]} and {b}", "they"
        else:
            household = [{"name": f"{first} {last}", "role": "primary"}]
            holders, name, pron = [f"{first} {last}"], f"{first} {last}", ("she" if female else "he")
        advisory = story == "advisory_fee"
        holdings = {
            "sticky_revert": {"Balanced Growth Fund": 90000 + 1000 * rng.randint(0, 60), "US Total Market Index Fund": 50000 + 1000 * rng.randint(0, 40)},
            "sticky_lasting": {"Balanced Growth Fund": 80000 + 1000 * rng.randint(0, 60), "US Total Market Index Fund": 40000 + 1000 * rng.randint(0, 40)},
            "sticky_evidence": {"Balanced Growth Fund": 100000 + 1000 * rng.randint(0, 40), "US Total Market Index Fund": 60000 + 1000 * rng.randint(0, 30)},
            "said_vs_did": {"US Total Market Index Fund": 150000 + 1000 * rng.randint(0, 80), "International Index Fund": 60000 + 1000 * rng.randint(0, 40)},
            "one_off_stock": {"US Total Market Index Fund": 150000 + 1000 * rng.randint(0, 80), "International Index Fund": 60000 + 1000 * rng.randint(0, 40)},
            "advisory_fee": {"Managed Advisory Portfolio": 300000 + 5000 * rng.randint(0, 40)},
            "life_event": {"Balanced Growth Fund": 120000 + 1000 * rng.randint(0, 60), "Short-Term Bond Fund": 50000 + 1000 * rng.randint(0, 40)},
            "contact_pref": {"Balanced Growth Fund": 90000 + 1000 * rng.randint(0, 50), "Income Fund": 60000 + 1000 * rng.randint(0, 40)},
            "wrong_decision_maker": {"Cash": 40000 + 1000 * rng.randint(0, 30), "Balanced Growth Fund": 120000 + 1000 * rng.randint(0, 40)},
            "stale_overwrite": {"Balanced Growth Fund": 250000 + 5000 * rng.randint(0, 20), "Income Fund": 80000 + 1000 * rng.randint(0, 40)},
            "control": {"Income Fund": 180000 + 1000 * rng.randint(0, 60), "Short-Term Bond Fund": 120000 + 1000 * rng.randint(0, 40)},
        }[story]
        age = rng.randint(60, 64) if story == "stale_overwrite" else rng.randint(68, 76) if story == "control" else rng.randint(36, 62)
        c = {"client_id": cid, "name": name, "age": age, "tier": pick(["A", "B", "C"]), "household": household,
             "accounts": [{"account_id": f"A{i + 1:0{CW}d}", "type": ("advisory managed account" if advisory else "joint brokerage" if joint else "individual brokerage"),
                           "holders": holders, "advisory": advisory}]}
        CLIENTS.append(c); CL[cid] = c; START_HOLDINGS[cid] = holdings
        STORY[cid] = {"story": story, "k": k, "split": split, "pron": pron, "first": holders[0].split()[0],
                      "usual_risk": "conservative" if story == "control" else "moderate",
                      "style": "index_only" if story in ("said_vs_did", "one_off_stock") else "managed" if advisory else "funds"}

# ---------------------------------------------------------------- hidden truth + scripted client behaviour
def build_truth_and_stories():
    rev_i = last_i = con_i = 0
    for c in CLIENTS:
        cid, s = c["client_id"], STORY[c["client_id"]]
        story, holders = s["story"], c["accounts"][0]["holders"]
        dm = holders[1] if story == "wrong_decision_maker" else holders[0]
        t = {"risk_attitude": [(T0, s["usual_risk"])], "investing_style": [(T0, s["style"])],
             "decision_maker": [(T0, dm)], "retirement_date": [(T0, None)], "contact_preference": [(T0, "phone_ok")], "life_events": []}
        if story in ("sticky_revert", "sticky_lasting", "sticky_evidence"):
            if story == "sticky_revert":
                sell = REVERT_SELLS[rev_i % len(REVERT_SELLS)]; rev_i += 1
                life = REVERT_DAYS + rng.randint(-REVERT_JITTER, REVERT_JITTER)
            elif story == "sticky_lasting":
                sell, life = LASTING_PLAN[last_i % len(LASTING_PLAN)], None; last_i += 1
            else:
                sell, life = "2026-04-15", None
            s["sell"] = sell
            for inst, amt in START_HOLDINGS[cid].items():
                ev(cid, sell, "trade_sell", instrument=inst, instrument_type=INSTR[inst], amount=amt)
            t["risk_attitude"].append((sell, "cautious"))
            if story == "sticky_revert":
                back = D(sell) + timedelta(days=life)
                s["revert"] = back.isoformat()
                t["risk_attitude"].append((back.isoformat(), s["usual_risk"]))
                # weak signals only: resumed small monthly contributions, no further selling
                x = back + timedelta(days=rng.randint(3, 12))
                while x <= END:
                    ev(cid, x, "trade_buy", instrument="Balanced Growth Fund", instrument_type="fund", amount=rng.choice([1000, 1500, 2000]))
                    x += timedelta(days=30)
            if story == "sticky_evidence":
                q = D(sell) + timedelta(days=52)
                ev(cid, q, "tool_used", tool="risk questionnaire", result="growth")
                t["risk_attitude"].append((q.isoformat(), "growth"))
                ev(cid, q + timedelta(days=6), "trade_buy", instrument="Balanced Growth Fund", instrument_type="fund", amount=50000)
        elif story == "said_vs_did":
            # planted: a real change starts with STYLE_CHANGE_BUYS buys inside a week (days 1-10 of June or July,
            # so no prep falls between them), then keeps going every 12-22 days
            start = date(2026, rng.choice([6, 7]), rng.randint(1, 10))
            for j in range(3):
                ev(cid, start - timedelta(days=20 - 7 * j), "article_read", topic="tech stocks")
            x, n = start, 0
            while x <= END:
                ev(cid, x, "trade_buy", instrument=STOCKS[n % len(STOCKS)], instrument_type="single_stock", amount=rng.choice([1500, 2500, 3000, 4000]))
                n += 1
                x += timedelta(days=rng.randint(1, 5) if n < STYLE_CHANGE_BUYS else rng.randint(12, 22))
            t["investing_style"].append((start.isoformat(), "index_plus_single_stocks"))
            s["style_change"] = start.isoformat()
        elif story == "one_off_stock":
            # planted: one single-stock buy, once; the client still invests index-only (truth unchanged)
            day = date(2026, 6, 1) + timedelta(days=rng.randint(0, 60))
            e = ev(cid, day, "trade_buy", instrument=pick(STOCKS), instrument_type="single_stock", amount=rng.choice([1500, 2500, 3000, 4000]))
            s["one_off"] = e["event_id"]
        elif story == "wrong_decision_maker":
            ev(cid, "2026-05-06", "email_to_banker", sender=dm, topic="cash to bond fund",
               text="Can we move some of the cash into the bond fund? Happy to talk this week.")
            ev(cid, "2026-06-11", "tool_used", tool="retirement calculator", user=dm)
            ev(cid, "2026-07-14", "email_to_banker", sender=dm, topic="cash to bond fund",
               text="Following up on the bond fund switch from May. Did this go through?")
        elif story == "stale_overwrite":
            plan = f"{2027 + rng.randint(0, 1)}-{rng.choice(['03', '06', '09'])}"
            s["plan"] = plan
            t["retirement_date"].append(("2026-05-20", plan))
            ev(cid, "2026-06-04", "tool_used", tool="retirement calculator")
            ev(cid, "2026-06-18", "article_read", topic="retirement income")
        elif story == "life_event":
            kind = LIFE_TYPES[s["k"] % 4]
            day = date(2026, 5, 26) + timedelta(days=rng.randint(0, 50))
            if kind == "beneficiary_changed":
                e = ev(cid, day, "beneficiary_changed", detail="primary beneficiary changed on the IRA")
            elif kind == "spouse_removed":
                e = ev(cid, day, "spouse_removed", holder_removed=holders[1], note="joint holder removed from the account")
            elif kind == "employer_changed":
                e = ev(cid, day, "employer_changed", new_employer="Meridian Freightworks", source="payroll deposit")
                ev(cid, day + timedelta(days=14), "cash_in", amount=8200, counterparty="salary (Meridian Freightworks)")
            else:
                e = ev(cid, day, "large_transfer_in", amount=rng.choice([240000, 310000, 185000]), likely_source="house sale")
            t["life_events"].append({"event_id": e["event_id"], "date": e["date"], "type": kind})
            s["life"] = e
            for m in (6, 7, 8):   # quieter engagement around the event
                ev(cid, date(2026, m, 3), "email_ignored", email="monthly market letter")
        elif story == "contact_pref":
            d0 = date(2026, 4, 12) + timedelta(days=rng.randint(0, 60))
            life = CONTACT_LIFETIMES[con_i % len(CONTACT_LIFETIMES)]; con_i += 1
            back = d0 + timedelta(days=life)
            e = ev(cid, d0, "email_to_banker", sender=holders[0], topic="contact",
                   text=f"Travelling for work until {back.strftime('%B %d')}. Email only please, no calls until then.")
            s["contact"] = {"email": e, "back": back.isoformat()}
            t["contact_preference"] += [(d0.isoformat(), "email_only"), (back.isoformat(), "phone_ok")]
        TRUTH[cid] = t

# ---------------------------------------------------------------- background feed and market
def background_feed():
    for c in CLIENTS:
        cid, story = c["client_id"], STORY[c["client_id"]]["story"]
        lo, hi = (1, 3) if story == "control" else (4, 14)
        m0 = FEED_START
        for (y, m) in [(2026, 2), (2026, 3), (2026, 4)] + MONTHS:
            for _ in range(rng.randint(lo, hi)):
                ev(c["client_id"], rand_day(y, m), "app_login")
            if story == "control":
                ev(cid, date(y, m, 3), "cash_in", amount=3900, counterparty="pension")
            else:
                for dd in (1, 15):
                    ev(cid, date(y, m, dd), "cash_in", amount=rng.choice([4200, 5100, 6800, 7400]), counterparty="salary")
            for _ in range(rng.randint(1, 2)):
                ev(cid, rand_day(y, m), "cash_out", amount=rng.randrange(500, 6000, 50), counterparty="withdrawal")
            if rng.random() < 0.5:
                ev(cid, rand_day(y, m), "article_read", topic=pick(["market outlook", "retirement income", "ESG", "mortgages", "estate planning"]))
            if story != "life_event" or m < 6:
                ev(cid, date(y, m, 3), "email_opened" if rng.random() < 0.6 else "email_ignored", email="monthly market letter")
            if rng.random() < 0.2:
                ev(cid, rand_day(y, m), "document_requested", document=pick(["statement", "tax form", "valuation"]))
        if STORY[cid]["story"] == "advisory_fee":
            ev(cid, "2026-06-02", "email_opened", email="Advisory fee schedule update (effective June 1)")

def market_feed():
    x = FEED_START
    corr = [-1.2, -0.8, -2.1, 0.4, -1.6, -1.1, -0.9, 0.6, -2.4, -1.3]
    ci = 0
    while x <= END:
        if x.weekday() < 5:
            if x.isoformat() in DROPS:
                mv = DROPS[x.isoformat()]
            elif D("2026-08-10") <= x <= D("2026-08-21") and ci < len(corr):
                mv = corr[ci]; ci += 1
            else:
                mv = round(rng.gauss(0.05, 0.7), 2)
            MARKET.append({"event_id": nid("M"), "date": x.isoformat(), "type": "index_move", "index": "US Total Market", "pct": mv})
        x += timedelta(1)
    MARKET.append({"event_id": nid("M"), "date": "2026-07-29", "type": "rate_change", "bps": -25})
    x = D("2026-06-05")
    while x <= END:
        for name in STOCKS:
            MARKET.append({"event_id": nid("M"), "date": x.isoformat(), "type": "news_on_holding", "instrument": name,
                           "instrument_type": "single_stock", "sentiment": pick(["positive", "neutral", "negative"])})
        x += timedelta(7)

def events_between(cid, a, b, types=None):
    return [e for e in EVENTS if e["client_id"] == cid and a < D(e["date"]) <= b and (types is None or e["type"] in types)]

def holdings_at(cid, day):
    h = dict(START_HOLDINGS[cid])
    for e in EVENTS:
        if e["client_id"] == cid and D(e["date"]) <= day and e["type"] in ("trade_buy", "trade_sell"):
            sign = 1 if e["type"] == "trade_buy" else -1
            h[e["instrument"]] = h.get(e["instrument"], 0) + sign * e["amount"]
            h["Cash"] = h.get("Cash", 0) - sign * e["amount"]
    return {k: v for k, v in h.items() if v > 0}

def instr_type(name):
    return INSTR.get(name, "single_stock")

# ---------------------------------------------------------------- documents
def claim(doc_id, doc_type, cid, day, attr, value, kind, basis=None, extra=None):
    c = {"claim_id": nid("K"), "doc_id": doc_id, "doc_type": doc_type, "client_id": cid,
         "date": day if isinstance(day, str) else day.isoformat(), "attribute": attr, "value": value, "kind": kind, "basis": basis or []}
    if extra:
        c.update(extra)
    CLAIMS.append(c)
    return c

def add_note(cid, day, author, text, claims):
    n = {"note_id": nid("N"), "client_id": cid, "date": day if isinstance(day, str) else day.isoformat(), "author": author, "text": text}
    NOTES.append(n)
    for (attr, value, kind, basis, *extra) in claims:
        claim(n["note_id"], "note", cid, n["date"], attr, value, kind, basis, extra[0] if extra else None)
    return n

STYLE_WORDS = {"funds": "happy in funds", "index_only": "index funds only", "managed": "leaves the day-to-day to us"}

def backstory():
    for c in CLIENTS:
        cid, s = c["client_id"], STORY[c["client_id"]]
        day = date(2021 + rng.randint(0, 4), rng.randint(1, 12), rng.randint(1, 27)).isoformat()
        who = c["accounts"][0]["holders"]
        cl = [("risk_attitude", s["usual_risk"], "observation", []), ("investing_style", s["style"], "observation", [])]
        if s["story"] in ("said_vs_did", "one_off_stock"):
            text = say([f"{s['first']}: \"I only do index funds, I don't pick stocks, don't call me about that.\" Moderate risk.",
                         f"{s['first']} was clear: index funds only, no stock picking, please don't pitch single names. Moderate risk."])
        elif s["story"] == "wrong_decision_maker":
            text = f"Joint account opened. {who[1].split()[0]} handles the household finances; {who[0].split()[0]} rarely involved. Moderate risk, funds only."
            cl.append(("decision_maker", who[1], "observation", []))
        elif s["story"] == "advisory_fee":
            text = say(["Moved to the advisory account. Fee 0.85%. Moderate risk, leaves the day-to-day to us.",
                         "Signed up for the managed account at 0.85%. Moderate risk; wants us to run it."])
            cl.append(("fee_rate", 0.85, "fee_quote", []))
        elif s["story"] == "stale_overwrite":
            text = f"{s['first']}: working full time, no retirement plans for now. Moderate risk, {STYLE_WORDS[s['style']]}."
            cl.append(("retirement_date", None, "plan", []))
        elif s["story"] == "control":
            text = say(["Retired teacher. Conservative, wants steady income from her funds.",
                         "Retired. Conservative; income first, low touch."])
        else:
            text = say([f"Opened account. {s['usual_risk'].capitalize()} risk, {STYLE_WORDS[s['style']]}, long horizon.",
                         f"New client. {s['usual_risk'].capitalize()} risk appetite, {STYLE_WORDS[s['style']]}."])
        if s["story"] == "sticky_revert" and s["k"] == 0:
            pass
        add_note(cid, day, "banker", text, cl)
    # a scattered near-copy of approved wording in an old note (as in v2)
    adv = [c for c in CLIENTS if STORY[c["client_id"]]["story"] == "advisory_fee"]
    if adv:
        add_note(adv[0]["client_id"], "2025-07-09", "junior", "Reminded him past performance is no guarantee of future results.",
                 [("disclosure", "AS-01", "disclosure", [], {"step": 1, "text": LADDER["AS-01"][1]})])

def build_contacts():
    """banker: reactive after a sell-off or a contact email, quarterly in May for others, a few in August;
    junior: monthly June to August (and May for joint accounts)."""
    contacts = {}
    for c in CLIENTS:
        cid, s = c["client_id"], STORY[c["client_id"]]
        lst = []
        if "sell" in s:
            lst.append((D(s["sell"]) + timedelta(days=1), "banker"))
        elif s["story"] == "stale_overwrite":
            lst.append((date(2026, 5, 20), "banker"))        # the day she tells the banker (truth changes that day)
        elif s["story"] == "wrong_decision_maker":
            lst.append((date(2026, 5, 8), "banker"))         # banker speaks with the real decision maker first
        elif s["story"] != "contact_pref":                    # no call while a client has asked for email only
            lst.append((date(2026, 5, rng.randint(5, 22)), "banker"))
        if s["story"] == "contact_pref":
            lst.append((D(s["contact"]["email"]["date"]) + timedelta(days=1), "banker"))
        if s["story"] == "wrong_decision_maker":
            lst.append((date(2026, 5, 21), "junior"))
        for m in (6, 7, 8):
            lst.append((date(2026, m, rng.randint(19, 27)), "junior"))
        if s["story"] in ("said_vs_did", "one_off_stock", "control"):
            lst.append((date(2026, 8, rng.randint(26, 29)), "banker"))
        contacts[cid] = sorted(set(lst))
    return contacts

def he(s):
    return {"he": "he", "she": "she", "they": "they"}[s["pron"]]

def banker_write(c, day, last_prep):
    cid, s = c["client_id"], STORY[c["client_id"]]
    who = c["accounts"][0]["holders"]
    if "sell" in s and day == D(s["sell"]) + timedelta(days=1):
        sells = [e["event_id"] for e in events_between(cid, day - timedelta(3), day, {"trade_sell"})]
        return (say([f"Called after {he(s)} sold everything on {D(s['sell']).strftime('%b %d')}, against my advice. Rattled by the headlines.",
                      f"{s['first']} sold out yesterday in the drop. Very nervous; wants to sit in cash for now.",
                      f"Emergency call: {s['first']} liquidated on the {D(s['sell']).day}th. Shaken by the news; revisit in a few weeks."]),
                [("risk_attitude", "cautious", "observation", sells)])
    if s["story"] == "contact_pref" and day == D(s["contact"]["email"]["date"]) + timedelta(days=1):
        e = s["contact"]["email"]
        return (f"{s['first']} emailed: travelling for work, email only until {D(s['contact']['back']).strftime('%b %d')}. No calls.",
                [("contact_preference", "email_only", "observation", [e["event_id"]])])
    if day.month == 8:
        if s["story"] in ("said_vs_did", "one_off_stock"):
            return ("Walked through the index portfolio. No questions from him.",
                    [("investing_style", "index_only", "observation", [last_prep["prep_id"]] if last_prep else [])])
        return ("Quarterly review. Comfortable with the conservative mix; nothing to change.",
                [("risk_attitude", "conservative", "observation", [])])
    story = s["story"]
    if story in ("said_vs_did", "one_off_stock"):
        return (say(["Annual review. Happy with the index portfolio, no changes wanted.", "Review done. Still index only; content."]),
                [("investing_style", "index_only", "observation", [])])
    if story == "wrong_decision_maker":
        em = [e["event_id"] for e in events_between(cid, day - timedelta(10), day, {"email_to_banker"})]
        return (f"Spoke with {who[1].split()[0]}. Wants 20k of the cash moved into the bond fund. She runs the household finances.",
                [("decision_maker", who[1], "observation", em)])
    if story == "stale_overwrite":
        y, m = s["plan"].split("-")
        mon = date(int(y), int(m), 1).strftime("%B %Y")
        return (f"{s['first']} plans to retire {'' if PLAIN_WORDING else 'in '}{mon}. Wants to shift toward income over the next year.",
                [("retirement_date", s["plan"], "plan", [])])
    if story == "advisory_fee":
        return (say(["Annual review. Advisory fee 0.85% reconfirmed. No concerns.", "Review meeting. Fee (0.85%) discussed; happy with service."]),
                [("fee_rate", 0.85, "fee_quote", [])])
    if story == "control":
        return ("Quarterly review. Comfortable with the conservative mix. Pension covers her spending.",
                [("risk_attitude", "conservative", "observation", [])])
    return (say(["Quarterly review. No changes to the plan.", "Check-in meeting. Plan unchanged.", "Review call; nothing new to record."]), [])

def junior_write(c, day, state):
    holders = c["accounts"][0]["holders"]
    s = STORY[c["client_id"]]
    spoke = holders[0]
    first = spoke.split()[0]
    email_only = truth_at(c["client_id"], "contact_preference", day) == "email_only" and s["story"] == "contact_pref"
    if len(holders) > 1 and state.get("junior_spoke_before") != spoke and s["story"] == "wrong_decision_maker":
        state["junior_spoke_before"] = spoke
        return (f"Spoke with {first}. He makes the decisions on the account.", [("decision_maker", spoke, "label", [])])
    if len(holders) > 1 and s["story"] == "wrong_decision_maker":
        return (f"Spoke with {first}. No changes.", [("_all", None, "no_change", []), ("decision_maker", spoke, "label", [])])
    if email_only:
        return ("Checked in by email. No changes.", [("_all", None, "no_change", [])])
    return (say(["Monthly check-in call. No changes.", "Monthly check-in. Nothing new.", "Routine check-in; no changes reported.",
                  "Called for the monthly check-in. All the same."]), [("_all", None, "no_change", [])])

LABEL_TEXT = {
    ("risk_attitude", "moderate"): "Moderate risk appetite.",
    ("risk_attitude", "cautious"): "Panic seller: expect a nervous call, reassure first, keep to low-volatility products.",
    ("risk_attitude", "growth"): "Growth-minded: open to more equity exposure.",
    ("risk_attitude", "conservative"): "Conservative: income and capital preservation first.",
    ("investing_style", "index_only"): "Index-only investor: do not raise single stocks.",
    ("investing_style", "funds"): "Fund investor.",
    ("investing_style", "managed"): "Managed advisory account.",
    ("contact_preference", "email_only"): "Email only: do not call.",
    ("contact_preference", "phone_ok"): "Calls are fine.",
}
def label_text(attr, value):
    if attr == "decision_maker":
        return f"Decision maker: {value}."
    return LABEL_TEXT.get((attr, value), f"{attr}: {value}")

def required_disclosures(cid, day):
    """Showcase: only AS-01 and AS-02 exist, and both apply to every prep."""
    return [a["claim_id"] for a in APPROVED]

def assistant_prep(c, day, state):
    cid = c["client_id"]
    notes = sorted([n for n in NOTES if n["client_id"] == cid and D(n["date"]) <= day and n["author"] != "assistant"], key=lambda n: (n["date"], n["note_id"]))
    by_doc = {}
    for cc in CLAIMS:
        by_doc.setdefault(cc["doc_id"], []).append(cc)
    tags = dict(state.get("tags", {}))
    if not state.get("tags"):
        for n in notes:
            for cc in by_doc.get(n["note_id"], []):
                if cc["attribute"] in LABEL_ATTRS:
                    tags[cc["attribute"]] = (cc["value"], n["note_id"])
    else:
        new = [n for n in notes if D(n["date"]) > D(state["last_prep_date"])]
        if new:
            for cc in by_doc.get(new[-1]["note_id"], []):
                if cc["attribute"] in LABEL_ATTRS:
                    tags[cc["attribute"]] = (cc["value"], new[-1]["note_id"])
    if "decision_maker" not in tags:
        tags["decision_maker"] = (c["household"][0]["name"], "profile")
    latest = notes[-1] if notes else None
    retirement = None
    if latest:
        for cc in by_doc.get(latest["note_id"], []):
            if cc["attribute"] == "retirement_date" and cc["value"]:
                retirement = (cc["value"], latest["note_id"])
    fee = None
    if c["accounts"][0]["advisory"]:
        for n in reversed(notes):
            fc = [cc for cc in by_doc.get(n["note_id"], []) if cc["attribute"] == "fee_rate"]
            if fc:
                fee = (fc[0]["value"], n["note_id"]); break
    steps = dict(state.get("steps", {}))
    req = required_disclosures(cid, day)
    for k in req:
        if k not in steps:
            steps[k] = 0
        elif k in LADDER and STORY[cid]["story"] != "control" and rng.random() < DRIFT_P:
            steps[k] = min(4, steps[k] + rng.choice([2, 3]))
    order = ["risk_attitude", "investing_style", "decision_maker", "contact_preference"]
    if tags.get("risk_attitude", (None,))[0] == "cautious":
        order = ["risk_attitude"] + [o for o in order if o != "risk_attitude"]
    h = holdings_at(cid, day)
    lines = [f"CALL PREP: {c['name']} ({day.isoformat()})", "", "Key points:"]
    lines += [f"- {label_text(a, tags[a][0])}" for a in order if a in tags]
    if retirement:
        lines.append(f"- Retirement planned for {retirement[0]}.")
    if fee:
        lines.append(f"- Advisory fee: {fee[0]:.2f}%.")
    lines.append(f"- Latest update: {latest['text'] if latest else 'none'}")
    lines += ["", "Holdings: " + "; ".join(f"{k} ${v:,.0f}" for k, v in sorted(h.items(), key=lambda kv: -kv[1])), "",
              "Disclosures: " + " ".join((LADDER[k][steps[k]] if k in LADDER else APPROVED_TEXT[k]) for k in req)]
    prep = {"prep_id": nid("P"), "client_id": cid, "date": day.isoformat(), "text": "\n".join(lines),
            "used_note_ids": [latest["note_id"]] if latest else []}
    PREPS.append(prep)
    pid = prep["prep_id"]
    for a in order:
        if a in tags:
            claim(pid, "prep", cid, day, a, tags[a][0], "label", [tags[a][1]])
    claim(pid, "prep", cid, day, "retirement_date", retirement[0] if retirement else None,
          "plan" if retirement else "omission", [retirement[1]] if retirement else [])
    if fee:
        claim(pid, "prep", cid, day, "fee_rate", fee[0], "fee_quote", [fee[1]])
    for k in req:
        claim(pid, "prep", cid, day, "disclosure", k, "disclosure", [], {"step": steps[k], "text": LADDER[k][steps[k]] if k in LADDER else APPROVED_TEXT[k]})
    if latest:
        for cc in by_doc.get(latest["note_id"], []):
            if cc["attribute"] == "disclosure":
                claim(pid, "prep", cid, day, "disclosure", cc["value"], "disclosure", [latest["note_id"]],
                      {"step": cc["step"], "text": cc["text"], "copied_from": latest["note_id"]})
    add_note(cid, day, "assistant", "Client summary (assistant): " + " ".join(label_text(a, tags[a][0]) for a in order if a in tags),
             [(a, tags[a][0], "label", [tags[a][1]]) for a in order if a in tags])
    state.update({"tags": tags, "steps": steps, "last_prep_date": day.isoformat(), "last_prep": prep})

def simulate(contacts):
    for c in CLIENTS:
        cid, state = c["client_id"], {}
        prep_days = {}
        for dd, _ in contacts[cid]:
            if dd >= SIM_START and (dd.year, dd.month) not in prep_days:
                prep_days[(dd.year, dd.month)] = dd - timedelta(2)
        actions = sorted([(pd, 0, "prep") for pd in prep_days.values()] + [(dd, 1, who) for dd, who in contacts[cid]])
        last_contact = FEED_START
        for day, _, what in actions:
            if what == "prep":
                assistant_prep(c, day, state); continue
            text, cl = banker_write(c, day, state.get("last_prep")) if what == "banker" else junior_write(c, day, state)
            n = add_note(cid, day, what, text, cl)
            n["_prev_contact"] = last_contact.isoformat()
            last_contact = day

# ---------------------------------------------------------------- stale book summary (Feb 2026)
def build_book():
    lines = [{"line_id": "B1", "text": "Targets: grow managed assets 8% this year; two new advisory accounts per quarter.", "claims": []},
             {"line_id": "B2", "text": "Advisory fee: 0.85% on managed accounts.", "claims": [("*", "fee_rate", 0.85, "fee_quote")]}]
    for i, c in enumerate(CLIENTS):
        s = STORY[c["client_id"]]
        last = c["household"][0]["name"].split()[-1]
        txt = f"{last} (Tier {c['tier']}): {s['usual_risk']} risk, {STYLE_WORDS[s['style']]}."
        cl = [(c["client_id"], "risk_attitude", s["usual_risk"], "label"), (c["client_id"], "investing_style", s["style"], "label")]
        if s["story"] == "stale_overwrite":
            txt += " No retirement plans before 2030."
            cl.append((c["client_id"], "retirement_date", None, "plan"))
        lines.append({"line_id": f"B{i + 3}", "text": txt, "claims": cl})
    lines.append({"line_id": f"B{len(lines) + 1}", "text": "Rule: always use the approved disclosure wording.", "claims": []})
    return {"doc_id": "BOOK-1", "last_updated": "2026-02-15", "owner": "banker", "lines": lines}

# ---------------------------------------------------------------- scoring (hidden)
FIXES = {
    "sticky_label": "Keep the label as a dated observation with its source; expire it after its lifetime unless reconfirmed; ask when newer evidence disagrees.",
    "said_vs_did": "Keep the statement dated to when it was said, and ask about the activity that contradicts it.",
    "wrong_decision_maker": "Record who said it, on which call. The banker note and the other holder's emails point elsewhere: brief both holders.",
    "stale_overwrite": "'No changes' does not erase an earlier plan. Carry it from the banker note until something contradicts it.",
    "stale_reference": "Quote the fee from reference data by date, never from old notes.",
    "compliance_drift": "Insert the approved sentence by id, word for word, or block the draft.",
    "missed_change": "'No changes' conflicts with what the client did since the last contact. Flag for the banker.",
    "missed_life_event": "The feed shows a life event the file never mentions. Raise it in the prep and ask about it.",
}

def evidence_for(cid, attr, since, upto):
    types = {"risk_attitude": {"tool_used", "trade_buy", "trade_sell"}, "investing_style": {"trade_buy", "article_read"},
             "decision_maker": {"email_to_banker", "tool_used", "reply_lag"}, "retirement_date": {"tool_used"},
             "contact_preference": {"email_to_banker"}}.get(attr)
    evs = events_between(cid, D(since) - timedelta(1), upto, types)
    if attr == "investing_style":
        evs = [e for e in evs if e.get("instrument_type") == "single_stock" or e.get("topic") == "tech stocks"]
    return [e["event_id"] for e in evs]

def score_claim(c, notes_by_id=None):
    """None when true, else (fault_type, truth, evidence, severity). Value claims use as_of when present."""
    cid, attr = c["client_id"], c["attribute"]
    day = D(c.get("as_of") or c["date"])
    if attr in LABEL_ATTRS or attr == "retirement_date":
        t = truth_at(cid, attr, day)
        v = c["value"]
        ok = (t in v and len(v) > 1) if (attr == "decision_maker" and isinstance(v, list)) else (v == t)
        if ok:
            return None
        since = max((s for s, _ in TRUTH[cid][attr] if D(s) <= day), default=T0)
        return FAULT_BY_ATTR[attr], t, evidence_for(cid, attr, since, day), None
    if attr == "fee_rate":
        t, ref = fee_at(day)
        return None if t is None or abs(c["value"] - t) < 1e-9 else ("stale_reference", t, [ref], None)
    if attr == "disclosure":
        return None if c["step"] == 0 else ("compliance_drift", APPROVED_TEXT[c["value"]], [c["value"]], c["step"])
    if attr == "life_event":
        ok = any(e["event_id"] == c["value"] and D(e["date"]) <= day for e in TRUTH[cid]["life_events"])
        return None if ok else ("missed_life_event", [e["event_id"] for e in TRUTH[cid]["life_events"]], [], None)
    if c.get("kind") == "no_change":
        n = (notes_by_id or {}).get(c["doc_id"])
        prev = n.get("_prev_contact") if n else None
        if prev:
            ch = truth_changes(cid, D(prev), D(c["date"]))
            if ch:
                ev_ids = []
                for a, s in ch:
                    ev_ids += ([e["event_id"] for e in TRUTH[cid]["life_events"] if e["date"] == s] if a == "life_event"
                               else evidence_for(cid, a, s, D(c["date"])))
                return "missed_change", [a for a, _ in ch], ev_ids, None
        return None
    return None

def life_events_due(cid, day):
    return [e for e in TRUTH[cid]["life_events"] if day - timedelta(days=LIFE_WINDOW_DAYS) < D(e["date"]) <= day]

def answer_key(book):
    faults, true_claims, faulty = [], 0, set()
    notes_by_id = {n["note_id"]: n for n in NOTES}
    def add(c, ftype, t, evidence, severity=None):
        f = {"fault_id": nid("F"), "client_id": c["client_id"], "doc_type": c["doc_type"], "doc_id": c["doc_id"], "date": c["date"],
             "fault_type": ftype, "attribute": c["attribute"], "claimed": c["value"], "truth": t, "evidence": evidence, "fix": FIXES[ftype]}
        if severity is not None:
            f.update({"severity": severity, "claimed_text": c.get("text"), "level": "warning" if severity == 1 else "fault"})
        faults.append(f); faulty.add(c["doc_id"])
    for c in CLAIMS:
        r = score_claim(c, notes_by_id)
        if r is None:
            true_claims += 1; continue
        ftype, t, evidence, sev = r
        add(dict(c, value="no changes") if ftype == "missed_change" else c, ftype, t, evidence, sev)
    # preps must raise recent life events
    for p in PREPS:
        mentioned = {c["value"] for c in CLAIMS if c["doc_id"] == p["prep_id"] and c["attribute"] == "life_event"}
        for e in life_events_due(p["client_id"], D(p["date"])):
            if e["event_id"] not in mentioned:
                add({"client_id": p["client_id"], "doc_type": "prep", "doc_id": p["prep_id"], "date": p["date"],
                     "attribute": "life_event", "value": None}, "missed_life_event", e["event_id"], [e["event_id"]])
    for line in book["lines"]:
        for (cid, attr, val, kind) in line["claims"]:
            if attr == "fee_rate":
                t, ref = fee_at(END)
                if abs(val - t) > 1e-9:
                    faults.append({"fault_id": nid("F"), "client_id": "*", "doc_type": "book_summary", "doc_id": line["line_id"], "date": END.isoformat(),
                                   "fault_type": "stale_reference", "attribute": attr, "claimed": val, "truth": t, "evidence": [ref], "fix": FIXES["stale_reference"]})
                continue
            t = truth_at(cid, attr, END)
            if val != t:
                since = max((s for s, _ in TRUTH[cid][attr] if D(s) <= END), default=T0)
                faults.append({"fault_id": nid("F"), "client_id": cid, "doc_type": "book_summary", "doc_id": line["line_id"], "date": END.isoformat(),
                               "fault_type": FAULT_BY_ATTR[attr], "attribute": attr, "claimed": val, "truth": t,
                               "evidence": evidence_for(cid, attr, since, END), "fix": FIXES[FAULT_BY_ATTR[attr]]})
            else:
                true_claims += 1
    prep_faults = [f for f in faults if f["doc_type"] == "prep"]
    by_type, by_client = {}, {}
    for f in faults:
        by_type[f["fault_type"]] = by_type.get(f["fault_type"], 0) + 1
        by_client[f["client_id"]] = by_client.get(f["client_id"], 0) + 1
    summary = {"seed": SEED, "n_clients": N_CLIENTS, "notes": len(NOTES), "preps": len(PREPS), "events": len(EVENTS), "market_events": len(MARKET),
               "claims": len(CLAIMS), "true_claims": true_claims, "faults": len(faults),
               "preps_with_a_fault": len({f["doc_id"] for f in prep_faults}),
               "preps_with_a_fault_ignoring_warnings": len({f["doc_id"] for f in prep_faults if f.get("level") != "warning"}),
               "faults_by_type": dict(sorted(by_type.items())), "faults_by_client": dict(sorted(by_client.items())),
               "severity1_drift_warnings": sum(1 for f in faults if f.get("level") == "warning"),
               "forbidden_promises": sum(1 for f in faults if f.get("severity") == 4)}
    return faults, summary

def expected_actions(faults):
    by_doc = {}
    for c in CLAIMS:
        by_doc.setdefault(c["doc_id"], []).append(c)
    human = sorted([n for n in NOTES if n["author"] in ("banker", "junior")], key=lambda n: (n["date"], n["note_id"]))
    missed = [f for f in faults if f["fault_type"] == "missed_change"]
    rows = []
    for cid in CL:
        prev_day = None
        for p in sorted([p for p in PREPS if p["client_id"] == cid], key=lambda p: p["date"]):
            day = D(p["date"])
            for attr in LABEL_ATTRS + ["retirement_date"]:
                stmts = [(n, c) for n in human if n["client_id"] == cid and D(n["date"]) <= day
                         for c in by_doc.get(n["note_id"], []) if c["attribute"] == attr]
                if not stmts:
                    continue
                n, c = stmts[-1]
                t = truth_at(cid, attr, day)
                if c["value"] == t:
                    continue
                since = max((s for s, _ in TRUTH[cid][attr] if D(s) <= day), default=T0)
                joint = len(CL[cid]["accounts"][0]["holders"]) > 1
                action = "brief_both" if attr == "decision_maker" and joint else "ask"
                evidence = evidence_for(cid, attr, since, day)
                if action == "brief_both":
                    evidence = [x["note_id"] for x, y in stmts if y["value"] == t] + evidence
                rows.append({"prep_id": p["prep_id"], "client_id": cid, "date": p["date"], "attribute": attr, "action": action,
                             "evidence": evidence, "reason": f"last stated {attr} = {c['value']} in {n['author']} note {n['note_id']} ({n['date']}); the truth changed on {since}"})
            for e in life_events_due(cid, day):
                rows.append({"prep_id": p["prep_id"], "client_id": cid, "date": p["date"], "attribute": "life_event", "action": "ask",
                             "evidence": [e["event_id"]], "reason": f"{e['type']} on {e['date']}; nothing in the file mentions it"})
            for f in missed:
                fd = D(f["date"])
                if f["client_id"] == cid and fd <= day and (prev_day is None or fd > prev_day):
                    rows.append({"prep_id": p["prep_id"], "client_id": cid, "date": p["date"], "attribute": f["truth"][0], "action": "flag",
                                 "note_id": f["doc_id"], "evidence": f["evidence"],
                                 "reason": f"junior note {f['doc_id']} ({f['date']}) says No changes; activity since the last contact says otherwise"})
            prev_day = day
    rows.sort(key=lambda r: (r["date"], r["prep_id"], r["action"]))
    return rows

# ---------------------------------------------------------------- expiry reference (hidden): does the expiry length matter?
EXPIRY_TYPES = {"risk_attitude": "behavioural", "contact_preference": "contact", "investing_style": "stated"}
def expiry_reference(n_by_type):
    """A reference policy, NOT the harness: keep the latest banker/junior statement while it is not expired
    (age <= N days), unless strong evidence supersedes it (held fixed for every N: a risk questionnaire result,
    or single-stock trades for index_only). Once expired: risk falls back to the client's oldest statement
    (the long-run stance), contact to phone_ok, investing style to unknown. Returns faults per split."""
    by_doc = {}
    for c in CLAIMS:
        by_doc.setdefault(c["doc_id"], []).append(c)
    human = sorted([n for n in NOTES if n["author"] in ("banker", "junior")], key=lambda n: (n["date"], n["note_id"]))
    out = {"tune": 0, "test": 0}
    for p in PREPS:
        cid, day = p["client_id"], D(p["date"])
        split = STORY[cid]["split"]
        for attr, typ in EXPIRY_TYPES.items():
            N = n_by_type.get(typ)
            stmts = [(n, c) for n in human if n["client_id"] == cid and D(n["date"]) <= day
                     for c in by_doc.get(n["note_id"], []) if c["attribute"] == attr]
            if stmts:
                n, c = stmts[-1]
                val, sd = c["value"], D(n["date"])
                if attr == "risk_attitude":
                    q = [e for e in events_between(cid, sd, day, {"tool_used"}) if e.get("tool") == "risk questionnaire"]
                    if q:
                        val = q[-1]["result"]
                    elif N is not None and (day - sd).days > N:
                        val = stmts[0][1]["value"]
                elif attr == "investing_style":
                    if val == "index_only" and [e for e in events_between(cid, sd, day, {"trade_buy"}) if e.get("instrument_type") == "single_stock"]:
                        val = "index_plus_single_stocks"
                    elif N is not None and (day - sd).days > N:
                        val = None
                else:
                    if N is not None and (day - sd).days > N:
                        val = "phone_ok"
            else:
                val = "phone_ok" if attr == "contact_preference" else None
            if val != truth_at(cid, attr, day):
                out[split] += 1
    return out

def expiry_sweep():
    grid = [15, 30, 45, 60, 90, 120, 150, 180, None]
    res = {"rule": EXPIRY_RULE, "by_type": {}, "one_setting_for_everything": {}}
    for typ in EXPIRY_TYPES.values():
        res["by_type"][typ] = {("never" if n is None else str(n)): expiry_reference({typ: n, **{t: None for t in EXPIRY_TYPES.values() if t != typ}}) for n in grid}
    for n in grid:
        res["one_setting_for_everything"]["never" if n is None else str(n)] = expiry_reference({t: n for t in EXPIRY_TYPES.values()})
    tot = lambda r: r["tune"] + r["test"]
    best = {}
    for typ, row in res["by_type"].items():   # choose on the tune split only, report test too
        k = min(row, key=lambda x: (row[x]["tune"], grid.index(None if x == "never" else int(x))))
        best[typ] = None if k == "never" else int(k)
    res["best_per_type_chosen_on_tune"] = {t: ("never" if v is None else v) for t, v in best.items()}
    res["best_per_type_result"] = expiry_reference(best)
    one = res["one_setting_for_everything"]
    res["best_single_setting"] = min(one, key=lambda x: (tot(one[x]), x))
    beh = res["by_type"]["behavioural"]
    res["done_when"] = {
        "60_vs_120_differ": tot(beh["60"]) != tot(beh["120"]),
        "never_is_not_best": tot(beh["never"]) > min(tot(v) for v in beh.values()),
        "30_days_is_not_best": tot(beh["30"]) > min(tot(v) for v in beh.values()),
        "per_type_beats_one_setting": tot(res["best_per_type_result"]) < min(tot(v) for v in one.values()),
    }
    return res

# ---------------------------------------------------------------- readable view
def cabinet_view(faults):
    fault_docs = {}
    for f in faults:
        fault_docs.setdefault(f["doc_id"], []).append(f["fault_type"])
    out = ["# Banker cabinet showcase (planted regularities; see hidden/planted.json)", "",
           f"One banker, {N_CLIENTS} clients, notes from before 2026, feed from Feb 2026, preps May to Aug 2026. SEED={SEED}.",
           "Lines marked **[fault: ...]** come from the hidden answer key; the harness never sees those marks.", ""]
    notable = {"trade_buy", "trade_sell", "tool_used", "email_to_banker", "beneficiary_changed", "spouse_removed", "employer_changed", "large_transfer_in"}
    for c in CLIENTS:
        cid, s = c["client_id"], STORY[c["client_id"]]
        out += [f"## {cid} {c['name']}  ({s['story']}, {s['split']})", ""]
        items = [(n["date"], 1, f"NOTE ({n['author']})", n["text"], n["note_id"]) for n in NOTES if n["client_id"] == cid]
        items += [(p["date"], 0, "PREP", p["text"], p["prep_id"]) for p in PREPS if p["client_id"] == cid]
        items += [(e["date"], 2, "FEED", e["type"] + " " + ", ".join(f"{k}={v}" for k, v in e.items() if k not in ("event_id", "client_id", "date", "type")), e["event_id"])
                  for e in EVENTS if e["client_id"] == cid and e["type"] in notable]
        for dt, _, kind, text, did in sorted(items, key=lambda x: (x[0], x[1])):
            mark = f" **[fault: {', '.join(sorted(set(fault_docs[did])))}]**" if did in fault_docs else ""
            if kind == "PREP":
                out += [f"**{dt} PREP {did}**{mark}", "", "```", text, "```", ""]
            else:
                out.append(f"- {dt} {kind} {did}: {text}{mark}")
        out.append("")
    return "\n".join(out)

def planted():
    """The regularities this set was built with, for checking what the loop learned. Answer side only."""
    rev = [(cid, s) for cid, s in STORY.items() if s["story"] == "sticky_revert"]
    lifetimes = sorted((D(s["revert"]) - D(s["sell"])).days for _, s in rev)
    return {
        "what": "Values the showcase generator planted. The harness never reads this file.",
        "cautious_stance_lasts_days": {"planted": REVERT_DAYS, "jitter": REVERT_JITTER, "observed_min": min(lifetimes, default=None),
                                       "observed_max": max(lifetimes, default=None),
                                       "note": "days from the sell-off to the quiet return; the banker's note comes the next day"},
        "style_change_buys_in_first_week": {"planted": STYLE_CHANGE_BUYS,
                                            "one_off_buyers_buy": 1,
                                            "note": "one single-stock buy is never a style change in this book"},
        "stories": {st: sum(1 for s in STORY.values() if s["story"] == st) for st in sorted({s["story"] for s in STORY.values()})},
        "splits": {sp: sum(1 for s in STORY.values() if s["split"] == sp) for sp in ("tune", "test")},
        "seed": SEED, "n_clients": N_CLIENTS, "plain_wording": PLAIN_WORDING,
    }

def dump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)

def build():
    build_clients(); build_truth_and_stories(); background_feed(); market_feed()
    EVENTS.sort(key=lambda e: (e["date"], e["event_id"]))
    backstory(); simulate(build_contacts())
    NOTES.sort(key=lambda n: (n["date"], n["note_id"]))
    book = build_book()
    faults, summary = answer_key(book)
    return book, faults, summary, expected_actions(faults), expiry_sweep()

def main():
    book, faults, summary, expected, sweep = build()
    for n in NOTES:
        n.pop("_prev_contact", None)
    dump(f"{OUT}/visible/clients.json", [dict(c, holdings=[{"instrument": k, "instrument_type": INSTR[k], "amount": v}
                                                           for k, v in START_HOLDINGS[c["client_id"]].items()]) for c in CLIENTS])
    dump(f"{OUT}/visible/notes.json", NOTES)
    dump(f"{OUT}/visible/events.json", EVENTS)
    dump(f"{OUT}/visible/market.json", MARKET)
    dump(f"{OUT}/visible/approved_language.json", APPROVED)
    dump(f"{OUT}/visible/reference_data.json", REFERENCE)
    dump(f"{OUT}/visible/book_summary.json", {k: v for k, v in book.items() if k != "lines"} | {"lines": [{"line_id": l["line_id"], "text": l["text"]} for l in book["lines"]]})
    dump(f"{OUT}/visible/preps_baseline.json", PREPS)
    dump(f"{OUT}/visible/vocabulary.json", VOCABULARY)
    truth_out = {}
    for cid, attrs in TRUTH.items():
        truth_out[cid] = {a: ([{"since": s, "value": v} for s, v in tl] if a != "life_events" else tl) for a, tl in attrs.items()}
    dump(f"{OUT}/hidden/truth.json", truth_out)
    dump(f"{OUT}/hidden/claims.json", CLAIMS)
    dump(f"{OUT}/hidden/answer_key.json", {"summary": summary, "faults": faults, "expected": expected})
    dump(f"{OUT}/hidden/planted.json", planted())
    dump(f"{OUT}/hidden/splits.json", [{"client_id": cid, "split": s["split"], "story": s["story"]} for cid, s in STORY.items()])
    dump(f"{OUT}/hidden/expiry_reference.json", sweep)
    with open(f"{OUT}/cabinet_view.md", "w") as f:
        f.write(cabinet_view(faults))
    print(json.dumps(summary, indent=2))

if __name__ == "__main__":
    main()
