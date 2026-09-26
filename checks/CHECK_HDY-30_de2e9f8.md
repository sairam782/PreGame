VERDICT: SEND BACK
.....................................                                    [100%]

## Blocking findings

1. `pregame/drafter.py:86-99` — after the second invalid live-model response, the salvage path does not re-enforce the configured `likely_questions` count. `_validate_sections` correctly flags a wrong count, but `_salvage_sections` can retain any nonzero number of valid question claims, and `_draft_live` then accepts the result merely because the section is nonempty. Thus a policy requesting (for example) 3 questions can yield a live brief with 1, contrary to the interface requirement that the policy's question count be honored and contrary to this module's own validation contract. This matters in the live demo because a persistently malformed model response is silently published as a policy-noncompliant brief. Smallest fix: after salvage, require `len(sections["likely_questions"]) == min(policy["likely_questions"], len(ctx["facts"]))` and raise `LLMError` otherwise (or deterministically fill only from valid context facts). Add the missing live-path test where both responses have the wrong question count; the current tests at `tests/test_compiler_drafter.py:339-432` do not exercise that case.

## Other findings

None.

## What I checked and found correct

The prior check's main live-output defect is otherwise fixed: live responses now get shape, text, citation-membership, and question-count validation, one bounded corrective retry, and citation-safe salvage; live-path tests cover prompt content, a missing section, unknown fact ids, and wholly invalid output. The four required modules and public signatures are present. Settings load `.env`, choose fake/live defaults, cache the result, and mask API keys and MongoDB URI passwords in `repr`. LLM fake/live/record/replay behavior, JSON retry, cassette keying, and locks match the interface. The compiler applies as-of filtering, supersession, tools, kinds, recency, exposure ranking, capping, and receipt construction in the specified order. The fake drafter is deterministic and cites only context facts. No production secret was found in the scoped files. These pure modules do not access MongoDB, transactions, validators, ServerApi settings, the oracle, held-out scenarios, or any self-editable grading surface.
