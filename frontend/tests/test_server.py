"""Tests for the Pregame viewer server. Standard library unittest; no network (the one database test points at a
closed port on 127.0.0.1 and is skipped when pymongo is not installed).

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import re
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server as S  # noqa: E402

HAS_PYMONGO = importlib.util.find_spec("pymongo") is not None


# ---------------------------------------------------------------------------------------------------------------
# Sample ledger payloads (shapes copied from the real pregame_alex ledger)
# ---------------------------------------------------------------------------------------------------------------
SAMPLES = {
    "seed": ("world", {"seeded": ["policy:retirement", "rules:retirement", "tools:retirement", "policy:families",
                                  "rules:families", "tools:families", "policy:business_owners",
                                  "rules:business_owners", "tools:business_owners", "guardrails:global"]}),
    "event_load": ("world", {"action": "load_world", "base_facts": 38, "events": 18,
                             "fields": ["retirement", "families", "business_owners"]}),
    "event_fire": ("world", {"action": "fire_event", "event_id": "ret-rmd-age", "field": "retirement",
                             "title": "RMD starting age rises",
                             "fact_ids": ["a@2026-02-11", "b@2026-02-11", "c@2026-02-11"]}),
    "feedback": ("world", {"feedback_id": "fb-ret-rmd-age-e8949453", "event_id": "ret-rmd-age",
                           "brief_id": "e8949453d0724f85894fc89eee2f155f"}),
    "brief": ("drafter", {"brief_id": "78fa8a52408a4a55981861c6edafb0e4", "field": "retirement",
                          "account_id": "okafor-household", "as_of": "2026-01-05T00:00:00+00:00",
                          "fact_ids": ["f1", "f2", "f3", "f4", "f5", "f6"],
                          "versions": {"policy": 1, "rules": 1, "tools": 1, "guardrails": 1}}),
    "proposal": ("improver", {"proposal_id": "prop-478062b0", "field": "retirement", "kind": "policy",
                              "key": "retirement", "base_version": 1,
                              "diff": ["include_kinds: + regulation, + disruption", "max_facts: 6 -> 10"],
                              "rationale": "...", "idem_key": "9437b0"}),
    "eval": ("gate", {"proposal_id": "prop-07e9a6ac", "field": "retirement", "kind": "policy", "key": "retirement",
                      "tier": "G",
                      "tuning": {"candidate": {"mean_accuracy": 0.905556}, "champion": {"mean_accuracy": 0.844444}},
                      "heldout": {"candidate": {"mean_accuracy": 0.966667}, "champion": {"mean_accuracy": 0.938889},
                                  "win": False, "reasons": ["mean accuracy 0.94 -> 0.97 (+0.03); needs at least +0.05"]},
                      "outcome": "rejected", "decision": "Rejected: held-out accuracy 0.97 vs champion 0.94 is inside "
                                                         "the 0.05 margin."}),
    "commit": ("gate", {"kind": "policy", "key": "retirement", "version": 2, "base_version": 1, "rationale": "...",
                        "proposal_id": "prop-cff89922", "restores": None}),
    "reject": ("gate", {"proposal_id": "prop-07e9a6ac", "field": "retirement", "kind": "policy", "key": "retirement",
                        "status": "rejected", "decision": "Rejected: held-out accuracy 0.97 vs champion 0.94 is inside "
                                                          "the 0.05 margin."}),
    "refused": ("gate", {"proposal_id": "prop-2b77cedd", "field": "retirement", "kind": "scenarios",
                         "key": "retirement:heldout",
                         "decision": "Refused: scenarios is frozen; the harness may not change its own yardstick."}),
}


def kind_of(sample_name: str) -> str:
    return sample_name.split("_")[0]


class SummaryTests(unittest.TestCase):
    def summary(self, name: str, ctx=None) -> str:
        actor, payload = SAMPLES[name]
        return S.summarize(kind_of(name), actor, payload, ctx)

    def test_every_kind_gives_one_plain_sentence(self):
        for name in SAMPLES:
            with self.subTest(name=name):
                text = self.summary(name)
                self.assertTrue(text.endswith((".", '."')), text)
                self.assertNotIn("\n", text)
                self.assertNotEqual(text, f"{SAMPLES[name][0]} {kind_of(name)}", "fell back unexpectedly")

    def test_seed(self):
        self.assertEqual(self.summary("seed"),
                         "Seeded version 1 of 10 config surfaces (guardrails, policy, rules, tools).")

    def test_event_load_and_fire(self):
        self.assertEqual(self.summary("event_load"),
                         "Loaded the simulated world: 38 base facts and 18 scripted market events across 3 fields.")
        self.assertEqual(self.summary("event_fire"),
                         "Market event fired in retirement: RMD starting age rises (3 new facts).")

    def test_feedback_with_and_without_lookup(self):
        self.assertIn("missed a material change", self.summary("feedback"))
        ctx = {"event_titles": {"ret-rmd-age": "RMD starting age rises"},
               "feedback_texts": {"fb-ret-rmd-age-e8949453": "Ruth asked about the RMD age, and I didn't have it."}}
        self.assertEqual(self.summary("feedback", ctx),
                         'Advisor feedback after RMD starting age rises: '
                         '"Ruth asked about the RMD age, and I didn\'t have it."')

    def test_brief(self):
        self.assertEqual(self.summary("brief"),
                         "Drafted a brief for okafor-household as of 2026-01-05 from 6 facts "
                         "(policy v1, rules v1, tools v1, guardrails v1).")

    def test_proposal(self):
        self.assertEqual(self.summary("proposal"),
                         "Proposed a change to policy:retirement v1 (prop-478062b0): "
                         "include_kinds: + regulation, + disruption; max_facts: 6 -> 10.")

    def test_eval(self):
        self.assertEqual(self.summary("eval"),
                         "Scored prop-07e9a6ac (tier G) on held-out scenarios: candidate 0.97 vs champion 0.94, "
                         "not a win; rejected.")
        actor, payload = SAMPLES["eval"]
        tuning_only = dict(payload, heldout=None)
        self.assertIn("did not reach held-out", S.summarize("eval", actor, tuning_only))

    def test_commit_reject_refused(self):
        self.assertEqual(self.summary("commit"), "Committed policy:retirement v2 (was v1) from prop-cff89922.")
        self.assertEqual(self.summary("reject"),
                         "Rejected prop-07e9a6ac: held-out accuracy 0.97 vs champion 0.94 is inside the 0.05 margin.")
        self.assertEqual(self.summary("refused"),
                         "Refused prop-2b77cedd: scenarios is frozen; the harness may not change its own yardstick.")

    def test_other_refusals(self):
        self.assertIn("Stopped the improver from reading heldout",
                      S.summarize("refused", "improver", {"attempted": "heldout", "reason": "not allowed"}))
        self.assertIn("Blocked a brief for okafor-household: 2 guardrail violations",
                      S.summarize("refused", "guardrails", {"what": "brief", "account_id": "okafor-household",
                                                            "violations": ["no-advice", "cite-facts"]}))

    def test_fallbacks(self):
        self.assertEqual(S.summarize("teleport", "improver", {"x": 1}), "improver teleport")
        self.assertEqual(S.summarize("seed", "world", {}), "world seed")
        self.assertEqual(S.summarize("eval", "gate", "not a dict"), "gate eval")

    def test_status_field_ref(self):
        expected = {"proposal": "pending", "eval": "rejected", "commit": "committed", "reject": "rejected",
                    "refused": "refused", "brief": None, "seed": None, "feedback": None}
        for name, (actor, payload) in SAMPLES.items():
            kind = kind_of(name)
            if kind in expected:
                with self.subTest(kind=kind):
                    self.assertEqual(S.entry_status(kind, payload), expected[kind])
        self.assertEqual(S.entry_status("refused", {"attempted": "x"}), None)
        self.assertEqual(S.entry_field("commit", SAMPLES["commit"][1]), "retirement")
        self.assertEqual(S.entry_ref("reject", SAMPLES["reject"][1]), {"type": "proposal", "id": "prop-07e9a6ac"})
        self.assertEqual(S.entry_ref("brief", SAMPLES["brief"][1])["type"], "brief")

    def test_activity_item_shape(self):
        entry = {"_id": 21, "seq": 21, "kind": "reject", "actor": "gate", "payload": SAMPLES["reject"][1],
                 "sim_time": dt.datetime(2026, 3, 17, tzinfo=dt.timezone.utc),
                 "recorded_at": dt.datetime(2026, 9, 26, 17, 32, 43, 80000, tzinfo=dt.timezone.utc),
                 "hash": "532f", "prev_hash": "8186"}
        item = S.activity_item(entry)
        self.assertEqual(set(item), {"seq", "kind", "actor", "actor_label", "sim_time", "recorded_at", "field",
                                     "summary", "summary_technical", "status", "ref", "hash", "prev_hash", "payload"})
        self.assertEqual(item["actor_label"], "test gate (code)")
        self.assertEqual(item["summary_technical"], S.summarize("reject", "gate", SAMPLES["reject"][1]))
        self.assertEqual(item["sim_time"], "2026-03-17T00:00:00Z")
        self.assertEqual(item["recorded_at"], "2026-09-26T17:32:43Z")
        self.assertEqual(item["status"], "rejected")


class PlainSummaryTests(unittest.TestCase):
    """The judges' sentence: vocabulary applied at display time, built from the numbers."""
    CTX = {"proposals": {
        "prop-07e9a6ac": {"kind": "policy", "field": "retirement", "tier": "G", "cand": 0.966667, "champ": 0.938889},
        "prop-cff89922": {"kind": "policy", "field": "retirement", "tier": "G", "cand": 0.938889, "champ": 0.736508},
        "prop-2b77cedd": {"kind": "scenarios", "field": "retirement", "tier": "X", "cand": None, "champ": None}}}

    def plain(self, name, ctx=None):
        actor, payload = SAMPLES[name]
        return S.plain_summary(kind_of(name), actor, payload, self.CTX if ctx is None else ctx)

    def test_the_coordinators_example(self):
        self.assertEqual(self.plain("reject"),
                         "Test gate rejected the proposed change to what goes into the brief (retirement): the "
                         "proposed version answered 97% of unseen test meetings' questions correctly vs 94% for the "
                         "current version, a gain too small to trust.")

    def test_every_kind_uses_the_vocabulary(self):
        banned = re.compile(r"held-?out|tuning|champion|candidate|ledger|proposal|committed|include_kinds|"
                            r"max_facts|prefer_exposed|tier", re.I)
        for name in SAMPLES:
            with self.subTest(name=name):
                text = self.plain(name)
                self.assertTrue(text.endswith((".", '."')), text)
                self.assertIsNone(banned.search(text), text)

    def test_sentences(self):
        self.assertEqual(self.plain("commit"),
                         "Test gate adopted the proposed change to what goes into the brief (retirement) as version 2 "
                         "(was version 1): the proposed version answered 94% of unseen test meetings' questions "
                         "correctly vs 74% for the current version.")
        self.assertEqual(self.plain("refused"),
                         "Test gate refused the proposed change to the unseen test meetings' questions (retirement): "
                         "it is never allowed, because it would change how the system is graded.")
        self.assertEqual(self.plain("proposal"),
                         "Opus proposed a change to what goes into the brief (retirement): also include tax and rule "
                         "facts, and market-shock facts; use up to 10 facts (was 6).")
        self.assertIn("answered 97% of the questions correctly vs 94% for the current version; rejected",
                      self.plain("eval"))
        self.assertIn("adopted automatically if it wins", self.plain("eval"))

    def test_reasons_come_from_the_stored_decision(self):
        decision = ("Rejected: held-out accuracy 0.72 -> 0.92, but guardrail violations rose 0.00 -> 0.17 per run; "
                    "context grew 240 -> 365 tokens (+52%, over the +50% allowed).")
        too_small, reasons = S.plain_reasons(decision)
        self.assertFalse(too_small)
        self.assertEqual(reasons, ["it failed more safety checks",
                                   "the brief size grew too much (240 -> 365, +52%; the limit is +50%)"])
        self.assertIn("false alarms (flagging news that doesn't affect the client)",
                      " ".join(S.plain_reasons("false alarms rose 0.17 -> 0.25 per run")[1]))

    def test_fallbacks_and_labels(self):
        self.assertEqual(S.plain_summary("teleport", "improver", {}), "Opus (proposer) teleport")
        self.assertIn("rejected", S.plain_summary("reject", "gate", {"proposal_id": "p", "decision": "Rejected: no."}))
        self.assertEqual(S.actor_names("improver")[0], "Opus (proposer)")
        self.assertEqual(S.actor_names("improver", "Haiku")[0], "Haiku (proposer)")
        self.assertEqual(S.actor_names("drafter")[0], "Sonnet (writer)")
        self.assertEqual(S.actor_names("world")[0], "simulated world")
        self.assertEqual(S.actor_names("owner:sam")[0], "sam (person)")

    def test_stored_text_is_untouched(self):
        actor, payload = SAMPLES["reject"]
        before = json.dumps(payload, sort_keys=True)
        item = S.activity_item({"seq": 21, "kind": "reject", "actor": actor, "payload": payload}, self.CTX)
        self.assertEqual(json.dumps(item["payload"], sort_keys=True), before)
        self.assertEqual(item["payload"]["decision"], payload["decision"])
        self.assertEqual(item["actor"], "gate")


