VERDICT: SEND BACK
236 passed, 5 skipped in 19.8s

## Blocking findings

1. `pregame/oracle.py:338` — Exact approved disclosure text is explicitly allowed when it appears inside a model-authored claim. `pregame/drafter.py:87-96` likewise accepts such a response, so a live drafter can write AS-01 or AS-02 into an ordinary section and `render_markdown` will then add the locked block again. This violates the requirement that the drafter never writes disclosure text and can produce duplicate/misplaced disclosures. The test at `tests/test_approved_language.py:119-134` incorrectly blesses exact approved text as a claim; it does not test the inserted block, which is already outside `sections`. Smallest fix: reject exact approved sentences during live-response validation (or flag them when they occur in claims), while continuing to exclude the code-inserted Markdown disclosure block from claim scanning; change the test to distinguish those two locations.

2. `pregame/oracle.py:340-346` — The 80% set-coverage rule false-positives on normal brief prose because four generic AS-01 words are sufficient regardless of meaning. For example, `Past performance and future results are shown in the appendix.` is reported as a reworded disclosure. Since live briefs fail closed, ordinary meeting-material language can be redrafted and then blocked. The supplied threshold tests cover curated legitimate sentences but never exercise a normal sentence at the 80% boundary. Smallest fix: retain the specified 80% threshold but require an additional disclosure-semantic signal (for example, the negation/indicator relationship) for the word-coverage branch, and add boundary false-positive tests such as this sentence.

## Other findings

None.

## What I checked and found correct

The approved AS-01/AS-02 mapping is immutable and has the specified verbatim text; `render_markdown` appends both sentences word for word outside model claims. The new check is registered consistently in `oracle.GUARDRAIL_CHECKS`, `gate.DEFAULT_GUARDRAIL_CHECKS`, `improver.BUILTIN_CHECKS`, and enabled defaults. The explicit protective-promise patterns, stated legitimate examples, world fact corpus, live redraft/block path, and fake demo path are covered and passed. The whole-text and clause thresholds are exactly 0.70 and 0.85, and the word threshold is 0.80. The scoped gate/improver changes do not expose held-out yardsticks or allow proposals to modify frozen surfaces, and I found no secrets or Atlas-specific transaction/ServerApi regressions in the scoped change.
