#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ROOT_DIR}/.env"

usage() {
  cat <<'EOF'
Usage:
  bash run_batch.sh <CATALOGUE_OFFSET> [MAX_LAWS] [--allow-empty]

Examples:
  bash run_batch.sh 900
  bash run_batch.sh 1200 300
  bash run_batch.sh 2400 300 --allow-empty

Notes:
  - If DATABASE_URL points to localhost:5432 via SSH tunnel, start
    'bash 0_start_postgres_tunnel.sh' in a separate terminal first.
  - This script updates .env keys: CATALOGUE_OFFSET, MAX_LAWS, FORCE_RESCRAPE.
  - By default, the script stops after 1_fetch_catalogue.py when the filtered
    catalogue is empty, to avoid silent no-op runs.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ -z "${1:-}" ]]; then
  usage
  exit 1
fi

OFFSET="$1"
MAX_LAWS="${2:-300}"
ALLOW_EMPTY="${3:-}"

if ! [[ "$OFFSET" =~ ^[0-9]+$ ]]; then
  echo "Error: CATALOGUE_OFFSET must be a non-negative integer. Got: $OFFSET"
  exit 1
fi

if ! [[ "$MAX_LAWS" =~ ^[1-9][0-9]*$ ]]; then
  echo "Error: MAX_LAWS must be a positive integer. Got: $MAX_LAWS"
  exit 1
fi

if [[ -n "$ALLOW_EMPTY" && "$ALLOW_EMPTY" != "--allow-empty" ]]; then
  echo "Error: unsupported third argument '$ALLOW_EMPTY'"
  echo "Use --allow-empty or omit it."
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Error: missing $ENV_FILE"
  echo "Create it first: cp .env.example .env"
  exit 1
fi

set_env_value() {
  local key="$1"
  local value="$2"

  if grep -q "^${key}=" "$ENV_FILE"; then
    sed -i "s/^${key}=.*/${key}=${value}/" "$ENV_FILE"
  else
    echo "${key}=${value}" >> "$ENV_FILE"
  fi
}

set_env_value "CATALOGUE_OFFSET" "$OFFSET"
set_env_value "MAX_LAWS" "$MAX_LAWS"
set_env_value "FORCE_RESCRAPE" "1"

PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi

if [[ -z "$PYTHON_BIN" ]]; then
  echo "Error: no Python executable found (.venv/bin/python or python3)."
  exit 1
fi

echo "Batch config set in .env: CATALOGUE_OFFSET=${OFFSET}, MAX_LAWS=${MAX_LAWS}, FORCE_RESCRAPE=1"
echo "Using Python: ${PYTHON_BIN}"

run_step() {
  local script_name="$1"
  echo
  echo ">>> Running ${script_name}"
  "$PYTHON_BIN" "${ROOT_DIR}/${script_name}"
}

run_step "1_fetch_catalogue.py"

CATALOGUE_COUNT="$($PYTHON_BIN - <<PY
import json
from pathlib import Path

path = Path(r"${ROOT_DIR}") / "data" / "catalogue.json"
if not path.exists():
    print(-1)
else:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        print(len(data) if isinstance(data, list) else -1)
    except Exception:
        print(-1)
PY
)"

if [[ "$CATALOGUE_COUNT" == "-1" ]]; then
  echo
  echo "Error: could not read data/catalogue.json after fetch."
  exit 1
fi

if [[ "$CATALOGUE_COUNT" == "0" && "$ALLOW_EMPTY" != "--allow-empty" ]]; then
  echo
  echo "Stop: filtered catalogue has 0 entries at offset ${OFFSET}."
  echo "Most common reasons:"
  echo "  1) offset is beyond available catalogue size"
  echo "  2) live fetch failed and fallback data was too small for this offset"
  echo
  echo "No scrape/embed steps were run."
  echo "Retry later or rerun with a lower offset."
  echo "If you intentionally want an empty run, add: --allow-empty"
  exit 2
fi

run_step "2_scrape_laws.py"
run_step "6_retry_failed_ingest.py"
run_step "3_chunk_embed.py"

echo
echo "Batch complete. Next batch offset: $((OFFSET + MAX_LAWS))"
