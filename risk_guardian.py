"""Single-User Risk Guardian & Hard Daily Loss Watcher.

Monitors real-time Kotak Neo account P&L during market hours and enforces
the configured MAX_DAILY_LOSS limit:
- On breach: squares off all open Kotak Neo positions at market, writes
  RISK_LOCK.json + STOP_TRADING, and dispatches a Telegram alert.
- Auto-resets at the start of each new trading day.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from broker_kotak import get_kotak_client, place_kotak_order
from telegram_notifier import notify_system

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent
RISK_LOCK_FILE = ROOT / "RISK_LOCK.json"
STOP_FILE = ROOT / "STOP_TRADING"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("risk_guardian")


def today_str() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def is_market_hours() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return datetime.strptime("09:14", "%H:%M").time() <= t <= datetime.strptime("15:35", "%H:%M").time()


def get_guardian_config() -> dict:
    enabled = os.getenv("RISK_GUARDIAN_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
    auto_close = os.getenv("RISK_GUARDIAN_AUTO_CLOSE", "true").strip().lower() in ("1", "true", "yes", "on")
    try:
        limit = float(os.getenv("MAX_DAILY_LOSS", "2000.0") or 2000.0)
    except ValueError:
        limit = 2000.0
    return {"enabled": enabled, "auto_close": auto_close, "daily_loss_limit": abs(limit)}


def is_risk_locked() -> tuple[bool, dict]:
    """Return (locked, info) for today's trading session."""
    if not RISK_LOCK_FILE.exists():
        return False, {}
    try:
        data = json.loads(RISK_LOCK_FILE.read_text(encoding="utf-8"))
        if data.get("date") == today_str() and data.get("locked"):
            return True, data
        # Stale lock from previous day -> auto-reset
        clear_risk_lock()
        return False, {}
    except Exception:
        return False, {}


def clear_risk_lock() -> None:
    if RISK_LOCK_FILE.exists():
        try:
            RISK_LOCK_FILE.unlink()
        except Exception:
            pass
    if STOP_FILE.exists():
        try:
            if "risk_guardian" in STOP_FILE.read_text(encoding="utf-8"):
                STOP_FILE.unlink()
        except Exception:
            pass


def fetch_kotak_mtm_and_positions() -> tuple[float, list[dict]]:
    """Fetch current day realized + unrealized MTM P&L and open positions from Kotak Neo."""
    client = get_kotak_client()
    if not client:
        return 0.0, []
    try:
        resp = client.positions()
        if not isinstance(resp, dict):
            return 0.0, []
        rows = resp.get("data") or resp.get("result") or []
        if not isinstance(rows, list):
            return 0.0, []

        total_pnl = 0.0
        open_positions = []
        for pos in rows:
            try:
                buy_amt = float(pos.get("buyAmt") or 0.0)
                sell_amt = float(pos.get("sellAmt") or 0.0)
                cf_buy_amt = float(pos.get("cfBuyAmt") or 0.0)
                cf_sell_amt = float(pos.get("cfSellAmt") or 0.0)
                fl_buy_qty = int(pos.get("flBuyQty") or 0) + int(pos.get("cfBuyQty") or 0)
                fl_sell_qty = int(pos.get("flSellQty") or 0) + int(pos.get("cfSellQty") or 0)
                net_qty = fl_buy_qty - fl_sell_qty
                ltp = float(pos.get("ltp") or pos.get("stkPrc") or 0.0)
                mult = float(pos.get("multiplier") or 1.0)
                realized_part = (sell_amt + cf_sell_amt) - (buy_amt + cf_buy_amt)
                unrealized_part = net_qty * ltp * mult if net_qty != 0 else 0.0
                pnl = realized_part + unrealized_part
                total_pnl += pnl

                if net_qty != 0:
                    total_buy = buy_amt + cf_buy_amt
                    total_sell = sell_amt + cf_sell_amt
                    avg_px = 0.0
                    if net_qty > 0 and fl_buy_qty > 0:
                        avg_px = round(total_buy / (fl_buy_qty * mult), 2)
                    elif net_qty < 0 and fl_sell_qty > 0:
                        avg_px = round(total_sell / (fl_sell_qty * mult), 2)
                    open_positions.append({
                        "symbol": str(pos.get("trdSym") or pos.get("sym") or ""),
                        "exchange_segment": str(pos.get("exSeg") or "nse_fo"),
                        "product": str(pos.get("prod") or "NRML"),
                        "net_qty": net_qty,
                        "ltp": round(ltp, 2),
                        "avg_price": avg_px,
                        "pnl": round(pnl, 2),
                    })
            except Exception:
                continue
        return round(total_pnl, 2), open_positions
    except Exception as exc:
        logger.debug(f"Risk guardian position fetch error: {exc}")
        return 0.0, []


