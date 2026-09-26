"""The banker-cabinet adapter, policy and preps, on the VISIBLE files only (no Atlas; the scorer only in the
last test, skipped when cabinet-eval is absent)."""
from datetime import date, datetime

import pytest

from pregame import cabinet
from pregame.contracts import APPROVED_LANGUAGE

VISIBLE = cabinet.visible_dir()
pytestmark = pytest.mark.skipif(not (VISIBLE / "notes.json").exists(), reason="cabinet-eval visible data not present")


@pytest.fixture(scope="module")
def data():
    return cabinet.load_files(VISIBLE)


@pytest.fixture(scope="module")
def facts(data):
    return cabinet.build_facts(data)


def _f(facts, source_id, attr):
    hits = [f for f in facts if f["source_id"] == source_id and f["attribute"] == attr]
    assert len(hits) == 1, (source_id, attr, hits)
    return hits[0]


def _claims(prep, attr, kind=None):
    return [c for c in prep["claims"] if c["attribute"] == attr and (kind is None or c["kind"] == kind)]


def _prep(preps, cid, day):
    return next(p for p in preps if p["client_id"] == cid and p["date"] == day)


# --- adapter -----------------------------------------------------------------------------------------------------
def test_dates_parse_from_files_and_bson(data):
    assert all(isinstance(n["date"], date) for n in data["notes"])
    assert cabinet._day(datetime(2026, 6, 1)) == date(2026, 6, 1)
    assert cabinet._day({"$date": "2026-06-01T00:00:00Z"}) == date(2026, 6, 1)
    assert cabinet._day("2026-06-01") == date(2026, 6, 1)


def test_atlas_path_reads_the_same_facts(data, facts):
    mongomock = pytest.importorskip("mongomock")
    client = mongomock.MongoClient()
    raw = cabinet.load_files(VISIBLE)
    for name in cabinet.LIST_COLLECTIONS:
        docs = []
        for d in raw[name]:
            d = dict(d)
            for k in ("date", "valid_from", "valid_to"):
                if isinstance(d.get(k), date):
                    d[k] = datetime(d[k].year, d[k].month, d[k].day)   # BSON dates, as in Atlas v2
            docs.append(d)
        client["cabinet"][name].insert_many(docs)
    atlas = cabinet.load_data(client)
    assert atlas["source"] == "atlas:cabinet"
    assert [f["fact_id"] for f in cabinet.build_facts(atlas)] == [f["fact_id"] for f in facts]


def test_daniel_panic_label_then_growth_questionnaire(facts):
    label = _f(facts, "N-0011", "risk_attitude")
    assert (label["value"], label["author"], label["label"]) == ("cautious", "assistant", True)
    banker = _f(facts, "N-0010", "risk_attitude")
    assert (banker["value"], banker["author"], banker["valid_from"]) == ("cautious", "banker", date(2026, 5, 13))
    quiz = _f(facts, "E-0320", "risk_attitude")
    assert (quiz["value"], quiz["author"], quiz["kind"]) == ("growth", "client", "questionnaire")


def test_robert_index_only_statement_and_single_stock_buys(facts):
    said = _f(facts, "N-0002", "investing_style")
    assert (said["value"], said["author"], said["valid_from"]) == ("index_only", "banker", date(2021, 3, 10))
    buys = cabinet.single_stock_buys(facts, "C02", date(2026, 5, 15), date(2026, 8, 31))
    assert [b["source_id"] for b in buys] == ["E-0327", "E-0328", "E-0329", "E-0330", "E-0331", "E-0332"]
    assert all(b["value"]["instrument_type"] == "single_stock" for b in buys)


def test_brennans_decision_maker_sources(facts):
    assert _f(facts, "N-0004", "decision_maker")["value"] == "Lisa Brennan"
    assert _f(facts, "N-0028", "decision_maker")["value"] == "Lisa Brennan"        # "She runs the household finances"
    junior = _f(facts, "N-0029", "decision_maker")
    assert (junior["value"], junior["author"]) == ("Tom Brennan", "junior")
    email = _f(facts, "E-0334", "decision_maker")
    assert (email["value"], email["author"]) == ("Lisa Brennan", "client")
    chosen = cabinet.resolve(facts, "C03", "decision_maker", date(2026, 6, 22), cabinet.HARNESS_POLICY)
    assert chosen["value"] == "Lisa Brennan"
    naive = cabinet.resolve(facts, "C03", "decision_maker", date(2026, 6, 22), cabinet.NAIVE_POLICY)
    assert naive["value"] == "Tom Brennan"


