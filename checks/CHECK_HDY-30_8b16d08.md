VERDICT: PASS WITH FIXES
................................                                         [100%]

## Blocking findings

None.

## Other findings

1. `pregame/drafter.py:63-79` — live drafter output is silently coerced into a brief without validating the required response shape or claim citation invariant. Unknown/missing sections become empty lists, non-list section values are iterated and discarded, and hallucinated fact ids are filtered out while the associated claim is retained with `fact_ids: []`. This violates the `Claim` contract (citations must be non-empty and belong to the context) and can put an empty or uncited brief into the live demo instead of surfacing a model failure. Smallest fix: validate that `sections` is an object containing every configured section, each section is a list, every item has non-empty text and a non-empty list of only in-context fact ids, and `likely_questions` has the configured count; raise `LLMError` (or perform one bounded corrective retry) on invalid output. Add live-path tests for malformed sections, unknown citations, and wrong question count.

2. `tests/test_compiler_drafter.py:252-301` — drafter coverage exercises only the fake path. Consequently, the live-path defect above cannot fail the suite even though live drafting is part of the card's interface and demo behavior. Smallest fix: use a stub non-fake LLM and assert the prompt contains rules, enabled guardrails, fact ids, section order, and question count, plus assert invalid returned JSON is rejected.

## What I checked and found correct

The required four modules exist with the specified public signatures. Settings load `.env`, select fake/live defaults correctly, cache the result, and redact the API key and URI password in `repr`. LLM fake mode, role models, one JSON retry, record/replay keys, locked cassette appends, and locked usage accounting match the interface; no committed or printed production secret was found in the scoped files. The compiler applies the required as-of, supersession, tool, kind, recency, exposure ranking, cap, and receipt steps in the specified order. The fake drafter is deterministic, uses only context fact ids, honors section order/question count subject to available facts, produces UUID hex ids, and renders citations. The requested tests passed, and the scoped code does not access MongoDB, transactions, validators, ServerApi settings, held-out scenarios, the oracle, or any self-editable grading surface.
