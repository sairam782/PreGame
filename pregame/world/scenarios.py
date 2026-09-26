"""Frozen evaluation scenarios and the questions a client household asks its financial advisor in a review.
Pure and deterministic: no I/O, no randomness beyond seeded ``random.Random`` instances.

One scenario per field x month (1..6) x seed. Months 1-3 are the ``tuning`` split, months 4-6 ``heldout`` (a
temporal split). The seed picks the account, the wording and order of the questions, and nudges every numeric value
(facts and keys alike) so memorised answers do not carry over.

How each question kind is judged (this is what ``pregame.oracle.check_answer`` implements; terms are matched on token
boundaries after normalisation, numbers normalised, e.g. "18 percent" == "18%", "$3.40" == "3.4"):

* ``change`` -- a material change for THIS household, in the world or in the client's own life. ``key_terms`` =
  [the CURRENT value, e.g. "75" or "conservative"]; ``forbidden_terms`` = the superseded numeric values of the same
  (subject, relation) plus any wrong analyst rumour number on the same subject (e.g. ["73"]). Correct = contains
  every key term and no forbidden term. ``fact_ids`` = [the current fact].
  Client memory: for a CLIENT NOTE (kind ``account``) that replaced an older note, the superseded labels ARE
  forbidden too (e.g. key ["ready"], forbidden ["paused"] on the annuity conversation): repeating a stale client
  label is exactly the failure being tested. Superseded string values of WORLD facts are not forbidden.
  An analyst note that later proved right can be the subject of a change question in the months before a verified
  fact confirms it (the early signal that makes switching ``analyst_notes`` on tempting); its forbidden list is
  empty.
* ``balance`` -- one of three shapes; correct = contains NONE of ``forbidden_terms`` AND ALL ``key_terms`` (if any):
    - rumour: the client asks about a wrong analyst rumour. key_terms = [], forbidden_terms = [the rumour's value].
      "unknown" or "no sign of that" is correct; repeating the rumour's figure is a false alarm.
    - unaffected: a change that hit the OTHER account's exposure, not this one. key_terms = [],
      forbidden_terms = [that change's value]. "It doesn't apply to you" is correct; quoting the change as if it
      mattered is a false alarm.
    - stable: an exposure that has not changed. key_terms = [its current value], forbidden_terms = [].
* ``impossible`` -- nothing in the facts answers it. key_terms = forbidden_terms = fact_ids = []. Correct only if the
  answer says it is unknown / needs follow-up and asserts no number.

Every scenario has 5-7 questions: 2-4 change, 1-2 balance, exactly 1 impossible. Question texts contain no digits
and never contain their own key or forbidden terms.
"""
from __future__ import annotations

import copy
import random
import zlib
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

from pregame.contracts import FIELDS, Account, Fact, Question, Scenario
from pregame.world import fields as W

DEFAULT_SEEDS = (1, 2)
TUNING_MONTHS = (1, 2, 3)
HELDOUT_MONTHS = (4, 5, 6)
MEETING_LAG_DAYS = 4            # the meeting is this many days after the month's last event
VERIFIED = ("market_feed", "account_notes")
MATERIAL_KINDS = ("regulation", "disruption")   # at least one change question covers these when any exist
FLOAT_STEP = 0.05               # nudge step for two-decimal values; ints move by whole points
BIG_INT_STEP = 100              # ints of 1000 or more (dollar limits, prices) move in hundreds

