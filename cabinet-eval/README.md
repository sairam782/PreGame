# cabinet-eval (copy for the team)

The scorer and the showcase data check. The working copy lives at `C:\Projects\cabinet-eval` on the laptop that runs
the harness (pregame/cabinet.py calls the scorer there).

- `score_preps.py`: grades preps against a banker data set's answer side. Supports v2 and v3 (per-product
  disclosures, life events, contact preference, tune/test splits in `by_split`), and reproduces each set's answer
  key exactly with `--baseline`.
- `export_atlas.py`: exports a cabinet database pair from Atlas to the file layout the scorer reads (read-only;
  never prints the connection string).
- `SHOWCASE.md`: the showcase data set (planted habits), its held-out results and what they don't show. Start here.
- `data/banker_sim_showcase/gen.py`: its generator. **No data folders are committed**, because the answer keys
  stay off GitHub. Run gen.py to rebuild a set.
- `showcase_sweep.py` + `results/`: the setting sweep (code only, no model calls) and its outputs.
