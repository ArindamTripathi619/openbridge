#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python}"

"$PYTHON_BIN" -m pip install -r requirements-dev.txt
"$PYTHON_BIN" -m flake8 --max-complexity=20 --ignore=E501,F401,W293 src/openbridge/workflows.py src/openbridge/app.py
"$PYTHON_BIN" -m mypy --config-file mypy.ini src/openbridge/workflows.py
"$PYTHON_BIN" scripts/check_config_drift.py
"$PYTHON_BIN" -m pytest -q
"$PYTHON_BIN" -m build --sdist --wheel

echo "Preflight passed: lint, complexity, typing, config/docs drift, tests, and build checks succeeded."