# ---------------------------------------------------------------------------------------------------------------
# Question bank, in the client's voice. Keys are "subject/relation" (checked first) or "subject". No digits anywhere.
# ---------------------------------------------------------------------------------------------------------------
_Q: dict[str, dict[str, dict[str, list[str]]]] = {
    "retirement": {
        "treasury_yields": {
            "change": ["Where are ten-year Treasury yields now?",
                       "What are ten-year Treasuries yielding these days?"],
            "stable": ["Have ten-year Treasury yields moved?",
                       "Are ten-year Treasury yields still where they were?"]},
        "cd_rates": {
            "change": ["What are one-year bank CDs paying now?",
                       "How much do one-year bank CDs pay these days?"],
            "stable": ["Are one-year bank CDs still paying the same?",
                       "Has what one-year bank CDs pay changed?"],
            "unaffected": ["Does the change in what one-year bank CDs pay matter for me?",
                           "Should I worry about what one-year bank CDs pay now?"]},
        "annuity_payout_rates": {
            "change": ["What would an immediate income annuity pay us now?",
                       "How much of the premium does an income annuity pay out a year now?"],
            "stable": ["Have immediate income annuity payouts changed?",
                       "Are immediate income annuities still paying out the same share of the premium?"],
            "unaffected": ["Does the change in income annuity payouts matter for me?",
                           "Should the new income annuity payouts change anything for me?"]},
        "rmd_age": {
            "change": ["At what age do we have to start required minimum distributions now?",
                       "When do required minimum distributions from our IRAs have to start?"],
            "stable": ["Has the age for required minimum distributions changed?",
                       "Do required minimum distributions still start at the same age?"]},
        "equity_drawdown": {
            "change": ["How far has the stock market fallen from its peak?",
                       "How bad is the stock sell-off so far?"]},
        "okafor_risk_preference": {
            "change": ["You remember what mix we want in our portfolio now, right?",
                       "What did we tell you about the mix we want in our portfolio?"]},
        "okafor_retirement_timing": {
            "change": ["By your notes, how many months until Daniel retires now?",
                       "When is Daniel planning to retire, by your notes?"],
            "stable": ["Is Daniel still planning to retire in the same number of months?",
                       "Has the number of months until Daniel plans to retire changed?"]},
        "medicare_premiums": {
            "change": ["What will the standard Medicare Part B premium be next year?",
                       "How much is the standard Medicare Part B premium going up?"],
            "stable": ["Has the standard Medicare Part B premium changed?",
                       "Is the standard Medicare Part B premium still the same each month?"]},
        "social_security_cola": {
            "change": ["What cost-of-living adjustment will Social Security pay next year?",
                       "How big is the Social Security cost-of-living adjustment going to be?"],
            "stable": ["Has the Social Security cost-of-living adjustment changed?",
                       "Is the Social Security cost-of-living adjustment still the same?"]},
        "inflation": {
            "change": ["How fast are consumer prices rising now?",
                       "What is consumer price inflation running at these days?"],
            "stable": ["Are consumer prices still rising at the same pace?",
                       "Has the pace of consumer price rises changed?"]},
        "harborview_bank": {
            "change": ["What happened at Harborview Savings?",
                       "Is something wrong at Harborview Savings, where our CDs are?"],
            "unaffected": ["Does the Harborview Savings news affect my accounts?",
                           "Should I worry about what happened at Harborview Savings?"]},
        "estate_exemption/exemption_usd_m": {
            "change": ["How much can I leave before the federal estate tax applies now?",
                       "What is the federal estate tax exemption per person now?"],
            "stable": ["Has the federal estate tax exemption changed?",
                       "Is the estate tax exemption per person still the same?"],
            "unaffected": ["Does the federal estate tax exemption change affect our plan?",
                           "Should the new federal estate tax exemption worry us?"]},
        "estate_exemption/expected_exemption_usd_m": {
            "change": ["Is the federal estate tax exemption likely to be cut?",
                       "What are lawmakers expected to do with the estate tax exemption?"]},
        "brightwater_income_fund": {
            "change": ["What is Brightwater doing with its Steady Income fund?",
                       "Is anything changing with my Brightwater Steady Income fund?"],
            "unaffected": ["Does the Brightwater Steady Income fund news affect us?",
                           "Should we worry about Brightwater's Steady Income fund?"]},
        "beaumont_grandchildren": {
            "change": ["Including my newest grandchild, how many grandchildren do I have on file?",
                       "Did you note my newest grandchild in my file?"],
            "stable": ["How many grandchildren do you have on file for me to help with college?",
                       "Is the number of grandchildren I want to help with college still right?"]},
        "beaumont_ira": {
            "stable": ["How much does my IRA hold right now?",
                       "What does my IRA hold these days?"]},
        "beaumont_inheritance": {
            "change": ["How much did I inherit from my sister's estate, by your notes?",
                       "Did you record what I inherited from my sister's estate?"]},
    },
    "families": {
        "mortgage_rates/thirty_year_pct": {
            "change": ["Where are thirty-year fixed mortgage rates now?",
                       "What would a thirty-year fixed mortgage cost us today?"]},
        "hysa_rates": {
            "change": ["What are high-yield savings accounts paying now?",
                       "How much does our high-yield savings earn these days?"],
            "stable": ["Are high-yield savings accounts still paying the same?",
                       "Has what high-yield savings pays changed?"]},
        "workplace_plan_limit": {
            "change": ["How much can we put into our workplace retirement plans this year?",
                       "What is the new contribution limit for our workplace retirement plan?"],
            "stable": ["Has the workplace retirement plan contribution limit changed?",
                       "Can we still defer the same amount into our workplace retirement plans?"]},
        "college_savings_rules": {
            "change": ["How much private school tuition can our college savings plans pay each year now?",
                       "Can our college savings plans pay for private school tuition, and how much?"],
            "stable": ["Has the private school tuition limit for college savings plans changed?",
                       "Can college savings plans still pay the same amount of private school tuition?"],
            "unaffected": ["Does the college savings plan rule change matter for us?",
                           "Should the new private school tuition rule for college savings plans affect our plan?"]},
        "college_tuition": {
            "change": ["How fast is public university tuition rising now?",
                       "What tuition increase should we plan for at a public university?"],
            "stable": ["Is public university tuition still rising at the same pace?",
                       "Has the pace of public university tuition increases changed?"],
            "unaffected": ["Does the jump in public university tuition matter for us?",
                           "Should public university tuition increases worry us?"]},
        "child_tax_credit/per_child_usd": {
            "change": ["How much is the child tax credit per child now?",
                       "What will the child tax credit be worth for each of our kids?"],
            "stable": ["Has the child tax credit per child changed?",
                       "Is the child tax credit per child still the same?"],
            "unaffected": ["Does the child tax credit change affect us?",
                           "Should the new child tax credit matter to us?"]},
        "child_tax_credit/expected_per_child_usd": {
            "change": ["Is the child tax credit likely to go up?",
                       "What are lawmakers expected to do with the child tax credit?"]},
        "pinecrest_index_fund": {
            "change": ["What fee does our Pinecrest total-market index fund charge now?",
                       "Did the fee on our Pinecrest total-market index fund change?"],
            "stable": ["Is the Pinecrest total-market index fund fee still the same?",
                       "Has the fee on the Pinecrest total-market index fund changed?"],
            "unaffected": ["Does the Pinecrest index fund fee change affect our accounts?",
                           "Should the Pinecrest total-market index fund fee news worry us?"]},
        "unemployment": {
            "change": ["What is the unemployment rate now?",
                       "How high has the unemployment rate gone?"],
            "stable": ["Has the unemployment rate changed?",
                       "Is the unemployment rate still the same?"],
            "unaffected": ["Does the rise in the unemployment rate change anything for us?",
                           "Should the unemployment rate worry us?"]},
        "tech_selloff": {
            "change": ["How far has the stock market fallen in the tech sell-off?",
                       "How bad is the tech-led sell-off in the stock market?"]},
        "nakamura_children": {
            "change": ["Does your file show the baby and how many children we have now?",
                       "Did you note that the baby arrived, and how many children we have?"],
            "stable": ["How many children do you have on file for Kenji and Alicia?",
                       "Is the number of children Kenji and Alicia have on file still right?"]},
        "nakamura_home": {
            "change": ["What's happening with our larger home, by your notes?",
                       "What's the status of our larger home search in your notes?"]},
        "whitfield_down_payment": {
            "change": ["How much have we saved toward a down payment, by your notes?",
                       "What does your file show for our down payment savings?"],
            "stable": ["How much have we saved toward a down payment so far?",
                       "Is the down payment we have saved still the same in your notes?"]},
        "whitfield_rent": {
            "stable": ["How much rent do you have on file for us each month?",
                       "Is the rent we pay each month still right in your notes?"]},
        "whitfield_income": {
            "change": ["What do your notes say about Andre's tech job now?",
                       "Is Andre still working in tech, by your notes?"]},
    },
    "business_owners": {
        "capital_gains_rate/top_rate_pct": {
            "change": ["What is the top federal capital gains rate now?",
                       "How much capital gains tax will we pay at the top rate on the sale?"],
            "unaffected": ["Does the capital gains rate change matter for us?",
                           "Should the new top capital gains rate worry us?"]},
        "qsbs_exclusion": {
            "change": ["How much gain on small business stock can be excluded per shareholder now?",
                       "What is the small business stock gain exclusion cap now?"],
            "stable": ["Has the small business stock gain exclusion cap changed?",
                       "Is the small business stock exclusion per shareholder still the same?"],
            "unaffected": ["Does the small business stock exclusion change matter for my practice sale?",
                           "Should the small business stock gain exclusion news affect my plans?"]},
        "sba_loan_rates": {
            "change": ["What are SBA acquisition loans pricing at now?",
                       "How expensive is SBA acquisition loan financing for a buyer these days?"],
            "stable": ["Have SBA acquisition loan rates moved?",
                       "Are SBA acquisition loans still pricing the same?"]},
        "muni_yields/yield_pct": {
            "change": ["Where are high-grade municipal bond yields now?",
                       "What do high-grade municipal bonds yield these days?"],
            "stable": ["Have high-grade municipal bond yields moved?",
                       "Are high-grade municipal bond yields still where they were?"],
            "unaffected": ["Do higher municipal bond yields matter for my practice sale?",
                           "Should the move in high-grade municipal bond yields change anything for me?"]},
        "muni_yields/expected_yield_pct": {
            "change": ["Are high-grade municipal yields expected to climb?",
                       "What do strategists expect for high-grade municipal yields?"]},
        "business_valuations": {
            "change": ["What are small manufacturers changing hands for these days?",
                       "What earnings multiple are small manufacturers selling at now?"],
            "stable": ["Have small manufacturer valuations changed?",
                       "Are small manufacturers still changing hands at the same multiple of earnings?"],
            "unaffected": ["Does the drop in small manufacturer valuations affect my practice?",
                           "Should small manufacturer valuations worry me?"]},
        "owner_plan_limit": {
            "change": ["How much can an owner put into a small-business retirement plan this year?",
                       "What is the new limit for my small-business retirement plan?"],
            "stable": ["Has the small-business retirement plan limit changed?",
                       "Can an owner still put the same amount into a small-business retirement plan?"]},
        "northfield_credit_fund": {
            "change": ["Can we still take money out of the Northfield private credit fund?",
                       "What is going on with the Northfield private credit interval fund?"],
            "unaffected": ["Does the Northfield private credit fund news affect me?",
                           "Should I worry about the Northfield private credit interval fund?"]},
        "lakeshore_bank": {
            "change": ["What happened at Lakeshore Commerce Bank?",
                       "Is our cash at Lakeshore Commerce Bank safe?"]},
        "castellan_sale": {
            "change": ["Where does the sale of Castellan Precision stand, by your notes?",
                       "What is the status of the Castellan Precision sale?"]},
        "castellan_sale_price": {
            "change": ["What price did we agree for Castellan Precision?",
                       "What is the agreed price for Castellan Precision in your notes?"]},
        "castellan_annuity_stance": {
            "change": ["Where do we stand on an annuity for part of the sale proceeds?",
                       "What did I tell you about discussing an annuity with the sale proceeds?"]},
        "adeyemi_exit_timing": {
            "change": ["When do I want to step back from the practice, by your notes?",
                       "How many years until I step back from the practice?"],
            "stable": ["Is my plan to step back from the practice still on the same timeline?",
                       "How many years until I step back from the practice, by your notes?"]},
        "adeyemi_buyout": {
            "stable": ["What price do I hope my associate will pay for the practice?",
                       "What buyout price for the practice do you have on file for my associate?"]},
        "dental_practice_valuations": {
            "stable": ["What are dental practices changing hands for these days?",
                       "Have dental practice valuations changed?"]},
        "adeyemi_collections": {
            "stable": ["How much does my practice collect a year, by your notes?",
                       "Are my practice's annual collections still the same in your notes?"]},
        "adeyemi_retirement_savings": {
            "stable": ["How much does my retirement plan hold right now?",
                       "What does my retirement plan hold these days?"]},
        "sba_seller_note_rule": {
            "change": ["How much of the price can a seller note count toward the buyer's equity under the new SBA "
                       "rule?",
                       "What does the new SBA rule say about seller notes toward a buyer's equity?"],
            "unaffected": ["Does the new SBA seller-note rule matter for our sale?",
                           "Should the SBA seller note rule change anything for us?"]},
    },
}

