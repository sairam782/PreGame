"""Serve Pregame's live page on http://127.0.0.1:8000 from any working directory."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from pregame.cli import main  # noqa: E402

sys.argv = ["pregame", "serve"]
sys.exit(main())
