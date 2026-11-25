"""Lightweight Single-User Interactive Telegram Bot.

Strictly authorized for the single configured TELEGRAM_CHAT_ID.
Commands:
  /status    - Engine mode, Kotak session, Risk Guardian & active positions
  /funds     - Live available margin from Kotak Neo
  /positions - Open Kotak Neo positions & real-time MTM P&L
  /login     - Trigger fresh Kotak Neo TOTP + MPIN authentication
  /pause     - Pause new trade entries (writes STOP_TRADING)
  /resume    - Resume trade entries (if not risk-locked)
  /eod       - Send today's P&L summary
  /help      - Show available commands
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import requests

from broker_kotak import available_kotak_funds, kotak_login, kotak_token_status
from risk_guardian import fetch_kotak_mtm_and_positions, is_risk_locked
from telegram_notifier import dispatch_daily_eod_report, load_telegram_config

logger = logging.getLogger("telegram_bot")

ROOT = Path(__file__).resolve().parent
STOP_FILE = ROOT / "STOP_TRADING"


def _send_reply(bot_token: str, chat_id: str, text: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as exc:
        logger.warning(f"Telegram reply failed: {exc}")


def _handle_command(cmd: str, status_provider: Optional[Callable[[], str]] = None) -> str:
    cmd = cmd.strip().lower().split()[0]

    if cmd in ("/start", "/help"):
        return (
            "🤖 <b>Single-Tenant Trading Engine</b>\n\n"
            "/status — Engine, session &amp; active position status\n"
            "/funds — Available Kotak Neo margin\n"
            "/positions — Open Kotak positions &amp; MTM P&amp;L\n"
            "/login — Refresh Kotak Neo TOTP session\n"
            "/pause — Pause new trade entries\n"
            "/resume — Resume trade entries\n"
            "/eod — Send today's P&amp;L summary"
        )

    if cmd == "/status":
        tok = kotak_token_status(auto_login=False)
        locked, lock_info = is_risk_locked()
        paused = STOP_FILE.exists()
        mode = os.getenv("TRADING_MODE", "paper").strip().upper()
        lines = [
            f"📊 <b>ENGINE STATUS</b>",
            f"Mode: <b>{mode}</b>",
            f"Kotak Session: {'✅ Valid (' + str(tok.get('ucc', '')) + ')' if tok.get('valid') else '❌ ' + str(tok.get('reason', 'Expired'))}",
            f"Risk Lock: {'🔒 LOCKED (Loss ₹' + str(lock_info.get('pnl', 0)) + ')' if locked else '🟢 Clear'}",
            f"Entries Paused: {'⏸️ Yes' if paused else '▶️ Active'}",
        ]
        if status_provider:
            extra = status_provider()
            if extra:
                lines.append("")
                lines.append(extra)
        return "\n".join(lines)

    if cmd == "/funds":
        res = available_kotak_funds()
        if res.get("ok"):
            return f"💰 Available Margin: <b>₹{res.get('available_equity_funds', 0.0):,.2f}</b>"
        return f"❌ Margin check failed: {res.get('error', 'Unknown error')}"

    if cmd == "/positions":
        pnl, positions = fetch_kotak_mtm_and_positions()
        if not positions:
            return f"📭 No open Kotak Neo positions.\nToday's MTM P&amp;L: <b>₹{pnl:+,.2f}</b>"
        lines = [f"📌 <b>OPEN POSITIONS</b> (MTM: <b>₹{pnl:+,.2f}</b>)"]
        for p in positions:
            lines.append(f"• <code>{p['symbol']}</code> | Qty: {p['net_qty']} | P&amp;L: ₹{p['pnl']:+,.2f}")
        return "\n".join(lines)

    if cmd == "/login":
        res = kotak_login()
        if res.get("ok"):
            return f"✅ Kotak Neo authenticated for UCC <code>{res.get('ucc')}</code> ({res.get('greeting_name', '')})"
        return f"❌ Kotak Neo login failed: {res.get('error')}"

    if cmd == "/pause":
        STOP_FILE.write_text("manual_telegram_pause\n", encoding="utf-8")
        return "⏸️ New trade entries <b>PAUSED</b>."

    if cmd == "/resume":
        locked, _ = is_risk_locked()
        if locked:
            return "🔒 Cannot resume: Risk Guardian daily loss lock is active for today."
        if STOP_FILE.exists():
            STOP_FILE.unlink()
        return "▶️ Trading entries <b>RESUMED</b>."

    if cmd == "/eod":
        dispatch_daily_eod_report()
        return "📨 EOD report dispatched."

    return "Unknown command. Send /help for available commands."


def _poll_loop(stop_event: threading.Event, status_provider: Optional[Callable[[], str]]) -> None:
    cfg = load_telegram_config()
    if not cfg["enabled"] or not cfg["bot_token"] or not cfg["chat_id"]:
        logger.info("Telegram bot poller disabled (missing token or chat_id).")
        return

    bot_token = cfg["bot_token"]
    allowed_chat_id = str(cfg["chat_id"]).strip()
    offset = 0
    logger.info("Single-user Telegram bot poller started.")

    while not stop_event.is_set():
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{bot_token}/getUpdates",
                params={"offset": offset, "timeout": 25},
                timeout=35,
            )
            if resp.status_code != 200:
                time.sleep(5)
                continue

            data = resp.json()
            for upd in data.get("result", []):
                offset = max(offset, int(upd["update_id"]) + 1)
                msg = upd.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id", "")).strip()
                text = str(msg.get("text") or "").strip()

                # Strict single-user isolation: ignore any unauthorized chat IDs
                if chat_id != allowed_chat_id or not text.startswith("/"):
                    continue

                reply = _handle_command(text, status_provider=status_provider)
                _send_reply(bot_token, allowed_chat_id, reply)
        except Exception as exc:
            logger.debug(f"Telegram poll error: {exc}")
            time.sleep(5)


def start_telegram_bot(
    stop_event: Optional[threading.Event] = None,
    status_provider: Optional[Callable[[], str]] = None,
) -> threading.Thread:
    ev = stop_event or threading.Event()
    t = threading.Thread(target=_poll_loop, args=(ev, status_provider), daemon=True, name="telegram-bot")
    t.start()
    return t
