# Pregame frontend

The demo follows Fran’s **26 September front-end brief**: a desktop client record with three panes, cited call
preparation, and approved debrief notes. No chart, KPI dashboard, branding, or admin navigation is shown.

## Run on the presenting laptop

From the repository root, with Python 3.10 or newer:

```bash
python3 frontend/server.py --offline --no-browser
```

Open the URL printed by the server, normally http://127.0.0.1:8877/. No network, model call, package installation,
or MongoDB credentials are required in offline mode. Windows: `python frontend/server.py --offline --no-browser`.
The existing start scripts also work. Restart an older viewer process after updating the Python files.

## Stage path

1. **C08 Clara & Hugo Adeyemi** opens by default. The Notes tab shows the junior’s decision-maker assumption;
   Activity shows Clara’s two emails.
2. **Prepare call** renders five sections at 300 ms intervals. The decision-maker line is struck through and
   cites the banker note and emails. Click a source chip to select its record tab, scroll, and highlight it.
3. Use **Ask instead** to replace the claim with a call question. In Disclosures, **Replace** inserts the locked
   AS-01 sentence. The fee discrepancy cites the versioned fee schedule under Documents.
4. Select **C02 Robert Okafor**, then Prepare. Six actual recorded single-stock buys contradict the 2021 note.
   Ask instead adds one question; mild disclosure drift is gray, with no action.
5. Select **C04 Priya Nair**, then Prepare. **Raise it** adds a question about the 3 July beneficiary change.
6. Return to **C08 → Debrief → Use example → Propose notes**. Review or edit each value, then approve the three
   cards. Notes update, old entries show a superseded chip, and a later preparation uses the approved owner.
7. Expand **Change ledger**. It shows recorded changes and explicitly distinguishes the local 120-day rehearsal
   policy, whose held-out result and approval are unavailable.

## What is real, cached, and scripted

- `fixtures/cabinet_*` and collection fixtures are the original recorded synthetic snapshot, unchanged.
- `fixtures/rehearsal_overlay.json` contains the explicitly requested C08 local fixture. It also adds the missing
  C04 beneficiary event and carries C02/C04 baseline text to a 26 September rehearsal prep. Those additions are
  labeled in the interface. C08 holdings, notes, and emails are scripted data, not observed client data.
- `demo_harness.py` is a deterministic **local rehearsal adapter**, not the repository’s production LLM harness
  or its scorer. It checks source conflicts, expiry, recorded stock buys, missed beneficiary events, fee versions,
  and locked disclosures. It reads visible records only, never the answer key. Counters come from its output;
  two presentations of one conflict count as one checked claim.
- `fixtures/workspace-demo.json` caches all seven client records and these outputs. Rebuild it after changing
  fixtures with `python3 frontend/build_demo.py`. The recorded collection snapshot can be truncated (for example,
  market events); the workspace does not claim to show records absent from the snapshot.
- Approved notes are atomically saved to `frontend/local_state/approved-notes.json`, ignored by Git. **These are
  local file writes, not MongoDB writes.** The only write APIs are debrief proposal and approval. Approvals are
  idempotent and reject stale drafts. Each attribute has one active owner; historical notes are retained.
- The local parser accepts the three stage attributes: a household member “runs the money”, “Bond switch: …”,
  and “Retirement [still] 2027”. It is deliberately bounded, not a general natural-language extraction model.
- No “22 → 14”, Fran approval, or v7 change is invented. The actual recorded config change is displayed; the
  120-day policy is marked **Not evaluated / No recorded approval / Local only**.
- The public snapshot has no answer key, so it shows no fault dots, including under `?demo=1`. A private snapshot
  with `db_cabinet_truth.answer_key.json` can supply only a latest-prep boolean under that flag.

To repeat a clean rehearsal, stop the server and move `frontend/local_state/approved-notes.json` out of that folder.
Keep it if the approved notes matter. Restart; the original fixtures have never been overwritten.

## MongoDB handoff

This page intentionally uses the cached rehearsal contract even if the server is started without `--offline`.
Existing `/api/cabinet/*`, `/api/db/*`, ledger, and other read endpoints still support live MongoDB and preserve
their existing read-only wrapper. There is no live MongoDB writer in this change.

To connect the final stage dataset, replace the rehearsal adapter with the team’s harness output and bind the
approved-note operation to its authoritative write service. Preserve the note fields (`client_id`, `attribute`,
`value`, `date`, `source`, `fact_type`, `expires_at`, `supersedes`) and the one-owner / stale-draft checks. The missing
production C08 data, beneficiary event, and actual 120-day policy evaluation/approval need to come from the team;
the local scripted records must not be silently imported into the live cabinet.

## Development checks

```bash
python3 -m unittest discover -s frontend/tests -q
node --check frontend/static/workspace.js
```

Tests cover the old read APIs, cached source resolution, all three rehearsal cases, expiry, edits, durable and
idempotent approvals, stale drafts, failed disk writes, and localhost request restrictions. Node is only needed
for JavaScript syntax checking. There is no frontend dependency or build step.

`CONTRACT.md` documents the retained data APIs and the new local rehearsal endpoints. The old inspector JS/CSS
remain available as source assets; they are not loaded or linked by the demo page.