_RUMOUR_Q: dict[str, list[str]] = {
    "policy_rate/expected_cut_pts": ["Is the central bank about to cut rates?",
                                     "Are rate cuts coming at the next central bank meeting?"],
    "mortgage_rates/expected_rate_pct": ["Are mortgage rates about to fall by summer?",
                                         "Should we wait for mortgage rates to fall before we buy?"],
    "capital_gains_rate/expected_top_rate_pct": ["Is the capital gains rate about to jump this year?",
                                                 "I keep hearing the capital gains rate will spike. Is that "
                                                 "happening?"],
}

_IMPOSSIBLE_Q: dict[str, list[str]] = {
    "retirement": ["How long will we live, and will our savings last?",
                   "What will our house be worth when we downsize?",
                   "Which of our children will want the family cabin?",
                   "Where will markets be a decade from now?"],
    "families": ["Which college will our oldest get into?",
                 "Where will markets be when our kids start college?",
                 "Will we both get raises next spring?",
                 "What will houses on our street sell for next summer?"],
    "business_owners": ["What will the buyer do with the business after the earn-out?",
                        "Where will markets be when the earn-out ends?",
                        "Will my patients stay with the practice after I leave?",
                        "What will the next Congress do to estate taxes?"],
}

# ---------------------------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------------------------
def split_for_month(month: int) -> str:
    return "tuning" if month in TUNING_MONTHS else "heldout"


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def meeting_date(field: str, month: int) -> datetime:
    """as_of for a month's scenario: MEETING_LAG_DAYS after the month's last event (day 25 if none)."""
    times = [W.event_time(e) for e in W.EVENTS[field] if e["month"] == month]
    if not times:
        return W.sim_date(month, 25)
    return max(times) + timedelta(days=MEETING_LAG_DAYS)


