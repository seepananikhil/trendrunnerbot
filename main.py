#!/usr/bin/env python3
"""Autonomous Single-Tenant Trading Execution Engine.

Headless orchestrator for a dedicated single-user VM with a whitelisted static IP:
- Loads credentials and runtime flags from .env and portfolio_config.json
- Authenticates with Kotak Neo via TOTP + MPIN (auto-refreshes pre-market)
- Streams live 1-min candles & quotes for NIFTY, SENSEX, and INDIA VIX
- Evaluates Trend Runner & VIX Trap setups with Dual-Confluence & Race Guard
- Executes live Kotak Neo orders (when TRADING_MODE=live) or paper fills
- Enforces hard daily loss limits via Risk Guardian
- Dispatches Telegram alerts & responds to single-user bot commands
"""

from __future__ import annotations

import csv
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent


def _load_env_file(env_path: Path) -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
        return
    except ImportError:
        pass
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file(ROOT / ".env")

IST = timezone(timedelta(hours=5, minutes=30))
DATA_DIR = ROOT / "data"
LIVE_DIR = DATA_DIR / "live"
LOG_DIR = ROOT / "logs"
LIVE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
STOP_FILE = ROOT / "STOP_TRADING"
PORTFOLIO_CONFIG_PATH = ROOT / "portfolio_config.json"

# Configure logging (stdout + rotating file)
LOG_LEVEL = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(
    level=LOG_LEVEL,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(LOG_DIR / "engine.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"),
    ],
)
logger = logging.getLogger("engine")

from broker_kotak import kotak_login, kotak_token_status, place_kotak_order
from market_data import MarketDataStreamer
from option_pricing import clear_scrip_cache, get_live_option_ltp
from risk_guardian import clear_risk_lock, is_risk_locked, start_risk_guardian
from strategy_engine import TrendRunnerPaperEngine, compute_indicators, get_vix_change_pct
from telegram_bot import start_telegram_bot
from telegram_notifier import dispatch_daily_eod_report, notify_system
from web_panel import start_web_panel

STOP_EVENT = threading.Event()
LIVE_ENGINES: Dict[str, TrendRunnerPaperEngine] = {}
DUAL_CONFLUENCE_STATE: Dict[str, Any] = {
    "armed": {},
    "confirmed": None,
    "history": [],
}
_STATE_LOCK = threading.Lock()


def today_str() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def is_live_mode() -> bool:
    return os.getenv("TRADING_MODE", "paper").strip().lower() == "live"


def load_portfolio_config() -> dict:
    if PORTFOLIO_CONFIG_PATH.exists():
        try:
            return json.loads(PORTFOLIO_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"Failed to parse portfolio_config.json: {exc}")
    return {
        "trading_instruments": "BOTH",
        "cross_instrument_concurrency": "FIRST_TRIGGERED",
        "require_dual_activation": True,
        "require_dual_ema20_cross": True,
        "dual_activation_window_mins": 60,
        "trailing_mode": "ema20_5m",
        "setups": {"trend_runner": True, "vix_trap": True},
    }


def log_live_order_event(
    action: str,
    symbol: str,
    qty: int,
    order_id: str,
    fill_price: float,
    reason: str,
    mode: str,
    pnl_rs: float = 0.0,
) -> None:
    path = LIVE_DIR / f"live_orders_{today_str()}.csv"
    is_new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["timestamp", "action", "symbol", "qty", "order_id", "fill_price", "reason", "mode", "pnl_rs"])
        w.writerow([
            datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
            action,
            symbol,
            qty,
            order_id,
            fill_price,
            reason,
            mode,
            round(pnl_rs, 2),
        ])


