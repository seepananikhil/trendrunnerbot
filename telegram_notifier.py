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

    for cfg_file in (CONFIG_PATH, ROOT.parent / "telegram_config.json"):
        if cfg_file.exists():
            try:
                file_cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
                bot_token = bot_token or str(file_cfg.get("bot_token", "")).strip()
                chat_id = chat_id or str(file_cfg.get("chat_id", "")).strip()
                if bot_token and chat_id:
                    break
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


def _format_strategy_name(strategy: Optional[str]) -> str:
    s = (strategy or "").upper()
    if "MANUAL" in s and "SCALP" in s:
        return "Manual Scalp"
    if "REENTRY" in s or "STAGE2" in s:
        return "Trend Runner (Re-Entry)"
    if "TREND" in s or "RUNNER" in s or "MORNING" in s or "STAGE1" in s or "ORB" in s:
        return "Trend Runner (15m ORB)"
    if "KEY_LEVEL" in s or "FADE" in s or "REVERSE" in s:
        return "Key Levels"
    if "MIDPOINT" in s:
        return "Midpoint Scalp"
    return strategy.replace("_", " ").title() if strategy else "Trend Runner (15m ORB)"


def _clean_option_name(option_str: str) -> str:
    """Strip strike moneyness tags like (ITM-150) / (ATM) for clean display."""
    if not option_str:
        return ""
    return re.sub(r"\s*\([^)]*\)", "", option_str).strip()


def notify_trade_entry(
    symbol: str,
    stage: str,
    side: str,
    option_str: str,
    spot_entry: Optional[float] = None,
    sl_pts: float = 0.0,
    initial_sl: Optional[float] = None,
    lots: int = 1,
    qty: int = 0,
    target_pts: Optional[float] = None,
    option_symbol: Optional[str] = None,
    option_entry_price: Optional[float] = None,
    vix_chg: Optional[float] = None,
    order_id: Optional[str] = None,
    mode: str = "PAPER",
    option_sl_px: Optional[float] = None,
    entry_time: Optional[str] = None,
) -> None:
    is_bullish = side.upper() in ("LONG", "CALL")
    action = "BUY CALL" if is_bullish else "BUY PUT"
    dot = "🟢" if is_bullish else "🔴"
    strat_name = _format_strategy_name(stage)
    raw_opt = option_symbol or option_str
    clean_opt = _clean_option_name(raw_opt)
    if clean_opt and symbol.upper() not in clean_opt.upper():
        clean_opt = f"{symbol} {clean_opt}"
    now_ist = entry_time or datetime.now(IST).strftime("%H:%M")

    prem_lines = []
    if option_entry_price and option_entry_price > 0:
        prem_lines.append(f"💵 <b>Option Entry Price:</b> ₹{option_entry_price:,.2f}")
        total_prem = option_entry_price * qty
        prem_lines.append(f"💼 <b>Capital Deployed:</b> ₹{total_prem:,.2f}")
    prem_block = ("\n" + "\n".join(prem_lines)) if prem_lines else ""

    loc_lines = []
    if spot_entry is not None and spot_entry > 0 and initial_sl is not None:
        loc_lines.append(f"📍 <b>Spot Entry:</b> {spot_entry:,.2f}")
        loc_lines.append(f"🛑 <b>Stop Loss:</b> {initial_sl:,.2f} ({sl_pts:.1f} pts)")
    elif option_sl_px is not None and option_sl_px > 0:
        loc_lines.append(f"🛑 <b>Stop Loss:</b> ₹{option_sl_px:,.2f} ({sl_pts:.1f} pts)")
    elif sl_pts > 0:
        loc_lines.append(f"🛑 <b>Trailing SL:</b> {sl_pts:.1f} pts")
    if target_pts:
        loc_lines.append(f"🎯 <b>Target:</b> +{target_pts:.0f} pts")
    if vix_chg is not None:
        mom = "Bullish Momentum Confirmed" if is_bullish else "Bearish Momentum Confirmed"
        loc_lines.append(f"🌊 <b>India VIX Δ:</b> {vix_chg:+.2f}% ({mom})")
    loc_block = ("\n\n" + "\n".join(loc_lines)) if loc_lines else ""

    if "MANUAL" in (stage or "").upper():
        footer = f"🛡️ <i>Trailing SL / Target active · Order #{_escape_html(order_id)}</i>" if order_id else "🛡️ <i>Manual Scalp Live Execution</i>"
    else:
        footer = "🛡️ <i>Trailing 5m 20-EMA active (No ratchets · Clean exit on signal)</i>"

    msg = (
        f"{dot} <b>TRADE ENTRY · {_escape_html(symbol)}</b>\n\n"
        f"⚡ <b>Setup:</b> {_escape_html(strat_name)}\n"
        f"🎯 <b>Position Taken:</b> <b>{action} · {_escape_html(clean_opt)}</b>\n"
        f"📦 <b>Size:</b> {lots} Lot{'s' if lots > 1 else ''} ({qty} qty)"
        f"{prem_block}"
        f"{loc_block}\n\n"
        f"⏰ <b>Entry Time:</b> {_escape_html(now_ist)} IST\n"
        f"{footer}"
    )
    dedup = f"entry_{symbol}_{option_str}_{order_id}" if order_id else f"entry_{symbol}_{option_str}"
    send_telegram_message(msg, dedup_key=dedup)