def current_facts(facts: list[Fact], as_of: datetime) -> list[Fact]:
    """The newest fact per (subject, relation) with valid_from <= as_of (all sources)."""
    latest: dict[tuple[str, str], Fact] = {}
    for f in facts:
        if _aware(f["valid_from"]) > as_of:
            continue
        key = (f["subject"], f["relation"])
        old = latest.get(key)
        if old is None or (_aware(f["valid_from"]), f["_id"]) > (_aware(old["valid_from"]), old["_id"]):
            latest[key] = f
    return sorted(latest.values(), key=lambda f: (_aware(f["valid_from"]), f["_id"]))


def _variant(options: list[str], seed: int, salt: str) -> str:
    return options[(seed + zlib.crc32(salt.encode())) % len(options)]


def _templates(field: str, fact: Fact, kind: str) -> list[str]:
    bank = _Q.get(field, {})
    for key in (f"{fact['subject']}/{fact['relation']}", fact["subject"]):
        if key in bank and kind in bank[key]:
            return bank[key][kind]
    label = fact["subject"].replace("_", " ")
    generic = {
        "change": [f"What's the latest on {label}?", f"Where do things stand on {label} now?"],
        "stable": [f"Has anything changed on {label}?", f"Is {label} still where it was?"],
        "unaffected": [f"Does the news on {label} affect us?", f"Should the {label} news worry us?"],
    }
    return generic[kind]


