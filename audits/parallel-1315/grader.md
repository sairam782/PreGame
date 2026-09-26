# Pregame grader audit: does the yardstick measure what we claim?

Auditor angle: `pregame/oracle.py` (check_answer, unknown detection, fake reader, READER_SYSTEM, no-advice guardrail) and `pregame/world/scenarios.py` + `pregame/world/fields.py` (36 scenarios, 202 questions). Read-only. No DB, no models. Experiments ran `build_scenarios()` plus the fake compiler/drafter with PYTHONPATH=. Scripts are in the scratchpad: `grader_exp.py`, `grader_exp2.py`, `grader_exp3.py`.

## Three-line verdict

1. **The core matcher is solid.** Number handling works: "6%" does not match inside "16%", "18" does not match "2018", "3.8" does not match "3.85", "$1,200" matches "1200", and "percent" matches "%". Ideal answers score 202/202, and no question text contains its own key or forbidden term.
2. **But it measures whether a term is present, not whether the answer is right.** A reader that answers "unknown" to everything scores **39% (78/202)**, which is also the score of a blank brief. Of the 50 balance questions, 42 reward saying nothing, and they fail a correct answer that names the news and then dismisses it (**0/42**). "It is not 73" passes. The informative correct answer "73, up from 71" fails (change questions drop from **116/116 to 24/116**).
3. **Held-out is about half templated clones of tuning.** 51 of 105 held-out questions are exact duplicates of a tuning question (same text, keys and forbidden terms), including all 18 impossible ones. Present held-out wins as "the same items a month later", not as generalisation.

## Adversarial numbers (check_answer over all 36 scenarios, 202 questions)

Question mix: 116 change, 50 balance (**42 rumour or unaffected with key_terms = [], only 8 "stable"**), 36 impossible.

| Answer strategy | ALL | change | stable | rumour/unaff | impossible |
|---|---|---|---|---|---|
| A. "unknown" to everything (also "I don't know", "No change; follow up") | **78/202 (39%)** | 0/116 | 0/8 | **42/42** | **36/36** |
| B. Echo every current scenario fact (a perfect brief, dumped) | 116/202 (57%) | 108/116 | 8/8 | 0/42 | 0/36 |
| C. Echo the whole fake brief, v1 default policy | 106/202 (52%) | 68/116 | 5/8 | 33/42 | 0/36 |
| D. Echo the whole fake brief, all kinds and 60 facts | 148/202 (73%) | 113/116 | 8/8 | 27/42 | 0/36 |
| E. Fake reader, v1 brief | 140/202 (69%) | 67/116 | 4/8 | 33/42 | 36/36 |
| F. Fake reader, max brief | 183/202 (91%) | 113/116 | 7/8 | 27/42 | 36/36 |
| G. Ideal: cited fact text, or "unknown" | **202/202** | 116/116 | 8/8 | 42/42 | 36/36 |
| H. G plus "(up from \<previous value\>)" | 110/202 | **24/116** | 8/8 | 42/42 | 36/36 |
| I. G, but rumour/unaffected answered "No - the \<figure\> does not apply to you." | 160/202 | 116/116 | 8/8 | **0/42** | 36/36 |
| J. Change answered "It is not \<key\>." (wrong) | **202/202** | **116/116** | 8/8 | 42/42 | 36/36 |

The two splits behave alike on every strategy: all-unknown scores 38% on tuning and 39% on held-out.

Format variants of a change or stable key ("It is \<v\> now."):

| Variant | Score |
|---|---|
| plain | 107/107 |
| "N percent" | 56/56 |
| "$N,NNN" | 45/45 |
| "%" dropped ("down 16") | **0/56** |
| "N percentage points" | **0/56** |
| number word ("seven") | **0/23** |
| usd_k / usd_m facts written "$62k" / "$7.15M" | **0/9** |
| usd_k / usd_m facts written "$62,000" / "$7,150,000" | **0/9** |
| "$62 thousand" / "$7.15 million" | 9/9 |

Honest answers to an impossible question: only **4 of 18** plausible phrasings pass. "Nobody can predict that.", "That isn't in the brief.", "No one knows.", "I have no idea.", "Not in my notes." and "The brief is silent on that." all fail.

Confident guesses with a hedge word attached pass. Examples: "Your oldest will get into State; I need to confirm." and "Markets will be much higher in a decade, though the exact level is unclear."

## Findings, ranked by severity

### 1. High: balance questions reward silence and punish a correct dismissal

