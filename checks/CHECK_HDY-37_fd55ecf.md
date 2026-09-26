VERDICT: SEND BACK
............................                                             [100%]

## Blocking findings

1. `pregame/oracle.py:234-257` — the expanded `no-advice` patterns still miss ordinary, direct allocation advice. With the repository code, `check_no_advice` returns `[]` for “Allocate 60% of your portfolio to equities.”, “Move 10% from bonds into stocks.”, and “I recommend a 60/40 stock-bond allocation.” Because `loop._guarded_draft` relies on this detector, any of those recommendations can be stored and shown as a live brief, contrary to the done line. The new tests at `tests/test_guardrails_live.py:42-50` cover only verb forms already enumerated by the implementation, so they cannot expose these gaps. Smallest fix: add narrowly targeted patterns for imperative allocation/move/shift/reallocate forms and `recommend` followed by an allocation/portfolio noun, while retaining prep-language exclusions; add each reproduced phrase as a positive regression test and advisor-prep variants such as “Consider discussing whether moving more into bonds fits their goals” as negative tests.

## Other findings

1. `pregame/loop.py:181-186` and `pregame/loop.py:380-384` — `market_event` correctly returns `call_accuracy: None` when guardrails block its brief, but `run_demo` unconditionally formats that value with `:.2f`. A twice-unsafe live draft therefore fails closed but crashes the scripted demo with `TypeError` instead of reporting the refusal. Smallest fix: have `run_demo` detect `result["blocked"]`/a `None` accuracy and print a blocked result without float formatting (and add a test for this branch).

## What I checked and found correct

Read `DESIGN.md`, `INTERFACES.md`, and `pregame/contracts.py` first, then reviewed every file in scope and the prior report. The previous live-path defect is fixed: `make_brief` and `market_event` both route drafting through `_guarded_draft`, run enabled guardrails before insertion or return, allow one bounded live redraft, record a refusal, and store no blocked brief. Unknown enabled checks fail closed. The added integration tests exercise both live storage paths, and the advisor-prep examples they include remain unflagged. Oracle grading remains code-based and isolated from the improver/database surfaces; the CLI does not print credentials. The mandated command `C:/Projects/prep-harness/.venv/Scripts/python -m pytest tests -q` exited 0; with this pytest configuration the exact final output line is reproduced above and no numeric summary line was emitted.