class ChainTests(unittest.TestCase):
    def entries(self, n: int = 4) -> list:
        out, prev = [], "GENESIS"
        for seq in range(1, n + 1):
            h = f"h{seq}"
            out.append({"seq": seq, "prev_hash": prev, "hash": h})
            prev = h
        return out

    def test_intact_chain(self):
        c = S.check_chain(self.entries())
        self.assertEqual(c, {"entries": 4, "links_ok": True, "first_break_seq": None, "last_seq": 4,
                             "last_hash": "h4"})

    def test_broken_link(self):
        e = self.entries()
        e[2]["prev_hash"] = "tampered"
        c = S.check_chain(e)
        self.assertFalse(c["links_ok"])
        self.assertEqual(c["first_break_seq"], 3)

    def test_first_entry_must_start_at_genesis(self):
        e = self.entries()
        e[0]["prev_hash"] = "x"
        self.assertEqual(S.check_chain(e)["first_break_seq"], 1)

    def test_gap_and_order(self):
        e = self.entries(5)
        del e[2]
        self.assertEqual(S.check_chain(e)["first_break_seq"], 4)
        self.assertTrue(S.check_chain(list(reversed(self.entries())))["links_ok"])

    def test_empty(self):
        self.assertEqual(S.check_chain([]), {"entries": 0, "links_ok": True, "first_break_seq": None,
                                             "last_seq": 0, "last_hash": None})


