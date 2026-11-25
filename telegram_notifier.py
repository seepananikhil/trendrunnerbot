#!/usr/bin/env python3
"""Lightweight Single-User Telegram Notification Engine.

Non-blocking (daemon thread pool) alerts for:
- Trade Entries (Symbol, Option Strike, Spot Price, SL, Lots, Order ID)
- Profit Ratchet / Stop-Loss Trail updates
- Trade Exits & P&L (Spot points, Option points, Realized INR P&L, Exit Reason)
- Daily EOD P&L Summaries
- System & Session Errors (Token expiration, Risk Guardian breach, Order failures)
"""

from __future__ import annotations

import concurrent.futures
import csv
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger("telegram_notifier")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [Telegram] %(message)s", "%H:%M:%S"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "telegram_config.json"
IST = timezone(timedelta(hours=5, minutes=30))

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="tg_worker")
_last_sent_cache: Dict[str, tuple[float, str]] = {}
_send_lock = threading.Lock()


def load_telegram_config() -> Dict[str, Any]:
    """Load single-user Telegram config from env vars or optional telegram_config.json."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    env_enabled = os.getenv("TELEGRAM_ENABLED", "").strip().lower()

    if CONFIG_PATH.exists():
        try:
            file_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            bot_token = bot_token or str(file_cfg.get("bot_token", "")).strip()
            chat_id = chat_id or str(file_cfg.get("chat_id", "")).strip()
        except Exception:
            pass

    if env_enabled in ("0", "false", "no", "off"):
        enabled = False
    elif env_enabled in ("1", "true", "yes", "on"):
        enabled = bool(bot_token and chat_id)
    else:
        enabled = bool(bot_token and chat_id)

    return {
        "bot_token": bot_token,
        "chat_id": chat_id,
        "enabled": enabled,
    }


def is_configured() -> bool:
    cfg = load_telegram_config()
    return bool(cfg["enabled"] and cfg["bot_token"] and cfg["chat_id"])


def _escape_html(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _post_telegram_sync(
    message: str,
    parse_mode: str = "HTML",
    dedup_key: Optional[str] = None,
    cooldown_sec: float = 3.0,
) -> bool:
    cfg = load_telegram_config()
    if not cfg["enabled"] or not cfg["bot_token"] or not cfg["chat_id"]:
        return False

    now = time.time()
    clean_text = re.sub(r"<[^>]+>", "", message).strip()
    if dedup_key:
        with _send_lock:
            last = _last_sent_cache.get(dedup_key)
            if last and (now - last[0]) < cooldown_sec and last[1] == clean_text:
                return True
            _last_sent_cache[dedup_key] = (now, clean_text)

    url = f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage"
    payload = {
        "chat_id": cfg["chat_id"],
        "text": message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                return True
            if resp.status_code == 429:
                retry_after = 2
                try:
                    retry_after = int(resp.json().get("parameters", {}).get("retry_after", 2))
                except Exception:
                    pass
                time.sleep(min(retry_after, 5))
                continue
            logger.warning(f"Telegram API error {resp.status_code}: {resp.text[:200]}")
            return False
        except Exception as exc:
            if attempt == 2:
                logger.warning(f"Telegram send failed: {exc}")
            time.sleep(1)
    return False


def send_telegram_message(
    message: str,
    parse_mode: str = "HTML",
    dedup_key: Optional[str] = None,
    cooldown_sec: float = 3.0,
    blocking: bool = False,
) -> None:
    if not is_configured():
        return
    if blocking:
        _post_telegram_sync(message, parse_mode, dedup_key, cooldown_sec)
    else:
        _executor.submit(_post_telegram_sync, message, parse_mode, dedup_key, cooldown_sec)


def notify_trade_entry(
    symbol: str,
    stage: str,
    side: str,
    option_str: str,
    spot_entry: float,
    sl_pts: float,
    initial_sl: float,
    lots: int,
    qty: int,
    target_pts: Optional[float] = None,
    option_symbol: Optional[str] = None,
    option_entry_price: Optional[float] = None,
    vix_chg: Optional[float] = None,
    order_id: Optional[str] = None,
    mode: str = "PAPER",
) -> None:
    icon = "🟢" if side in ("LONG", "CALL") else "🔴"
    opt_display = option_symbol or option_str
    tgt_line = f"Target: <code>+{target_pts:.1f} pts</code>\n" if target_pts else ""
    opt_px_line = f"Option Fill: <code>₹{option_entry_price:.2f}</code>\n" if option_entry_price else ""
    vix_line = f"India VIX Δ: <code>{vix_chg:+.2f}%</code>\n" if vix_chg is not None else ""
    ord_line = f"Order ID: <code>{_escape_html(order_id)}</code>\n" if order_id else ""

    msg = (
        f"{icon} <b>TRADE ENTRY [{_escape_html(mode.upper())}] — {_escape_html(symbol)}</b>\n"
        f"Setup: <code>{_escape_html(stage)}</code> | Side: <b>{_escape_html(side)}</b>\n"
        f"Contract: <code>{_escape_html(opt_display)}</code>\n"
        f"Qty: <b>{lots} Lot(s)</b> ({qty} qty)\n"
        f"Spot Entry: <code>{spot_entry:.2f}</code> | SL: <code>{initial_sl:.2f}</code> (-{sl_pts:.1f} pts)\n"
        f"{opt_px_line}{tgt_line}{vix_line}{ord_line}"
    )
    send_telegram_message(msg, dedup_key=f"entry_{symbol}_{option_str}")


def notify_ratchet(
    symbol: str,
    side: str,
    tier_label: str,
    new_sl: float,
    locked_pts: float,
    spot_price: float,
) -> None:
    msg = (
        f"🛡️ <b>SL RATCHET — {_escape_html(symbol)} ({_escape_html(side)})</b>\n"
        f"Tier: <b>{_escape_html(tier_label)}</b>\n"
        f"Spot: <code>{spot_price:.2f}</code> | New SL: <code>{new_sl:.2f}</code> (Locked: <b>{locked_pts:+.1f} pts</b>)"
    )
    send_telegram_message(msg, dedup_key=f"ratchet_{symbol}_{tier_label}", cooldown_sec=10.0)


def notify_trade_exit(
    symbol: str,
    side: str,
    option_str: str,
    pnl_pts: float,
    option_pnl_pts: Optional[float],
    pnl_rupees: float,
    exit_reason: str,
    exit_price: float,
    exit_time: str,
    total_lots: int,
    qty: int,
    order_id: Optional[str] = None,
    mode: str = "PAPER",
) -> None:
    win = pnl_rupees >= 0
    icon = "✅" if win else "❌"
    opt_line = f"Option P&amp;L: <code>{option_pnl_pts:+.2f} pts</code>\n" if option_pnl_pts is not None else ""
    ord_line = f"Exit Order ID: <code>{_escape_html(order_id)}</code>\n" if order_id else ""

    msg = (
        f"{icon} <b>TRADE EXIT [{_escape_html(mode.upper())}] — {_escape_html(symbol)}</b>\n"
        f"Contract: <code>{_escape_html(option_str)}</code> ({total_lots} Lot / {qty} qty)\n"
        f"Reason: <b>{_escape_html(exit_reason)}</b> @ <code>{exit_time}</code>\n"
        f"Spot Exit: <code>{exit_price:.2f}</code> (<code>{pnl_pts:+.2f} pts</code>)\n"
        f"{opt_line}"
        f"Realized P&amp;L: <b>₹{pnl_rupees:+,.2f}</b>\n"
        f"{ord_line}"
    )
    send_telegram_message(msg, dedup_key=f"exit_{symbol}_{exit_time}")


def notify_system(title: str, details: str, level: str = "INFO") -> None:
    icons = {"INFO": "ℹ️", "WARN": "⚠️", "ERROR": "🚨", "OK": "✅"}
    icon = icons.get(level.upper(), "ℹ️")
    msg = (
        f"{icon} <b>{_escape_html(title)}</b>\n"
        f"<pre>{_escape_html(details)}</pre>"
    )
    send_telegram_message(msg, dedup_key=f"sys_{title}_{level}", cooldown_sec=30.0)


def dispatch_daily_eod_report(target_date: Optional[str] = None) -> bool:
    """Summarize today's closed trades and send EOD P&L report via Telegram."""
    date_str = target_date or datetime.now(IST).strftime("%Y-%m-%d")
    trades_csv = ROOT / "data" / "performance" / "trend_runner_trades.csv"

    rows = []
    if trades_csv.exists():
        try:
            with trades_csv.open("r", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    if str(r.get("date", "")).strip() == date_str:
                        rows.append(r)
        except Exception as exc:
            logger.warning(f"Failed reading trades CSV for EOD report: {exc}")

    if not rows:
        notify_system(
            f"EOD Summary ({date_str})",
            "No trades executed today.",
            level="INFO",
        )
        return True

    total_pnl = 0.0
    wins = 0
    lines = []
    for r in rows:
        pnl = float(r.get("pnl_rupees") or 0.0)
        total_pnl += pnl
        if pnl >= 0:
            wins += 1
        sym = r.get("symbol", "NIFTY")
        side = r.get("side", "")
        reason = r.get("exit_reason", "")
        lines.append(f"• {sym} {side}: ₹{pnl:+,.2f} ({reason})")

    losses = len(rows) - wins
    icon = "📈" if total_pnl >= 0 else "📉"
    summary = (
        f"{icon} <b>DAILY EOD P&amp;L SUMMARY — {date_str}</b>\n"
        f"Trades: <b>{len(rows)}</b> (Wins: {wins} | Losses: {losses})\n"
        f"Net Realized P&amp;L: <b>₹{total_pnl:+,.2f}</b>\n\n"
        + "\n".join(lines)
    )
    send_telegram_message(summary, dedup_key=f"eod_{date_str}", cooldown_sec=300.0)
    return True