def check_dual_activation_gate(sym: str, sig: str, t_str: str) -> bool:
    """Concurrency & Dual-Activation Confluence Gate between NIFTY and SENSEX."""
    with _STATE_LOCK:
        port_cfg = load_portfolio_config()
        inst = port_cfg.get("trading_instruments", "BOTH")
        cross_mode = port_cfg.get("cross_instrument_concurrency", "FIRST_TRIGGERED")
        require_dual = bool(port_cfg.get("require_dual_activation", True))
        window_mins = int(port_cfg.get("dual_activation_window_mins", 60))

        other_sym = "SENSEX" if sym.upper() == "NIFTY" else "NIFTY"
        other_key = other_sym.lower()

        if inst in ("BOTH", "PARALLEL_FIRST_TRIGGERED") and cross_mode in ("FIRST_TRIGGERED", "DUAL_ACTIVATION_FIRST_TRIGGERED"):
            other_eng = LIVE_ENGINES.get(other_key)
            if other_eng and other_eng.active_position is not None:
                logger.info(f"[{t_str}] [{sym} RACE GATE] {other_sym} already in trade; {sym} blocked.")
                return False

        if require_dual and inst in ("BOTH", "PARALLEL_FIRST_TRIGGERED"):
            armed = DUAL_CONFLUENCE_STATE["armed"]
            for k in list(armed.keys()):
                try:
                    m_diff = abs((datetime.strptime(t_str, "%H:%M") - datetime.strptime(armed[k]["time"], "%H:%M")).total_seconds()) / 60.0
                    if m_diff > window_mins:
                        del armed[k]
                except Exception:
                    pass

            if other_sym in armed and armed[other_sym]["side"] == sig:
                t_arm = armed[other_sym]["time"]
                m_diff = abs((datetime.strptime(t_str, "%H:%M") - datetime.strptime(t_arm, "%H:%M")).total_seconds()) / 60.0
                if m_diff <= window_mins:
                    conf_event = {
                        "side": sig,
                        "time": t_str,
                        "first_symbol": other_sym,
                        "confirming_symbol": sym,
                        "diff_mins": round(m_diff, 1),
                    }
                    DUAL_CONFLUENCE_STATE["confirmed"] = conf_event
                    DUAL_CONFLUENCE_STATE["history"].append(conf_event)
                    armed.clear()
                    logger.info(f"[{t_str}] [DUAL CONFLUENCE CONFIRMED] {other_sym} + {sym} agree on {sig} (delta {m_diff:.1f}m)")
                    return True

            if other_sym in armed and armed[other_sym]["side"] != sig:
                logger.info(f"[{t_str}] [DUAL DIVERGENCE] {other_sym}={armed[other_sym]['side']} vs {sym}={sig}. Cleared.")
                armed.clear()
                return False

            if sym not in armed or armed[sym].get("side") != sig:
                armed[sym] = {"side": sig, "time": t_str}
                logger.info(f"[{t_str}] [{sym} DUAL GATE ARMED] {sig} armed; awaiting {other_sym} within {window_mins}m.")
            return False

        return True


def check_dual_ema_cross_exit(sym: str, sig: str, t_str: str, c_5m: float, ema20_5m: float) -> bool:
    """Require sister index to also cross 5m 20-EMA before confirming EMA20 exit."""
    try:
        port_cfg = load_portfolio_config()
        if not port_cfg.get("require_dual_ema20_cross", True):
            return True
        other_sym = "SENSEX" if sym.upper() == "NIFTY" else "NIFTY"
        other_engine = LIVE_ENGINES.get(other_sym.lower())
        if not other_engine:
            return True

        other_c = getattr(other_engine, "last_completed_5m_close", None)
        other_ema = getattr(other_engine, "last_completed_5m_ema20", None)
        if other_c is None or other_ema is None:
            return True

        other_c = float(other_c)
        other_ema = float(other_ema)
        if other_c == 0.0:
            return True

        other_crossed = (other_c > other_ema) if sig in ("SHORT", "PUT") else (other_c < other_ema)
        if not other_crossed:
            logger.info(f"[{t_str}] [DUAL EMA HOLD] {sym} crossed 5m EMA20, waiting on {other_sym}.")
            return False
        return True
    except Exception as exc:
        logger.warning(f"Dual EMA exit check error: {exc}")
        return True


