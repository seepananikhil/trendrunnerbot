#!/usr/bin/env bash
# ==============================================================================
# Single-VM Provisioning Script (Ubuntu / Debian)
# ==============================================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

echo "[1/4] Setting up Python virtual environment..."
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip -q
pip install --no-cache-dir -r requirements.txt

echo "[2/4] Preparing local configuration..."
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example — edit .env with your Kotak Neo & Telegram credentials."
fi

mkdir -p data/live data/kotak data/performance logs

echo "[3/4] Testing module imports..."
python3 -c "import broker_kotak, market_data, option_pricing, strategy_engine, risk_guardian, telegram_notifier, telegram_bot, main; print('All modules OK')"

echo "[4/4] Provisioning complete!"
echo "Next steps:"
echo "  1. Whitelist this VM's static IP ($(curl -s ifconfig.me || echo 'YOUR_VM_IP')) in Kotak Neo API portal."
echo "  2. Edit .env with your credentials and set TRADING_MODE=paper or live."
echo "  3. Start with Docker:   docker compose up -d --build"
echo "     OR with systemd:     sudo cp trading-engine.service /etc/systemd/system/ && sudo systemctl enable --now trading-engine"
