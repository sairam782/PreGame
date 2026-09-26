"""Frozen evaluation scenarios and the client questions they ask. Pure and deterministic: no I/O, no randomness
beyond seeded ``random.Random`` instances.

One scenario per field x month (1..6) x seed. Months 1-3 are the ``tuning`` split, months 4-6 ``heldout`` (a
temporal split). The seed picks the account, the wording and order of the questions, and nudges every numeric value
(facts and keys alike) so memorised answers do not carry over.

How each question kind is judged (this is what ``pregame.oracle.check_answer`` implements; terms are matched on token
boundaries after normalisation, numbers normalised, e.g. "18 percent" == "18%", "$3.40" == "3.4"):

* ``change`` -- a material change for THIS account. ``key_terms`` = [the CURRENT value, e.g. "22%" or "strike"];
  ``forbidden_terms`` = the superseded numeric values of the same (subject, relation) plus any wrong analyst rumour
  number on the same subject (e.g. ["7%"]). Correct = contains every key term and no forbidden term. Superseded
  *string* values (e.g. "strike" -> "settled") are NOT forbidden, because a correct answer naturally says "the
  strike is settled". ``fact_ids`` = [the current fact].
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

# ---------------------------------------------------------------------------------------------------------------
# Question bank. Keys are "subject/relation" (checked first) or "subject". No digits anywhere.
# ---------------------------------------------------------------------------------------------------------------
_Q: dict[str, dict[str, dict[str, list[str]]]] = {
    "insurance": {
        "reinsurance_rates": {
            "change": ["Where did property reinsurance renewal pricing land this time?",
                       "How much did property catastrophe reinsurance rates move at the latest renewals?",
                       "What happened to the reinsurance rates behind our property program at renewal?"],
            "stable": ["Have property reinsurance renewal rates moved lately?",
                       "Is property catastrophe reinsurance pricing still where it was at renewal?"]},
        "casualty_reinsurance_rates": {
            "change": ["How did casualty reinsurance treaty renewals come in?",
                       "What are reinsurers charging on casualty treaty renewals now?"],
            "stable": ["Have casualty reinsurance treaty rates moved at renewal?",
                       "Is casualty reinsurance treaty pricing unchanged?"]},
        "property_rates": {
            "change": ["What increase should we expect on commercial property premiums at renewal?",
                       "How fast are commercial property premiums rising for manufacturers like us?"],
            "stable": ["Has commercial property premium pricing for manufacturers moved at renewal?",
                       "Are commercial property premiums still rising at the same pace for manufacturers?"],
            "unaffected": ["Does the jump in manufacturers' commercial property premiums hit our clinics?",
                           "Should manufacturers' property premium increases worry our clinics?"]},
        "casualty_rates": {
            "change": ["What is happening to general liability and commercial auto premiums?",
                       "How much are liability and commercial auto premiums rising at renewal now?"],
            "stable": ["Has general liability and commercial auto pricing changed at renewal?",
                       "Are liability and auto premiums still rising at the same pace?"]},
        "cyber_rates": {
            "change": ["What's happening to cyber premiums for healthcare buyers at renewal?",
                       "How much will cyber premiums for healthcare buyers like us rise?"],
            "stable": ["Have cyber premiums for healthcare buyers moved at renewal?",
                       "Are cyber premiums for healthcare buyers still rising at the same pace?"],
            "unaffected": ["Does the rise in healthcare cyber premiums touch our program?",
                           "Should the healthcare cyber premium increases worry us?"]},
        "texas_property_capacity": {
            "change": ["How many carriers are still actively quoting Texas manufacturing property?",
                       "How much carrier competition is left for manufacturing property in Texas?"],
            "stable": ["Are as many carriers still quoting manufacturing property in Texas?",
                       "Has carrier appetite for Texas manufacturing property changed?"],
            "unaffected": ["Does the shrinking number of Texas property carriers matter for our clinics?",
                           "Should fewer carriers quoting Texas manufacturing property worry us?"]},
        "halvard_texas_exit": {
            "change": ["Is Halvard Mutual still writing Texas commercial property?",
                       "What is Halvard Mutual doing with its Texas commercial property policies?"],
            "unaffected": ["Does Halvard Mutual's Texas property move affect our clinics?",
                           "Should we care about what Halvard Mutual is doing in Texas property?"]},
        "hail_cat_losses": {
            "change": ["How bad were insured losses from the hail and wind storms this quarter?",
                       "What did the hail and wind storms cost insurers in losses?"],
            "unaffected": ["Do the Texas hail and wind storm losses change anything for our Ohio clinics?",
                           "Should the hail storm losses worry our clinics?"]},
        "ohio_ai_claims_rule/denial_review": {
            "change": ["Can an AI model still deny our claims in Ohio without a human sign-off?",
                       "What does Ohio now require before an AI-recommended claim denial goes out?"]},
        "ohio_ai_claims_rule/compliance_window_days": {
            "change": ["How long do carriers have to bring their AI claims tools into line with the Ohio rule?",
                       "How many days until carriers must comply with Ohio's AI claims rule?"]},
        "cyber_exclusion/wording": {
            "change": ["What does the new cyber exclusion carriers are attaching take out of cover?",
                       "Is there a new gap in cyber cover from the exclusion carriers now attach?"],
            "unaffected": ["Does the new cyber exclusion wording change anything for our property and casualty "
                           "program?",
                           "Do we need to worry about the cyber exclusion carriers are attaching?"]},
        "nuclear_verdicts": {
            "change": ["How many huge liability verdicts have courts returned lately?",
                       "How bad has the run of large liability verdicts got in the last half year?"]},
        "copperline_renewal_quote": {
            "change": ["What increase did our incumbent carrier indicate on the Texas plant's property renewal?",
                       "Where did the Texas plant property renewal indication come in?"]},
        "copperline_deductible": {
            "stable": ["Is our per-occurrence property deductible still the same?",
                       "What per-occurrence deductible are we carrying on our property program?"]},
        "copperline_tiv": {
            "stable": ["What total insured value of buildings, plant and equipment do you have on file for us?",
                       "How much of our buildings, plant and equipment are we insuring across our sites?"]},
        "northgate_cyber_limit": {
            "stable": ["What cyber limit are we buying through you?",
                       "Is the cyber limit we buy through you unchanged?"]},
    },
    "logistics": {
        "diesel_price/usd_per_gal": {
            "change": ["What is retail diesel costing per gallon now?",
                       "Where are retail diesel prices per gallon sitting?"],
            "stable": ["Has the retail diesel price per gallon moved?",
                       "Is retail diesel still costing about the same per gallon?"]},
        "diesel_surcharge": {
            "change": ["What fuel surcharge are carriers charging on linehaul now?",
                       "Where are carrier fuel surcharges as a share of linehaul?"],
            "stable": ["Have carrier fuel surcharges on linehaul moved?",
                       "Are carrier fuel surcharges still running at the same share of linehaul?"]},
        "kestrel_bay_labour": {
            "change": ["What's the dockworker situation at the Port of Kestrel Bay right now?",
                       "Are the Kestrel Bay dockworkers working normally, and are vessels moving?"],
            "unaffected": ["Does the Kestrel Bay dockworker situation touch our domestic lanes?",
                           "Should the dockworker news from Kestrel Bay worry our stores?"]},
        "kestrel_bay_port": {
            "change": ["How long are import containers sitting at the Kestrel Bay terminals?",
                       "How many days are import containers waiting at Kestrel Bay terminals?"],
            "stable": ["Are import containers still clearing Kestrel Bay terminals as quickly as before?",
                       "Has container dwell time at the Kestrel Bay terminals changed?"],
            "unaffected": ["Do the Kestrel Bay terminal container delays affect our stores?",
                           "Should container dwell times at Kestrel Bay worry us?"]},
        "rail_embargo/status": {
            "change": ["Can we still move our import containers inland by rail from Kestrel Bay?",
                       "What has the railroad done to intermodal containers out of Kestrel Bay?"],
            "unaffected": ["Does the railroad's move on Kestrel Bay intermodal containers hit our truck lanes?",
                           "Should the railroad news about Kestrel Bay intermodal worry us?"]},
        "rail_embargo/expected_status": {
            "change": ["Is rail for our intermodal containers out of Kestrel Bay at risk if the dock dispute drags "
                       "on?",
                       "What might the railroad do to Kestrel Bay intermodal traffic if the dock dispute continues?"]},
        "rail_intermodal_service": {
            "change": ["How reliable are intermodal trains out of Kestrel Bay right now?",
                       "What share of intermodal trains out of Kestrel Bay are running on time?"],
            "stable": ["Are intermodal trains out of Kestrel Bay still running on time?",
                       "Has intermodal train reliability out of Kestrel Bay changed?"],
            "unaffected": ["Does intermodal train reliability out of Kestrel Bay matter for our trucks?",
                           "Should late intermodal trains out of Kestrel Bay worry us?"]},
        "transpacific_tariff": {
            "change": ["What duty do our imported home furnishings pay on the trans-Pacific lane now?",
                       "Where did the duty on trans-Pacific home furnishings imports land?"],
            "stable": ["Has the duty on home furnishings imported on the trans-Pacific lane changed?",
                       "Is the trans-Pacific duty on imported home furnishings still the same?"],
            "unaffected": ["Does the trans-Pacific duty on imported home furnishings raise our costs?",
                           "Should the trans-Pacific import duty change worry us?"]},
        "truckload_spot_rates": {
            "change": ["Where are dry van truckload spot rates per mile now?",
                       "What does a dry van spot truckload cost per mile these days?"],
            "stable": ["Have dry van truckload spot rates per mile moved?",
                       "Are dry van spot rates per mile still about the same?"]},
        "truckload_capacity": {
            "change": ["How often are carriers turning down contract truckload tenders now?",
                       "What share of contract truckload tenders are carriers rejecting now?"],
            "stable": ["Are carriers still turning down contract truckload tenders at the same rate?",
                       "Has the share of contract truckload tenders carriers turn down changed?"]},
        "reefer_spot_rates": {
            "change": ["Where are refrigerated truckload spot rates per mile now?",
                       "What does a refrigerated spot truckload cost per mile?"],
            "stable": ["Have refrigerated truckload spot rates per mile moved?",
                       "Are refrigerated spot rates per mile still about the same?"],
            "unaffected": ["Does the move in refrigerated truckload spot rates matter for our dry van freight?",
                           "Should refrigerated spot rate changes worry us?"]},
        "reefer_emissions_rule": {
            "change": ["What does California now require of new trailer refrigeration units?",
                       "Is there a new California rule on trailer refrigeration units we should know about?"],
            "unaffected": ["Does California's new trailer refrigeration unit rule affect our dry van freight?",
                           "Should the California trailer refrigeration rule worry us?"]},
        "fernway_stockouts": {
            "change": ["How many of our store SKUs are out of stock while containers wait?",
                       "What share of our store SKUs ran out of stock?"]},
        "fernway_contract_rate": {
            "stable": ["What is our contracted dry van rate per mile with you?",
                       "Is our contracted dry van rate per mile unchanged?"]},
        "fernway_import_share": {
            "stable": ["What share of our inventory arrives as imports through Kestrel Bay?",
                       "How much of our inventory comes in as imports through Kestrel Bay?"]},
        "summit_contract_rate": {
            "stable": ["What is our contracted refrigerated rate per mile with you?",
                       "Is our contracted refrigerated rate per mile still the same?"]},
    },
    "energy": {
        "wholesale_power_price/usd_per_mwh": {
            "change": ["Where is day-ahead wholesale power averaging per MWh now?",
                       "What did day-ahead wholesale power average per MWh lately?"],
            "stable": ["Has the day-ahead wholesale power average per MWh moved?",
                       "Is day-ahead wholesale power still averaging about the same per MWh?"]},
        "natural_gas_price": {
            "change": ["What is natural gas trading at the regional hub now?",
                       "Where did natural gas prices at the regional hub go?"],
            "stable": ["Has natural gas at the regional hub moved?",
                       "Is natural gas at the regional hub still trading about the same?"]},
        "transmission_rate_case": {
            "change": ["What did regulators do on Northline Transmission's rate case?",
                       "How much are Northline Transmission's charges going up?"],
            "stable": ["Has anything happened on Northline Transmission's rate case?",
                       "Is Northline Transmission's requested charge increase still where it was?"],
            "unaffected": ["Does Northline Transmission's rate case affect our campus?",
                           "Should Northline Transmission's charge increase worry us?"]},
        "municipal_bond_yield": {
            "change": ["Where are long-dated municipal bond yields now?",
                       "What would long-dated municipal bond yields mean for our borrowing today?"],
            "stable": ["Have long-dated municipal bond yields moved?",
                       "Are long-dated municipal bond yields still about the same?"],
            "unaffected": ["Do higher long-dated municipal bond yields matter to our campus?",
                           "Should the move in municipal bond yields worry us?"]},
        "project_finance_rate": {
            "change": ["What are construction loans for renewable projects pricing at now?",
                       "How expensive are construction loans for renewable projects now?"]},
        "solar_itc/credit_pct": {
            "change": ["What investment tax credit can public-power solar projects claim now?",
                       "Where did the investment tax credit for public-power solar projects end up?"],
            "stable": ["Has the investment tax credit for public-power solar projects changed?",
                       "Is the public-power solar investment tax credit still the same?"],
            "unaffected": ["Does the change to the public-power solar investment tax credit affect our campus?",
                           "Should the public-power solar tax credit news worry us?"]},
        "solar_itc/expected_credit_pct": {
            "change": ["Is the public-power solar investment tax credit likely to be cut?",
                       "What are lawmakers expected to do with the public-power solar investment tax credit?"]},
        "interconnection_queue": {
            "change": ["How many months will a project entering the interconnection queue wait for a study now?",
                       "How long is the wait for an interconnection queue study now?"],
            "stable": ["Has the wait for an interconnection queue study changed?",
                       "Are projects entering the interconnection queue still waiting as long for a study?"]},
        "interconnection_queue_rule": {
            "change": ["How will the grid operator study new interconnection requests now?",
                       "What changed in how the grid operator batches new interconnection requests?"]},
        "peak_demand": {
            "change": ["What is the regional peak demand record now?",
                       "How high did regional peak demand go?"],
            "stable": ["Has the regional peak demand record changed?",
                       "Is the regional system peak demand record still the same?"]},
        "grid_emergency": {
            "change": ["What kind of alert did the grid operator issue when it asked large users to cut load?",
                       "Did the grid operator issue an alert to large users during the heat wave?"]},
        "capacity_price": {
            "change": ["Where did the regional capacity auction clear?",
                       "What will capacity cost per MW-day after the regional auction?"]},
        "demand_response_rule": {
            "change": ["What does the state now require of large new loads during grid emergencies?",
                       "Is there a new state curtailment program for large new loads?"],
            "unaffected": ["Does the state's curtailment program for large new loads apply to our utility?",
                           "Should the state's new curtailment program for large loads worry us?"]},
        "ppa_prices": {
            "change": ["Where are solar power purchase agreements in the region pricing now?",
                       "What would a new solar power purchase agreement cost per MWh in the region?"],
            "stable": ["Have regional solar power purchase agreement prices moved?",
                       "Are solar power purchase agreements in the region still pricing about the same?"],
            "unaffected": ["Do rising solar power purchase agreement prices matter for our own solar build?",
                           "Should solar power purchase agreement pricing worry our utility?"]},
        "riverton_project_budget": {
            "change": ["How far over plan is our solar project budget now?",
                       "What did our board flag about the solar project budget?"]},
        "riverton_hedge_ratio": {
            "stable": ["How much of next summer's expected load have we hedged?",
                       "Is the share of next summer's load we have hedged unchanged?"]},
        "riverton_solar_project": {
            "stable": ["How big is our planned solar-plus-storage project?",
                       "What size is our planned solar-plus-storage project?"]},
        "clearwater_ppa_volume": {
            "stable": ["How many MW of renewable supply do we contract through you?",
                       "How much renewable supply do we contract through you?"]},
    },
}

_RUMOUR_Q: dict[str, list[str]] = {
    "property_rate_cut/expected_cut_pct": ["Are carriers about to cut property rates to win business back?",
                                           "I keep hearing regional carriers will cut property rates. Is that "
                                           "real?"],
    "cyber_exclusion/expected_status": ["Is the new cyber exclusion here to stay?",
                                        "Will carriers back away from the new cyber exclusion?"],
    "diesel_price/expected_usd_per_gal": ["Is diesel about to fall back down?",
                                          "Should we wait for diesel to fall back before we lock in surcharges?"],
    "wholesale_power_price/expected_drop_pct": ["Is wholesale power going to get cheaper this summer?",
                                                "Should we expect wholesale power prices to drop this summer?"],
}

_IMPOSSIBLE_Q: dict[str, list[str]] = {
    "insurance": ["What will our loss ratio be at the end of next year?",
                  "Which underwriter will be assigned to our account next spring?",
                  "What will the state insurance commissioner decide about hail deductibles next session?",
                  "How will our board vote on self-insuring part of our program?"],
    "logistics": ["Which day next month will our next container vessel berth?",
                  "Will our CEO approve a second distribution centre this year?",
                  "How many trucks will our biggest competitor add next year?",
                  "What will diesel cost on the first day of next winter?"],
    "energy": ["What will the city council decide about next year's electricity tariffs?",
               "Which day will the next heat wave start?",
               "What will our largest customer's load be in five years?",
               "Who will win the next regional transmission line tender?"],
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
    return value + k


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
    """Facts up to as_of, nudged for the seed; other clients' account notes are left out."""
    offsets = dict(_offsets(field, seed))
    out = []
    for f in facts:
        if _aware(f["valid_from"]) > as_of:
            continue
        if f["source"] == "account_notes" and f["subject"] not in account["exposures"]:
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
        if _aware(g["valid_from"]) > as_of or g["_id"] == fact["_id"] or not W.is_number(g["value"]):
            continue
        same_chain = (g["subject"], g["relation"]) == (fact["subject"], fact["relation"]) and g["source"] in VERIFIED
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
    n_balance = rng.choice([1, 2]) if n_change >= 3 else 2
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