def make_entry_handler(symbol: str, lot_size: int):
    def _on_entry(pos: dict) -> None:
        if not is_live_mode():
            return
        locked, _ = is_risk_locked()
        if locked or STOP_FILE.exists():
            logger.warning(f"[{symbol}] Live entry skipped: Risk Lock or STOP_TRADING active.")
            return

        opt_symbol = pos.get("option_symbol") or ""
        if not opt_symbol or opt_symbol == pos.get("option"):
            q = get_live_option_ltp(symbol, pos["strike"], pos["opt_type"])
            if q and q.get("symbol"):
                opt_symbol = q["symbol"]
                pos["option_symbol"] = opt_symbol

        if not opt_symbol:
            err = f"Cannot place live entry for {symbol}: unresolved option trading symbol."
            logger.error(err)
            notify_system("Live Order Error", err, level="ERROR")
            return

        qty = int(pos.get("base_lots", 1)) * lot_size
        product = os.getenv("ORDER_PRODUCT", "NRML").strip().upper() or "NRML"
        ex_seg = "bse_fo" if symbol.upper() == "SENSEX" else "nse_fo"
        try:
            order_id, fill_px = place_kotak_order(
                symbol=opt_symbol,
                qty=qty,
                side=1,
                product=product,
                exchange_segment=ex_seg,
            )
            pos["live_order_id"] = order_id
            if fill_px > 0:
                pos["option_entry_price"] = fill_px
            log_live_order_event("BUY", opt_symbol, qty, order_id, fill_px, pos.get("stage", "ENTRY"), "LIVE")
            logger.info(f"[{symbol}] LIVE BUY {opt_symbol} qty={qty} -> order_id={order_id} @ {fill_px}")
        except Exception as exc:
            logger.error(f"[{symbol}] Live entry order failed: {exc}")
            notify_system(f"Live Entry Failed ({symbol})", f"Contract: {opt_symbol}\nQty: {qty}\nError: {exc}", level="ERROR")

    return _on_entry


def make_exit_handler(symbol: str, lot_size: int):
    def _on_exit(pos: dict, trade_result: dict) -> None:
        if not is_live_mode():
            return
        opt_symbol = pos.get("option_symbol") or trade_result.get("option_symbol") or ""
        if not opt_symbol or not pos.get("live_order_id"):
            return

        qty = int(trade_result.get("total_lots", pos.get("base_lots", 1))) * lot_size
        product = os.getenv("ORDER_PRODUCT", "NRML").strip().upper() or "NRML"
        ex_seg = "bse_fo" if symbol.upper() == "SENSEX" else "nse_fo"
        try:
            order_id, fill_px = place_kotak_order(
                symbol=opt_symbol,
                qty=qty,
                side=-1,
                product=product,
                exchange_segment=ex_seg,
            )
            pnl_rs = float(trade_result.get("pnl_rupees") or 0.0)
            log_live_order_event("SELL", opt_symbol, qty, order_id, fill_px, trade_result.get("exit_reason", "EXIT"), "LIVE", pnl_rs)
            logger.info(f"[{symbol}] LIVE SELL {opt_symbol} qty={qty} -> order_id={order_id} @ {fill_px}")
        except Exception as exc:
            logger.error(f"[{symbol}] Live exit order failed: {exc}")
            notify_system(f"CRITICAL: Live Exit Failed ({symbol})", f"Contract: {opt_symbol}\nQty: {qty}\nError: {exc}", level="ERROR")

    return _on_exit


