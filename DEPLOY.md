# Publishing the demo pages, and running the live demo on another computer

**Vercel shows a snapshot; it cannot run the demo.** The live demo (the loop running against MongoDB Atlas, and Claude
writing a brief on the spot) runs on the presenter's own computer: see "Live demo on your computer" at the end.

The pages are static snapshots (no server, no database connection, no secrets): they show the stage replay's data.
The live demo, where the loop really runs against MongoDB Atlas and Claude writes a brief on the spot, runs on the
presenter's laptop (`scripts/serve.py` + the stage replay; see README).

What gets published (from this repository, nothing to build):

| Address | File | What it is |
|---|---|---|
| `/` | `viewer/pregame-viewer.html` | the viewer, one self-contained file with the stage data |
| `/pregame` | `docs/index.html` | the live page's snapshot (proposals, versions, brief before/after, banker book, audit log) |
| `/architecture` | `docs/architecture.html` | the architecture page for judges |
| `/faq` | `docs/FAQ.md` | the fact-checked FAQ (plain text) |

`vercel.json` maps those addresses; `.vercelignore` keeps recordings and local files out.

## Option 1: Vercel website (about 3 minutes, needs access to this GitHub repo)

1. Sign in at https://vercel.com, then **Add New… → Project**.
2. **Import** the GitHub repository `sairam782/PreGame` (the repo owner may need to grant Vercel access to it).
3. Settings: **Framework Preset: Other**; **Root Directory: `./`** (the repository root); leave Build Command and
   Install Command empty; **Output Directory: `.`** (these match `vercel.json`).
4. **Do not add any environment variables.** The pages need none, and a database address must never go on a public site.
5. **Deploy.** Open `<your-project>.vercel.app/`, `/pregame` and `/architecture` to check them.

## Option 2: Vercel command line (about 2 minutes, from a clone of this repo)

```bash
npx vercel@latest login              # opens a browser to sign in
npx vercel@latest deploy --prod      # run in the repository root; accept the defaults
```

## Alternative without Vercel: GitHub Pages

The repo owner: **Settings → Pages → Source: Deploy from a branch → `main` / `/docs` → Save.** The snapshot is then at
https://sairam782.github.io/PreGame/ and the architecture page at https://sairam782.github.io/PreGame/architecture.html.

## Live demo on your computer (about 10 minutes to set up)

Needs: this repo, Python 3.11+, access to the team's Atlas cluster (its connection string, and your computer's IP on
the Atlas access list; the cluster owner has both), and, for step 2 only, Claude (the `claude` command-line tool signed
in to a Claude subscription, or an `ANTHROPIC_API_KEY`).

```bash
git clone https://github.com/sairam782/PreGame && cd PreGame
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt          # macOS/Linux: .venv/bin/pip
.venv/Scripts/python scripts/set_env.py               # paste the Atlas connection string; writes .env (git-ignored)
```

Then, in one window, the live page watching the stage database:

```powershell
$env:PREGAME_DB="pregame_stage"; .venv\Scripts\python scripts\serve.py     # open http://127.0.0.1:8000
```

and in another, the demo (Windows `scripts\demo_live.ps1`, macOS/Linux `scripts/demo_live.sh`):

1. **Step 1, about 50 seconds, no model needed:** replays today's recorded live run into `pregame_stage`. The page
   rebuilds as it runs: the broad change rejected, the narrow change adopted (74% → 94% on unseen test meetings), the
   planted tamper refused, a too-small gain rejected. The test gate, grading, the MongoDB transaction and the audit
   log run for real; only the models' words come from the recording.
2. **Step 2, about 25 seconds, needs Claude:** Claude Sonnet writes a new brief live, using the settings the system
   adopted itself: it leads with the market sell-off and includes the new required-minimum-distribution law.

If Atlas is unreachable (wrong network, IP not on the list), the page says so within seconds; fall back to the Vercel
page or `docs/index.html`.
