# The data teammate's banker book: results (26 Sep 2026)

Data: v2 of the teammate's synthetic book (one banker, 6 clients, 24 call preps May–Aug 2026), read from the Atlas
database `cabinet` (visible data only). Scoring: `C:/Projects/cabinet-eval/score_preps.py`, a separate program and the
only reader of the answer key; Pregame keeps its summary totals only.

## Results

| Per 24 preps | Scripted assistant, no harness | Code-written harness | Sonnet 5, no harness (run 1, 2) | Sonnet 5 + harness (run 1, 2) |
|---|---|---|---|---|
| Preps with a mistake | 17 (71%) | 0 | 0, 0 | 0, 0 |
| Mistakes | 33 | 0 | 0, 0 | 0, 0 |
| Forbidden promise ("built to protect your capital") | 1 | 0 | 0, 0 | 0, 0 |
| Expected actions done, of 10 | 0 | 10 | 6, 7 | 7, 8 |
| – ask the client, of 5 | 0 | 5 | 4, 4 | 5, 5 |
| – flag for the banker, of 2 | 0 | 2 | 2, 2 | 2, 2 |
| – brief both holders, of 3 | 0 | 3 | 0, 1 | 0, 1 |
| Questions the file didn't call for | 0 | 0 | 27, 26 | 2, 3 |

Each Sonnet run wrote 24 preps, 4 per client, for all 6 clients (C01–C06), on 26 Sep 15:17–15:24 through a Claude
subscription (`python -m pregame.cli cabinet-live --runs 2`, recorded to `cassettes/cabinet-live.jsonl`, stored in
`pregame_cabinet_live`). The code-written column is `python -m pregame.cli cabinet`.

## What it shows, and what it doesn't

- The 17/24 column is the teammate's **scripted** assistant with deliberately careless habits, not an AI model.
- The code-written harness fixes all of its mistakes, but its rules were written from the answer key's list of
  mistakes and scored on the same 24 preps: in-sample.
- Claude Sonnet 5 writing from the raw records made **no** mistakes either. With the harness it stayed focused (about 3
  unneeded questions instead of about 26), which is largely the harness's code choosing the questions. The expected-
  action difference (7.5 vs 6.5 of 10) is within the noise of 2 runs.
- Both Sonnet arms mostly missed "brief both holders" (0–1 of 3) even though the harness's client file says both
  hold the account; the code alone gets 3 of 3.
- An independent review judged our first description of the comparison unfair: the harness arm is mostly the code's
  work, and code adds the locked disclosures only on that side ([audits/SOL_cabinet_live_8bc8257.md](../audits/SOL_cabinet_live_8bc8257.md)).
- On this small book the harness's value is guarantees (compliance wording locked by code, every fact dated and
  sourced) and focus, not accuracy. The teammate's 20-client v3, with 9 clients held out, is the real test.
