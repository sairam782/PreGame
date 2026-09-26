#!/usr/bin/env bash
# Pregame live demo (macOS/Linux). Same as scripts/demo_live.ps1: bash scripts/demo_live.sh
# Before: .venv/bin/pip install -r requirements.txt; .venv/bin/python scripts/set_env.py; this computer's IP on the Atlas
# access list; in another terminal: PREGAME_DB=pregame_stage .venv/bin/python scripts/serve.py -> http://127.0.0.1:8000
set -e
cd "$(dirname "$0")/.."
export PREGAME_DB=pregame_stage
echo; echo "=== Step 1: the self-improvement loop, rebuilt live on MongoDB Atlas (about 50 seconds) ==="
PREGAME_LLM_MODE=replay .venv/bin/python -m pregame.cli demo
read -r -p $'\nPress Enter for step 2: Claude Sonnet writes a new brief live (about 25 seconds) '
[ -z "$ANTHROPIC_API_KEY" ] && export PREGAME_PROVIDER=claude-cli
PREGAME_LLM_MODE=live .venv/bin/python -m pregame.cli brief retirement
