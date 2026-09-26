"""Deterministic rehearsal checks. Reads visible records only, never answer_key.

Output is a small versioned UI contract, not an evaluation score or a production
LLM run. The shipped cache is rebuilt with `python viewer/build_demo.py`.
"""
from __future__ import annotations
import datetime as dt
import re

POLICY = {"behavioural_days": 120, "contact_days": 30, "stated_days": None}
TITLES = {"decision_maker": "Decision maker", "investing_style": "Investing style",
          "risk_attitude": "Risk attitude", "retirement_date": "Retirement plan",
          "bond_switch": "Bond switch", "fee_rate": "Advisory fee"}


def day(value):
    return dt.date.fromisoformat(str(value)[:10])


def latest_prep(record, today):
    preps = record.get("preps", [])
    future = [p for p in preps if p["date"][:10] >= today]
    return min(future, key=lambda p: p["date"]) if future else max(preps, key=lambda p: p["date"], default=None)


def attribute(text):
    t = text.lower()
    for attr, pattern in [("decision_maker", r"decision|runs the money"),
                          ("investing_style", r"index|fund investor|investing style"),
                          ("retirement_date", r"retir"), ("bond_switch", r"bond switch"),
                          ("fee_rate", r"fee"), ("risk_attitude", r"risk|panic seller|conservative")]:
        if re.search(pattern, t):
            return attr
    return None


def sections(text):
    key = re.search(r"Key points:\s*(.*?)(?:\n\s*Holdings:|\n\s*Disclosures:|$)", text, re.S)
    disclosure = text.split("Disclosures:", 1)[-1].strip() if "Disclosures:" in text else ""
    return ([x.strip().removeprefix("- ") for x in key.group(1).splitlines() if x.strip()] if key else [],
            [s.strip().removeprefix("- ") for s in re.split(r"(?<=[.!?])\s+|\n", disclosure) if s.strip()])


