# Lightweight Single-Tenant Trading Execution Engine

Standalone, headless execution engine extracted for isolated single-user VM deployments (SEBI & broker static-IP whitelisting compliant).

## What's Included
- **`main.py`** — Autonomous orchestrator (pre-market TOTP login, market data, NIFTY/SENSEX strategy loops, Risk Guardian, EOD Telegram summary, midnight rollover).
- **`broker_kotak.py`** — Single-user Kotak Neo client (`.env` credentials, TOTP + MPIN auto-login, market order placement, margin & session checks).
- **`market_data.py`** — Live 1-min OHLC & 3-sec quote streamer for NIFTY 50, SENSEX, and INDIA VIX.
- **`option_pricing.py`** — Real-time nearest weekly option strike & LTP resolver.
- **`strategy_engine.py`** — Trend Runner & VIX Trap signal evaluation with Dual-Confluence / Race Guard & trailing SL ratchets.
- **`risk_guardian.py`** — Real-time MTM P&L watcher with hard `MAX_DAILY_LOSS` auto square-off & daily lock.
- **`telegram_notifier.py` & `telegram_bot.py`** — Trade entry/exit/ratchet/EOD/error alerts + single-user bot commands (`/status`, `/funds`, `/positions`, `/login`, `/pause`, `/resume`, `/eod`).

## Quick Start (Docker)
1. Whitelist your VM's static public IP with Kotak Neo.
2. Configure credentials:
   ```bash
   cp .env.example .env
   nano .env
   ```
3. Launch:
   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```

## Quick Start (Bare-Metal Linux VM)
```bash
chmod +x setup.sh
./setup.sh
nano .env
.venv/bin/python -u main.py
```
To run as a background service, update paths/user in `trading-engine.service` and install it with `systemctl enable --now trading-engine`.
