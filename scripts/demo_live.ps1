# Pregame live demo, on any computer with this repo, Python and access to the team's MongoDB Atlas cluster.
#   powershell -ExecutionPolicy Bypass -File scripts\demo_live.ps1
# Before: .venv\Scripts\pip install -r requirements.txt ; .venv\Scripts\python scripts\set_env.py (Atlas URI, never
# committed); this computer's IP on the Atlas access list. In a second window: .venv\Scripts\python scripts\serve.py
# with $env:PREGAME_DB="pregame_stage", then open http://127.0.0.1:8000 beside this one.
# Step 1 needs no model (it replays today's recorded live run; the test gate, grading, MongoDB transaction and audit
# log run for real). Step 2 needs Claude: the `claude` command-line tool signed in (claude-cli), or ANTHROPIC_API_KEY.
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:PREGAME_DB = "pregame_stage"

Write-Host "`n=== Step 1: the self-improvement loop, rebuilt live on MongoDB Atlas (about 50 seconds) ===" -ForegroundColor Cyan
$env:PREGAME_LLM_MODE = "replay"
.venv\Scripts\python -m pregame.cli demo

Read-Host "`nPress Enter for step 2: Claude Sonnet writes a new brief live (about 25 seconds)"
if (-not $env:ANTHROPIC_API_KEY) { $env:PREGAME_PROVIDER = "claude-cli" }
$env:PREGAME_LLM_MODE = "live"
.venv\Scripts\python -m pregame.cli brief retirement
