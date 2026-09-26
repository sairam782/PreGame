"""The three simulated advisory segments: client households, month-0 facts and scripted events.

A financial advisor meets the same households over time and has to keep up with the WORLD (rates, markets, tax
rules, products) and the CLIENTS (life events and preferences, kept as ``account`` facts owned via ``account_id``).
Every household, fund, bank, business and person is invented, every number is made up, and every fact carries
``simulated: True``.

Design rules the scenario generator and the oracle rely on (tests/test_world.py checks them):

* A fact's ``text`` contains no digits except its own value, rendered by ``format_value`` (ints as-is, floats with
  two decimals). String values appear verbatim in the text. So a key term is always findable in its fact's text and a
  superseded value never leaks into the text of the fact that replaced it.
* Within one field every number is distinct across subjects, so a forbidden (superseded / rumoured / other client's)
  number never collides with a current one.
* Analyst notes (``source == "analyst_notes"``, unverified) use their own relation, prefixed ``expected_``, so they
  never supersede verified facts or get superseded by them. ``ANALYST_VERDICTS`` records which turned out right.
* A client note that replaces an older one uses the same (subject, relation), so the old label is superseded
  (e.g. "paused" -> "ready" on an annuity conversation); keeping the stale label is the failure the questions test.
* No fact text uses advice phrasing ("you should buy", "guaranteed", ...), so the no-advice guardrail stays quiet on
  the world itself.
* ``sim_date(month, day)``: SIM_START is 2026-01-05 UTC and a simulated month is 30 days.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from pregame.contracts import FIELDS, Account, Fact, MarketEvent

SIM_START = datetime(2026, 1, 5, tzinfo=timezone.utc)
DAYS_PER_MONTH = 30
MONTHS = (1, 2, 3, 4, 5, 6)


def sim_date(month: int, day: int = 1) -> datetime:
    """Sim time for day ``day`` (1-based) of simulated month ``month`` (0 = the starting month)."""
    return SIM_START + timedelta(days=DAYS_PER_MONTH * month + (day - 1))


def month_of(when: datetime) -> int:
    """The simulated month a sim time falls in, clamped to 0..6."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, min(MONTHS[-1], (when - SIM_START).days // DAYS_PER_MONTH))


# ---------------------------------------------------------------------------------------------------------------
# Value formatting (shared with scenarios.py so fact texts and key terms always agree)
# ---------------------------------------------------------------------------------------------------------------
def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def format_value(value: Any) -> str:
    """How a value is written in fact texts and key terms: ints as-is, floats with two decimals."""
    if is_number(value) and isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def key_term(value: Any, unit: str) -> str:
    """The code-checkable term for a value: '18%' for percentages, '4.45' for other numbers, the string otherwise."""
    text = format_value(value)
    if is_number(value) and unit == "%":
        return text + "%"
    return text


def fact_id(field: str, subject: str, relation: str, valid_from: datetime) -> str:
    return f"{field}:{subject}:{relation}@{valid_from.date().isoformat()}"


# fact _id -> text template with a "{v}" placeholder (lets scenarios.py re-render nudged values)
TEXT_TEMPLATES: dict[str, str] = {}
# analyst fact _id -> "right" | "wrong"
ANALYST_VERDICTS: dict[str, str] = {}


def render_text(fid: str, value: Any) -> str:
    return TEXT_TEMPLATES[fid].format(v=format_value(value))


def _fact(field: str, when: datetime, spec: tuple, event_id: str | None) -> Fact:
    subject, relation, value, unit, kind, template, source, verdict = spec
    fid = fact_id(field, subject, relation, when)
    TEXT_TEMPLATES[fid] = template
    if source == "analyst_notes":
        ANALYST_VERDICTS[fid] = verdict or "wrong"
    owner = None
    if kind == "account":
        owner = account_owner(field, subject)
        if owner is None:
            raise ValueError(f"account fact {fid} has no owning account")
    return {
        "_id": fid, "field": field, "subject": subject, "relation": relation, "value": value, "unit": unit,
        "text": template.format(v=format_value(value)), "kind": kind, "source": source, "valid_from": when,
        "event_id": event_id, "account_id": owner, "simulated": True,
    }


