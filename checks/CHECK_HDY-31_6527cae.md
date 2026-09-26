VERDICT: PASS WITH FIXES
........................................................................ [ 94%]
....                                                                     [100%]

## Blocking findings

- None.

## Other findings

- `pregame/improver.py:176-180,205-221`: forbidden collection and held-out reads raise `PermissionError`, but they only append an item to the in-memory `view.refusals` list. Contrary to the binding `INTERFACES.md` requirement, no `ledger` `refused` entry is appended unless some separate trusted caller later invokes the extra, unspecified `record_refusals` API. A caller other than `loop.improve`, or an interrupted path before its `finally` flush, can therefore lose the required audit receipt. The tests at `tests/test_improver.py:107-118` explicitly accept an empty ledger after denial and manually flush it, so they cannot catch this contract violation. This matters because attempted access to the frozen yardstick is supposed to be durably auditable. Smallest fix: make construction of the capability view require a trusted refusal recorder/sink that persists each denial as it happens without exposing the database through the view, and change the tests to assert the ledger entry immediately after each `PermissionError` (or revise the binding interface explicitly if deferred flushing is intended).

## What I checked and found correct

- The exact requested pytest invocation exited 0 and printed the two progress/result lines above; pytest emitted no conventional passed-count summary under this configuration.
- The prior blocking finding is fixed: `ImproverView` stores only copied plain data, filters `eval_runs` to `split == "tuning"`, excludes the question bank and evaluation briefs, redacts held-out proposal summaries/hashes, and exposes no database/client/collection/cursor handle in its object graph.
- `pregame/oracle.py:448-473` refuses mixed splits, wrong-field scenarios, duplicate scenario IDs, and invalid `k`; the reader receives only brief markdown and question text, while code decides correctness.
- The oracle supplies the required citation, stale-fact, and advice checks; stale claims are checked against full scenario history. Numeric normalization and honest-unknown handling are code-based.
- Metrics implement equal scenario weighting, pass^k, the accuracy margin, worst-scenario floor, false-alarm and stale-claim floors, identical scenario-set checks, and the +50% context ceiling.
- The source, transitive-import, runtime tripwire, object-graph, and held-out-marker tests meaningfully exercise yardstick isolation. No scoped production file contains secrets or performs Atlas transactions, validator setup, or ServerApi configuration.