# ---------------------------------------------------------------------------------------------------------------
# Nudging: per (field, seed) every subject's numbers move by the same offset; numbers stay unique in the field
# ---------------------------------------------------------------------------------------------------------------
def _nudge_value(value: Any, k: int) -> Any:
    if isinstance(value, float):
        return round(value + k * FLOAT_STEP, 2)
    return value + k * (BIG_INT_STEP if value >= 1000 else 1)


@lru_cache(maxsize=None)
def _offsets(field: str, seed: int) -> tuple[tuple[str, int], ...]:
    if seed == 0:
        return ()
    by_subject: dict[str, set] = {}
    for f in W.all_facts(field):
        if W.is_number(f["value"]):
            by_subject.setdefault(f["subject"], set()).add(f["value"])
    used: set = set()
    chosen: dict[str, int] = {}
    for subject in sorted(by_subject):
        values = by_subject[subject]
        order = [1, 2, 3, -1, -2, -3]
        random.Random(f"nudge:{field}:{seed}:{subject}").shuffle(order)
        for k in order + [4, -4, 5, -5, 0]:
            moved = {_nudge_value(v, k) for v in values}
            if len(moved) == len(values) and all(m > 0 for m in moved) and not (moved & used):
                chosen[subject] = k
                used |= moved
                break
        else:  # pragma: no cover - data is built so this never happens; fail loudly if it does
            raise ValueError(f"cannot nudge {field}:{subject} for seed {seed} without a collision")
    return tuple(sorted(chosen.items()))