def F(subject: str, relation: str, value: Any, unit: str, kind: str, template: str,
      source: str = "market_feed", verdict: str | None = None) -> tuple:
    return (subject, relation, value, unit, kind, template, source, verdict)


def _base(field: str, *specs: tuple) -> list[Fact]:
    return [_fact(field, sim_date(0), s, None) for s in specs]


def _event(field: str, eid: str, title: str, month: int, day: int, feedback: str, *specs: tuple) -> MarketEvent:
    when = sim_date(month, day)
    return {"id": eid, "field": field, "title": title, "month": month,
            "facts": [_fact(field, when, s, eid) for s in specs], "feedback": feedback}


def event_time(event: MarketEvent) -> datetime:
    """When an event lands (all of its facts share this valid_from)."""
    return event["facts"][0]["valid_from"]


# ---------------------------------------------------------------------------------------------------------------
# Accounts (first per field is the demo account). Profiles contain no digits on purpose.
# ---------------------------------------------------------------------------------------------------------------
ACCOUNTS: dict[str, list[Account]] = {
    "retirement": [
        {"id": "okafor-household", "field": "retirement", "name": "The Okafor household",
         "counterpart": "Ruth and Daniel Okafor",
         "profile": ("Pre-retirees in their sixties; Daniel plans to stop work soon and Ruth already has. They hold a "
                     "rollover IRA, a taxable account and bank CDs with us. They want a steady income floor and worry "
                     "about a market drop right after Daniel retires."),
         "exposures": ["treasury_yields", "cd_rates", "annuity_payout_rates", "rmd_age", "equity_drawdown",
                       "policy_rate", "medicare_premiums", "social_security_cola", "inflation", "harborview_bank",
                       "okafor_risk_preference", "okafor_retirement_timing"]},
        {"id": "beaumont-household", "field": "retirement", "name": "Evelyn Beaumont",
         "counterpart": "Evelyn Beaumont",
         "profile": ("Widowed retired teacher drawing a pension and required distributions from an IRA. Cares about "
                     "taxes on withdrawals, Medicare costs and what she leaves her grandchildren."),
         "exposures": ["treasury_yields", "rmd_age", "equity_drawdown", "medicare_premiums", "social_security_cola",
                       "inflation", "estate_exemption", "brightwater_income_fund", "beaumont_ira",
                       "beaumont_grandchildren", "beaumont_inheritance"]},
    ],
    "families": [
        {"id": "nakamura-family", "field": "families", "name": "The Nakamura family",
         "counterpart": "Kenji and Alicia Nakamura",
         "profile": ("Working parents saving for college and a larger home. They max out their workplace plans, fund "
                     "college savings accounts for the children and keep an emergency fund in high-yield savings."),
         "exposures": ["mortgage_rates", "hysa_rates", "workplace_plan_limit", "college_savings_rules",
                       "college_tuition", "child_tax_credit", "tech_selloff", "nakamura_children", "nakamura_home"]},
        {"id": "whitfield-household", "field": "families", "name": "The Whitfield household",
         "counterpart": "Andre and Marisol Whitfield",
         "profile": ("Dual-income renters with no children, saving for a first home. Their savings sit in index funds "
                     "and high-yield savings; Andre works in tech."),
         "exposures": ["mortgage_rates", "hysa_rates", "workplace_plan_limit", "pinecrest_index_fund", "unemployment",
                       "tech_selloff", "whitfield_down_payment", "whitfield_rent", "whitfield_income"]},
    ],
    "business_owners": [
        {"id": "castellan-family", "field": "business_owners", "name": "Marco and Elena Castellan",
         "counterpart": "Marco and Elena Castellan",
         "profile": ("Marco founded Castellan Precision, a machine shop, and is selling it to a private buyer. They "
                     "plan for the liquidity event: taxes on the sale, where the proceeds go and income afterwards."),
         "exposures": ["capital_gains_rate", "qsbs_exclusion", "sba_loan_rates", "muni_yields",
                       "business_valuations", "owner_plan_limit", "northfield_credit_fund", "lakeshore_bank",
                       "castellan_sale", "castellan_sale_price", "castellan_annuity_stance"]},
        {"id": "adeyemi-practice", "field": "business_owners", "name": "Dr. Funmi Adeyemi",
         "counterpart": "Dr. Funmi Adeyemi",
         "profile": ("Owns a dental practice and plans to sell it to her associate over the next few years. Cares about "
                     "how the buyer finances the purchase, taxes on the sale and her own retirement plan."),
         "exposures": ["capital_gains_rate", "sba_loan_rates", "owner_plan_limit", "lakeshore_bank",
                       "sba_seller_note_rule", "dental_practice_valuations", "adeyemi_exit_timing", "adeyemi_buyout",
                       "adeyemi_collections", "adeyemi_retirement_savings"]},
    ],
}


