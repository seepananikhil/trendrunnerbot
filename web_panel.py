"""Lightweight Single-User Web Control Panel.

Zero external dependencies (uses Python stdlib http.server).
Protected by WEB_PANEL_PIN in .env.
Allows the user to:
- View live Kotak session, margin, MTM P&L, open positions & today's trades
- Toggle TRADING_MODE (paper/live), lots, product, max daily loss, and strategy flags
- Update Kotak Neo & Telegram credentials
- Trigger Pause, Resume, Refresh Login, Test Telegram, and Emergency Square-Off
"""

from __future__ import annotations

import csv
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

from broker_kotak import available_kotak_funds, kotak_login, kotak_token_status, place_kotak_order, reset_kotak_session
import requests
from risk_guardian import (
    fetch_kotak_mtm_and_positions,
    is_risk_locked,
    square_off_all_kotak_positions,
)
from telegram_notifier import is_configured, send_telegram_message

logger = logging.getLogger("web_panel")

ROOT = Path(__file__).resolve().parent
PANEL_HTML = ROOT / "panel.html"
ENV_PATH = ROOT / ".env"
PORTFOLIO_PATH = ROOT / "portfolio_config.json"
STOP_FILE = ROOT / "STOP_TRADING"
IST = timezone(timedelta(hours=5, minutes=30))

_ENV_SAFE_KEYS = ("TRADING_MODE", "TRADING_LOTS", "MAX_DAILY_LOSS", "ACCOUNT_CAPITAL", "STRIKE_MODE")
_ENGINES_REF: dict = {}
_BROKER_CACHE: dict = {"ts": 0.0, "funds": {"ok": False}, "mtm": 0.0, "positions": []}