def strategy_runner_loop(symbol: str) -> None:
    """Run autonomous 1-min bar evaluation loop for NIFTY or SENSEX."""
    sym_upper = symbol.upper()
    sym_lower = symbol.lower()
    lot_size = 20 if sym_upper == "SENSEX" else 65
    ratchet_key = "sensex_spot_ratchet_settings" if sym_upper == "SENSEX" else "spot_ratchet_settings"
    trailing_key = "sensex_trailing_mode" if sym_upper == "SENSEX" else "nifty_trailing_mode"

    cur_day = today_str()
    live_file = LIVE_DIR / f"{sym_lower}_1min_{cur_day}.csv"
    engine: Optional[TrendRunnerPaperEngine] = None
    processed_times: set[str] = set()

    logger.info(f"Strategy loop started for {sym_upper} (lot_size={lot_size}).")

    while not STOP_EVENT.is_set():
        try:
            now = datetime.now(IST)
            if now.weekday() >= 5:
                time.sleep(60)
                continue

            port_cfg = load_portfolio_config()
            active_inst = port_cfg.get("trading_instruments", "BOTH")
            if (sym_upper == "NIFTY" and active_inst == "SENSEX_ONLY") or (sym_upper == "SENSEX" and active_inst == "NIFTY_ONLY"):
                time.sleep(5)
                continue

            new_day = now.strftime("%Y-%m-%d")
            if new_day != cur_day:
                cur_day = new_day
                live_file = LIVE_DIR / f"{sym_lower}_1min_{cur_day}.csv"
                engine = None
                processed_times.clear()
                with _STATE_LOCK:
                    DUAL_CONFLUENCE_STATE["armed"].clear()
                    DUAL_CONFLUENCE_STATE["confirmed"] = None
                    DUAL_CONFLUENCE_STATE["history"].clear()

            if not live_file.exists():
                time.sleep(5)
                continue

            df1 = pd.read_csv(live_file)
            if df1.empty or len(df1) < 15:
                time.sleep(5)
                continue

            setups_cfg = port_cfg.get("setups", {})
            enable_trend = bool(setups_cfg.get("trend_runner", True))
            enable_trap = bool(setups_cfg.get("vix_trap", True))
            ratchet = port_cfg.get(ratchet_key, {})

            if engine is None:
                lots_cfg = int(os.getenv("TRADING_LOTS", "1") or 1)
                sizing_cfg = os.getenv("SIZING_MODE", port_cfg.get("sizing_mode", "fixed")).lower()
                engine = TrendRunnerPaperEngine(
                    symbol=sym_upper,
                    trailing_mode=str(port_cfg.get(trailing_key, port_cfg.get("trailing_mode", "ema20_5m"))).lower(),
                    sl_points=float(ratchet.get("initial_sl_pts", 120.0 if sym_upper == "SENSEX" else 30.0)),
                    sl_mode="fixed",
                    lots=lots_cfg,
                    sizing_mode=sizing_cfg,
                    max_trades_per_day=int(port_cfg.get("max_trades_per_day", 3)),
                    call_cutoff_time=str(ratchet.get("call_cutoff_time", "10:45")),
                    reentry_cooloff_bars=int(ratchet.get("reentry_cooloff_bars", 20)),
                    vix_th=float(ratchet.get("vix_th", 1.8)),
                    vix_call_th=float(ratchet.get("vix_call_th", 1.8)),
                    r1_trigger=float(ratchet.get("tier1_trigger_pts", 70.0)),
                    r1_lock=float(ratchet.get("tier1_lock_pts", 20.0)),
                    r2_trigger=float(ratchet.get("tier2_trigger_pts", 110.0)),
                    r2_lock=float(ratchet.get("tier2_lock_pts", 50.0)),
                    r3_trigger=float(ratchet.get("tier3_trigger_pts", 180.0)),
                    r3_lock=float(ratchet.get("tier3_lock_pts", 110.0)),
                    r4_trigger=float(ratchet.get("tier4_trigger_pts", 230.0)),
                    r4_lock=float(ratchet.get("tier4_lock_pts", 165.0)),
                    r5_trigger=float(ratchet.get("tier5_trigger_pts", 280.0)),
                    r5_lock=float(ratchet.get("tier5_lock_pts", 220.0)),
                    r6_trigger=float(ratchet.get("tier6_trigger_pts", 330.0)),
                    r6_lock=float(ratchet.get("tier6_lock_pts", 270.0)),
                    reentry=True,
                    notify_telegram=True,
                    fetch_live_quotes=True,
                    enable_trend_runner=enable_trend,
                    enable_vix_trap=enable_trap,
                    entry_gate_fn=check_dual_activation_gate,
                    exit_gate_fn=check_dual_ema_cross_exit,
                    on_entry_fn=make_entry_handler(sym_upper, lot_size),
                    on_exit_fn=make_exit_handler(sym_upper, lot_size),
                    live_trading=is_live_mode(),
                )
                engine.current_day_str = cur_day
                LIVE_ENGINES[sym_lower] = engine

            df5, or_high, or_low, or_range = compute_indicators(df1)
            if df5.empty:
                time.sleep(5)
                continue

            df1["time_str"] = pd.to_datetime(df1["timestamp"]).dt.strftime("%H:%M")
            tp1 = (df1["high"] + df1["low"] + df1["close"]) / 3.0
            vol1 = df1["volume"] if "volume" in df1.columns else 1.0
            pv1 = tp1 * vol1
            cum_pv1 = pv1.cumsum()
            cum_vol1 = vol1.cumsum()
            df1["vwap"] = np.where(cum_vol1 > 0, cum_pv1 / cum_vol1, tp1.expanding().mean())

            vix_curr = get_vix_change_pct(cur_day)

            for idx in range(len(df1) - 1):
                bar = df1.iloc[idx]
                bar_t = str(bar["time_str"])
                if bar_t not in processed_times:
                    next_open = float(df1.iloc[idx + 1]["open"]) if idx + 1 < len(df1) else None
                    res = engine.evaluate_bar(bar, or_high, or_low, or_range, vix_curr, next_bar_open=next_open)
                    if res:
                        logger.info(f"[{sym_upper}] Position Event: {res}")
                    processed_times.add(bar_t)
        except Exception as exc:
            logger.exception(f"Error in {sym_upper} strategy loop: {exc}")
        time.sleep(2)


