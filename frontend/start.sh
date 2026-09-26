#!/usr/bin/env bash
# Pregame viewer (macOS / Linux): creates .venv in this folder if missing, installs requirements.txt, starts the
# read-only server and opens the browser. Arguments pass through to server.py:
#   ./start.sh                 online (MongoDB Atlas, config in viewer.env)
#   ./start.sh --offline       serve the snapshot in fixtures/
#   ./start.sh --port 8900 --no-browser
set -u
cd "$(dirname "$0")" || exit 1

VENV_PY=.venv/bin/python
VERSION_CHECK='import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'

find_python() {
  for candidate in python3 python3.13 python3.12 python3.11 python3.10 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "$VERSION_CHECK" >/dev/null 2>&1; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

# A .venv copied from another laptop points at that laptop's Python: rebuild it.
if [ -e "$VENV_PY" ] && ! "$VENV_PY" -c 'import sys' >/dev/null 2>&1; then
  echo "[start] .venv does not run on this machine (copied from another laptop?); rebuilding it."
  rm -rf .venv
fi

created=0
if [ ! -x "$VENV_PY" ]; then
  if ! PYTHON=$(find_python); then
    echo "[start] Python 3.10 or newer is needed (macOS: brew install python@3.12, or python.org)."
    exit 1
  fi
  echo "[start] creating .venv with $PYTHON ..."
  rm -rf .venv
  if ! "$PYTHON" -m venv .venv || [ ! -x "$VENV_PY" ]; then
    echo "[start] could not create .venv (on Debian/Ubuntu: sudo apt install python3-venv)"
    exit 1
  fi
  created=1
fi

# Install requirements when the venv is new or requirements.txt changed since the last successful install.
if [ "$created" = 1 ] || ! cmp -s requirements.txt .venv/requirements.stamp; then
  echo "[start] installing requirements.txt into .venv ..."
  if "$VENV_PY" -m pip install --disable-pip-version-check -q -r requirements.txt; then
    cp requirements.txt .venv/requirements.stamp
  else
    echo "[start] warning: pip install failed (no internet?). --offline still works; online mode needs pymongo."
  fi
fi

exec "$VENV_PY" server.py "$@"