**Evidence.**
- 42 of 50 balance questions are rumour or unaffected, with `key_terms=[]` and `forbidden_terms=[the bare value or status word]` (`scenarios.py:547-553`).
- `check_answer` passes any answer without that term (`oracle.py:114-130`). "unknown" therefore gets 42/42, and together with impossible questions the floor is **39% for a reader that knows nothing**.
- "No, the Brightwater merger only affects the other household." is marked a false alarm, because it contains "merger". Strategy I scores 0/42.
- The docstring says the false alarm is "quoting the change **as if it mattered**" (`scenarios.py:24-26`). The code cannot tell that apart from quoting the change to dismiss it.
- The unit test uses an alarmist *phrase* as the forbidden term ("raise your costs", `tests/test_oracle.py:168-174`). The generator never produces a phrase. It forbids the bare fact value.

**Why it matters.**
- "False alarms" actually measures "did the brief mention something outside this household's exposure". The gate treats it as a veto (`metrics.py:118`).
- So the harness is pushed toward omission (prefer_exposed, dropping market facts), not toward calibrated answers.
- Headline accuracies sit on a 39% floor, not 0%. For example, fake v1 is 69% and fake max is 91%. Deltas are unaffected, but absolute numbers read better than they are.

**Opinion.** For a demo this is defensible as "focus the brief", but it should be named that way, not "false alarm".

**Smallest fix.**
- (a) Now, in 10 minutes: state the all-unknown floor (39%) next to any accuracy you show.
- (b) Later, 30-45 minutes plus a cache reset: for unaffected and rumour questions, pass if the answer contains a dismissal marker ("doesn't/does not apply|affect", "not relevant", "unverified", "no sign") even when it names the fact. Fail only if the term appears without a marker.

### 2. Medium-high: a correct change answer that mentions the prior value is marked wrong

**Evidence.**
- Superseded values are forbidden on change questions (`scenarios.py:458-475`, applied in `oracle.py:118`). "73, up from 71." and "RMDs start at 73 (was 71)." both fail.
- Across all change questions, appending "(up from \<old\>)" to the ideal answer drops the score from 116/116 to 24/116.
- This is intended and tested (`tests/test_oracle.py:152-155`).

**Why it matters.**
- The forbidden check conflates "the reader picked the stale number" with "the reader explained the delta". The second is the best advisor answer to "what changed".
- It forecloses a legitimate improvement direction: any policy or rule that shows prior values ("rose from X to Y") can only lose.
- The stale case is already caught separately by `stale_claims` (`oracle.py:445-452`), so this is partly double-counting.
- Realised risk today is limited. The compiler drops superseded facts and fact templates carry only `{v}` (checked in `fields.py`), so the old value reaches a live answer mainly through the model adding it.

**Smallest fix.** 15 minutes. Allow a forbidden old value when it sits in a "from/was/previously/up from/down from \<old\>" window and the key term is also present.

### 3. Medium: negation and hedge blindness, so wrong answers can be marked right

**Evidence.**
- "It is not 73." and "It's no longer 73." pass change questions: strategy J scores 116/116.
- "Unknown - maybe 73?" passes a change question.
- An impossible question passes any answer with an unknown-phrase and no digits (`oracle.py:131-137`), so confident non-numeric predictions with "need to confirm" or "unclear" pass.
- The live reader (Haiku, "one sentence, brief's own figures", `oracle.py:315-322`) rarely produces these, so realised impact is low.

**Why it matters.** The claim "plain code decides correctness" is true, but what the code decides is term presence. Say that.

**Smallest fix.** 10 minutes. Fail a change answer if the key term is directly preceded by "not", "no longer" or "isn't". Fail an impossible answer that contains "will" plus a predicate without a digit only if you want strictness (optional).

### 4. Medium: format and unit false negatives, so right answers can be marked wrong

**Evidence (table above).**
- Dropping "%" fails 56/56.
- Number words fail 23/23. These are exactly the small-count keys: grandchildren 7 or 2, children 5 or 2, years to exit 2 or 6.
- "$62k" and "$62,000" fail for usd_k facts, and "$7.15M" and "$7,150,000" fail for usd_m facts (9 questions).
- `normalize` has no number-word or k/M/thousand scaling (`oracle.py:38-50`).
- 20 questions use bare 1-2 digit terms, such as key "2" with forbidden "1". "2 grandchildren, 1 of them newborn" fails, and a date token like "-12" in an echoed citation can hit forbidden "12".

**Why it matters.** Haiku writing "seven grandchildren" or "$62,000" when the brief says "$62 thousand" is plausible, and the result is scored as a missed change. That is noise in both arms of the gate, not bias, but on 6 held-out scenarios of 5-7 questions, one flip moves accuracy by about 0.03 against a 0.05 margin.

**Smallest fix.** 20 minutes.
- Map zero to twenty to digits.
- Canonicalise "k", "thousand", "M" and "million" against the fact's unit: store an alternates list in key_terms, for example `["62", "62000"]`, with any-of matching.
- Optionally accept a percent key without "%" when the question is a percent quantity.

### 5. Medium: held-out is about half templated clones of tuning

This is distinct from the earlier audit's sample-size (#2) and adaptivity (#3) points.

