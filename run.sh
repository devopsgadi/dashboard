#!/usr/bin/env bash
# One-command launcher. Creates a venv, installs deps, and starts the dashboard.
#
#   ./run.sh            # mock mode (default) — no cluster/CI needed
#   ./run.sh --live     # live mode — reads real credentials.json, needs kubectl/helm
#   ./run.sh --port 8600
set -euo pipefail
cd "$(dirname "$0")"

MODE="mock"
PORT="8501"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --live) MODE="live"; shift ;;
    --port) PORT="$2"; shift 2 ;;
    *) echo "unknown arg: $1"; exit 1 ;;
  esac
done

# venv
if [[ ! -d .venv ]]; then
  echo "→ creating virtualenv (.venv)"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "→ installing dependencies"
pip install -q -r requirements.txt

if [[ "$MODE" == "live" ]]; then
  export DASHBOARD_USE_MOCK=false
  echo "→ LIVE mode — using credentials.json"
  command -v kubectl >/dev/null || echo "  ! kubectl not on PATH (Diagnostics/Actions/Terminal need it)"
  command -v helm    >/dev/null || echo "  ! helm not on PATH (rollback / helm status need it)"
  if [[ -f credentials.json ]]; then
    perm=$(stat -c '%a' credentials.json 2>/dev/null || stat -f '%A' credentials.json)
    [[ "$perm" == *"6"?? || "$perm" == "600" ]] || echo "  ! credentials.json is $perm — run: chmod 600 credentials.json"
  fi
else
  export DASHBOARD_USE_MOCK=true
  echo "→ MOCK mode — no cluster or CI needed"
fi

echo "→ starting on http://localhost:${PORT}"
exec streamlit run app.py --server.port "${PORT}"
