"""The three simulated markets: accounts, month-0 facts and scripted market events.

Everything here is fictional and every fact carries ``simulated: True``. Real US states are used as places; every
company, carrier, port, railroad and person is invented.

Design rules the scenario generator and the oracle rely on (tests/test_world.py checks them):

* A fact's ``text`` contains no digits except its own value, rendered by ``format_value`` (ints as-is, floats with
  two decimals). String values appear verbatim in the text. So a key term is always findable in its fact's text and a
  superseded value never leaks into the text of the fact that replaced it.
* Within one field every number is distinct across subjects, so a forbidden (superseded / rumoured / other client's)
  number never collides with a current one.
* Analyst notes (``source == "analyst_notes"``, unverified) use their own relation, prefixed ``expected_``, so they
  never supersede verified facts or get superseded by them. ``ANALYST_VERDICTS`` records which turned out right.
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
    return {
        "_id": fid, "field": field, "subject": subject, "relation": relation, "value": value, "unit": unit,
        "text": template.format(v=format_value(value)), "kind": kind, "source": source, "valid_from": when,
        "event_id": event_id, "simulated": True,
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
    "insurance": [
        {"id": "copperline-fabrication", "field": "insurance", "name": "Copperline Fabrication Co.",
         "counterpart": "Dana Ruiz, Director of Risk Management",
         "profile": ("Mid-size metal fabrication manufacturer with plants in Ohio and Texas. Buys commercial property "
                     "and casualty cover through us, placed with regional carriers. Cares about renewal pricing, "
                     "deductibles and whether carriers keep writing Texas property."),
         "exposures": ["reinsurance_rates", "casualty_reinsurance_rates", "property_rates", "casualty_rates",
                       "texas_property_capacity", "halvard_texas_exit", "hail_cat_losses", "ohio_ai_claims_rule",
                       "nuclear_verdicts", "property_rate_cut", "copperline_deductible", "copperline_tiv",
                       "copperline_renewal_quote"]},
        {"id": "northgate-clinics", "field": "insurance", "name": "Northgate Community Clinics",
         "counterpart": "Priya Nandakumar, VP Finance and Risk",
         "profile": ("Network of outpatient clinics across Ohio. Buys cyber, general liability and property cover "
                     "through us. Cares about gaps in cyber cover, how claims are handled and liability pricing."),
         "exposures": ["reinsurance_rates", "casualty_reinsurance_rates", "casualty_rates", "cyber_rates",
                       "cyber_exclusion", "ohio_ai_claims_rule", "nuclear_verdicts", "northgate_cyber_limit"]},
    ],
    "logistics": [
        {"id": "fernway-home-goods", "field": "logistics", "name": "Fernway Home Goods",
         "counterpart": "Marcus Bell, Director of Freight Procurement",
         "profile": ("Regional home-goods retailer with stores across the Southwest. Imports most furniture through "
                     "the Port of Kestrel Bay and buys drayage, intermodal and dry van truckload through us. Cares "
                     "about landed cost, container delays and fuel surcharges."),
         "exposures": ["diesel_price", "diesel_surcharge", "kestrel_bay_labour", "kestrel_bay_port", "rail_embargo",
                       "rail_intermodal_service", "transpacific_tariff", "truckload_spot_rates",
                       "truckload_capacity", "fernway_contract_rate", "fernway_import_share", "fernway_stockouts"]},
        {"id": "summit-ridge-grocers", "field": "logistics", "name": "Summit Ridge Grocers",
         "counterpart": "Alana Brooks, VP Supply Chain",
         "profile": ("Grocery chain in California and Nevada that buys from domestic suppliers only. Buys "
                     "refrigerated and dry van truckload through us. Cares about reefer rates, fuel costs and "
                     "equipment rules."),
         "exposures": ["diesel_price", "diesel_surcharge", "truckload_spot_rates", "truckload_capacity",
                       "reefer_spot_rates", "reefer_emissions_rule", "summit_contract_rate"]},
    ],
    "energy": [
        {"id": "riverton-municipal-power", "field": "energy", "name": "Riverton Municipal Power",
         "counterpart": "Gloria Mensah, Head of Power Procurement",
         "profile": ("City-owned electric utility that buys wholesale power, capacity and transmission service and "
                     "hedges through us. Is developing its own solar-plus-storage project. Cares about wholesale "
                     "costs, transmission charges and project financing."),
         "exposures": ["wholesale_power_price", "natural_gas_price", "transmission_rate_case",
                       "municipal_bond_yield", "project_finance_rate", "solar_itc", "interconnection_queue",
                       "interconnection_queue_rule", "peak_demand", "grid_emergency", "capacity_price",
                       "riverton_hedge_ratio", "riverton_solar_project", "riverton_project_budget"]},
        {"id": "clearwater-data-campus", "field": "energy", "name": "Clearwater Data Campus",
         "counterpart": "Tomas Reyes, Director of Energy Procurement",
         "profile": ("Data-centre operator with a large campus on the regional grid. Buys renewable power purchase "
                     "agreements and hedges through us. Cares about power prices, grid reliability and rules on "
                     "large loads."),
         "exposures": ["wholesale_power_price", "natural_gas_price", "ppa_prices", "project_finance_rate",
                       "interconnection_queue", "interconnection_queue_rule", "peak_demand", "grid_emergency",
                       "demand_response_rule", "capacity_price", "clearwater_ppa_volume"]},
    ],
}

AN = "analyst_notes"
AC = "account_notes"

# ---------------------------------------------------------------------------------------------------------------
# Month-0 facts
# ---------------------------------------------------------------------------------------------------------------
BASE_FACTS: dict[str, list[Fact]] = {
    "insurance": _base(
        "insurance",
        F("reinsurance_rates", "renewal_change_pct", 7, "%", "price",
          "Property catastrophe reinsurance rates rose {v}% at the January treaty renewals."),
        F("casualty_reinsurance_rates", "renewal_change_pct", 4, "%", "price",
          "Casualty reinsurance treaty rates rose {v}% at the January renewals."),
        F("property_rates", "renewal_change_pct", 5, "%", "price",
          "Commercial property premiums for mid-size manufacturers are rising about {v}% at renewal."),
        F("casualty_rates", "renewal_change_pct", 6, "%", "price",
          "General liability and commercial auto premiums are rising about {v}% at renewal."),
        F("cyber_rates", "renewal_change_pct", 9, "%", "price",
          "Cyber premiums for mid-size healthcare buyers are rising about {v}% at renewal."),
        F("texas_property_capacity", "active_carriers", 14, "carriers", "competitor",
          "About {v} carriers are actively quoting mid-size manufacturing property in Texas."),
        F("copperline_deductible", "per_occurrence_usd_k", 250, "USD thousand", "account",
          "Copperline's property program carries a ${v} thousand per-occurrence deductible.", AC),
        F("copperline_tiv", "total_insured_value_usd_m", 180, "USD million", "account",
          "Copperline insures about ${v} million of buildings, plant and equipment across its Ohio and Texas sites.", AC),
        F("northgate_cyber_limit", "limit_usd_m", 15, "USD million", "account",
          "Northgate buys a ${v} million cyber limit through us, renewing with its liability program.", AC),
    ),
    "logistics": _base(
        "logistics",
        F("diesel_price", "usd_per_gal", 3.65, "USD/gal", "price",
          "Retail diesel averages ${v} per gallon."),
        F("diesel_surcharge", "pct_of_linehaul", 21, "%", "price",
          "Carrier fuel surcharges are running at {v}% of linehaul."),
        F("truckload_spot_rates", "usd_per_mile", 2.35, "USD/mile", "price",
          "Dry van truckload spot rates average ${v} per mile."),
        F("reefer_spot_rates", "usd_per_mile", 2.90, "USD/mile", "price",
          "Refrigerated truckload spot rates average ${v} per mile."),
        F("truckload_capacity", "tender_rejection_pct", 17, "%", "demand",
          "Carriers are turning down {v}% of contract truckload tenders."),
        F("transpacific_tariff", "duty_pct", 7, "%", "regulation",
          "Home furnishings imported on the trans-Pacific lane carry a {v}% duty."),
        F("kestrel_bay_port", "container_dwell_days", 3, "days", "demand",
          "Import containers clear the Port of Kestrel Bay terminals in about {v} days."),
        F("rail_intermodal_service", "on_time_pct", 88, "%", "demand",
          "Intermodal trains out of Kestrel Bay are running {v}% on time."),
        F("fernway_contract_rate", "usd_per_mile", 2.60, "USD/mile", "account",
          "Fernway's contracted dry van rate with us is ${v} per mile.", AC),
        F("fernway_import_share", "pct_of_inventory", 45, "%", "account",
          "About {v}% of Fernway's inventory arrives as imports through Kestrel Bay.", AC),
        F("summit_contract_rate", "usd_per_mile", 3.10, "USD/mile", "account",
          "Summit Ridge's contracted refrigerated rate with us is ${v} per mile.", AC),
    ),
    "energy": _base(
        "energy",
        F("wholesale_power_price", "usd_per_mwh", 48, "USD/MWh", "price",
          "Day-ahead wholesale power is averaging ${v} per MWh."),
        F("natural_gas_price", "usd_per_mmbtu", 2.85, "USD/MMBtu", "price",
          "Natural gas at the regional hub is trading at ${v} per MMBtu."),
        F("transmission_rate_case", "increase_pct", 14, "%", "regulation",
          "Northline Transmission has asked regulators for a {v}% increase in transmission charges."),
        F("municipal_bond_yield", "yield_pct", 3.40, "%", "price",
          "Long-dated municipal bond yields stand at {v}%."),
        F("solar_itc", "credit_pct", 30, "%", "regulation",
          "Public-power solar projects can claim a {v}% investment tax credit."),
        F("interconnection_queue", "study_wait_months", 38, "months", "regulation",
          "Projects entering the regional interconnection queue wait about {v} months for a study."),
        F("peak_demand", "record_gw", 18.60, "GW", "demand",
          "The regional system peak demand record stands at {v} GW."),
        F("ppa_prices", "usd_per_mwh", 57, "USD/MWh", "price",
          "Solar power purchase agreements in the region are pricing around ${v} per MWh."),
        F("riverton_hedge_ratio", "hedged_pct", 70, "%", "account",
          "Riverton has hedged about {v}% of next summer's expected load.", AC),
        F("riverton_solar_project", "size_mw", 40, "MW", "account",
          "Riverton's planned solar-plus-storage project is sized at {v} MW.", AC),
        F("clearwater_ppa_volume", "contracted_mw", 150, "MW", "account",
          "Clearwater contracts about {v} MW of renewable supply through us.", AC),
    ),
}

# ---------------------------------------------------------------------------------------------------------------
# Scripted market events, months 1..6 (one per month per field)
# ---------------------------------------------------------------------------------------------------------------
EVENTS: dict[str, list[MarketEvent]] = {
    "insurance": [
        _event("insurance", "ins-reinsurance-jump", "Reinsurance renewal rates jump", 1, 8,
               "The client opened with the reinsurance jump and I had nothing on it. She had already heard her "
               "property renewal would be up double digits.",
               F("reinsurance_rates", "renewal_change_pct", 22, "%", "price",
                 "Property catastrophe reinsurance rates rose {v}% at the spring treaty renewals, the sharpest "
                 "rise in years."),
               F("casualty_reinsurance_rates", "renewal_change_pct", 12, "%", "price",
                 "Casualty reinsurance treaty rates rose {v}% at the spring renewals as reinsurers priced in "
                 "reserve pressure.")),
        _event("insurance", "ins-ohio-ai-claims-rule", "Ohio sets rules for AI in claims", 2, 12,
               "Dana asked whether Ohio's new rule on AI claim denials will slow her claims down. The brief never "
               "mentioned it.",
               F("ohio_ai_claims_rule", "denial_review", "adjuster", "", "regulation",
                 "Ohio's new rule requires a licensed human {v} to sign off on any claim denial that an AI model "
                 "recommends."),
               F("ohio_ai_claims_rule", "compliance_window_days", 90, "days", "regulation",
                 "Carriers have {v} days to bring their AI claims tools into line with the Ohio rule."),
               F("property_rate_cut", "expected_cut_pct", 8, "%", "price",
                 "Analyst note: regional carriers are said to be preparing property rate cuts of about {v}% to "
                 "win back share.", AN, "wrong")),
        _event("insurance", "ins-cat-loss-quarter", "Record hail and wind loss quarter", 3, 6,
               "Dana wanted to know how the record hail quarter hits her Texas plant renewal. The brief had last "
               "quarter's pricing and nothing on the storm losses.",
               F("hail_cat_losses", "insured_losses_usd_bn", 31, "USD billion", "disruption",
                 "Severe hail and wind storms caused about ${v} billion of insured losses this quarter, concentrated in "
                 "Texas."),
               F("property_rates", "renewal_change_pct", 17, "%", "price",
                 "After the storm quarter, commercial property premiums for mid-size manufacturers are rising "
                 "about {v}% at renewal."),
               F("halvard_texas_exit", "expected_status", "non-renew", "", "competitor",
                 "Analyst note: Halvard Mutual is expected to {v} its Texas commercial property book after the "
                 "storm losses.", AN, "right")),
        _event("insurance", "ins-halvard-exits-texas", "Halvard Mutual exits Texas property", 4, 9,
               "The client asked who is still writing Texas property after Halvard's exit. I didn't even know "
               "Halvard had left.",
               F("halvard_texas_exit", "status", "non-renew", "", "competitor",
                 "Halvard Mutual will {v} all Texas commercial property policies at expiry and has stopped quoting "
                 "new business."),
               F("texas_property_capacity", "active_carriers", 10, "carriers", "competitor",
                 "Only {v} carriers are still actively quoting mid-size manufacturing property in Texas."),
               F("copperline_renewal_quote", "indicated_change_pct", 24, "%", "account",
                 "Copperline's incumbent carrier indicated a {v}% increase on the Texas plant's property renewal.",
                 AC)),
        _event("insurance", "ins-cyber-exclusion", "Carriers adopt a new cyber exclusion", 5, 11,
               "Priya asked whether the new cyber exclusion leaves a gap in her clinics' cover. The brief said "
               "nothing about the wording change.",
               F("cyber_exclusion", "wording", "infrastructure", "", "regulation",
                 "Most carriers now attach a cyber exclusion that removes cover for state-backed attacks on "
                 "critical {v}."),
               F("cyber_rates", "renewal_change_pct", 26, "%", "price",
                 "Cyber premiums for mid-size healthcare buyers are now rising about {v}% at renewal."),
               F("cyber_exclusion", "expected_status", "rollback", "", "regulation",
                 "Analyst note: some brokers expect a {v} of the new cyber exclusion before year end.",
                 AN, "wrong")),
        _event("insurance", "ins-verdict-wave", "Verdict wave pushes casualty pricing", 6, 7,
               "Dana had read about the verdict wave and asked what it does to her liability renewal. The brief was "
               "silent on it.",
               F("nuclear_verdicts", "count_half_year", 43, "verdicts", "disruption",
                 "Courts returned {v} liability verdicts above ten million dollars in the last half year."),
               F("casualty_rates", "renewal_change_pct", 19, "%", "price",
                 "General liability and commercial auto premiums are now rising about {v}% at renewal as verdicts "
                 "climb.")),
    ],
    "logistics": [
        _event("logistics", "log-diesel-spike", "Diesel spike lifts fuel surcharges", 1, 9,
               "Marcus opened by asking why his fuel surcharge jumped, and the brief still showed last quarter's "
               "diesel price.",
               F("diesel_price", "usd_per_gal", 4.45, "USD/gal", "price",
                 "Retail diesel has jumped to ${v} per gallon after refinery outages."),
               F("diesel_surcharge", "pct_of_linehaul", 34, "%", "price",
                 "Carrier fuel surcharges have climbed to {v}% of linehaul."),
               F("diesel_price", "expected_usd_per_gal", 3.20, "USD/gal", "price",
                 "Analyst note: a trading newsletter expects diesel to fall back to ${v} per gallon within weeks.",
                 AN, "wrong")),
        _event("logistics", "log-port-strike", "Dockworkers strike at Kestrel Bay", 2, 14,
               "The client asked how long the Kestrel Bay strike would hold up his containers. There was nothing in "
               "the brief about the strike.",
               F("kestrel_bay_labour", "status", "strike", "", "disruption",
                 "Dockworkers at the Port of Kestrel Bay are on {v}, and vessels are waiting at anchor."),
               F("kestrel_bay_port", "container_dwell_days", 11, "days", "disruption",
                 "Import containers are now sitting about {v} days at Kestrel Bay terminals."),
               F("rail_embargo", "expected_status", "embargo", "", "disruption",
                 "Analyst note: the railroad is expected to put an {v} on Kestrel Bay intermodal traffic if the "
                 "dock dispute drags on.", AN, "right")),
        _event("logistics", "log-rail-embargo", "Railroad embargoes Kestrel Bay intermodal", 3, 8,
               "Marcus asked whether we could reroute his containers around the rail embargo. The brief had nothing "
               "on it.",
               F("rail_embargo", "status", "embargo", "", "disruption",
                 "The Western Plains railroad has placed an {v} on intermodal containers out of Kestrel Bay."),
               F("rail_intermodal_service", "on_time_pct", 61, "%", "disruption",
                 "Intermodal trains out of Kestrel Bay are now running only {v}% on time."),
               F("fernway_stockouts", "pct_of_skus", 12, "%", "account",
                 "Fernway reports {v}% of store SKUs out of stock while import containers wait.", AC)),
        _event("logistics", "log-tariff-change", "Duty on trans-Pacific home goods rises", 4, 10,
               "The client asked what the new duty does to his landed cost. The brief still had the old tariff.",
               F("transpacific_tariff", "duty_pct", 25, "%", "regulation",
                 "Home furnishings imported on the trans-Pacific lane now carry a {v}% duty."),
               F("kestrel_bay_labour", "status", "settled", "", "disruption",
                 "The Kestrel Bay dockworker dispute is {v}, and vessels are berthing again."),
               F("kestrel_bay_port", "container_dwell_days", 6, "days", "disruption",
                 "Import containers are sitting about {v} days at Kestrel Bay terminals as the backlog clears.")),
        _event("logistics", "log-truckload-glut", "Truckload capacity glut drags spot rates", 5, 12,
               "Alana wanted to renegotiate her contract rate because spot rates had fallen. I didn't know the "
               "market had loosened.",
               F("truckload_spot_rates", "usd_per_mile", 1.85, "USD/mile", "price",
                 "Dry van truckload spot rates have fallen to ${v} per mile as capacity floods the market."),
               F("truckload_capacity", "tender_rejection_pct", 5, "%", "demand",
                 "Carriers are now turning down only {v}% of contract truckload tenders."),
               F("reefer_spot_rates", "usd_per_mile", 2.40, "USD/mile", "price",
                 "Refrigerated truckload spot rates have eased to ${v} per mile.")),
        _event("logistics", "log-reefer-rule", "California rule on trailer refrigeration", 6, 8,
               "Alana asked about California's electric reefer rule and whether our carriers comply. The brief "
               "skipped regulation entirely.",
               F("reefer_emissions_rule", "status", "electric", "", "regulation",
                 "California now requires new trailer refrigeration units sold for its roads to be fully {v}."),
               F("diesel_price", "usd_per_gal", 3.95, "USD/gal", "price",
                 "Retail diesel has eased to ${v} per gallon."),
               F("diesel_surcharge", "pct_of_linehaul", 29, "%", "price",
                 "Carrier fuel surcharges have eased to {v}% of linehaul.")),
    ],
    "energy": [
        _event("energy", "nrg-rate-case", "Transmission rate case approved", 1, 11,
               "Gloria asked what the approved transmission rate case adds to her costs, and the brief didn't have "
               "it.",
               F("transmission_rate_case", "increase_pct", 9, "%", "regulation",
                 "Regulators approved a {v}% increase in Northline Transmission's charges, effective next quarter."),
               F("natural_gas_price", "usd_per_mmbtu", 3.55, "USD/MMBtu", "price",
                 "Natural gas at the regional hub has risen to ${v} per MMBtu."),
               F("ppa_prices", "usd_per_mwh", 61, "USD/MWh", "price",
                 "Solar power purchase agreements in the region now price around ${v} per MWh.")),
        _event("energy", "nrg-rate-rise", "Interest-rate rise lifts financing costs", 2, 13,
               "The client asked how the rate rise changes the financing on her solar project. The brief still had "
               "last quarter's yields.",
               F("municipal_bond_yield", "yield_pct", 4.15, "%", "price",
                 "Long-dated municipal bond yields have climbed to {v}% after the central bank's rate rise."),
               F("project_finance_rate", "loan_rate_pct", 7.50, "%", "price",
                 "Construction loans for renewable projects now price around {v}%."),
               F("solar_itc", "expected_credit_pct", 22, "%", "regulation",
                 "Analyst note: lawmakers are expected to cut the public-power solar investment tax credit to {v}%.",
                 AN, "right")),
        _event("energy", "nrg-queue-reform", "Interconnection queue reform", 3, 7,
               "Gloria asked where her solar project stands under the new queue rules. The brief didn't mention the "
               "reform.",
               F("interconnection_queue_rule", "study_process", "cluster", "", "regulation",
                 "The grid operator will now study new interconnection requests in annual {v} batches instead of "
                 "one at a time."),
               F("interconnection_queue", "study_wait_months", 26, "months", "regulation",
                 "Projects entering the interconnection queue now wait about {v} months for a study."),
               F("wholesale_power_price", "expected_drop_pct", 15, "%", "price",
                 "Analyst note: a regional trading desk expects wholesale power prices to drop {v}% this summer on "
                 "new solar supply.", AN, "wrong")),
        _event("energy", "nrg-solar-credit-cut", "Solar tax credit cut for public power", 4, 9,
               "Gloria asked whether the credit cut kills her solar project economics. The brief still had the old "
               "credit.",
               F("solar_itc", "credit_pct", 22, "%", "regulation",
                 "Public-power solar projects can now claim only a {v}% investment tax credit."),
               F("ppa_prices", "usd_per_mwh", 68, "USD/MWh", "price",
                 "Solar power purchase agreements in the region have risen to around ${v} per MWh."),
               F("riverton_project_budget", "overrun_pct", 12, "%", "account",
                 "Riverton's board flagged that its solar project budget is now {v}% over plan.", AC)),
        _event("energy", "nrg-heat-wave", "Heat wave sets a demand record", 5, 10,
               "Tomas asked whether the heat-wave emergency means the grid operator can curtail his campus. The "
               "brief never mentioned it.",
               F("peak_demand", "record_gw", 19.75, "GW", "demand",
                 "A heat wave pushed regional peak demand to a record {v} GW."),
               F("wholesale_power_price", "usd_per_mwh", 93, "USD/MWh", "price",
                 "Day-ahead wholesale power averaged ${v} per MWh during the heat wave."),
               F("grid_emergency", "status", "emergency", "", "disruption",
                 "The grid operator issued an energy {v} alert and asked large users to cut load.")),
        _event("energy", "nrg-capacity-auction", "Capacity auction clears high", 6, 8,
               "Tomas asked whether the new mandatory curtailment rule applies to his data campus. The brief had no "
               "regulation in it at all.",
               F("capacity_price", "usd_per_mw_day", 290, "USD/MW-day", "price",
                 "The regional capacity auction cleared at ${v} per MW-day."),
               F("demand_response_rule", "status", "mandatory", "", "regulation",
                 "The state now requires large new loads to join a {v} curtailment program during grid "
                 "emergencies.")),
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


def account_owner(field: str, subject: str) -> str | None:
    """The account whose exposures list this subject first (used to keep account notes with their client)."""
    for account in ACCOUNTS[field]:
        if subject in account["exposures"]:
            return account["id"]
    return None