class HelperTests(unittest.TestCase):
    def test_changed_keys(self):
        self.assertEqual(S.changed_keys(None, {"a": 1}), [])
        self.assertEqual(S.changed_keys({"max_facts": 6, "x": 1}, {"max_facts": 8, "x": 1}), ["max_facts"])
        rules_a = [{"id": "r1", "text": "a"}, {"id": "r2", "text": "b"}]
        rules_b = [{"id": "r1", "text": "a"}, {"id": "r3", "text": "c"}]
        self.assertEqual(S.changed_keys(rules_a, rules_b), ["r2", "r3"])
        self.assertEqual(S.changed_keys(rules_a, list(reversed(rules_a))), ["order"])

    def test_norm_date(self):
        self.assertEqual(S.norm_date("2026-05-11"), "2026-05-11")
        self.assertEqual(S.norm_date("2026-5-1"), "2026-05-01")
        self.assertEqual(S.norm_date(dt.datetime(2026, 5, 12)), "2026-05-12")
        self.assertEqual(S.norm_date(dt.datetime(2026, 5, 12, 23, tzinfo=dt.timezone(dt.timedelta(hours=-5)))),
                         "2026-05-13")
        self.assertIsNone(S.norm_date(None))

    def test_feed_text_and_notable(self):
        sell = {"date": dt.datetime(2026, 5, 12), "client_id": "C01", "type": "trade_sell", "event_id": "E-0316",
                "amount": 110000, "instrument": "Balanced Growth Fund"}
        self.assertEqual(S.feed_text(sell), "trade_sell instrument=Balanced Growth Fund, amount=110000")
        notable = [sell, {"type": "tool_used"}, {"type": "email_to_banker"}, {"type": "document_requested"},
                   {"type": "reply_lag"}, {"type": "article_read", "topic": "tech stocks"},
                   {"type": "email_opened", "email": "Advisory fee schedule update (effective June 1)"},
                   {"type": "trade_buy"}]
        routine = [{"type": "app_login"}, {"type": "cash_in"}, {"type": "cash_out"},
                   {"type": "article_read", "topic": "retirement income"},
                   {"type": "email_opened", "email": "monthly market letter"},
                   {"type": "email_ignored", "email": "monthly market letter"}]
        for ev in notable:
            self.assertTrue(S.is_notable(ev), ev)
        for ev in routine:
            self.assertFalse(S.is_notable(ev), ev)

    def test_feed_item_carries_instrument_type(self):
        buy = {"date": dt.datetime(2026, 6, 3, tzinfo=dt.timezone.utc), "client_id": "C02", "type": "trade_buy",
               "event_id": "E-0327", "instrument": "Lumen Arc Semiconductors (LASC)", "amount": 4000,
               "instrument_type": "single_stock"}
        self.assertEqual(S.feed_text(buy), "trade_buy instrument=Lumen Arc Semiconductors (LASC), "
                                           "instrument_type=single_stock, amount=4000")
        item = S.build_timeline([], [], [buy])[0]
        self.assertEqual((item["date"], item["instrument_type"], item["notable"]), ("2026-06-03", "single_stock", True))

    def test_bson_dates_and_string_dates_mix(self):
        notes = [{"note_id": "N-0002", "date": dt.datetime(2021, 3, 10, tzinfo=dt.timezone.utc), "text": "a"},
                 {"note_id": "N-0001", "date": "2022-04-11", "text": "b"}]
        preps = [{"prep_id": "P-0001", "date": dt.datetime(2026, 5, 11, tzinfo=dt.timezone.utc), "text": "c"}]
        self.assertEqual([(i["date"], i["id"]) for i in S.build_timeline(notes, preps, [])],
                         [("2021-03-10", "N-0002"), ("2022-04-11", "N-0001"), ("2026-05-11", "P-0001")])

    def test_expected_actions_and_levels_on_the_answer_side(self):
        preps = [{"prep_id": "P-0004", "date": "2026-08-10", "text": "CALL PREP"}]
        faults = {"P-0004": [{"fault_type": "compliance_drift", "severity": 1, "level": "warning", "doc_id": "P-0004"}]}
        expected = {"P-0004": [
            {"_id": "x", "prep_id": "P-0004", "client_id": "C01", "attribute": "risk_attitude", "action": "ask",
             "evidence": ["E-0320"], "reason": "feed says otherwise", "date": dt.datetime(2026, 8, 10)},
            {"prep_id": "P-0004", "attribute": "risk_attitude", "action": "flag", "note_id": "N-0014"}]}
        item = S.build_timeline([], preps, [], faults, expected)[0]
        self.assertEqual(item["faults"][0]["level"], "warning")
        self.assertEqual(item["expected"], [
            {"attribute": "risk_attitude", "action": "ask", "reason": "feed says otherwise", "evidence": ["E-0320"]},
            {"attribute": "risk_attitude", "action": "flag", "note_id": "N-0014"}])
        hidden = S.build_timeline([], preps, [], None, expected)[0]
        self.assertNotIn("expected", hidden)
        self.assertNotIn("expected", S.strip_faults([item])[0])

    def test_harness_prep_items(self):
        doc = {"_id": "CR-1:P-0001", "run_id": "CR-1", "policy_name": "harness", "prep_id": "P-0001",
               "client_id": "C01", "date": dt.datetime(2026, 5, 11, tzinfo=dt.timezone.utc), "markdown": "PREP",
               "claims": [{"attribute": "risk_attitude"}]}
        item = S.harness_prep_item(doc)
        self.assertEqual({k: item[k] for k in ("date", "kind", "id", "client_id", "prep_id", "text", "policy",
                                                "run_id")},
                         {"date": "2026-05-11", "kind": "harness_prep", "id": "CR-1:P-0001", "client_id": "C01",
                          "prep_id": "P-0001", "text": "PREP", "policy": "harness", "run_id": "CR-1"})
        self.assertEqual(item["fields"]["claims"], [{"attribute": "risk_attitude"}])
        preps = [{"prep_id": "P-0001", "date": "2026-05-11", "text": "baseline"}]
        tl = S.build_timeline([], preps, [], {}, {"P-0001": [{"attribute": "a", "action": "ask"}]}, [doc])
        self.assertEqual([i["kind"] for i in tl], ["prep", "harness_prep"])
        self.assertEqual(tl[1]["expected"], [{"attribute": "a", "action": "ask"}])

    def test_run_summary(self):
        t0 = dt.datetime(2026, 9, 26, 17, 50, 1, tzinfo=dt.timezone.utc)
        ledger, prev = [], "GENESIS"
        for seq in range(1, 22):
            kind = "refused" if seq == 18 else "event"
            ledger.append({"seq": seq, "prev_hash": prev, "hash": f"h{seq}", "kind": kind,
                           "recorded_at": t0 + dt.timedelta(minutes=seq)})
            prev = f"h{seq}"
        champ = {"mean_accuracy": 0.719841, "per_scenario": {"s": 1}}
        proposals = [
            {"_id": "p1", "status": "rejected", "tier": "G", "field": "retirement", "kind": "policy",
             "diff": ["max_facts: 6 -> 10"], "decision": "Rejected", "heldout_champion": champ,
             "heldout_candidate": {"mean_accuracy": 0.91}},
            {"_id": "p2", "status": "committed", "tier": "G", "field": "retirement", "kind": "policy",
             "diff": ["prefer_exposed: False -> True"], "decision": "Committed", "heldout_champion": champ,
             "heldout_candidate": {"mean_accuracy": 0.938889}},
            {"_id": "p3", "status": "rejected", "tier": "X", "field": "retirement", "kind": "scenarios",
             "decision": "Refused", "heldout_champion": None},
        ]
        row = S.run_summary("pregame_run_a", ledger, proposals, ["claude-sonnet-5", None],
                            {"label": "Run A", "replayable": False, "note": "earlier live run", "mode": "recorded",
                             "proposer": "Opus", "proposer_model": "Opus 5.5", "writer_model": "Sonnet 5",
                             "grader_model": "Haiku 4.5"})
        self.assertEqual(row["chain"], {"entries": 21, "links_ok": True, "last_seq": 21})
        self.assertEqual(row["proposals_by_status"]["committed"], 1)
        self.assertEqual(row["proposals_by_status"]["rejected"], 2)
        self.assertEqual(row["models"], ["claude-sonnet-5"])
        self.assertEqual((row["first_at"], row["last_at"]), ("2026-09-26T17:51:01Z", "2026-09-26T18:11:01Z"))
        self.assertEqual([c["id"] for c in row["committed"]], ["p2"])
        self.assertNotIn("per_scenario", row["committed"][0]["heldout_champion"])
        self.assertEqual([r["id"] for r in row["rejected"]], ["p1"])
        self.assertEqual(row["refused"], 1)
        self.assertEqual(row["champion_mean_accuracy"], [0.719841, 0.719841])
        self.assertEqual((row["label"], row["stage"], row["replayable"], row["recorded_from"]),
                         ("Run A", False, False, None))
        self.assertEqual((row["mode"], row["proposer_model"], row["writer_model"], row["grader_model"]),
                         ("recorded", "Opus 5.5", "Sonnet 5", "Haiku 4.5"))

    def test_run_info_and_llm_mode(self):
        tmp = Path(tempfile.mkdtemp(prefix="viewer-test-"))
        try:
            write_json(tmp / "run_info.json", {"_note": "ignored", "pregame_demo": {"mode": "replay", "x": 1},
                                               "pregame_run_a": "Run A"})
            info = S.load_run_info(tmp)
            self.assertEqual(info, {"pregame_demo": {"mode": "replay"}, "pregame_run_a": {"label": "Run A"}})
            cfg = {"PREGAME_LLM_MODE": "live"}
            self.assertEqual(S.llm_mode_for("pregame_demo", cfg, info), ("replay", "run_info"))
            self.assertEqual(S.llm_mode_for("pregame_run_a", cfg, info), ("live", "env"))
            self.assertEqual(S.llm_mode_for("pregame_x", {}, info), (None, "env"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_cabinet_source_picker(self):
        rows = [{"name": "pregame_demo", "cabinet_preps": 0, "cabinet_runs": 0},
                {"name": "pregame_dev_old", "cabinet_preps": 24, "cabinet_written_at": "2026-09-26T17:00:00Z"},
                {"name": "pregame_dev_new", "cabinet_preps": 0, "cabinet_runs": 1,
                 "cabinet_written_at": "2026-09-26T18:31:37Z"}]
        self.assertEqual(S.pick_cabinet_source("pregame_demo", rows), ("pregame_dev_new", True))
        self.assertEqual(S.pick_cabinet_source("pregame_dev_old", rows), ("pregame_dev_old", False))
        self.assertEqual(S.pick_cabinet_source("pregame_demo", rows[:1]), ("pregame_demo", False))
        self.assertEqual(S.pick_cabinet_source("pregame_unknown", []), ("pregame_unknown", False))

    def test_timeline_sorts_and_carries_faults(self):
        notes = [{"note_id": "N-0010", "date": "2026-05-13", "author": "banker", "text": "Called him"},
                 {"note_id": "N-0001", "date": "2022-04-11", "author": "banker", "text": "Opened"}]
        preps = [{"prep_id": "P-0003", "date": "2026-05-13", "text": "CALL PREP", "used_note_ids": ["N-0010"]}]
        events = [{"event_id": "E-0316", "date": dt.datetime(2026, 5, 13), "type": "trade_sell", "amount": 1},
                  {"event_id": "E-0001", "date": dt.datetime(2026, 5, 1), "type": "app_login"}]
        faults = {"P-0003": [{"fault_type": "sticky_label", "attribute": "risk_attitude", "claimed": "cautious",
                              "truth": "growth", "fix": "ask", "evidence": ["E-0320"], "doc_id": "P-0003"}]}
        tl = S.build_timeline(notes, preps, events, faults)
        self.assertEqual([(i["date"], i["kind"], i["id"]) for i in tl],
                         [("2022-04-11", "note", "N-0001"), ("2026-05-01", "feed", "E-0001"),
                          ("2026-05-13", "feed", "E-0316"), ("2026-05-13", "note", "N-0010"),
                          ("2026-05-13", "prep", "P-0003")])
        self.assertEqual(tl[-1]["faults"][0]["truth"], "growth")
        self.assertNotIn("doc_id", tl[-1]["faults"][0])
        self.assertEqual(tl[0]["faults"], [])
        self.assertNotIn("faults", tl[1])
        no_faults = S.build_timeline(notes, preps, events, None)
        self.assertFalse(any("faults" in i for i in no_faults))
        self.assertFalse(any("faults" in i for i in S.strip_faults(tl)))

    def test_jsonable(self):
        value = {"when": dt.datetime(2026, 9, 26, 17, 40, tzinfo=dt.timezone.utc), "raw": b"\x00\x01",
                 "nan": float("nan"), "day": dt.date(2026, 1, 5), "nested": [(1, 2)]}
        out = S.jsonable(value)
        self.assertEqual(out["when"], "2026-09-26T17:40:00Z")
        self.assertEqual(out["raw"], "base64:AAE=")
        self.assertIsNone(out["nan"])
        self.assertEqual(out["day"], "2026-01-05")
        self.assertEqual(out["nested"], [[1, 2]])
        json.dumps(out, allow_nan=False)

    @unittest.skipUnless(HAS_PYMONGO, "pymongo (bson) not installed")
    def test_objectid_becomes_id(self):
        from bson import ObjectId
        oid = ObjectId("6ab7fce47cbcb46dec9d6dae")
        self.assertEqual(S.clean({"_id": oid, "client_id": "C01"}), {"id": "6ab7fce47cbcb46dec9d6dae",
                                                                     "client_id": "C01"})
        self.assertEqual(S.clean({"_id": "ret-rmd-age", "id": "ret-rmd-age"}), {"id": "ret-rmd-age"})


class PublicCopyTests(unittest.TestCase):
    """The public copy: no answer-side data, no per-client / per-prep rows, no local paths."""

    def test_scrub_local_paths(self):
        self.assertEqual(S.scrub_local_paths({"scorer": "D:\\work\\cabinet-eval\\score_preps.py",
                                              "src": ["/home/someone/work/notes.md (section x)"],
                                              "ok": "ratio 3:4 and a/b"}),
                         {"scorer": "score_preps.py", "src": ["notes.md (section x)"], "ok": "ratio 3:4 and a/b"})

    def test_public_findings(self):
        summary = {"preps": 24, "trusted_prep_rate": 0.292, "faulty_preps_by_client": {"C01": "2/4"},
                   "expected_actions": 10}
        findings = {"db": "pregame_demo", "research": {"source": "D:\\work\\x\\report.md"},
                    "cabinet": {"baseline": summary, "harness": None, "baseline_faults_by_client": {"C01": "2/4"},
                                "baseline_preps": [{"prep_id": "P-0001", "fault_types": ["sticky_label"]}],
                                "baseline_source": "baseline (no harness)"},
                    "cabinet_runs": [{"id": "CR-1", "scorer": "D:\\work\\cabinet-eval\\score_preps.py",
                                      "prep_ids": ["P-0001"], "scores": {"naive": summary}}]}
        out = S.public_findings(findings)
        self.assertEqual(out["cabinet"], {"baseline": {"preps": 24, "trusted_prep_rate": 0.292,
                                                       "expected_actions": 10},
                                          "harness": None, "baseline_source": "baseline (no harness)",
                                          "harness_source": None})
        self.assertEqual(out["cabinet_runs"], [{"id": "CR-1", "scores": {"naive": {
            "preps": 24, "trusted_prep_rate": 0.292, "expected_actions": 10}}}])
        self.assertEqual(out["research"]["source"], "report.md")
        self.assertTrue(out["public"])
        self.assertEqual(S._drop_per_row_keys({"counts": {"harness_preps": 4}, "harness_preps": [{"x": 1}]}),
                         {"counts": {"harness_preps": 4}})

    def test_baseline_file_without_preps(self):
        tmp = Path(tempfile.mkdtemp(prefix="viewer-test-"))
        try:
            write_json(tmp / "cabinet_baseline.json", {"source": "baseline (no harness)",
                                                       "summary": {"preps": 24, "trusted_prep_rate": 0.292}})
            cab = S.findings_from_files(tmp)["cabinet"]
            self.assertEqual((cab["baseline"]["preps"], cab["baseline_preps"], cab["baseline_faults_by_client"]),
                             (24, [], None))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ReadOnlyTests(unittest.TestCase):
    def test_pipeline_guard(self):
        S.assert_read_only_pipeline([{"$match": {}}, {"$group": {"_id": "$field", "n": {"$sum": 1}}}])
        for bad in ([{"$out": "x"}], [{"$merge": {"into": "x"}}],
                    [{"$facet": {"a": [{"$match": {}}, {"$out": "y"}]}}]):
            with self.assertRaises(S.ApiError):
                S.assert_read_only_pipeline(bad)

    def test_no_write_calls_in_the_source(self):
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        writes = re.findall(r"\.(insert_one|insert_many|update_one|update_many|replace_one|delete_one|delete_many|"
                            r"drop_collection|drop_database|drop|create_collection|create_index|create_indexes|"
                            r"bulk_write|find_one_and_\w+|rename|with_transaction)\(", source)
        self.assertEqual(writes, [])

    def test_wrapper_offers_only_reads(self):
        public = {n for n in dir(S.ReadOnlyMongo) if not n.startswith("_")}
        self.assertEqual(public, {"find", "count", "aggregate", "collections", "databases", "close"})


class SecretTests(unittest.TestCase):
    URI = "mongodb+srv://viewer:S3cr3t%21pw@cluster9.abcde.mongodb.net/?retryWrites=true"

    def test_secrets_detect_and_redact(self):
        sec = S.Secrets(self.URI)
        self.assertTrue(sec.leaks("x S3cr3t%21pw y"))
        self.assertTrue(sec.leaks("x S3cr3t!pw y"))
        self.assertTrue(sec.leaks("mongodb+srv://anything"))
        self.assertFalse(sec.leaks('{"ok": true}'))
        red = sec.redact("failed: cluster9.abcde.mongodb.net:27017 as viewer with S3cr3t!pw " + self.URI)
        for token in ("S3cr3t", "cluster9", "abcde", "mongodb+srv"):
            self.assertNotIn(token, red)

    def test_fixtures_hold_no_connection_string(self):
        fixtures = sorted((ROOT / "fixtures").glob("*.json"))
        if not fixtures:
            self.skipTest("no fixtures yet (run server.py --snapshot)")
        env = S.read_env_file(ROOT / "viewer.env")
        password = ""
        m = re.match(r"^[a-z+]+://([^:@/]*):([^@/]*)@", env.get("MONGODB_URI", ""))
        if m:
            password = m.group(2)
        for path in fixtures:
            text = path.read_text(encoding="utf-8")
            with self.subTest(fixture=path.name):
                self.assertNotIn("mongodb+srv", text)
                self.assertNotIn("mongodb://", text)
                if len(password) >= 4:
                    self.assertNotIn(password, text)
                    self.assertNotIn(S.urllib.parse.unquote(password), text)


# ---------------------------------------------------------------------------------------------------------------
# HTTP tests (a real server on 127.0.0.1, port chosen by the OS)
# ---------------------------------------------------------------------------------------------------------------
class ServerCase(unittest.TestCase):
    server = None
    base = ""

    @classmethod
    def start(cls, cfg: dict, offline: bool, static_dir: Path, fixtures_dir: Path, data_dir: Path) -> None:
        cls.server = S.make_server(cfg, "127.0.0.1", 0, offline, static_dir, fixtures_dir, data_dir)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.server is not None:
            cls.server.shutdown()
            cls.server.server_close()
            cls.server.app.close()

    def get(self, path: str, method: str = "GET"):
        req = urllib.request.Request(self.base + path, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, resp.read().decode("utf-8"), resp.headers
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read().decode("utf-8"), exc.headers

    def get_json(self, path: str):
        status, body, _ = self.get(path)
        return status, json.loads(body)


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj), encoding="utf-8")


class OfflineServerTests(ServerCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="viewer-test-"))
        fixtures, static, data = cls.tmp / "fixtures", cls.tmp / "static", cls.tmp / "data"
        for d in (fixtures, static, data):
            d.mkdir()
        (static / "index.html").write_text("<!doctype html><title>Pregame viewer</title>", encoding="utf-8")
        (static / "app.js").write_text("console.log(1)", encoding="utf-8")
        (cls.tmp / "viewer.env").write_text("MONGODB_URI=mongodb+srv://u:TopSecretPw@c.x.mongodb.net/\n",
                                            encoding="utf-8")
        write_json(fixtures / "_meta.json", {"snapshot_at": "2026-09-26T17:55:00Z", "pregame_db": "pregame_test",
                                             "pregame_dbs": [{"name": "pregame_test", "ledger": 3, "in_snapshot": True},
                                                             {"name": "pregame_run_x", "ledger": 21,
                                                              "in_snapshot": True}]})
        write_json(fixtures / "overview.json", {"generated_at": "2026-09-26T17:55:00Z", "offline": False,
                                                "snapshot_at": None, "pregame_db": "pregame_test", "counts": {}})
        write_json(fixtures / "overview@pregame_run_x.json", {"pregame_db": "pregame_run_x", "counts": {"ledger": 21}})
        write_json(fixtures / "runs.json", {"runs": [{"db": "pregame_run_x"}]})
        write_json(fixtures / "cabinet_vocabulary.json", {"vocabulary": {"risk_attitude": {"moderate": "..."}}})
        write_json(fixtures / "activity.json", {"items": [{"seq": s, "kind": "event", "summary": "x"}
                                                          for s in (1, 2, 3)], "last_seq": 3})
        write_json(fixtures / "briefs.json", [{"id": "b2"}, {"id": "b1"}])
        write_json(fixtures / "findings.json", {"pregame_eval": [{"split": "heldout"}]})
        write_json(fixtures / "cabinet_client_C01.json", {"client": {"client_id": "C01"}, "timeline": [
            {"date": "2026-07-18", "kind": "prep", "id": "P-0003", "text": "CALL PREP",
             "faults": [{"fault_type": "sticky_label"}], "expected": [{"attribute": "risk_attitude", "action": "ask"}]},
            {"date": "2026-07-18", "kind": "harness_prep", "id": "CR-1:P-0003", "text": "HARNESS PREP"}]})
        write_json(fixtures / "db_cabinet.clients.json", {"db": "cabinet", "collection": "clients", "count": 3,
                                                          "limit": 50, "docs": [{"id": "1"}, {"id": "2"}, {"id": "3"}]})
        write_json(data / "cabinet_baseline.json", {"source": "baseline", "summary": {"preps": 24,
                   "faulty_preps_by_client": {"C01": "2/4"}}, "preps": [], "matches_answer_key": True})
        cfg = S.load_config(cls.tmp / "viewer.env", environ={})
        cls.start(cfg, True, static, fixtures, data)

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_overview_says_offline_with_snapshot_time(self):
        status, body = self.get_json("/api/overview")
        self.assertEqual(status, 200)
        self.assertTrue(body["offline"])
        self.assertEqual(body["snapshot_at"], "2026-09-26T17:55:00Z")

    def test_db_switching_between_snapshotted_databases(self):
        status, body = self.get_json("/api/overview?db=pregame_run_x")
        self.assertEqual(status, 200)
        self.assertEqual((body["active_db"], body["counts"]["ledger"], body["offline"]), ("pregame_run_x", 21, True))
        self.assertEqual([d["name"] for d in body["pregame_dbs"]], ["pregame_test", "pregame_run_x"])
        _, body = self.get_json("/api/overview")
        self.assertEqual(body["active_db"], "pregame_test")
        for bad in ("admin", "pregame_nope", "cabinet"):
            with self.subTest(db=bad):
                status, _ = self.get_json(f"/api/proposals?db={bad}")
                self.assertEqual(status, 403)
        status, body = self.get_json("/api/activity?db=pregame_run_x")
        self.assertEqual(status, 503)  # in the snapshot's database list, but this fixture was not written

    def test_runs_and_vocabulary(self):
        self.assertEqual(self.get_json("/api/runs")[1], {"runs": [{"db": "pregame_run_x"}]})
        self.assertIn("risk_attitude", self.get_json("/api/cabinet/vocabulary")[1]["vocabulary"])

    def test_activity_honours_after_seq_and_limit(self):
        _, body = self.get_json("/api/activity?after_seq=1&limit=300")
        self.assertEqual([i["seq"] for i in body["items"]], [2, 3])
        self.assertEqual(body["last_seq"], 3)
        _, body = self.get_json("/api/activity?after_seq=0&limit=1")
        self.assertEqual([i["seq"] for i in body["items"]], [3])
        status, _ = self.get_json("/api/activity?after_seq=abc")
        self.assertEqual(status, 400)

    def test_briefs_limit(self):
        _, body = self.get_json("/api/briefs?limit=1")
        self.assertEqual(body, [{"id": "b2"}])

    def test_findings_reads_data_files_and_snapshot_eval(self):
        status, body = self.get_json("/api/findings")
        self.assertEqual(status, 200)
        self.assertEqual(body["cabinet"]["baseline"]["preps"], 24)
        self.assertIsNone(body["cabinet"]["harness"])
        self.assertEqual(body["cabinet"]["baseline_faults_by_client"], {"C01": "2/4"})
        self.assertEqual(body["pregame_eval"], [{"split": "heldout"}])

    def test_cabinet_client_faults_toggle(self):
        _, with_faults = self.get_json("/api/cabinet/client/C01?faults=1")
        self.assertEqual(with_faults["timeline"][0]["faults"][0]["fault_type"], "sticky_label")
        self.assertEqual(with_faults["timeline"][0]["expected"][0]["action"], "ask")
        _, without = self.get_json("/api/cabinet/client/C01?faults=0")
        self.assertNotIn("faults", without["timeline"][0])
        self.assertNotIn("expected", without["timeline"][0])
        self.assertEqual(without["timeline"][1]["kind"], "harness_prep")
        _, other_db = self.get_json("/api/cabinet/client/C01?faults=1&db=pregame_run_x")
        self.assertEqual([i["kind"] for i in other_db["timeline"]], ["prep"])  # the default db's harness preps drop
        status, _ = self.get_json("/api/cabinet/client/C99")
        self.assertEqual(status, 404)

    def test_db_allowlist(self):
        for path in ("/api/db/admin/system.users", "/api/db/local/oplog.rs", "/api/db/config/x",
                     "/api/db/cabinet/system.views"):
            with self.subTest(path=path):
                status, body = self.get_json(path)
                self.assertEqual(status, 403)
                self.assertIn("error", body)
        status, body = self.get_json("/api/db/cabinet/clients?limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["docs"]), 2)
        status, body = self.get_json("/api/db/pregame_test/ledger")
        self.assertEqual(status, 200)
        self.assertEqual(body["docs"], [])

    def test_missing_fixture_is_a_clear_503(self):
        status, body = self.get_json("/api/world")
        self.assertEqual(status, 503)
        self.assertIn("snapshot", body["error"])

    def test_static_and_fixture_files(self):
        status, body, headers = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("Pregame viewer", body)
        status, _, headers = self.get("/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["Content-Type"])
        status, body, headers = self.get("/fixtures/overview.json")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        status, _, _ = self.get("/static/index.html")
        self.assertEqual(status, 200)

    def test_no_path_escapes_the_served_folders(self):
        for path in ("/../viewer.env", "/%2e%2e/viewer.env", "/fixtures/../viewer.env", "/fixtures/%2e%2e/viewer.env",
                     "/fixtures/..%5cviewer.env", "/static/../../viewer.env", "/C:/Windows/win.ini"):
            with self.subTest(path=path):
                status, body, _ = self.get(path)
                self.assertEqual(status, 404)
                self.assertNotIn("TopSecretPw", body)

    def test_read_only_methods_and_unknown_routes(self):
        status, _, _ = self.get("/api/overview", method="POST")
        self.assertEqual(status, 405)
        status, _, _ = self.get("/api/overview", method="DELETE")
        self.assertEqual(status, 405)
        status, _ = self.get_json("/api/nope")
        self.assertEqual(status, 404)


class OfflineCabinetFallbackTests(ServerCase):
    """Offline: harness preps and cabinet_runs come from the database itself, else the most recently written
    snapshotted database that has them, unless ?cabinet_db= says otherwise."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="viewer-test-"))
        fixtures, static, data = cls.tmp / "fixtures", cls.tmp / "static", cls.tmp / "data"
        for d in (fixtures, static, data):
            d.mkdir()
        rows = [{"name": "pregame_demo", "cabinet_preps": 0, "cabinet_runs": 0, "in_snapshot": True},
                {"name": "pregame_run_a", "cabinet_preps": 0, "cabinet_runs": 0, "in_snapshot": True},
                {"name": "pregame_cabinet_dev", "cabinet_preps": 24, "cabinet_runs": 1,
                 "cabinet_written_at": "2026-09-26T18:31:37Z", "in_snapshot": True}]
        write_json(fixtures / "_meta.json", {"snapshot_at": "2026-09-26T18:41:42Z", "pregame_db": "pregame_demo",
                                             "pregame_dbs": rows})
        for db in ("", "@pregame_run_a"):
            write_json(fixtures / f"overview{db}.json", {"llm_mode": "record", "counts": {}})
        prep = {"date": "2026-05-11", "kind": "prep", "id": "P-0001", "text": "baseline",
                "faults": [], "expected": []}
        harness = {"date": "2026-05-11", "kind": "harness_prep", "id": "CR-1:P-0001", "text": "harness"}
        write_json(fixtures / "cabinet_client_C01.json", {"client": {"client_id": "C01", "counts": {"harness_preps": 1}},
                                                          "db": "pregame_demo",
                                                          "cabinet_source_db": "pregame_cabinet_dev",
                                                          "timeline": [prep, harness]})
        write_json(fixtures / "cabinet_client_C01@pregame_cabinet_dev.json",
                   {"client": {"client_id": "C01", "counts": {"harness_preps": 1}}, "db": "pregame_cabinet_dev",
                    "cabinet_source_db": "pregame_cabinet_dev", "timeline": [prep, harness]})
        write_json(fixtures / "cabinet_clients.json", [{"client_id": "C01", "counts": {"harness_preps": 1},
                                                        "cabinet_source_db": "pregame_cabinet_dev"}])
        write_json(fixtures / "findings.json", {"db": "pregame_demo", "pregame_eval": [{"split": "heldout"}],
                                                "cabinet_source_db": "pregame_cabinet_dev",
                                                "cabinet_runs": [{"id": "CR-1"}]})
        write_json(fixtures / "findings@pregame_cabinet_dev.json", {"db": "pregame_cabinet_dev", "pregame_eval": [],
                                                                    "cabinet_source_db": "pregame_cabinet_dev",
                                                                    "cabinet_runs": [{"id": "CR-1"}]})
        write_json(data / "run_info.json", {"pregame_demo": {"mode": "replay"}, "pregame_run_a": {"mode": "recorded"}})
        cfg = S.load_config(cls.tmp / "missing.env", environ={"PREGAME_LLM_MODE": "live"})
        cls.start(cfg, True, static, fixtures, data)

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_llm_mode_per_database(self):
        for db, expected in (("pregame_demo", ("replay", "run_info")), ("pregame_run_a", ("recorded", "run_info"))):
            with self.subTest(db=db):
                _, body = self.get_json(f"/api/overview?db={db}")
                self.assertEqual((body["llm_mode"], body["llm_mode_source"]), expected)

    def test_fallback_to_the_database_with_harness_preps(self):
        for db in ("pregame_demo", "pregame_run_a"):
            with self.subTest(db=db):
                status, body = self.get_json(f"/api/cabinet/client/C01?faults=0&db={db}")
                self.assertEqual(status, 200)
                self.assertEqual((body["db"], body["cabinet_source_db"], body["cabinet_source_fallback"]),
                                 (db, "pregame_cabinet_dev", True))
                self.assertEqual([i["kind"] for i in body["timeline"]], ["prep", "harness_prep"])
        _, clients = self.get_json("/api/cabinet/clients?db=pregame_run_a")
        self.assertEqual((clients[0]["cabinet_source_db"], clients[0]["cabinet_source_fallback"],
                          clients[0]["counts"]["harness_preps"]), ("pregame_cabinet_dev", True, 1))
        _, findings = self.get_json("/api/findings?db=pregame_run_a")
        self.assertEqual((findings["cabinet_source_db"], findings["cabinet_source_fallback"],
                          findings["cabinet_runs"]), ("pregame_cabinet_dev", True, [{"id": "CR-1"}]))

    def test_explicit_cabinet_db(self):
        _, body = self.get_json("/api/cabinet/client/C01?db=pregame_demo&cabinet_db=pregame_run_a")
        self.assertEqual((body["cabinet_source_db"], body["cabinet_source_fallback"]), ("pregame_run_a", False))
        self.assertEqual([i["kind"] for i in body["timeline"]], ["prep"])  # run A has no harness preps
        _, body = self.get_json("/api/cabinet/client/C01?cabinet_db=pregame_cabinet_dev")
        self.assertEqual((body["cabinet_source_db"], body["cabinet_source_fallback"]), ("pregame_cabinet_dev", False))
        _, findings = self.get_json("/api/findings?cabinet_db=pregame_run_a")
        self.assertIsNone(findings["cabinet_runs"])
        for bad in ("admin", "cabinet", "pregame_nope"):
            with self.subTest(cabinet_db=bad):
                status, _ = self.get_json(f"/api/cabinet/clients?cabinet_db={bad}")
                self.assertEqual(status, 403)


class OfflinePublicSnapshotTests(ServerCase):
    """A public snapshot never serves the answer key, even for faults=1, and findings stay summaries-only even when
    the local data files are the full ones."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="viewer-test-"))
        fixtures, static, data = cls.tmp / "fixtures", cls.tmp / "static", cls.tmp / "data"
        for d in (fixtures, static, data):
            d.mkdir()
        write_json(fixtures / "_meta.json", {"snapshot_at": "2026-09-26T19:00:00Z", "public": True,
                                             "pregame_db": "pregame_demo",
                                             "pregame_dbs": [{"name": "pregame_demo", "in_snapshot": True}]})
        write_json(fixtures / "overview.json", {"counts": {}})
        write_json(fixtures / "findings.json", {"db": "pregame_demo", "pregame_eval": []})
        write_json(fixtures / "cabinet_client_C01.json", {
            "client": {"client_id": "C01"}, "db": "pregame_demo", "answer_key_included": False, "timeline": [
                {"kind": "prep", "id": "P-0003", "faults": [{"fault_type": "sticky_label"}],
                 "expected": [{"action": "ask"}]}]})
        write_json(data / "cabinet_baseline.json", {"source": "baseline", "summary": {
            "preps": 24, "faulty_preps_by_client": {"C01": "2/4"}}, "preps": [{"prep_id": "P-0001", "faults": []}]})
        cls.start(S.load_config(cls.tmp / "missing.env", environ={}), True, static, fixtures, data)

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_faults_request_gets_no_answer_key(self):
        _, body = self.get_json("/api/cabinet/client/C01?faults=1")
        self.assertIs(body["answer_key_included"], False)
        self.assertNotIn("faults", body["timeline"][0])
        self.assertNotIn("expected", body["timeline"][0])
        _, overview = self.get_json("/api/overview")
        self.assertIs(overview["public"], True)

    def test_answer_side_databases_are_refused(self):
        for path in ("/api/db/cabinet_truth/answer_key", "/api/db/harness/episodes"):
            with self.subTest(path=path):
                self.assertEqual(self.get_json(path)[0], 403)

    def test_findings_are_summaries_only(self):
        _, body = self.get_json("/api/findings")
        self.assertEqual(body["cabinet"]["baseline"], {"preps": 24})
        for key in ("baseline_preps", "baseline_faults_by_client", "harness_preps"):
            self.assertNotIn(key, body["cabinet"])


class OnlineWithoutDatabaseTests(ServerCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="viewer-test-"))
        (cls.tmp / "static").mkdir()
        (cls.tmp / "fixtures").mkdir()
        (cls.tmp / "data").mkdir()
        cfg = S.load_config(cls.tmp / "missing.env", environ={})
        cls.start(cfg, False, cls.tmp / "static", cls.tmp / "fixtures", cls.tmp / "data")

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_missing_uri_is_a_503_not_a_crash(self):
        for path in ("/api/overview", "/api/activity", "/api/cabinet/clients", "/api/dbs"):
            with self.subTest(path=path):
                status, body = self.get_json(path)
                self.assertEqual(status, 503)
                self.assertEqual(body["error"], "MONGODB_URI is not set")
                self.assertIn("--offline", body["hint"])

    def test_allowlist_is_checked_before_the_database(self):
        status, _ = self.get_json("/api/db/admin/system.users")
        self.assertEqual(status, 403)
        for path in ("/api/overview?db=admin", "/api/runs?db=x", "/api/proposals?db=cabinet_truth",
                     "/api/briefs?db=local", "/api/cabinet/clients?cabinet_db=admin",
                     "/api/cabinet/client/C01?cabinet_db=cabinet", "/api/findings?cabinet_db=local"):
            with self.subTest(path=path):
                status, _ = self.get_json(path)
                self.assertEqual(status, 403 if "runs" not in path else 503)
        status, _ = self.get_json("/api/db/cabinet/clients")
        self.assertEqual(status, 503)

    def test_findings_still_serve_files(self):
        status, body = self.get_json("/api/findings?cabinet_db=pregame_demo")
        self.assertEqual(status, 200)
        self.assertEqual(body["pregame_eval"], [])
        self.assertIn("pregame_eval_error", body)


@unittest.skipUnless(HAS_PYMONGO, "pymongo not installed")
class UnreachableDatabaseTests(ServerCase):
    PASSWORD = "Unit7estPassw0rd"

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="viewer-test-"))
        for d in ("static", "fixtures", "data"):
            (cls.tmp / d).mkdir()
        cfg = S.load_config(cls.tmp / "missing.env", environ={
            "MONGODB_URI": f"mongodb://viewer:{cls.PASSWORD}@127.0.0.1:9/?directConnection=true",
            "PREGAME_DB": "pregame_test", "VIEWER_DB_TIMEOUT_MS": "300"})
        cls.start(cfg, False, cls.tmp / "static", cls.tmp / "fixtures", cls.tmp / "data")

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_unreachable_database_is_a_503_without_the_secret(self):
        for path in ("/api/overview", "/api/overview", "/api/world", "/api/db/cabinet/clients"):
            with self.subTest(path=path):
                status, body, _ = self.get(path)
                self.assertEqual(status, 503)
                data = json.loads(body)
                self.assertEqual(data["error"], "database unavailable")
                self.assertNotIn(self.PASSWORD, body)
                self.assertNotIn("mongodb://", body)


