# Showcase data: can the harness learn this book's habits, and does it hold on unseen clients?

26 Sep 2026. Generator: `data/banker_sim_showcase/gen.py`, built from the data seat's v3 generator: same schema,
same writers, same answer-key rules. Check: `showcase_sweep.py`. Scores: `score_preps.py`, which reproduces each
set's answer key exactly.

## Why a new set

On v2, Claude Sonnet 5 alone made no mistakes on 24 preps, so the harness had nothing to fix. v3's own check showed
it can't show learning either: each client's cautious stance lasted a different, arbitrary number of days.
- Expiry settings picked on the 11 tune clients cut mistakes there from 8 to 2.
- On the 9 test clients they did worse than never expiring: 5 mistakes against 4.

## What we planted (we control the data, and say so)

Two habits, the same in tune and test clients, one for each setting the harness can tune:

| Planted habit | The harness setting it tests | Harness default (written before this data existed) |
|---|---|---|
| A panic seller's cautious stance lasts 55 to 65 days after the sell-off, then quietly returns to their usual stance | `label_expiry_days`: how old a behaviour label can be before the prep asks instead of asserting | 90 days |
| A real change of investing style starts with 2 single-stock buys in the first week. "One-off" clients buy one stock once and don't change | `contradiction_threshold`: how many single-stock buys before the prep treats "index only" as contradicted | 1 buy |

- **Where the values are kept:** `hidden/planted.json`. The harness never reads it.
- **Left out on purpose:** life events, contact preferences and product-specific disclosures, because the harness
  doesn't handle them yet (v3 measures that gap).
- **Wording:** notes use one phrasing each (the v2 phrasing). A reader failure therefore can't pass for a
  learning failure. The harness's reader parses every note except the junior's "No changes" notes, which it
  ignores by design.

## Result so far (code only, no AI model)

The harness's own prep writer was run at every combination of the two settings:
- expiry: never, 30, 45, 50, 55, 60, 75, 90, 120, 150 or 180 days;
- buys: off, 1, 2, 3, 4 or 5.

Each best setting was chosen on the **tune clients only**, then scored on the **test clients**.

| Book (test clients' preps) | Harness default (90 days, 1 buy) | Setting chosen on tune (45 days, 2 buys) |
|---|---|---|
| 40 clients, seed 26 (74 preps) | 8 of 74 preps with a mistake; 28 of 34 expected actions; 12 unneeded questions | **0 of 74**; 30 of 34; 6 |
| 40 clients, seed 27 (74 preps) | 9 of 74; 30 of 37; 12 | **0 of 74**; 33 of 37; 6 |
| 40 clients, seed 28 (74 preps) | 9 of 74; 30 of 36; 13 | **0 of 74**; 32 of 36; 6 |
| 20 clients, seed 26 (33 preps) | 3 of 33; 13 of 17; 3 | **0 of 33**; 15 of 17; 3 |

- **Where the default's mistakes come from:**
  - one-off buyers relabelled as stock pickers (said_vs_did);
  - cautious labels kept after the client had quietly returned to their usual stance (sticky_label).
- **Recovery:** in all three books the setting chosen on tune was also the best possible setting on test.
- **Match with the planted values:** any expiry from 30 to 60 days scores the same on tune, so the data can't
  narrow it further. All of that range sits inside the planted 55 to 65 days, and the buy threshold matches exactly.

## What this does not show (say it with the numbers)

- **It is not the self-improving loop yet.** It is the best a loop tuning these two settings could reach, found by
  trying every setting. The loop (Opus proposes, the test gate adopts) still has to be connected to these two
  settings and run.
- **No AI model wrote these preps.** Claude Sonnet alone (no harness) hasn't been run on this set.
- **The habits were planted by us.** This is a controlled test: it shows the loop *can* recover known values and
  that they hold on unseen clients. It is not evidence about real client books.
- **Sizes are small.** 74 test preps per book, from 20 test clients.

## Reproduce

    cd cabinet-eval/data/banker_sim_showcase && python gen.py            # 20 clients (default); N_CLIENTS=40 OUT=out40, SEED=27 ...
    python score_preps.py --baseline --data data/banker_sim_showcase/out # must print matches_answer_key: true
    python showcase_sweep.py --data data/banker_sim_showcase/out40 --json results/showcase_sweep_out40.json
