#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${ROOT_DIR}/dist-nuitka"

usage() {
  cat <<'EOF'
Usage: ./scripts/build_nuitka.sh [--allow-downloads] [--help]

Options:
  --allow-downloads  Opt in to Nuitka implicit dependency downloads.
                     Equivalent to NUITKA_ALLOW_DOWNLOADS=1.
  --help             Show this help message.

Notes:
  - Default mode avoids implicit downloads.
  - In automated build environments, use a trusted package mirror and
    set NUITKA_ALLOW_DOWNLOADS=1 only when explicitly approved.
EOF
}

ALLOW_DOWNLOADS="${NUITKA_ALLOW_DOWNLOADS:-0}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --allow-downloads)
      ALLOW_DOWNLOADS=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

mkdir -p "${OUT_DIR}"

if [[ ! -x "${ROOT_DIR}/.venv/bin/python" ]]; then
  echo "Virtualenv not found at .venv. Create it and install dependencies first."
  exit 1
fi

NUITKA_ARGS=(
  --onefile
  --enable-plugin=pylint-warnings
  --include-data-files="${ROOT_DIR}/banner.txt=banner.txt"
  --include-data-files="${ROOT_DIR}/config/example.yaml=config/example.yaml"
  --include-data-files="${ROOT_DIR}/config/opencode-bridge.env.example=config/opencode-bridge.env.example"
  --include-data-files="${ROOT_DIR}/config/openbridge.service.example=config/openbridge.service.example"
  --output-dir="${OUT_DIR}"
  --output-filename="openbridge"
  "${ROOT_DIR}/scripts/openbridge_nuitka_entry.py"
)

if [[ "${ALLOW_DOWNLOADS}" == "1" ]]; then
  echo "Nuitka auto-downloads are enabled (explicit opt-in)."
  NUITKA_ARGS=(--assume-yes-for-downloads "${NUITKA_ARGS[@]}")
else
  echo "Nuitka auto-downloads are disabled by default."
  echo "If Nuitka needs external downloads, rerun with --allow-downloads."
fi

PYTHONPATH="${ROOT_DIR}/src:${PYTHONPATH:-}" "${ROOT_DIR}/.venv/bin/python" -m nuitka \
  "${NUITKA_ARGS[@]}"

if command -v sha256sum >/dev/null 2>&1; then
  (cd "${OUT_DIR}" && sha256sum openbridge > openbridge.sha256)
  echo "Checksum written to: ${OUT_DIR}/openbridge.sha256"
fi

echo "Nuitka build complete: ${OUT_DIR}/openbridge"