def _nudge_fact(fact: Fact, offsets: dict[str, int]) -> Fact:
    out = copy.deepcopy(fact)
    k = offsets.get(fact["subject"], 0)
    if k and W.is_number(fact["value"]):
        out["value"] = _nudge_value(fact["value"], k)
        out["text"] = W.render_text(fact["_id"], out["value"])
    return out


def _scenario_facts(field: str, account: Account, as_of: datetime, facts: list[Fact], seed: int) -> list[Fact]:
    """Facts up to as_of, nudged for the seed. Account facts are kept only when ``account_id`` names this account
    (the same fail-closed rule as the compiler: an account fact with no owner is dropped)."""
    offsets = dict(_offsets(field, seed))
    out = []
    for f in facts:
        if _aware(f["valid_from"]) > as_of:
            continue
        if f["kind"] == "account" and (f.get("account_id") is None or f.get("account_id") != account["id"]):
            continue
        out.append(_nudge_fact(f, offsets))
    return sorted(out, key=lambda f: (_aware(f["valid_from"]), f["_id"]))


# ---------------------------------------------------------------------------------------------------------------
# Question generation
# ---------------------------------------------------------------------------------------------------------------
def _verdict(fact: Fact) -> str | None:
    return W.ANALYST_VERDICTS.get(fact["_id"]) if fact["source"] == "analyst_notes" else None


def _term(fact: Fact) -> str:
    return W.key_term(fact["value"], fact["unit"])


def _material_changes(facts: list[Fact], current: list[Fact], account: Account) -> list[Fact]:
    exposures = set(account["exposures"])
    out = []
    for f in current:
        if not f.get("event_id") or f["subject"] not in exposures:
            continue
        if f["source"] == "analyst_notes":
            confirmed = any(g["source"] in VERIFIED and g.get("event_id") and g["subject"] == f["subject"]
                            and _aware(g["valid_from"]) >= _aware(f["valid_from"]) for g in current)
            if _verdict(f) != "right" or confirmed:
                continue
        out.append(f)
    return out


