VERDICT: SEND BACK
....................                                                     [100%]

## Blocking findings

1. `pregame/world/scenarios.py:44,583` — `build_scenarios()` defaults to `DEFAULT_SEEDS = (1, 2)`, but the binding interface requires the exact signature/behaviour `build_scenarios(seeds=(1, 2, 3))`. A normal setup therefore creates 36 scenarios instead of the required 54 and omits seed 3 from both tuning and held-out evaluation, weakening the gate's yardstick. Smallest fix: change the default to `(1, 2, 3)` (directly in the signature, or via `DEFAULT_SEEDS`) and update the expected count.

2. `tests/test_world.py:20-22,130-132` — the test fixture calls the default, but `test_scenario_shape_and_split` explicitly expects `3 * 6 * 2`, encoding the implementation's wrong two-seed behaviour. Thus the tests pass while the required signature/coverage is broken. Smallest fix: assert `3 * 6 * 3` and explicitly assert `{s["seed"] for s in scenarios} == {1, 2, 3}`.

3. `pregame/world/store.py:64-67` — `load_scenarios` uses unconditional `replace_one(..., upsert=True)`, so a second call can rewrite existing frozen questions, keys, facts, or split labels. That makes the oracle yardstick editable after creation, contrary to the frozen-scenario rule and the requirement that the self-improving loop cannot edit its own yardstick. Smallest fix: make scenario persistence insert-only (for example `$setOnInsert`) and reject an existing `_id` whose stored document differs; add a test proving a changed reload cannot alter the stored scenario.

## Other findings

1. `pregame/world/store.py:86-100` — `fire_event` implements idempotency as an unlocked read followed by writes. Two concurrent requests can both observe `fired == false`, both return the facts, and both append an `event` ledger entry. It also writes facts with `replace_one(..., upsert=True)`, which can overwrite an existing fact despite facts being insert-only. This can produce duplicate live-demo events/receipts and silently mutate market history. Smallest fix: atomically claim the event with a conditional update on `fired: false` (or perform the event transition in a transaction), and insert facts with insert-only semantics rather than replacement; test two competing fires.

## What I checked and found correct

The requested test command exited 0 with 20 passing test indicators and no failures. Months 1-3 and 4-6 are assigned to tuning and held-out respectively; generated question keys are consistently derived from their referenced, current facts; all fact records are marked `simulated: True`; the named accounts, companies, carriers, ports, railroads, and people are presented as fictional; event lookup, clock advancement, fact filtering, and account lookup otherwise match the documented interfaces. I found no committed or printed secrets in the scoped files, and this card does not configure MongoDB `ServerApi` or validators itself.