def account_owner(field: str, subject: str) -> str | None:
    """The account whose exposures list this subject (sets ``account_id`` on every kind-"account" fact)."""
    for account in ACCOUNTS[field]:
        if subject in account["exposures"]:
            return account["id"]
    return None


AN = "analyst_notes"
AC = "account_notes"

# ---------------------------------------------------------------------------------------------------------------
# Month-0 facts
# ---------------------------------------------------------------------------------------------------------------
BASE_FACTS: dict[str, list[Fact]] = {
    "retirement": _base(
        "retirement",
        F("treasury_yields", "ten_year_pct", 4.35, "%", "price", "Ten-year Treasury yields stand at {v}%."),
        F("cd_rates", "one_year_apy_pct", 4.60, "%", "price", "One-year bank CDs are paying about {v}% a year."),
        F("annuity_payout_rates", "payout_pct", 6.80, "%", "competitor",
          "An immediate income annuity for a new retiree pays out about {v}% of the premium a year."),
        F("rmd_age", "start_age", 73, "years", "regulation",
          "Required minimum distributions from IRAs start at age {v}."),
        F("social_security_cola", "cola_pct", 2.50, "%", "demand",
          "This year's Social Security cost-of-living adjustment is {v}%."),
        F("inflation", "cpi_pct", 2.90, "%", "demand", "Consumer prices are rising {v}% a year."),
        F("medicare_premiums", "part_b_monthly_usd", 185, "USD", "regulation",
          "The standard Medicare Part B premium is ${v} a month."),
        F("estate_exemption", "exemption_usd_m", 13.99, "USD million", "regulation",
          "The federal estate tax exemption is ${v} million per person."),
        F("okafor_retirement_timing", "months_to_retirement", 10, "months", "account",
          "Daniel plans to retire in about {v} months.", AC),
        F("okafor_risk_preference", "stance", "growth", "", "account",
          "Ruth and Daniel want to keep a {v} tilt in their portfolio until Daniel retires.", AC),
        F("beaumont_ira", "balance_usd_k", 640, "USD thousand", "account",
          "Evelyn's IRA holds about ${v} thousand.", AC),
        F("beaumont_grandchildren", "count", 3, "grandchildren", "account",
          "Evelyn has {v} grandchildren she wants to help with college.", AC),
    ),
    "families": _base(
        "families",
        F("mortgage_rates", "thirty_year_pct", 6.40, "%", "price", "Thirty-year fixed mortgage rates average {v}%."),
        F("hysa_rates", "savings_apy_pct", 4.10, "%", "price", "High-yield savings accounts pay about {v}% a year."),
        F("workplace_plan_limit", "employee_limit_usd", 23500, "USD", "regulation",
          "Employees can defer up to ${v} a year into a workplace retirement plan."),
        F("college_savings_rules", "k12_tuition_limit_usd", 10000, "USD", "regulation",
          "College savings plans can pay up to ${v} a year of private school tuition."),
        F("college_tuition", "annual_increase_pct", 4.20, "%", "demand",
          "Public university tuition is rising about {v}% a year."),
        F("child_tax_credit", "per_child_usd", 2000, "USD", "regulation", "The child tax credit is ${v} per child."),
        F("pinecrest_index_fund", "expense_ratio_pct", 0.20, "%", "competitor",
          "The Pinecrest total-market index fund charges {v}% a year."),
        F("unemployment", "rate_pct", 4.30, "%", "demand", "The unemployment rate is {v}%."),
        F("nakamura_children", "count", 2, "children", "account", "Kenji and Alicia have {v} children.", AC),
        F("nakamura_home", "status", "shopping", "", "account",
          "The Nakamuras are {v} for a larger home near better schools.", AC),
        F("whitfield_down_payment", "saved_usd_k", 48, "USD thousand", "account",
          "The Whitfields have saved about ${v} thousand toward a down payment.", AC),
        F("whitfield_rent", "monthly_usd", 2650, "USD", "account", "The Whitfields pay ${v} a month in rent.", AC),
        F("whitfield_income", "status", "employed", "", "account",
          "Andre is {v} full-time as a software engineer.", AC),
    ),
    "business_owners": _base(
        "business_owners",
        F("capital_gains_rate", "top_rate_pct", 20, "%", "regulation",
          "The top federal long-term capital gains rate is {v}%."),
        F("qsbs_exclusion", "cap_usd_m", 10, "USD million", "regulation",
          "The small business stock gain exclusion is capped at ${v} million per shareholder."),
        F("sba_loan_rates", "rate_pct", 8.75, "%", "price", "SBA business acquisition loans are pricing around {v}%."),
        F("muni_yields", "yield_pct", 3.35, "%", "price", "High-grade municipal bond yields stand at {v}%."),
        F("business_valuations", "earnings_multiple", 6.50, "x", "demand",
          "Small manufacturers are changing hands for about {v} times annual earnings."),
        F("owner_plan_limit", "total_limit_usd", 70000, "USD", "regulation",
          "An owner can put up to ${v} a year into a small-business retirement plan."),
        F("castellan_sale", "status", "negotiating", "", "account",
          "Marco is {v} a sale of Castellan Precision to a private buyer.", AC),
        F("castellan_annuity_stance", "stance", "paused", "", "account",
          "Marco asked to keep any annuity conversation {v} until the sale is done.", AC),
        F("adeyemi_exit_timing", "years_to_exit", 5, "years", "account",
          "Funmi wants to step back from the practice in about {v} years.", AC),
        F("adeyemi_buyout", "target_price_usd_k", 1450, "USD thousand", "account",
          "Funmi hopes her associate will buy the practice for about ${v} thousand.", AC),
        F("dental_practice_valuations", "collections_multiple", 0.80, "x", "demand",
          "Dental practices are changing hands for about {v} times annual collections."),
        F("adeyemi_collections", "annual_usd_k", 1900, "USD thousand", "account",
          "Funmi's practice collects about ${v} thousand a year.", AC),
        F("adeyemi_retirement_savings", "balance_usd_k", 820, "USD thousand", "account",
          "Funmi's retirement plan holds about ${v} thousand.", AC),
    ),
}

