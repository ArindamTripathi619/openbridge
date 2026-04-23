#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${ROOT_DIR}/dist-nuitka"

mkdir -p "${OUT_DIR}"

if [[ ! -x "${ROOT_DIR}/.venv/bin/python" ]]; then
  echo "Virtualenv not found at .venv. Create it and install dependencies first."
  exit 1
fi

"${ROOT_DIR}/.venv/bin/python" -m nuitka \
  --onefile \
  --assume-yes-for-downloads \
  --enable-plugin=pylint-warnings \
  --include-data-files="${ROOT_DIR}/banner.txt=banner.txt" \
  --include-data-files="${ROOT_DIR}/config/example.yaml=config/example.yaml" \
  --include-data-files="${ROOT_DIR}/config/opencode-bridge.env.example=config/opencode-bridge.env.example" \
  --include-data-files="${ROOT_DIR}/config/openbridge.service.example=config/openbridge.service.example" \
  --output-dir="${OUT_DIR}" \
  --output-filename="openbridge" \
  "${ROOT_DIR}/src/openbridge/app.py"

echo "Nuitka build complete: ${OUT_DIR}/openbridge"