def notify_trade_exit(
    symbol: str,
    side: str,
    option_str: str,
    pnl_pts: Optional[float],
    option_pnl_pts: Optional[float],
    pnl_rupees: float,
    exit_reason: str,
    exit_price: Optional[float],
    exit_time: str,
    total_lots: int,
    qty: int,
    order_id: Optional[str] = None,
    mode: str = "PAPER",
    option_exit_price: Optional[float] = None,
    strategy: Optional[str] = "Trend Runner",
) -> None:
    badge_icon = "🟢" if pnl_rupees >= 0 else "🔴"
    badge_pnl = f"+₹{pnl_rupees:,.2f}" if pnl_rupees >= 0 else f"-₹{abs(pnl_rupees):,.2f}"
    clean_opt = _clean_option_name(option_str)
    if clean_opt and symbol.upper() not in clean_opt.upper():
        clean_opt = f"{symbol} {clean_opt}"
    clean_reason = exit_reason.replace("_", " ").title()
    strat_name = _format_strategy_name(strategy)
    opt_pts_str = f" ({option_pnl_pts:+.2f} pts)" if option_pnl_pts is not None and option_pnl_pts != 0 else ""

    exit_px_val = option_exit_price if (option_exit_price is not None and option_exit_price > 0) else (exit_price or 0.0)
    px_line = f"💵 <b>Exit Price:</b> ₹{exit_px_val:,.2f}\n" if exit_px_val > 0 else ""
    spot_line = f"📊 <b>Spot Movement:</b> {pnl_pts:+.1f} pts\n" if pnl_pts is not None else ""
    time_display = exit_time if "IST" in exit_time else f"{exit_time} IST"
    ord_note = f" · Order #{_escape_html(order_id)}" if order_id else ""

    msg = (
        f"🏁 <b>TRADE EXIT · {_escape_html(symbol)}</b>\n\n"
        f"💰 <b>Realized PnL:</b> {badge_icon} <b>{badge_pnl}</b>{opt_pts_str}\n"
        f"🎯 <b>Position Closed:</b> {_escape_html(clean_opt)} ({total_lots} Lot{'s' if total_lots > 1 else ''} / {qty} qty)\n\n"
        f"{px_line}"
        f"📍 <b>Exit Reason:</b> <b>{_escape_html(clean_reason)}</b>\n"
        f"⏰ <b>Exit Time:</b> {_escape_html(time_display)}\n"
        f"{spot_line}\n"
        f"⚡ <i>Strategy: {_escape_html(strat_name)}{ord_note}</i>"
    )
    dedup = f"exit_{symbol}_{order_id}" if order_id else f"exit_{symbol}_{exit_time}"
    send_telegram_message(msg, dedup_key=dedup)


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