class PortTests(unittest.TestCase):
    def test_default_port_and_fallback_to_the_next_free_one(self):
        self.assertEqual(S.DEFAULT_PORT, 8877)
        self.assertEqual(S.load_config(Path("missing.env"), environ={})["VIEWER_PORT"], "8877")
        blocker = S.socket.socket(S.socket.AF_INET, S.socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy = blocker.getsockname()[1]
        tmp = Path(tempfile.mkdtemp(prefix="viewer-test-"))
        server = None
        try:
            server = S.make_server_on_free_port({}, "127.0.0.1", busy, True, fixtures_dir=tmp, static_dir=tmp,
                                                data_dir=tmp)
            self.assertGreater(server.server_address[1], busy)
            self.assertLessEqual(server.server_address[1], busy + S.PORT_TRIES)
        finally:
            if server is not None:
                server.server_close()
            blocker.close()
            shutil.rmtree(tmp, ignore_errors=True)


class RealPublicSnapshotTests(unittest.TestCase):
    """When fixtures/ holds a public snapshot: nothing answer-side, nothing from other databases."""

    def setUp(self):
        meta_path = ROOT / "fixtures" / "_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
        if not meta.get("public"):
            self.skipTest("fixtures/ is not a public snapshot")
        self.meta = meta

    def test_contents(self):
        names = sorted(p.name for p in (ROOT / "fixtures").glob("*.json"))
        default = self.meta["pregame_db"]
        self.assertFalse([n for n in names if "cabinet_truth" in n or "@" in n or n.startswith("db_harness")])
        self.assertFalse([n for n in names if n.startswith("db_pregame_") and not n.startswith(f"db_{default}.")])
        self.assertEqual([d["name"] for d in self.meta["pregame_dbs"]], [default])
        for n in names:
            text = (ROOT / "fixtures" / n).read_text(encoding="utf-8")
            with self.subTest(fixture=n):
                for marker in ('"fault_type"', '"claimed_text"', '"fault_id"', '"faults"', '"expected"',
                               "cabinet_truth", '"baseline_preps"', '"faulty_preps_by_client"'):
                    self.assertNotIn(marker, text)
        client = json.loads((ROOT / "fixtures" / "cabinet_client_C01.json").read_text(encoding="utf-8"))
        self.assertIs(client["answer_key_included"], False)
        clients = json.loads((ROOT / "fixtures" / "cabinet_clients.json").read_text(encoding="utf-8"))
        self.assertFalse([c for c in clients if "faults" in (c.get("counts") or {})])


class RealFixturesOfflineTests(ServerCase):
    """Every endpoint, served offline from the real snapshot in fixtures/, answers 200 with the contract's shape."""

    @classmethod
    def setUpClass(cls) -> None:
        if not (ROOT / "fixtures" / "overview.json").is_file():
            raise unittest.SkipTest("no snapshot in fixtures/")
        cls.start(S.load_config(), True, ROOT / "static", ROOT / "fixtures", ROOT / "data")

    def test_every_endpoint(self):
        clients = json.loads((ROOT / "fixtures" / "cabinet_clients.json").read_text(encoding="utf-8"))
        dbs = json.loads((ROOT / "fixtures" / "dbs.json").read_text(encoding="utf-8"))
        checks = {
            "/api/overview": lambda b: {"generated_at", "offline", "snapshot_at", "pregame_db", "sim_time", "llm_mode",
                                        "provider", "counts", "chain", "proposals_by_status", "latest"} <= set(b)
            and b["offline"] is True and b["snapshot_at"],
            "/api/activity?after_seq=0&limit=300": lambda b: isinstance(b["items"], list) and "last_seq" in b,
            "/api/proposals": lambda b: isinstance(b, list),
            "/api/versions": lambda b: {"heads", "versions"} <= set(b),
            "/api/briefs?limit=20": lambda b: isinstance(b, list),
            "/api/world": lambda b: {"events", "feedback", "facts_by_field"} <= set(b),
            "/api/findings": lambda b: {"cabinet", "research", "pregame_eval"} <= set(b),
            "/api/cabinet/clients": lambda b: isinstance(b, list) and all("counts" in c for c in b),
            "/api/dbs": lambda b: isinstance(b["dbs"], list),
            "/api/runs": lambda b: all({"db", "chain", "proposals_by_status", "models", "first_at", "last_at",
                                        "committed", "rejected", "refused", "champion_mean_accuracy"} <= set(r)
                                       for r in b["runs"]),
            "/api/cabinet/vocabulary": lambda b: isinstance(b["vocabulary"], dict),
        }
        overview = json.loads((ROOT / "fixtures" / "overview.json").read_text(encoding="utf-8"))
        for d in overview["pregame_dbs"]:
            for endpoint in ("overview", "activity?after_seq=0", "proposals", "versions", "briefs", "world",
                             "findings", "cabinet/clients"):
                sep = "&" if "?" in endpoint else "?"
                checks[f"/api/{endpoint}{sep}db={d['name']}"] = lambda b: b is not None
        for c in clients:
            checks[f"/api/cabinet/client/{c['client_id']}?faults=1"] = lambda b: {"client", "timeline"} <= set(b)
            checks[f"/api/cabinet/client/{c['client_id']}?faults=0"] = \
                lambda b: not any("faults" in i for i in b["timeline"])
        for d in dbs["dbs"]:
            for coll in d["collections"]:
                checks[f"/api/db/{d['name']}/{coll['name']}?limit=20"] = lambda b: isinstance(b["docs"], list)
        for path, check in checks.items():
            with self.subTest(path=path):
                status, body = self.get_json(path)
                self.assertEqual(status, 200, body)
                self.assertTrue(check(body), path)


if __name__ == "__main__":
    unittest.main()
