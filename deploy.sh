#!/usr/bin/env bash
# ==============================================================================
# One-Command Deploy / Update Script for Single-Tenant Trading Engine
# Usage: chmod +x deploy.sh && ./deploy.sh
# ==============================================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"
CURRENT_USER="$(whoami)"

echo "[1/5] Pulling latest code..."
if [ -d .git ]; then
    git pull origin main || true
fi

echo "[2/5] Updating Python virtual environment..."
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip -q
pip install --no-cache-dir -q -r requirements.txt

echo "[3/5] Checking directories & .env..."
mkdir -p data/live data/kotak data/performance logs
if [ ! -f .env ]; then
    cp .env.example .env
    echo "WARNING: Created .env from .env.example. Fill in your credentials before going live."
fi

echo "[4/5] Configuring systemd service (User=$CURRENT_USER, Dir=$APP_DIR)..."
sed -e "s|^User=.*|User=$CURRENT_USER|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=$APP_DIR|" \
    -e "s|^EnvironmentFile=.*|EnvironmentFile=$APP_DIR/.env|" \
    -e "s|^ExecStart=.*|ExecStart=$APP_DIR/.venv/bin/python -u main.py|" \
    trading-engine.service > /tmp/trading-engine.service

sudo mv /tmp/trading-engine.service /etc/systemd/system/trading-engine.service
sudo systemctl daemon-reload
sudo systemctl enable trading-engine

echo "[5/5] Restarting trading-engine..."
sudo systemctl restart trading-engine
sleep 2
sudo systemctl status trading-engine --no-pager -l || true

echo ""
echo "Deploy complete! Watch live logs with:"
echo "  journalctl -u trading-engine -f"