def test_priya_plan_survives_a_junior_no_changes(facts):
    plan = _f(facts, "N-0037", "retirement_date")
    assert (plan["value"], plan["author"]) == ("2027-06", "banker")
    contact = _f(facts, "N-0039", "contact")
    assert contact["value"] == "no_changes"
    assert not [f for f in facts if f["source_id"] == "N-0039" and f["attribute"] != "contact"]   # no fact
    day = date(2026, 7, 21)
    assert cabinet.resolve(facts, "C04", "retirement_date", day, cabinet.HARNESS_POLICY)["value"] == "2027-06"
    wiped = cabinet.resolve(facts, "C04", "retirement_date", day, cabinet.NAIVE_POLICY)
    assert wiped["value"] is None and wiped["source_id"] == "N-0039"


def test_fee_reference_rows_and_the_1_june_change(facts):
    v1, v2 = _f(facts, "FEE-v1", "fee_rate"), _f(facts, "FEE-v2", "fee_rate")
    assert (v1["value"], v1["valid_to"]) == (0.85, date(2026, 5, 31))
    assert (v2["value"], v2["valid_from"], v2["valid_to"]) == (0.75, date(2026, 6, 1), None)
    assert _f(facts, "N-0045", "fee_rate")["value"] == 0.85                       # the note's stale fee


def test_disclosures_match_contracts(data):
    assert cabinet.locked_disclosures(data) == dict(APPROVED_LANGUAGE)
    assert cabinet.check_disclosures(data)


# --- policy -------------------------------------------------------------------------------------------------------
def test_policies_validate_and_bounds_are_enforced():
    cabinet.validate_policy(cabinet.NAIVE_POLICY)
    cabinet.validate_policy(cabinet.HARNESS_POLICY)
    for bad in ({"label_expiry_days": 10}, {"label_expiry_days": 400}, {"contradiction_threshold": 0},
                {"fee_from_reference": "yes"}, {"not_a_knob": True}):
        with pytest.raises(ValueError):
            cabinet.validate_policy(dict(cabinet.HARNESS_POLICY, **bad))


def test_harness_claims_on_the_known_cases(data):
    preps = cabinet.write_preps(data, cabinet.HARNESS_POLICY)
    assert len(preps) == 24 and [p["prep_id"] for p in preps] == [p["prep_id"] for p in data["preps_baseline"]]
    daniel = _prep(preps, "C01", "2026-07-18")
    assert _claims(daniel, "risk_attitude", "observation")[0]["value"] == "growth"
    assert _claims(daniel, "risk_attitude", "question")
    assert _claims(_prep(preps, "C01", "2026-08-10"), "risk_attitude", "flag")[0]["basis"][-1] == "N-0014"
    robert = _prep(preps, "C02", "2026-06-21")
    assert _claims(robert, "investing_style", "observation")[0]["value"] == "index_plus_single_stocks"
    assert "E-0327" in _claims(robert, "investing_style", "question")[0]["basis"]
    assert _claims(_prep(preps, "C02", "2026-07-19"), "investing_style", "flag")
    assert not _claims(_prep(preps, "C02", "2026-08-11"), "investing_style", "flag")
    assert _claims(_prep(preps, "C03", "2026-06-22"), "decision_maker")[0]["value"] == ["Tom Brennan", "Lisa Brennan"]
    assert _claims(_prep(preps, "C03", "2026-05-06"), "decision_maker")[0]["value"] == "Lisa Brennan"
    assert _claims(_prep(preps, "C04", "2026-08-15"), "retirement_date")[0]["value"] == "2027-06"
    assert _claims(_prep(preps, "C05", "2026-05-12"), "fee_rate")[0]["value"] == 0.85
    fee = _claims(_prep(preps, "C05", "2026-08-16"), "fee_rate")[0]
    assert (fee["value"], fee["basis"]) == (0.75, ["FEE-v2"])
    for p in preps:
        assert {c["value"]: c["text"] for c in _claims(p, "disclosure")} == dict(APPROVED_LANGUAGE)
        assert "built to protect your capital" not in p["text"]


def test_naive_policy_reproduces_the_faults(data):
    preps = cabinet.write_preps(data, cabinet.NAIVE_POLICY)
    assert _claims(_prep(preps, "C01", "2026-07-18"), "risk_attitude")[0]["value"] == "cautious"
    assert _claims(_prep(preps, "C02", "2026-06-21"), "investing_style")[0]["value"] == "index_only"
    assert _claims(_prep(preps, "C03", "2026-06-22"), "decision_maker")[0]["value"] == "Tom Brennan"
    assert _claims(_prep(preps, "C04", "2026-07-21"), "retirement_date")[0]["value"] is None
    assert _claims(_prep(preps, "C05", "2026-08-16"), "fee_rate")[0]["value"] == 0.85
    texts = [c["text"] for c in _claims(_prep(preps, "C05", "2026-08-16"), "disclosure")]
    assert "This portfolio is built to protect your capital." in texts
    assert not any(c["kind"] in ("question", "flag") for p in preps for c in p["claims"])