def _update_env_file(updates: dict[str, str]) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    remaining = {str(k): str(v) for k, v in updates.items()}
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in remaining:
                val = remaining.pop(k)
                new_lines.append(f"{k}={val}")
                os.environ[k] = val
                continue
        new_lines.append(line)
    for k, val in remaining.items():
        new_lines.append(f"{k}={val}")
        os.environ[k] = val
    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _read_today_trades() -> list[dict]:
    trades_csv = ROOT / "data" / "performance" / "trend_runner_trades.csv"
    if not trades_csv.exists():
        return []
    today_s = datetime.now(IST).strftime("%Y-%m-%d")
    rows = []
    try:
        with trades_csv.open("r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if str(r.get("date", "")).strip() == today_s:
                    rows.append(r)
    except Exception:
        pass
    return rows[-20:]


class PanelHandler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in ("/", "/panel.html"):
            body = PANEL_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/api/state":
            import time as _t
            tok = kotak_token_status(auto_login=False)
            now_ts = _t.time()
            if tok.get("valid") and (now_ts - _BROKER_CACHE["ts"] >= 4.0):
                _BROKER_CACHE["funds"] = available_kotak_funds()
                mtm_pnl, positions = fetch_kotak_mtm_and_positions()
                _BROKER_CACHE["mtm"] = mtm_pnl
                _BROKER_CACHE["positions"] = positions
                _BROKER_CACHE["ts"] = now_ts
            elif not tok.get("valid"):
                _BROKER_CACHE["funds"] = {"ok": False}
                _BROKER_CACHE["mtm"] = 0.0
                _BROKER_CACHE["positions"] = []

            funds = _BROKER_CACHE["funds"]
            mtm_pnl = _BROKER_CACHE["mtm"]
            positions = _BROKER_CACHE["positions"]
            locked, _ = is_risk_locked()
            env_safe = {k: os.getenv(k, "") for k in _ENV_SAFE_KEYS}
            running_strategy = []
            for sym_key, eng in _ENGINES_REF.items():
                pos = getattr(eng, "active_position", None)
                if pos:
                    lot_sz = getattr(eng, "lot_size", 65 if sym_key == "nifty" else 20)
                    lots = int(pos.get("base_lots", 1)) + (1 if pos.get("pyramided") else 0)
                    running_strategy.append({
                        "index": sym_key.upper(),
                        "side": pos.get("side"),
                        "option": pos.get("option_symbol") or pos.get("option"),
                        "qty": lots * lot_sz,
                        "entry_price": pos.get("option_entry_price") or pos.get("entry_price"),
                        "pnl_rs": pos.get("real_pnl_rs", 0.0),
                        "sl": pos.get("current_sl"),
                    })
            self._send_json(200, {
                "kotak": tok,
                "telegram_configured": is_configured(),
                "funds": funds,
                "mtm_pnl": mtm_pnl,
                "positions": positions,
                "running_strategy": running_strategy,
                "risk_locked": locked,
                "paused": STOP_FILE.exists(),
                "env": env_safe,
                "trades": _read_today_trades(),
            })
            return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            self._send_json(400, {"ok": False, "error": "Invalid JSON"})
            return

        if self.path == "/api/settings":
            try:
                env_updates = data.get("env") or {}
                if env_updates:
                    clean_updates = {k: str(v) for k, v in env_updates.items() if str(v).strip() != ""}
                    _update_env_file(clean_updates)

                    neo_changed = any(k.startswith("NEO_") for k in clean_updates)
                    tg_changed = any(k.startswith("TELEGRAM_") for k in clean_updates)

                    if neo_changed:
                        reset_kotak_session()
                        k_res = kotak_login()
                        if not k_res.get("ok"):
                            self._send_json(200, {"ok": False, "message": f"Credentials saved, but Kotak login failed: {k_res.get('error')}"})
                            return

                    if tg_changed:
                        from telegram_bot import start_telegram_bot
                        if not any(t.name == "telegram-bot" and t.is_alive() for t in threading.enumerate()):
                            start_telegram_bot()
                        if is_configured():
                            send_telegram_message("✅ <b>Credentials Updated</b>: Engine reconnected.", blocking=False)

                self._send_json(200, {"ok": True, "message": "Settings saved and applied live!"})
            except Exception as exc:
                self._send_json(500, {"ok": False, "message": f"Save failed: {exc}"})
            return

        if self.path == "/api/action":
            action = str(data.get("action", "")).lower()
            if action == "pause":
                STOP_FILE.write_text("paused_via_web_panel\n", encoding="utf-8")
                self._send_json(200, {"ok": True, "message": "New trade entries PAUSED."})
            elif action == "resume":
                locked, _ = is_risk_locked()
                if locked:
                    self._send_json(200, {"ok": False, "message": "Cannot resume: Risk Guardian lock is active today."})
                    return
                if STOP_FILE.exists():
                    STOP_FILE.unlink()
                self._send_json(200, {"ok": True, "message": "Trade entries RESUMED."})
            elif action == "login":
                res = kotak_login()
                if res.get("ok"):
                    self._send_json(200, {"ok": True, "message": f"Kotak Neo logged in (UCC: {res.get('ucc')})."})
                else:
                    self._send_json(200, {"ok": False, "message": f"Login failed: {res.get('error')}"})
            elif action == "test_telegram":
                if not is_configured():
                    self._send_json(200, {"ok": False, "message": "Telegram not configured in .env."})
                    return
                send_telegram_message("✅ <b>Test Alert</b> from TrendRunner Control Panel.", blocking=True)
                self._send_json(200, {"ok": True, "message": "Test alert sent to Telegram."})
            elif action == "detect_chat_id":
                tok = str(data.get("bot_token") or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
                if not tok:
                    self._send_json(200, {"ok": False, "message": "Enter your Telegram Bot Token first."})
                    return
                try:
                    r = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=8)
                    j = r.json() if r.status_code == 200 else {}
                    results = j.get("result") or []
                    if not results:
                        self._send_json(200, {"ok": False, "message": "No messages found. Send /start to your bot in Telegram, then click Detect again."})
                        return
                    latest = results[-1].get("message") or results[-1].get("edited_message") or {}
                    chat = latest.get("chat") or {}
                    cid = str(chat.get("id") or "").strip()
                    uname = chat.get("username") or chat.get("first_name") or "User"
                    if not cid:
                        self._send_json(200, {"ok": False, "message": "Could not read Chat ID. Send /start to your bot and try again."})
                        return
                    self._send_json(200, {"ok": True, "chat_id": cid, "message": f"Detected Chat ID {cid} ({uname})!"})
                except Exception as exc:
                    self._send_json(500, {"ok": False, "message": f"Telegram lookup failed: {exc}"})
            elif action == "square_off":
                _, open_pos = fetch_kotak_mtm_and_positions()
                if not open_pos:
                    self._send_json(200, {"ok": True, "message": "No open Kotak positions to square off."})
                    return
                square_off_all_kotak_positions(open_pos, "WEB_PANEL_EMERGENCY_SQUARE_OFF")
                self._send_json(200, {"ok": True, "message": f"Square-off sent for {len(open_pos)} open position(s)."})
            elif action == "exit_broker_position":
                sym = str(data.get("symbol", "")).strip()
                net_qty = int(data.get("net_qty", 0) or 0)
                if not sym or net_qty == 0:
                    self._send_json(400, {"ok": False, "message": "Invalid position symbol or quantity."})
                    return
                side = -1 if net_qty > 0 else 1
                try:
                    oid, fill_px = place_kotak_order(
                        symbol=sym,
                        qty=abs(net_qty),
                        side=side,
                        product=str(data.get("product", "NRML")),
                        exchange_segment=data.get("exchange_segment") or None,
                    )
                    self._send_json(200, {"ok": True, "message": f"Exited {sym} (Order ID: {oid} @ ₹{fill_px:.2f})."})
                except Exception as exc:
                    self._send_json(500, {"ok": False, "message": f"Exit failed for {sym}: {exc}"})
            else:
                self._send_json(400, {"ok": False, "message": "Unknown action"})
            return

        self._send_json(404, {"error": "Not found"})

    def log_message(self, fmt: str, *args) -> None:
        pass


def start_web_panel(stop_event: Optional[threading.Event] = None, engines: Optional[dict] = None) -> Optional[threading.Thread]:
    global _ENGINES_REF
    if engines is not None:
        _ENGINES_REF = engines
    enabled = os.getenv("WEB_PANEL_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
    if not enabled:
        logger.info("Web control panel disabled (WEB_PANEL_ENABLED=false).")
        return None

    port = int(os.getenv("WEB_PANEL_PORT", "8080") or 8080)
    server = ThreadingHTTPServer(("0.0.0.0", port), PanelHandler)
    server.timeout = 1.0

    def _serve() -> None:
        logger.info(f"Web control panel listening on http://0.0.0.0:{port}/")
        while not (stop_event and stop_event.is_set()):
            server.handle_request()
        server.server_close()

    t = threading.Thread(target=_serve, daemon=True, name="web-panel")
    t.start()
    return t
