GO WITH FIXES
Recording: NO-GO until the cassette path is guaranteed new/empty; otherwise the new run is appended behind stale answers.
Replay: GO after that fix/check, using a database whose name is explicitly confirmed before `demo` resets it.

## Findings, ranked by severity

### HIGH — Record mode appends to an old cassette, while replay consumes the old answers first

- **File:** `pregame/llm.py:177-178`, `pregame/llm.py:183-195`, `pregame/llm.py:197-206`
- **Failure scenario:** The final recording uses the default or previously rehearsed cassette path. Every successful response is opened with `"a"` and appended. On stage, replay loads all rows in file order and serves the first row for each prompt first. Thus the replay can silently use an earlier rehearsal's answer rather than the final recorded answer; repeated prompt handling does not solve this because it deliberately starts at index zero. This can change briefs, grades, proposals, and whether later prompt hashes exist, causing either a misleading replay or a cassette miss partway through.
- **Smallest fix:** In record mode, fail fast if the cassette exists and is non-empty (safest), or explicitly truncate it once when constructing the recorder behind an opt-in overwrite flag. For today's run, delete/move the old cassette manually and verify the target is absent before recording, then preserve that exact file for replay.
- **Minutes:** 5

### HIGH — `demo` unconditionally bypasses the guarded-setup database-name fence

- **File:** `pregame/cli.py:272-275`, `pregame/loop.py:49-69`, `pregame/loop.py:401-403`
- **Failure scenario:** `setup` now refuses to reset a database not named `pregame*`, but `python -m pregame.cli demo` calls `run_demo`, which always passes `yes=True`. A stray `MONGODB_URI`/`PREGAME_DB` therefore lets the exact recording/stage command drop all Pregame collection names in the wrong database. The banner prints the target, but there is no confirmation or refusal.
- **Smallest fix:** Make `demo` use the same name fence by default and add an explicit CLI `demo --yes` bypass, or refuse any non-`pregame*` target outright. For today's run, inspect the printed `db=...@host` line before allowing setup to proceed and use a dedicated `pregame*` database.
- **Minutes:** 8

### MEDIUM (opinion) — Stage narration promises fake-mode proposal outcomes that live models are not constrained to produce

- **File:** `pregame/loop.py:429-443`, `pregame/improver.py:437-448`
- **Failure scenario:** The script prints “expect an over-broad proposal rejected” and then “expect a policy change committed,” but the live improver is free to return any valid one-surface proposal, and its one semantic retry only corrects invalid shape/content. The recorded run may produce a tier-H rules/tools/guardrails proposal, no proposal, or two different policy proposals. The status printed afterward is truthful, but the advance narration can mislead judges and makes the demo look broken when normal model variance occurs.
- **Smallest fix:** Change the headings to neutral “improve #1/#2” text, or only print the expectation in fake mode. Do not change the actual gate result.
- **Minutes:** 3

## What I checked and found correct

- Ran exactly `C:/Projects/prep-harness/.venv/Scripts/python -m pytest -q`: **491 passed, 5 skipped** (the five opt-in Atlas tests), exit 0, in 23.8 seconds.
- The merged gate logic composes: the evaluator cache fingerprints contracts/compiler/drafter/oracle/metrics (`pregame/gate.py:393-411`); held-out comparison delegates to the updated metric rule (`pregame/gate.py:632-645`); all four evaluated heads are captured and fenced at automatic commit (`pregame/gate.py:603-605`, `pregame/gate.py:657-668`) and owner approval (`pregame/gate.py:679-707`, `pregame/gate.py:721-734`). Interrupts/errors return an evaluating proposal to pending (`pregame/gate.py:561-569`).
- The approved-language path is internally consistent: live drafting says not to write disclosures (`pregame/drafter.py:34-44`), code renders locked text, and the checker examines claim sections rather than inserted markdown (`pregame/oracle.py:463-502`). The targeted normal-prose tests passed; I found no evidenced normal brief sentence that is wrongly blocked. The no-advice patterns include explicit third-party-plan and conversation exclusions (`pregame/oracle.py:363-401`), and its regression tests passed.
- The grader's new past-value exception is limited to change questions with all key terms already present (`pregame/oracle.py:241-247`), and every forbidden-term occurrence must have local past framing (`pregame/oracle.py:160-182`). Honest-unknown answers still fail if they assert a number (`pregame/oracle.py:260-266`). Percent variants exclude common alternate units and token collisions (`pregame/oracle.py:104-112`). I found no demonstrated new wrong-answer pass beyond the pre-existing lexical nature of this grader.
- Deterministic prompt inputs checked: feedback IDs are event/count based (`pregame/loop.py:227-243`); the improver retry is a deterministic extension of the original prompt (`pregame/improver.py:437-445`); JSON-repair replay stores the final parsed response under the original request key (`pregame/llm.py:164-180`). Cassette reads and writes and repeated-key counters are lock-protected (`pregame/llm.py:117-120`, `pregame/llm.py:149-162`, `pregame/llm.py:197-206`). Concurrent identical calls may be recorded in completion order, but their consumers aggregate runs for the same scenario; I found no evidence this changes the scored summary.
- The banner reports mode, provider, role models, database name, and host without the URI credentials (`pregame/config.py:49-75`), and command setup prints it before any reset (`pregame/cli.py:85-96`). The remaining reset bypass is the `demo` finding above.