def test_knobs_change_claims(data, facts):
    base = cabinet.HARNESS_POLICY
    # contradiction threshold: 3 buys needed -> no question on 21 Jun (2 buys), a question on 19 Jul (3)
    strict = dict(base, contradiction_threshold=3)
    jun = cabinet.write_prep(data, facts, "C02", date(2026, 6, 21), strict, "T")
    jul = cabinet.write_prep(data, facts, "C02", date(2026, 7, 19), strict, "T")
    assert not _claims(jun, "investing_style", "question")
    assert _claims(jul, "investing_style", "question")
    # label expiry: a 30-day lifetime turns Daniel's 13 May behaviour label into a question by 20 Jun
    short = dict(base, label_expiry_days=30)
    p = cabinet.write_prep(data, facts, "C01", date(2026, 6, 20), short, "T")
    assert _claims(p, "risk_attitude", "question") and not _claims(p, "risk_attitude", "observation")
    p90 = cabinet.write_prep(data, facts, "C01", date(2026, 6, 20), base, "T")
    assert _claims(p90, "risk_attitude", "observation")[0]["value"] == "cautious"
    # brief both off -> the ranked holder alone
    one = cabinet.write_prep(data, facts, "C03", date(2026, 6, 22), dict(base, brief_both_holders_on_conflict=False), "T")
    assert _claims(one, "decision_maker")[0]["value"] == "Lisa Brennan"
    # fee from notes vs from the reference table
    notes_fee = cabinet.write_prep(data, facts, "C05", date(2026, 7, 22), dict(base, fee_from_reference=False), "T")
    assert _claims(notes_fee, "fee_rate")[0]["value"] == 0.85
    # flag knob
    noflag = cabinet.write_prep(data, facts, "C01", date(2026, 8, 10), dict(base, flag_no_changes_vs_activity=False), "T")
    assert not _claims(noflag, "risk_attitude", "flag")


def test_claims_schema_valid_for_both_policies(data):
    for policy in (cabinet.NAIVE_POLICY, cabinet.HARNESS_POLICY):
        for p in cabinet.write_preps(data, policy):
            assert cabinet.validate_claims(p, data) == [], (policy["name"], p["prep_id"])
            assert p["text"].startswith("CALL PREP:")


def test_preps_are_deterministic(data):
    a = cabinet.write_preps(data, cabinet.HARNESS_POLICY)
    b = cabinet.write_preps(cabinet.load_files(VISIBLE), cabinet.HARNESS_POLICY)
    assert a == b


def test_store_run_writes_preps_and_receipt(db, data):
    preps = cabinet.write_preps(data, cabinet.HARNESS_POLICY)
    receipt = cabinet.store_run(db, preps, cabinet.HARNESS_POLICY, {"harness": {"faults_total": 0}}, data["source"])
    assert db[cabinet.PREPS_COLLECTION].count_documents({"run_id": receipt["_id"]}) == 24
    stored = db[cabinet.RUNS_COLLECTION].find_one({"_id": receipt["_id"]})
    assert stored["policy"]["name"] == "harness" and stored["prep_count"] == 24
    names = {ix["name"] for ix in db[cabinet.PREPS_COLLECTION].list_indexes()}
    assert {"run_prep", "client_date"} <= names


def test_store_refuses_the_cabinet_databases(data):
    mongomock = pytest.importorskip("mongomock")
    for name in ("cabinet", "cabinet_truth", "production"):          # Sol review: only pregame* databases
        with pytest.raises(ValueError):
            cabinet.store_run(mongomock.MongoClient()[name], [], cabinet.HARNESS_POLICY, {}, "x")


@pytest.mark.skipif(not (cabinet.eval_dir() / "score_preps.py").exists(), reason="cabinet-eval scorer not present")
def test_scorer_subprocess_before_after(data):
    harness = cabinet.score(cabinet.write_preps(data, cabinet.HARNESS_POLICY))
    naive = cabinet.score(cabinet.write_preps(data, cabinet.NAIVE_POLICY))
    assert "preps" not in harness or isinstance(harness["preps"], int)     # aggregates only
    assert harness["preps"] == 24
    assert harness["faults_total"] < naive["faults_total"]
    assert harness["forbidden_promises"] == 0 and naive["forbidden_promises"] >= 1
