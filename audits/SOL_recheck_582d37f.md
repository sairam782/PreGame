GO

1. Fixed — record mode refuses a non-empty existing cassette unless `PREGAME_CASSETTE_APPEND=1`; replay-order behavior and the opt-in guard are covered. (`pregame/llm.py:135`, `tests/test_llm.py:350`, `tests/test_llm.py:364`)
2. Fixed — `run_demo` passes its `yes` argument through the same setup database-name fence, and the CLI exposes `demo --yes`; refusal occurs before database writes. (`pregame/loop.py:386`, `pregame/loop.py:402`, `pregame/cli.py:275`, `pregame/cli.py:343`, `tests/test_loop.py:228`)
3. Fixed — scripted improvement outcome headings are appended only when `llm.is_fake`; live/record/replay modes receive neutral headings. (`pregame/loop.py:429`, `pregame/loop.py:433`, `pregame/loop.py:441`, `pregame/loop.py:466`)

Verification: `C:/Projects/prep-harness/.venv/Scripts/python -m pytest -q` passed (499 passed, 5 skipped).
