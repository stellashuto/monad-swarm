#!/usr/bin/env bash
# Start the Manager Agent
set -euo pipefail
cd "$(dirname "$0")/manager"
source ../.venv/bin/activate
exec python3 main.py
