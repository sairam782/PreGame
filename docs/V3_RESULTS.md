# Version 3 of the teammate's data: first out-of-sample look (26 Sep, 16:13)

Data: `pregamev0_abhi_cabinet20` (20 clients, 73 call preps; 11 tuning and 9 held-out clients by the teammate's split),
scored by the version-3 scorer ([cabinet-eval/score_preps.py](../cabinet-eval/score_preps.py) `--data <export of pregamev0_abhi_cabinet20>`),
which reproduces the answer key exactly. Receipts quoted: `pregame_v3_cabinet20.cabinet_runs` CR-20260926T201234251803Z (Sonnet
alone) and CR-20260926T201234251804Z (Sonnet + harness), the full 73-prep replays; the other receipts there are partial. Claude Sonnet 5 wrote each prep **once** per side (with and without the harness),
recorded to `cassettes/v3-cabinet20.jsonl` between 16:01 and ~16:08; the session crashed before the run finished, so the
preps were rebuilt by replaying the recording (no new model calls) and only clients whose preps were all recorded on
both sides are counted: 8 of the 11 tuning clients and 4 of the 9 held-out clients. Our harness rules were written on
the 6-client set and were **not** tuned on this data, so both groups are out-of-sample for our rules.

```

2 training (C01, C02): 7 preps
  Scripted assistant   preps with a mistake 6/7   mistakes 6   expected actions 0/4   unneeded questions 0   kinds {'sticky_label': 3, 'compliance_drift': 3}
  Sonnet alone         preps with a mistake 3/7   mistakes 3   expected actions 4/4   unneeded questions 7   kinds {'missing_disclosure': 3}
  Sonnet + harness     preps with a mistake 3/7   mistakes 3   expected actions 3/4   unneeded questions 0   kinds {'sticky_label': 3}

2 held-out (C06, C09): 7 preps
  Scripted assistant   preps with a mistake 5/7   mistakes 7   expected actions 0/5   unneeded questions 0   kinds {'compliance_drift': 3, 'sticky_label': 1, 'missed_life_event': 3}
  Sonnet alone         preps with a mistake 3/7   mistakes 3   expected actions 1/5   unneeded questions 12   kinds {'missed_life_event': 3}
  Sonnet + harness     preps with a mistake 3/7   mistakes 3   expected actions 1/5   unneeded questions 1   kinds {'missed_life_event': 3}

8 training (all fully recorded): 30 preps
  Scripted assistant   preps with a mistake 19/30   mistakes 29   expected actions 0/16   unneeded questions 0   kinds {'sticky_label': 5, 'compliance_drift': 14, 'said_vs_did': 3, 'missed_life_event': 2, 'stale_reference': 3, 'wrong_decision_maker': 2}
  Sonnet alone         preps with a mistake 10/30   mistakes 12   expected actions 6/16   unneeded questions 27   kinds {'missing_disclosure': 10, 'missed_life_event': 2}
  Sonnet + harness     preps with a mistake 6/30   mistakes 6   expected actions 8/16   unneeded questions 2   kinds {'sticky_label': 3, 'missed_life_event': 2, 'wrong_decision_maker': 1}

4 held-out (all fully recorded): 14 preps
  Scripted assistant   preps with a mistake 11/14   mistakes 14   expected actions 0/13   unneeded questions 0   kinds {'compliance_drift': 4, 'sticky_label': 4, 'missed_life_event': 3, 'said_vs_did': 3}
  Sonnet alone         preps with a mistake 4/14   mistakes 4   expected actions 8/13   unneeded questions 15   kinds {'missed_life_event': 3, 'missing_disclosure': 1}
  Sonnet + harness     preps with a mistake 3/14   mistakes 3   expected actions 9/13   unneeded questions 2   kinds {'missed_life_event': 3}
```

## What it says

- On this new data a strong model alone **does** make mistakes: mostly missing the product-specific disclosures the file
  requires (10 of its 12 mistakes on the training clients). The harness inserts them in code.
- The harness has gaps too: it misses **life events** (a mistake type new in version 3 that no rule covers), and on
  training clients it kept 3 outdated labels, because its single 90-day expiry doesn't fit this data (the teammate's
  expiry reference predicts exactly this).
- Focus holds on unseen data: about 2 questions the file didn't call for, against 15–27 without the harness.

## Caveats

One run per side; small samples (14 held-out preps, 30 training preps; the 2 + 2 client cut is 7 + 7 preps); the clients
counted are those fully recorded before a crash, not a random sample; "forbidden promises" are not reported (the scorer
reads drift severity from the answer key's own wordings, so it can undercount model wording).