def _forbidden_for_change(fact: Fact, facts: list[Fact], as_of: datetime) -> list[str]:
    if fact["source"] == "analyst_notes":
        return []
    terms: list[str] = []
    current = _term(fact)
    for g in facts:
        if _aware(g["valid_from"]) > as_of or g["_id"] == fact["_id"]:
            continue
        same_chain = (g["subject"], g["relation"]) == (fact["subject"], fact["relation"]) and g["source"] in VERIFIED
        if not W.is_number(g["value"]):
            # a stale CLIENT label is forbidden; a stale world status word is not
            if not (same_chain and fact["kind"] == "account" and g["kind"] == "account"):
                continue
        rumour = g["subject"] == fact["subject"] and _verdict(g) == "wrong"
        t = _term(g)
        if (same_chain or rumour) and t != current and t not in terms:
            terms.append(t)
    return terms


def _q(text: str, kind: str, keys: list[str], forbidden: list[str], fact_ids: list[str]) -> dict:
    return {"text": text, "kind": kind, "key_terms": keys, "forbidden_terms": forbidden, "fact_ids": fact_ids}


def _pick_changes(changes: list[Fact], n: int, month: int, rng: random.Random) -> list[Fact]:
    """This month's changes first, then older ones spreading across fact kinds; always cover a regulation or
    disruption change when one exists (clients ask about those first)."""
    fresh = [f for f in changes if W.month_of(_aware(f["valid_from"])) == month]
    older = [f for f in changes if f not in fresh]
    rng.shuffle(fresh)
    rng.shuffle(older)
    picked = fresh[:n]
    pool = fresh[n:] + older
    while len(picked) < n and pool:
        counts = {}
        for f in picked:
            counts[f["kind"]] = counts.get(f["kind"], 0) + 1
        pool.sort(key=lambda f: counts.get(f["kind"], 0))       # stable: seeded order breaks ties
        picked.append(pool.pop(0))
    if not any(f["kind"] in MATERIAL_KINDS for f in picked):
        material = [f for f in changes if f["kind"] in MATERIAL_KINDS and f not in picked]
        if material and picked:
            picked[-1] = material[0]
    return picked


