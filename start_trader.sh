#!/usr/bin/env bash
# Start the Trader Agent
set -euo pipefail
cd "$(dirname "$0")/trader"
source ../.venv/bin/activate
exec python3 main.py