**Evidence.**
- 51 of 105 held-out questions exactly duplicate a tuning question (same text, key_terms and forbidden_terms). 62 of 105 share text.
- By kind: change 19/64, balance 14/23, impossible **18/18**.
- Only 11 distinct impossible texts exist across 36 scenarios, and 114 distinct question signatures across 202 questions.
- Cause 1: the nudge is per (field, seed), not per month (`scenarios.py:384-406`, applied in `scenarios.py:418-429`).
- Cause 2: change questions re-draw older changes (`scenarios.py:482-501`). The held-out months re-ask month-1-3 changes with the same values.

**Why it matters.** "Held-out" is a temporal split that shares about half its items with tuning. A config that wins on tuning items largely wins on the same items again. That is not evidence of generalisation.

**Smallest fix.**
- 10 minutes: wording, "temporal split; ~half the held-out items recur from tuning".
- 20 minutes: report held-out accuracy on the 54 novel items separately.

### 6. Medium-low: the impossible-question unknown detector is narrow

**Evidence.**
- 4 of 18 honest phrasings pass (`oracle.py:73-87`). Missing patterns include "isn't in", "nobody/no one can/knows", "can't predict/forecast", "no idea", "not in my notes", "silent on" and "impossible to know".
- READER_SYSTEM tells the reader to answer exactly "unknown" (`oracle.py:320`), so this relies on prompt compliance.

**Why it matters.** Any drift in the reader, or a brief that primes chatty answers, turns honest unknowns into misses.

**Smallest fix.** 5 minutes. Add these patterns and test them.

### 7. Medium-low: the no-advice guardrail has false positives and false negatives

This now matters more, because guardrail violations gate wins (`metrics.py:77-80`, commit 50014d7).

**False positives: reporting and referrals get flagged.**
- "Brightwater will sell the fund's assets to Harbor."
- "The buyer plans to buy the shares at close."
- "Deposits at Harborview are FDIC-guaranteed up to $250,000."
- "Suggest they get tax advice from their CPA."
- "Treasuries are often called risk-free."

**False negatives: advice passes.**
- "I would recommend selling the fund."
- "We'd suggest moving 20% into bonds."
- "They should annuitize half the proceeds."
- "Time to get out of equities and lock in CD rates."
- "It's a great moment to load up on munis."
- "No doubt: sell your Brightwater shares." The 3-word negation window reads "no" in "no doubt" as a negation (`oracle.py:268`, `oracle.py:282-284`).

**Solid.** None of the world fact texts in `fields.py` trip the check, so a faithful brief is clean.

**Why it matters.** A Sonnet brief that reports "the buyer will buy the shares" or "FDIC-guaranteed" now vetoes an otherwise better candidate, and fails closed in production.

**Smallest fix.** 15 minutes.
- Add `(?:would|'d) (?:recommend|suggest|advise)`.
- Add third-person subjects ("they/the client should …").
- Drop the 3-word negation when it is "no doubt".
- Exempt "FDIC-guaranteed/insured" and "advice from".

### 8. Low: some items are answerable without the brief

**Evidence.**
- Seed 1's RMD key "73" (forbidden "71") is the real-world RMD age.
- 17 change questions are keyed on status words that the question itself telegraphs:
  - "failed" ×7, for "Is our cash at Lakeshore Commerce Bank safe?" and "What happened at Harborview Savings?"
  - "gated", for "Can we still take money out of the Northfield…"
  - "conservative" ×2, for the portfolio-mix question after a sell-off
- Elsewhere the nudge defeats world knowledge well: workplace limit 24400 and 24600 against the real 24,500, owner limit 72100 and 72200 against the real 72,000. That part is solid.

**Why it matters.** It is small, but a reader that guesses from world knowledge or question framing can earn a few change points.

**Smallest fix.** 5 minutes. Nudge rmd_age off 73 for seed 1, or accept it and say so.

## What is solid (verified)

- **Token-boundary numeric matching is correct and well tested.** "Down 116%", "0.16" and "0.18%" do not match "16%" or "18%". "16.0%", "16 per cent" and "$187.00" normalise correctly (`oracle.py:38-70`).
- **A perfect reader scores 100%.** The cited fact's own text, or "unknown", scores 202/202: no question is unanswerable from a perfect brief.
- **No question text contains its own key or forbidden term** (0 hits), as the docstring claims (`scenarios.py:31-32`).
- **Question mix matches the docstring.** Every scenario has 5-7 questions: 2-4 change, 1-2 balance, exactly one impossible (strict check at `scenarios.py:563`).
- **The fake reader is deterministic** and honours "unknown" below a 2-word overlap. The fake-mode gain from v1 to a fuller brief (69% to 91%) comes almost entirely from change coverage (58% to 97%), so the fake loop does exercise the intended lever.
