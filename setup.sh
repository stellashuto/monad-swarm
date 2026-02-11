#!/bin/bash
set -e

echo "=== Monad Swarm — Setup ==="

cd "$(dirname "$0")"

# Create virtual environment
if [ ! -d ".venv" ]; then
    echo "[1/3] Creating Python virtual environment..."
    python3 -m venv .venv
else
    echo "[1/3] Virtual environment already exists."
fi

# Activate and install dependencies
echo "[2/3] Installing Python dependencies..."
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# Install Playwright Chromium
echo "[3/3] Installing Playwright Chromium..."
playwright install chromium 2>/dev/null || echo "  (Playwright Chromium install skipped — may already be available via system chromium)"

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit manager/.env and trader/.env with your API keys and wallet keys"
echo "  2. Activate the .venv: source ~/monad-swarm/.venv/bin/activate"
echo "  3. Start Manager: cd manager && python main.py"
echo "  4. Start Trader:  cd trader && python main.py"
