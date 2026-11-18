#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then echo "Se requiere Python 3.10 o superior."; exit 1; fi
if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi
source .venv/bin/activate
python -m pip install --disable-pip-version-check -r requirements.txt
python scripts/setup_env.py
python scripts/init_db.py
exec python app.py