def square_off_all_kotak_positions(open_positions: list[dict], reason: str) -> None:
    """Close all open Kotak Neo positions at market."""
    for pos in open_positions:
        sym = pos["symbol"]
        net_qty = int(pos["net_qty"])
        if not sym or net_qty == 0:
            continue
        side = -1 if net_qty > 0 else 1
        qty = abs(net_qty)
        try:
            order_id, fill_px = place_kotak_order(
                symbol=sym,
                qty=qty,
                side=side,
                product=pos.get("product", "NRML"),
                exchange_segment=pos.get("exchange_segment"),
            )
            logger.warning(f"Risk Guardian squared off {sym} qty={qty} side={side} -> order_id={order_id} @ {fill_px} ({reason})")
        except Exception as exc:
            logger.error(f"Risk Guardian square-off failed for {sym}: {exc}")
            notify_system("Risk Guardian Square-Off Failed", f"Symbol: {sym}\nError: {exc}", level="ERROR")


def _guardian_loop(stop_event: threading.Event) -> None:
    logger.info("Risk Guardian started.")
    last_day = today_str()
    while not stop_event.is_set():
        try:
            cur_day = today_str()
            if cur_day != last_day:
                clear_risk_lock()
                last_day = cur_day

            cfg = get_guardian_config()
            if not cfg["enabled"] or not is_market_hours():
                time.sleep(10)
                continue

            total_pnl, open_positions = fetch_kotak_mtm_and_positions()
            locked, _ = is_risk_locked()

            if locked:
                if cfg["auto_close"] and open_positions:
                    square_off_all_kotak_positions(open_positions, "POST_BREACH_GUARD")
                time.sleep(2)
                continue

            limit = cfg["daily_loss_limit"]
            if total_pnl <= -limit:
                logger.error(f"DAILY LOSS LIMIT BREACHED: P&L ₹{total_pnl:+,.2f} <= -₹{limit:,.2f}")
                lock_payload = {
                    "date": cur_day,
                    "locked": True,
                    "limit": limit,
                    "pnl": total_pnl,
                    "breached_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
                }
                RISK_LOCK_FILE.write_text(json.dumps(lock_payload, indent=2), encoding="utf-8")
                STOP_FILE.write_text(f"risk_guardian_halt @ {lock_payload['breached_at']}\n", encoding="utf-8")

                if cfg["auto_close"] and open_positions:
                    square_off_all_kotak_positions(open_positions, "DAILY_LOSS_LIMIT_BREACH")

                notify_system(
                    "RISK GUARDIAN BREACH — TRADING HALTED",
                    f"Daily P&L: ₹{total_pnl:+,.2f}\nLimit: -₹{limit:,.2f}\nAll open positions squared off and trading locked for {cur_day}.",
                    level="ERROR",
                )
        except Exception as exc:
            logger.warning(f"Risk Guardian loop error: {exc}")
        time.sleep(2)


def start_risk_guardian(stop_event: threading.Event | None = None) -> threading.Thread:
    ev = stop_event or threading.Event()
    t = threading.Thread(target=_guardian_loop, args=(ev,), daemon=True, name="risk-guardian")
    t.start()
    return t