def scheduler_loop() -> None:
    """Handle pre-market auth refresh (08:55), EOD summary (15:30), and midnight rollover."""
    last_day = today_str()
    premarket_tag = ""
    eod_tag = ""

    while not STOP_EVENT.is_set():
        try:
            now = datetime.now(IST)
            cur_day = now.strftime("%Y-%m-%d")

            if cur_day != last_day:
                logger.info(f"Midnight rollover: {last_day} -> {cur_day}")
                clear_risk_lock()
                clear_scrip_cache()
                last_day = cur_day

            if now.weekday() < 5:
                # 08:55 IST: Pre-market Kotak Neo TOTP auto-login
                if (now.hour, now.minute) == (8, 55) and premarket_tag != cur_day:
                    premarket_tag = cur_day
                    res = kotak_login()
                    if res.get("ok"):
                        notify_system("Pre-Market Login OK", f"Kotak Neo authenticated for UCC {res.get('ucc')}.", level="OK")
                    else:
                        notify_system("Pre-Market Login Failed", str(res.get("error")), level="ERROR")

                # 15:30 IST: Daily EOD P&L summary
                if (now.hour, now.minute) == (15, 30) and eod_tag != cur_day:
                    eod_tag = cur_day
                    dispatch_daily_eod_report(cur_day)
        except Exception as exc:
            logger.warning(f"Scheduler error: {exc}")
        time.sleep(15)


def get_status_summary() -> str:
    lines = []
    for key in ("nifty", "sensex"):
        eng = LIVE_ENGINES.get(key)
        if eng:
            pos = eng.active_position
            pos_str = f"IN_POS ({pos['side']} {pos.get('option_symbol', pos.get('option'))})" if pos else "SCANNING"
            lines.append(f"{key.upper()}: {pos_str} | Trades: {eng.trades_today}")
    return "\n".join(lines)


def _handle_signal(signum, frame) -> None:
    logger.info(f"Received signal {signum}, shutting down...")
    STOP_EVENT.set()


def main() -> None:
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    mode = "LIVE" if is_live_mode() else "PAPER"
    logger.info(f"Starting Single-Tenant Trading Execution Engine [Mode: {mode}]")

    # Authenticate Kotak Neo on startup
    tok = kotak_token_status(auto_login=True)
    if tok.get("valid"):
        logger.info(f"Kotak Neo session valid for UCC {tok.get('ucc')}")
        notify_system("Trading Engine Started", f"Mode: {mode}\nUCC: {tok.get('ucc')}\nStatus: Online", level="OK")
    else:
        logger.warning(f"Kotak Neo session not ready: {tok.get('reason')}")
        notify_system("Trading Engine Started (Auth Warning)", f"Mode: {mode}\nReason: {tok.get('reason')}", level="WARN")

    # Start core autonomous threads
    streamer = MarketDataStreamer()
    streamer.stop_event = STOP_EVENT
    threading.Thread(target=streamer.run_forever, daemon=True, name="market-data").start()

    start_risk_guardian(STOP_EVENT)
    start_telegram_bot(STOP_EVENT, status_provider=get_status_summary)
    start_web_panel(STOP_EVENT, engines=LIVE_ENGINES)
    threading.Thread(target=scheduler_loop, daemon=True, name="scheduler").start()
    threading.Thread(target=strategy_runner_loop, args=("NIFTY",), daemon=True, name="strategy-nifty").start()
    threading.Thread(target=strategy_runner_loop, args=("SENSEX",), daemon=True, name="strategy-sensex").start()

    while not STOP_EVENT.is_set():
        time.sleep(1)

    logger.info("Trading Execution Engine stopped cleanly.")


if __name__ == "__main__":
    main()