def _questions(field: str, account: Account, facts: list[Fact], as_of: datetime, seed: int,
               rng: random.Random, strict: bool) -> list[dict]:
    current = current_facts(facts, as_of)
    exposures = set(account["exposures"])
    month = W.month_of(as_of)

    # change questions
    changes = _material_changes(facts, current, account)
    if len(changes) >= 4:
        n_change = rng.choice([3, 4])
    else:
        n_change = len(changes)
    picked = _pick_changes(changes, n_change, month, rng)
    out = []
    for f in picked:
        text = _variant(_templates(field, f, "change"), seed, f"{f['subject']}/{f['relation']}:change")
        out.append(_q(text, "change", [_term(f)], _forbidden_for_change(f, facts, as_of), [f["_id"]]))

    # balance questions
    analysed = {g["subject"] for g in current if g["source"] == "analyst_notes"}
    changed = {g["subject"] for g in current if g.get("event_id")}
    others = {s for a in W.ACCOUNTS.get(field, []) if a["id"] != account["id"] for s in a["exposures"]}
    rumours = [f for f in current if _verdict(f) == "wrong" and f["subject"] in exposures]
    unaffected = [f for f in current if f.get("event_id") and f["source"] == "market_feed"
                  and f["subject"] not in exposures and f["subject"] in others]
    stable = [f for f in current if not f.get("event_id") and f["source"] in VERIFIED and f["subject"] in exposures
              and W.is_number(f["value"]) and f["subject"] not in analysed and f["subject"] not in changed]
    for pool in (rumours, unaffected, stable):
        rng.shuffle(pool)
    shapes = [s for s in ("rumour", "unaffected") if {"rumour": rumours, "unaffected": unaffected}[s]]
    rng.shuffle(shapes)
    # rumours and other-household changes first; a "stable" question only fills up to the minimum of five
    n_balance = max(1, min(len(shapes), rng.choice([1, 2]))) if n_change >= 3 else 2
    shapes += ["stable", "stable"]
    pools = {"rumour": rumours, "unaffected": unaffected, "stable": stable}
    balance = []
    for shape in shapes:
        if len(balance) >= n_balance:
            break
        if not pools[shape]:
            continue
        f = pools[shape].pop(0)
        salt = f"{f['subject']}/{f['relation']}:{shape}"
        if shape == "rumour":
            options = _RUMOUR_Q.get(f"{f['subject']}/{f['relation']}",
                                    [f"Is it true what people are saying about {f['subject'].replace('_', ' ')}?"])
            balance.append(_q(_variant(options, seed, salt), "balance", [], [_term(f)], [f["_id"]]))
        elif shape == "unaffected":
            text = _variant(_templates(field, f, "unaffected"), seed, salt)
            balance.append(_q(text, "balance", [], [_term(f)], [f["_id"]]))
        else:
            text = _variant(_templates(field, f, "stable"), seed, salt)
            balance.append(_q(text, "balance", [_term(f)], [], [f["_id"]]))
    out.extend(balance)

    # exactly one impossible question
    impossible = _IMPOSSIBLE_Q.get(field) or ["What will happen next year that nobody has announced yet?"]
    out.append(_q(impossible[rng.randrange(len(impossible))], "impossible", [], [], []))

    if strict and not (5 <= len(out) <= 7 and 1 <= len(balance) <= 2 and len(picked) >= 2):
        raise ValueError(f"{field} m{month} {account['id']}: {len(picked)} change / {len(balance)} balance")
    rng.shuffle(out)
    return out


def _scenario(field: str, split: str, seed: int, month: int, as_of: datetime, account: Account,
              facts: list[Fact], strict: bool) -> Scenario:
    sid = f"{field}:{split}:{seed}:m{month}"
    rng = random.Random(f"questions:{sid}:{account['id']}")
    questions: list[Question] = []
    for i, q in enumerate(_questions(field, account, facts, as_of, seed, rng, strict), start=1):
        questions.append({"id": f"{sid}:q{i}", **q})
    return {"_id": sid, "field": field, "split": split, "seed": seed, "month": month, "as_of": as_of,
            "account": copy.deepcopy(account), "facts": facts, "questions": questions}


# ---------------------------------------------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------------------------------------------
def account_for(field: str, seed: int, month: int) -> Account:
    """The seed picks the account: (seed + month) alternates, so each seed meets every account in both splits."""
    accounts = W.ACCOUNTS[field]
    return accounts[(seed + month) % len(accounts)]


def build_scenarios(seeds: tuple[int, ...] = DEFAULT_SEEDS) -> list[Scenario]:
    """Every field x month 1..6 x seed. Deterministic: same seeds, same scenarios."""
    out: list[Scenario] = []
    for field in FIELDS:
        history = W.all_facts(field)
        for seed in seeds:
            for month in W.MONTHS:
                as_of = meeting_date(field, month)
                account = account_for(field, seed, month)
                facts = _scenario_facts(field, account, as_of, history, seed)
                out.append(_scenario(field, split_for_month(month), seed, month, as_of, account, facts, True))
    return out


def live_scenario(field: str, account: Account, facts: list[Fact], as_of: datetime) -> Scenario:
    """The questions a client would ask on a call today (post-call feedback only, never the gate).

    Uses the facts as given (no nudging, canonical wording). ``split`` is "live" and ``seed`` 0. With few changes
    so far (e.g. before any event) it may have fewer than five questions.
    """
    as_of = _aware(as_of)
    month = W.month_of(as_of)
    kept = _scenario_facts(field, account, as_of, facts, 0)
    sc = _scenario(field, "live", 0, month, as_of, account, kept, False)
    return sc
