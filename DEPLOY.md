# Publishing the demo pages on Vercel

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