# ---------------------------------------------------------------------------------------------------------------
# Scripted events, months 1..6 (one per month per segment): world events AND client life events
# ---------------------------------------------------------------------------------------------------------------
EVENTS: dict[str, list[MarketEvent]] = {
    "retirement": [
        _event("retirement", "ret-rmd-age", "RMD starting age rises", 1, 8,
               "Ruth asked whether the new RMD age changes when they have to start withdrawals, and I didn't have it.",
               F("rmd_age", "start_age", 75, "years", "regulation",
                 "A new law moves the start of required minimum distributions to age {v}."),
               F("treasury_yields", "ten_year_pct", 4.80, "%", "price", "Ten-year Treasury yields have climbed to {v}%."),
               F("beaumont_grandchildren", "count", 4, "grandchildren", "account",
                 "Evelyn's newest grandchild arrived; she now has {v} grandchildren.", AC)),
        _event("retirement", "ret-market-selloff", "Stocks sell off", 2, 12,
               "Daniel wanted to talk about the sell-off and whether they should change their mix. The brief had "
               "nothing on the market drop.",
               F("equity_drawdown", "decline_pct", 17, "%", "disruption",
                 "A broad stock sell-off has left the market {v}% below its peak."),
               F("okafor_risk_preference", "stance", "conservative", "", "account",
                 "After the sell-off, Ruth and Daniel want a more {v} mix in their portfolio.", AC),
               F("policy_rate", "expected_cut_pts", 0.50, "percentage points", "price",
                 "Analyst note: futures traders expect the central bank to cut its policy rate by {v} percentage "
                 "points at the next meeting.", AN, "wrong")),
        _event("retirement", "ret-annuity-payouts", "Annuity payouts rise and a fund merges", 3, 6,
               "Ruth asked what an income annuity would pay now, and the brief still had the old payout.",
               F("annuity_payout_rates", "payout_pct", 7.45, "%", "competitor",
                 "Immediate income annuities for new retirees now pay out about {v}% of the premium a year."),
               F("brightwater_income_fund", "status", "merger", "", "competitor",
                 "Brightwater Funds announced a {v} of its Steady Income fund into a higher-fee share class."),
               F("estate_exemption", "expected_exemption_usd_m", 7.25, "USD million", "regulation",
                 "Analyst note: lawmakers are expected to cut the estate tax exemption to ${v} million per person.",
                 AN, "right")),
        _event("retirement", "ret-retirement-moved", "Daniel moves his retirement date; Medicare premium rises", 4, 9,
               "Daniel told me he's working longer now and asked about the Medicare increase. My brief still had the "
               "old retirement date and nothing on the premium rule.",
               F("okafor_retirement_timing", "months_to_retirement", 16, "months", "account",
                 "Daniel has pushed his retirement back; he now plans to retire in about {v} months.", AC),
               F("medicare_premiums", "part_b_monthly_usd", 212, "USD", "regulation",
                 "The standard Medicare Part B premium rises to ${v} a month next year."),
               F("treasury_yields", "ten_year_pct", 4.05, "%", "price", "Ten-year Treasury yields have eased to {v}%.")),
        _event("retirement", "ret-estate-exemption", "Estate tax exemption cut", 5, 11,
               "Evelyn asked how the lower estate exemption changes what she leaves the grandchildren. I had nothing "
               "on the tax change.",
               F("estate_exemption", "exemption_usd_m", 7.25, "USD million", "regulation",
                 "The federal estate tax exemption has been cut to ${v} million per person."),
               F("beaumont_inheritance", "amount_usd_k", 380, "USD thousand", "account",
                 "Evelyn inherited about ${v} thousand from her sister's estate.", AC),
               F("inflation", "cpi_pct", 3.70, "%", "demand", "Consumer prices are now rising {v}% a year.")),
        _event("retirement", "ret-bank-failure", "Harborview Savings fails", 6, 7,
               "Ruth called about the Harborview failure because their CDs are there. The brief never mentioned it.",
               F("harborview_bank", "status", "failed", "", "disruption",
                 "Harborview Savings {v} and regulators have taken over its deposits."),
               F("social_security_cola", "cola_pct", 3.30, "%", "demand",
                 "Next year's Social Security cost-of-living adjustment is projected at {v}%."),
               F("cd_rates", "one_year_apy_pct", 3.95, "%", "price", "One-year bank CDs now pay about {v}% a year.")),
    ],
    "families": [
        _event("families", "fam-plan-limit", "Workplace plan contribution limit rises", 1, 9,
               "Alicia asked about the new 401(k) limit and I didn't have it.",
               F("workplace_plan_limit", "employee_limit_usd", 24500, "USD", "regulation",
                 "Employees can now defer up to ${v} a year into a workplace retirement plan."),
               F("hysa_rates", "savings_apy_pct", 4.45, "%", "price",
                 "High-yield savings accounts now pay about {v}% a year."),
               F("mortgage_rates", "expected_rate_pct", 5.50, "%", "price",
                 "Analyst note: a housing newsletter expects thirty-year mortgage rates to fall to {v}% by summer.",
                 AN, "wrong")),
        _event("families", "fam-mortgage-baby", "Mortgage rates jump; a new baby", 2, 14,
               "Kenji mentioned the new baby and asked what it changes for their plan. My notes still said two kids.",
               F("mortgage_rates", "thirty_year_pct", 7.10, "%", "price",
                 "Thirty-year fixed mortgage rates have jumped to {v}%."),
               F("nakamura_children", "count", 3, "children", "account",
                 "The Nakamuras welcomed a baby; they now have {v} children.", AC),
               F("child_tax_credit", "expected_per_child_usd", 2200, "USD", "regulation",
                 "Analyst note: lawmakers are expected to raise the child tax credit to ${v} per child.", AN,
                 "right")),
        _event("families", "fam-college-rule", "College savings plans cover more private tuition", 3, 8,
               "Alicia asked if the college savings account can pay for private school now. I had nothing on the rule "
               "change.",
               F("college_savings_rules", "k12_tuition_limit_usd", 20000, "USD", "regulation",
                 "A new rule lets college savings plans pay up to ${v} a year of private school tuition."),
               F("pinecrest_index_fund", "expense_ratio_pct", 0.45, "%", "competitor",
                 "Pinecrest raised the fee on its total-market index fund to {v}% a year."),
               F("whitfield_down_payment", "saved_usd_k", 61, "USD thousand", "account",
                 "The Whitfields have now saved about ${v} thousand toward a down payment.", AC)),
        _event("families", "fam-credit-contract", "Child tax credit rises; the Nakamuras go under contract", 4, 10,
               "Kenji told me they're under contract on the house; my brief still had them looking, and I missed the "
               "tax credit change.",
               F("child_tax_credit", "per_child_usd", 2200, "USD", "regulation",
                 "The child tax credit has risen to ${v} per child."),
               F("nakamura_home", "status", "under contract", "", "account",
                 "The Nakamuras are {v} on a larger home and expect to move next month.", AC),
               F("hysa_rates", "savings_apy_pct", 3.80, "%", "price",
                 "High-yield savings accounts have dropped to about {v}% a year.")),
        _event("families", "fam-tech-layoffs", "Tech sell-off and layoffs", 5, 12,
               "Andre told me he was laid off and asked about the market drop. The brief had neither.",
               F("tech_selloff", "decline_pct", 14, "%", "disruption",
                 "A tech-led sell-off has knocked the stock market {v}% off its high."),
               F("unemployment", "rate_pct", 4.90, "%", "demand", "The unemployment rate has risen to {v}%."),
               F("whitfield_income", "status", "laid off", "", "account",
                 "Andre was {v} in a round of tech cuts and has severance for four months.", AC)),
        _event("families", "fam-tuition-rates", "Tuition jumps; mortgage rates ease", 6, 8,
               "Alicia asked why the tuition estimate jumped. The brief was still using last year's increase.",
               F("college_tuition", "annual_increase_pct", 6.60, "%", "demand",
                 "Public university tuition is now rising about {v}% a year."),
               F("mortgage_rates", "thirty_year_pct", 6.15, "%", "price",
                 "Thirty-year fixed mortgage rates have eased to {v}%.")),
    ],
    "business_owners": [
        _event("business_owners", "biz-plan-limit", "Owner retirement plan limit rises", 1, 11,
               "Marco asked how much more he can put into the company plan this year, and I didn't have the new limit.",
               F("owner_plan_limit", "total_limit_usd", 72000, "USD", "regulation",
                 "An owner can now put up to ${v} a year into a small-business retirement plan."),
               F("sba_loan_rates", "rate_pct", 9.60, "%", "price", "SBA business acquisition loans now price around {v}%."),
               F("capital_gains_rate", "expected_top_rate_pct", 28, "%", "regulation",
                 "Analyst note: a policy newsletter says the top capital gains rate will jump to {v}% this year.",
                 AN, "wrong")),
        _event("business_owners", "biz-sale-signed", "Castellan signs a sale; the stock exclusion cap rises", 2, 13,
               "Marco told me they signed the purchase agreement; my brief still said they were negotiating, and it "
               "missed the new exclusion cap.",
               F("castellan_sale", "status", "signed", "", "account",
                 "Marco has {v} a purchase agreement for Castellan Precision.", AC),
               F("castellan_sale_price", "price_usd_m", 18.50, "USD million", "account",
                 "The agreed price for Castellan Precision is ${v} million.", AC),
               F("qsbs_exclusion", "cap_usd_m", 15, "USD million", "regulation",
                 "A new law raises the small business stock gain exclusion to ${v} million per shareholder.")),
        _event("business_owners", "biz-bank-failure", "Lakeshore Commerce Bank fails", 3, 7,
               "Funmi asked whether the Lakeshore failure puts her practice's cash at risk. The brief never mentioned "
               "it.",
               F("lakeshore_bank", "status", "failed", "", "disruption",
                 "Lakeshore Commerce Bank {v}; business deposits above the insured limit are frozen."),
               F("business_valuations", "earnings_multiple", 5.60, "x", "demand",
                 "Small manufacturers are now changing hands for about {v} times annual earnings."),
               F("adeyemi_exit_timing", "years_to_exit", 3, "years", "account",
                 "Funmi now wants to step back from the practice in about {v} years.", AC)),
        _event("business_owners", "biz-sale-closes", "Castellan sale closes; capital gains rate rises", 4, 9,
               "Marco said he's ready to talk about an annuity now that the sale closed; my notes still said not now, "
               "and I missed the capital gains tax change.",
               F("castellan_sale", "status", "closed", "", "account",
                 "The sale of Castellan Precision has {v}.", AC),
               F("capital_gains_rate", "top_rate_pct", 25, "%", "regulation",
                 "The top federal long-term capital gains rate has risen to {v}%."),
               F("castellan_annuity_stance", "stance", "ready", "", "account",
                 "Marco is now {v} to discuss an annuity with part of the sale proceeds.", AC)),
        _event("business_owners", "biz-fund-gated", "Private credit fund gates withdrawals", 5, 10,
               "Marco asked whether he can still get money out of the Northfield fund. The brief had nothing on the "
               "gate.",
               F("northfield_credit_fund", "status", "gated", "", "disruption",
                 "Northfield Capital has {v} its private credit interval fund, limiting withdrawals."),
               F("sba_loan_rates", "rate_pct", 10.40, "%", "price",
                 "SBA business acquisition loans have climbed to around {v}%."),
               F("muni_yields", "expected_yield_pct", 3.90, "%", "price",
                 "Analyst note: strategists expect high-grade municipal yields to climb to {v}% as issuance surges.",
                 AN, "right")),
        _event("business_owners", "biz-sba-rule", "Municipal yields climb; a new SBA seller-note rule", 6, 8,
               "Funmi asked about the new SBA rule on seller notes for her associate's buyout. The brief skipped "
               "regulation entirely.",
               F("muni_yields", "yield_pct", 3.90, "%", "price", "High-grade municipal bond yields have climbed to {v}%."),
               F("sba_seller_note_rule", "max_note_pct", 50, "%", "regulation",
                 "The SBA now counts a seller note of up to {v}% of the price toward a buyer's equity.")),
    ],
}

_EVENT_INDEX: dict[str, MarketEvent] = {e["id"]: e for f in FIELDS for e in EVENTS[f]}


def event_by_id(event_id: str) -> MarketEvent:
    """The scripted event with this id (KeyError if unknown)."""
    return _EVENT_INDEX[event_id]


def all_facts(field: str) -> list[Fact]:
    """Every fact the field will ever have: month-0 facts then each event's facts, oldest first."""
    out = list(BASE_FACTS[field])
    for event in EVENTS[field]:
        out.extend(event["facts"])
    return sorted(out, key=lambda f: (f["valid_from"], f["_id"]))

