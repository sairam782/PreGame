VERDICT: SEND BACK
.......................................                                  [100%]

## Blocking findings

1. `pregame/compiler.py:29-39` — account-note isolation is inferred from whether the note's `subject` appears in the current account's `exposures`; the note carries no owner check. Two clients routinely share exposure subjects, so an account note about such a subject is admitted to both contexts. Supersession also runs before this filter, so a newer note belonging to another client can replace the current client's note and then either leak or cause the correct note to disappear. `tests/test_account_isolation.py:20-24` only tests account-note subjects unique to the other fixture account, so it cannot detect the shared-subject case. This violates the explicit done line that account notes never cross clients and risks exposing one client's private notes in another client's live brief. Smallest robust fix: attach an explicit owner account id to every account-note fact, filter account notes by that id before `_drop_superseded`, and add a regression test with two clients sharing the same market exposure and distinct account notes for that subject.

## Other findings

1. `pregame/drafter.py:256-257` — the live prompt demands exactly the configured `likely_questions` value, while validation at `pregame/drafter.py:154-160` accepts exactly `min(configured count, number of facts)`. With fewer facts than the configured count, a model following the prompt is rejected and sent through an avoidable corrective call; repeated compliance can fail drafting. Smallest fix: put the same computed expected count in the prompt that validation enforces.

## What I checked and found correct

The prior finding in `checks/CHECK_HDY-30_de2e9f8.md` is fixed: after salvage, `pregame/drafter.py:100-108` rechecks the exact expected likely-question count, and `tests/test_compiler_drafter.py:442-469` covers the regression. The required public signatures and the remaining compiler ordering/receipt behavior match `INTERFACES.md`; fake drafting is deterministic and citations stay within the compiled context. Settings defaults, cached environment loading, secret-masking representation, and LLM fake/live/record/replay behavior match the interface. The scoped secret scan found only deliberate test placeholders and local example URIs, not production credentials. The scoped modules do not access MongoDB or the frozen oracle/held-out grading surfaces, so they introduce no Atlas transaction, validator, ServerApi strict-mode, or self-grading/editing issue.