def compile_prep(record, today):
    baseline = latest_prep(record, today)
    if not baseline:
        return {"sections": [], "counters": {"checked": 0, "flagged": 0, "expired": 0}, "available": False}
    as_of = baseline["date"][:10]
    notes = [n for n in record["notes"] if n["date"][:10] <= as_of and not n.get("superseded_by")]
    events = [e for e in record["events"] if e["date"][:10] <= as_of]
    ids = {n["id"] for n in record["notes"]} | {e["id"] for e in record["events"]}
    ids |= {a["claim_id"] for a in record["approved_language"]} | {r["ref_id"] for r in record["references"]}
    keys, disclosures = sections(baseline["text"])
    key_rows, changes, disclosure_rows, questions = [], [], [], []
    owners = {n["attribute"]: n for n in notes if n.get("approved")}
    def mark(row, kind, reason, citations, **extras):
        citations = list(dict.fromkeys(c for c in citations if c in ids))
        if citations:
            row.update(mark=kind, reason=reason, citations=citations, **extras)
        return row
    for i, text in enumerate(keys):
        attr = attribute(text)
        row = {"id": f"key-{i}", "text": text, "checked": bool(attr), "attribute": attr}
        relevant = [n for n in notes if attr and (n.get("attribute") == attr or attribute(n["text"]) == attr)]
        if attr in owners:
            n = owners[attr]
            row["text"] = f"{TITLES.get(attr, attr)}: {n['value']}"
            row["citations"] = [n["id"]]
            row["badge"] = f"{n['date']} · banker · confirmed"
            key_rows.append(row)
            continue
        if attr == "decision_maker":
            junior = [n for n in relevant if n.get("author") == "junior"]
            bank = [n for n in relevant if n.get("author") == "banker"]
            emails = [e for e in events if e.get("type") == "email_to_banker"]
            if junior and bank:
                n, b = max(junior, key=lambda x: x["date"]), max(bank, key=lambda x: x["date"])
                age = (day(as_of) - day(n["date"])).days
                holders = record["client"].get("household", [])
                named = [h["name"] for h in holders if h["name"].split()[0].lower() in b["text"].lower()]
                replacement = "Brief both holders; confirm who leads the decisions."
                question = "Who should lead investment decisions, and how would you both like to be involved?"
                if named:
                    replacement = f"The banker note points to {named[0]}. Brief both holders and confirm."
                mark(row, "contradicted", f"The junior's assumption conflicts with the banker note and {len(emails)} client email(s)." + (f" The label is {age} days old; its 120-day validity has expired." if age > 120 else ""),
                     [n["id"], b["id"]] + [e["id"] for e in emails], replacement=replacement, question=question,
                     action="Ask instead", expired=age > 120, badge=f"{n['date']} · junior · {age} days")
        if attr == "investing_style" and "index" in text.lower():
            original = sorted([n for n in notes if re.search(r"only do index|index.only|index portfolio", n["text"], re.I) and n.get("author") == "banker"], key=lambda x:x["date"])
            buys = [e for e in events if e.get("type") == "trade_buy" and e.get("instrument_type") == "single_stock"]
            if original and buys:
                question = "You have been buying individual stocks. Has your index-only approach changed?"
                mark(row, "contradicted", f"The {original[0]['date'][:4]} index-only statement conflicts with {len(buys)} single-stock buys in the feed.",
                     [original[0]["id"]] + [e["id"] for e in buys], replacement="Index funds plus recent single-stock activity; confirm the current approach.", question=question, action="Ask instead")
                changes.append({"id":"stock-buys", "text":f"{len(buys)} single-stock buys since the index-only statement.", "mark":"feed", "reason":"Recorded trades disagree with the standing investing-style note.", "citations":[e["id"] for e in buys], "question":question, "action":"Ask instead", "checked":False})
        if attr == "fee_rate":
            refs = [r for r in record["references"] if r.get("kind") == "fee_schedule" and r.get("valid_from", "")[:10] <= as_of and (not r.get("valid_to") or r["valid_to"][:10] >= as_of)]
            if refs:
                ref = max(refs, key=lambda x:x["valid_from"])
                if str(ref["value_pct"]) not in text:
                    mark(row, "contradicted", "The preparation quotes an older fee schedule.", [ref["ref_id"]], replacement=f"Advisory fee: {ref['value_pct']}%", action="Replace")
        if not row.get("mark") and relevant:
            n = max(relevant, key=lambda x:x["date"])
            age = max(0, (day(as_of)-day(n["date"])).days)
            row.update(citations=[n["id"]], badge=f"{n['date']} · {n.get('author','banker')} · {age} days")
            if n.get("author") in ("assistant", "junior") and age > POLICY["behavioural_days"]:
                mark(row, "expired", "This behavioural label is older than 120 days. Reconfirm before relying on it.", [n["id"]], expired=True, replacement="Reconfirm this label on the call.", question=f"Is this still accurate: {text}", action="Ask instead")
        key_rows.append(row)
    for event in events:
        if event.get("type") == "beneficiary_changed" and not any("beneficiar" in n["text"].lower() for n in notes):
            changes.append({"id":f"change-{event['id']}", "text":f"Beneficiary changed on {day(event['date']).strftime('%d %b').lstrip('0')}.", "mark":"feed", "reason":"The activity feed records this change; the notes never mention it.", "citations":[event["id"]], "action":"Raise it", "question":"Would you like to review the beneficiary change and any related family plans?", "checked":True})
    for i, text in enumerate(disclosures):
        row = {"id":f"disclosure-{i}", "text":text, "checked":True}
        approved = record["approved_language"]
        # Prefer semantic applies_to; the legacy cabinet uses fixed ordered AS ids.
        subject = "performance" if re.search(r"performance|returns|outperform", text, re.I) else "capital_risk"
        ref = next((a for a in approved if a.get("applies_to") == subject), next((a for a in approved if a["claim_id"] == ("AS-01" if subject == "performance" else "AS-02")), None))
        if ref and text != ref["text"]:
            severity = 4 if re.search(r"consistently outperform|will outperform|guaranteed returns|cannot lose", text, re.I) else (1 if text in ("Past performance is no guarantee of future results.", "Investments can go down as well as up, and you could get back less.") else 2)
            mark(row, "disclosure", f"Does not match {ref['claim_id']}. " + ("Severity 4: a promise." if severity == 4 else ("Severity 1: wording drift." if severity == 1 else "Use the locked disclosure wording.")),
                 [ref["claim_id"]], replacement=ref["text"], severity=severity, action=None if severity == 1 else "Replace")
        disclosure_rows.append(row)
    # Surface existing cached harness questions if their complete basis resolves.
    for q in record.get("harness_questions", []):
        if q.get("citations") and all(c in ids for c in q["citations"]):
            questions.append(q)
    groups = [{"id":"key", "title":"Key points", "rows":key_rows}, {"id":"changes", "title":"What changed since last call", "rows":changes},
              {"id":"holdings", "title":"Holdings", "rows":[]}, {"id":"disclosures", "title":"Disclosures", "rows":disclosure_rows},
              {"id":"questions", "title":"Questions to ask", "rows":questions}]
    rows = [r for group in groups for r in group["rows"]]
    return {"available":True, "engine":"local-rehearsal-rules-v1", "as_of":as_of, "baseline_id":baseline["id"], "sections":groups,
            "counters":{"checked":sum(bool(r.get("checked")) for r in rows), "flagged":sum(bool(r.get("mark")) and r.get("checked", False) for r in rows), "expired":sum(bool(r.get("expired")) for r in rows)}}
