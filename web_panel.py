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
import hmac
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from broker_kotak import (
    available_kotak_funds,
    cancel_kotak_order,
    get_kotak_client,
    kotak_login,
    kotak_token_status,
    modify_kotak_sl_order,
    modify_kotak_target_order,
    place_kotak_order,
    place_kotak_sl_order,
    place_kotak_target_order,
    reset_kotak_session,
)
from option_pricing import _get_or_search_scrip, get_live_option_ltp
import requests
from risk_guardian import (
    fetch_kotak_mtm_and_positions,
    get_guardian_config,
    is_risk_locked,
    square_off_all_kotak_positions,
)
from telegram_notifier import (
    is_configured,
    notify_system,
    notify_trade_entry,
    notify_trade_exit,
    send_telegram_message,
)

logger = logging.getLogger("web_panel")

ROOT = Path(__file__).resolve().parent
PANEL_HTML = ROOT / "panel.html"
ENV_PATH = ROOT / ".env"
PORTFOLIO_PATH = ROOT / "portfolio_config.json"
STOP_FILE = ROOT / "STOP_TRADING"
IST = timezone(timedelta(hours=5, minutes=30))

_ENV_SAFE_KEYS = (
    "TRADING_MODE",
    "TRADING_LOTS",
    "MAX_DAILY_LOSS",
    "ACCOUNT_CAPITAL",
    "STRIKE_MODE",
    "SENSEX_STRIKE_MODE",
    "NIFTY_STRIKE_MODE",
    "SIZING_MODE",
    "CAPITAL_PER_LOT",
    "MAX_LOTS_CAP",
)
_ENGINES_REF: dict = {}
_TELEMETRY_REF: dict = {}
_DUAL_REF: dict = {}
_BROKER_CACHE: dict = {"ts": 0.0, "funds": {"ok": False}, "mtm": 0.0, "positions": []}
_SCALP_MONITORS: dict[str, dict] = {}


def _record_scalp_order_csv(
    action: str,
    symbol: str,
    qty: int,
    order_id: str,
    fill_price: float,
    reason: str,
    status: str = "FILLED",
) -> None:
    today_s = datetime.now(IST).strftime("%Y-%m-%d")
    live_dir = ROOT / "data" / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    path = live_dir / f"live_orders_{today_s}.csv"
    is_new = not path.exists()
    try:
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow([
                    "timestamp", "action", "symbol", "qty", "order_id",
                    "signal_price", "fill_price", "slippage", "latency_ms",
                    "status", "reason", "mode", "pnl_rs",
                ])
            w.writerow([
                datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
                action,
                symbol,
                qty,
                order_id,
                round(fill_price, 2),
                round(fill_price, 2),
                0.0,
                0.0,
                status,
                reason,
                "MANUAL_SCALP",
                0.0,
            ])
    except Exception as exc:
        logger.warning(f"Failed to record scalp order CSV: {exc}")


def _notify_scalp_exit(
    sym: str,
    qty: int,
    is_long: bool,
    fill_px: float,
    entry_px: float,
    exit_reason: str,
    order_id: str,
) -> None:
    """Send a manual scalp exit notification using the main engine's format."""
    if not is_configured():
        return
    u = sym.upper()
    idx = "SENSEX" if u.startswith("SENSEX") else ("NIFTY" if u.startswith("NIFTY") else sym)
    lot_sz = 20 if idx == "SENSEX" else (65 if idx == "NIFTY" else 1)
    lots = max(1, abs(qty) // lot_sz)
    side = "CALL" if u.endswith("CE") else ("PUT" if u.endswith("PE") else ("LONG" if is_long else "SHORT"))
    opt_pnl_pts = ((fill_px - entry_px) if is_long else (entry_px - fill_px)) if (fill_px > 0 and entry_px > 0) else None
    pnl_rupees = (opt_pnl_pts * abs(qty)) if opt_pnl_pts is not None else 0.0
    notify_trade_exit(
        symbol=idx,
        side=side,
        option_str=sym,
        pnl_pts=None,
        option_pnl_pts=opt_pnl_pts,
        pnl_rupees=pnl_rupees,
        exit_reason=exit_reason,
        exit_price=None,
        exit_time=datetime.now(IST).strftime("%H:%M:%S"),
        total_lots=lots,
        qty=abs(qty),
        order_id=order_id or None,
        mode="LIVE",
        option_exit_price=fill_px if fill_px > 0 else None,
        strategy="Manual Scalp",
    )


def _scalp_sl_tp_watcher_loop(stop_event: Optional[threading.Event] = None) -> None:
    """Monitor manual scalp open positions for auto SL/TP exits."""
    while not (stop_event and stop_event.is_set()):
        try:
            if _SCALP_MONITORS:
                _, positions = fetch_kotak_mtm_and_positions()
                pos_map = {p.get("symbol"): p for p in positions if int(p.get("net_qty", 0) or 0) != 0}
                for sym, mon in list(_SCALP_MONITORS.items()):
                    pos = pos_map.get(sym)
                    if not pos:
                        _SCALP_MONITORS.pop(sym, None)
                        continue
                    ltp = float(pos.get("ltp") or 0.0)
                    entry_px = float(mon.get("entry_px") or pos.get("avg_price") or 0.0)
                    sl_pts = float(mon.get("sl_pts") or 0.0)
                    tp_pts = float(mon.get("tp_pts") or 0.0)
                    if ltp <= 0 or entry_px <= 0:
                        continue
                    net_qty = int(pos.get("net_qty") or 0)
                    is_long = net_qty > 0
                    peak_px = float(mon.get("peak_px") or entry_px)
                    last_mod_peak = float(mon.get("last_mod_peak") or peak_px)
                    if is_long and ltp > peak_px:
                        peak_px = ltp
                        mon["peak_px"] = peak_px
                        if sl_pts > 0 and mon.get("sl_order_id") and (peak_px - last_mod_peak >= 1.0):
                            if modify_kotak_sl_order(mon["sl_order_id"], abs(net_qty), peak_px - sl_pts):
                                mon["last_mod_peak"] = peak_px
                    elif not is_long and ltp < peak_px:
                        peak_px = ltp
                        mon["peak_px"] = peak_px
                    trail_diff = (ltp - peak_px) if is_long else (peak_px - ltp)
                    entry_diff = (ltp - entry_px) if is_long else (entry_px - ltp)
                    exit_reason = ""
                    if sl_pts > 0 and trail_diff <= -sl_pts:
                        exit_reason = f"SCALP_TL_HIT (-{sl_pts:.1f}pts from peak {peak_px:.2f})"
                    elif tp_pts > 0 and entry_diff >= tp_pts:
                        exit_reason = f"SCALP_TP_HIT (+{tp_pts:.1f}pts)"
                    if exit_reason:
                        side = -1 if is_long else 1
                        try:
                            if mon.get("sl_order_id"):
                                cancel_kotak_order(mon["sl_order_id"])
                            if mon.get("tp_order_id"):
                                cancel_kotak_order(mon["tp_order_id"])
                            oid, fill_px = place_kotak_order(
                                symbol=sym,
                                qty=abs(net_qty),
                                side=side,
                                product=str(mon.get("product", "NRML")),
                                exchange_segment=mon.get("exchange_segment") or None,
                            )
                            _record_scalp_order_csv("SELL" if side == -1 else "BUY", sym, abs(net_qty), oid, fill_px, exit_reason)
                            _SCALP_MONITORS.pop(sym, None)
                            _BROKER_CACHE["ts"] = 0.0
                            logger.info(f"[SCALP] {exit_reason} -> Exited {sym} @ ₹{fill_px:.2f} (Order {oid})")
                            _notify_scalp_exit(sym, abs(net_qty), is_long, fill_px, entry_px, exit_reason, oid)
                        except Exception as exc:
                            logger.error(f"[SCALP] Auto-exit failed for {sym}: {exc}")
        except Exception:
            pass
        time.sleep(1.5)


_EXPIRY_CACHE: dict[str, tuple[float, list[str]]] = {}


def _get_available_expiries(symbol: str) -> list[str]:
    sym = symbol.strip().upper()
    now_ts = time.time()
    cached = _EXPIRY_CACHE.get(sym)
    if cached and (now_ts - cached[0] < 3600.0):
        return cached[1]

    expiries: set[datetime] = set()
    client = get_kotak_client()
    if client:
        try:
            exch_seg = "bse_fo" if sym == "SENSEX" else "nse_fo"
            res = client.search_scrip(exchange_segment=exch_seg, symbol=sym, option_type="CE")
            if isinstance(res, list):
                today_d = datetime.now(IST).date()
                for item in res:
                    if str(item.get("pSymbolName", "")).upper() != sym:
                        continue
                    exp_str = item.get("pExpiryDate")
                    try:
                        exp_dt = datetime.strptime(str(exp_str), "%d%b%Y")
                        if exp_dt.date() >= today_d:
                            expiries.add(exp_dt)
                    except Exception:
                        pass
        except Exception as exc:
            logger.debug(f"Expiry search failed for {sym}: {exc}")

    if expiries:
        sorted_exp = [dt.strftime("%Y-%m-%d") for dt in sorted(expiries)[:6]]
        _EXPIRY_CACHE[sym] = (now_ts, sorted_exp)
        return sorted_exp

    # Fallback: next 4 weekly expiries (Thursday for SENSEX/NIFTY fallback)
    today_d = datetime.now(IST).date()
    target_wd = 3  # Thursday
    days_ahead = (target_wd - today_d.weekday()) % 7
    base = today_d + timedelta(days=days_ahead)
    return [(base + timedelta(days=7 * i)).strftime("%Y-%m-%d") for i in range(4)]


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


def _build_health_checks(tok: dict, locked: bool) -> list[dict]:
    alive_threads = {t.name for t in threading.enumerate() if t.is_alive()}
    today_s = datetime.now(IST).strftime("%Y-%m-%d")

    # 1. Kotak Credentials & Session
    neo_keys = ("NEO_CONSUMER_KEY", "NEO_MOBILE_NUMBER", "NEO_UCC", "NEO_MPIN", "NEO_TOTP_SECRET")
    missing_neo = [k for k in neo_keys if not os.getenv(k, "").strip()]
    if missing_neo:
        kotak_chk = {"name": "Broker Auth", "status": "err", "detail": f"Missing: {', '.join(missing_neo)}"}
    elif not tok.get("valid"):
        kotak_chk = {"name": "Broker Auth", "status": "warn", "detail": str(tok.get("reason") or "Session expired")}
    else:
        kotak_chk = {"name": "Broker Auth", "status": "ok", "detail": f"Active ({tok.get('ucc', '')})"}

    # 2. Live Market Data Feed
    quotes_file = ROOT / "data" / "live" / f"quotes_{today_s}.json"
    nifty_csv = ROOT / "data" / "live" / f"nifty_1min_{today_s}.csv"
    sensex_csv = ROOT / "data" / "live" / f"sensex_1min_{today_s}.csv"
    feed_alive = "market-data" in alive_threads
    if not feed_alive:
        feed_chk = {"name": "Live Market Feed", "status": "err", "detail": "MarketDataStreamer thread stopped"}
    elif quotes_file.exists():
        try:
            q = json.loads(quotes_file.read_text(encoding="utf-8"))
            n_ltp = (q.get("nifty") or {}).get("ltp")
            s_ltp = (q.get("sensex") or {}).get("ltp")
            v_ltp = (q.get("vix") or {}).get("ltp")
            feed_chk = {"name": "Live Market Feed", "status": "ok", "detail": f"NIFTY {n_ltp or '—'} · SENSEX {s_ltp or '—'} · VIX {v_ltp or '—'}"}
        except Exception:
            feed_chk = {"name": "Live Market Feed", "status": "ok", "detail": "Streaming active"}
    else:
        has_csv = nifty_csv.exists() or sensex_csv.exists()
        feed_chk = {
            "name": "Live Market Feed",
            "status": "ok" if has_csv else "warn",
            "detail": "1m candles active" if has_csv else "Waiting for market hours (09:15 IST)",
        }

    # 3. Risk Guardian
    g_cfg = get_guardian_config()
    rg_alive = "risk-guardian" in alive_threads
    if not g_cfg["enabled"]:
        rg_chk = {"name": "Risk Guardian", "status": "err", "detail": "Disabled in .env"}
    elif not rg_alive:
        rg_chk = {"name": "Risk Guardian", "status": "err", "detail": "Watcher thread stopped"}
    elif locked:
        rg_chk = {"name": "Risk Guardian", "status": "err", "detail": f"LOCKED (Loss limit ₹{g_cfg['daily_loss_limit']:,.0f} hit)"}
    else:
        rg_chk = {"name": "Risk Guardian", "status": "ok", "detail": f"Active · Limit ₹{g_cfg['daily_loss_limit']:,.0f}"}

    # 4. Strategy Loops
    n_up = "strategy-nifty" in alive_threads
    s_up = "strategy-sensex" in alive_threads
    if n_up and s_up:
        strat_chk = {"name": "Strategy Engines", "status": "ok", "detail": "NIFTY & SENSEX listening"}
    else:
        down = [x for x, ok in (("NIFTY", n_up), ("SENSEX", s_up)) if not ok]
        strat_chk = {"name": "Strategy Engines", "status": "err", "detail": f"Stopped: {', '.join(down)}"}

    # 5. Telegram Alerts & Bot
    tg_ok = is_configured()
    tg_bot_up = "telegram-bot" in alive_threads
    if not tg_ok:
        tg_chk = {"name": "Telegram Alerts", "status": "warn", "detail": "Token or Chat ID missing"}
    elif not tg_bot_up:
        tg_chk = {"name": "Telegram Alerts", "status": "warn", "detail": "Alerts OK · Bot poller offline"}
    else:
        tg_chk = {"name": "Telegram Alerts", "status": "ok", "detail": "Alerts & Bot connected"}

    # 6. Execution Config
    mode = os.getenv("TRADING_MODE", "paper").strip().upper()
    strike = os.getenv("STRIKE_MODE", "itm150").strip().upper()
    lots = os.getenv("TRADING_LOTS", "1").strip()
    cap = os.getenv("ACCOUNT_CAPITAL", "25000").strip()
    cfg_chk = {
        "name": "Execution Config",
        "status": "ok" if mode in ("PAPER", "LIVE") else "warn",
        "detail": f"{mode} · {lots} Lot · {strike} · ₹{cap}",
    }

    return [kotak_chk, feed_chk, rg_chk, strat_chk, tg_chk, cfg_chk]


def _get_latest_market_quotes() -> dict:
    today_s = datetime.now(IST).strftime("%Y-%m-%d")
    quotes_file = ROOT / "data" / "live" / f"quotes_{today_s}.json"
    if not quotes_file.exists():
        pattern = list((ROOT / "data" / "live").glob("quotes_*.json"))
        if pattern:
            quotes_file = sorted(pattern)[-1]
        else:
            return {}
    try:
        return json.loads(quotes_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_today_api_orders() -> list[dict]:
    today_s = datetime.now(IST).strftime("%Y-%m-%d")
    orders_csv = ROOT / "data" / "live" / f"live_orders_{today_s}.csv"
    if not orders_csv.exists():
        pattern = list((ROOT / "data" / "live").glob("live_orders_*.csv"))
        if pattern:
            orders_csv = sorted(pattern)[-1]
        else:
            return []
    rows = []
    try:
        with orders_csv.open("r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append(r)
    except Exception:
        pass
    return rows[-30:]


def _read_recent_logs(limit: int = 60) -> list[str]:
    log_file = ROOT / "logs" / "engine.log"
    if not log_file.exists():
        return []
    try:
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-limit:]
    except Exception:
        return []


def _read_today_trades() -> list[dict]:
    trades_csv = ROOT / "data" / "performance" / "trend_runner_trades.csv"
    if not trades_csv.exists():
        return []
    today_s = datetime.now(IST).strftime("%Y-%m-%d")
    rows = []
    try:
        with trades_csv.open("r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append(r)
    except Exception:
        pass
    today_rows = [r for r in rows if str(r.get("date", "")).strip() == today_s]
    return today_rows[-20:] if today_rows else rows[-15:]


_PANEL_SESSIONS: dict[str, float] = {}
_PIN_FAILS: dict[str, tuple[int, float]] = {}
_SESSION_TTL_SEC = 12 * 3600


def _get_active_pin() -> str:
    """Return configured Kotak Neo MPIN (or PANEL_PIN override) used to lock the web panel."""
    return (
        os.getenv("PANEL_PIN", "").strip()
        or os.getenv("WEB_PANEL_PIN", "").strip()
        or os.getenv("NEO_MPIN", "").strip()
        or os.getenv("KOTAK_MPIN", "").strip()
    )


def _extract_session_token(cookie_header: str) -> str:
    for part in (cookie_header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "tr_session" and v:
            return v.strip()
    return ""


class PanelHandler(BaseHTTPRequestHandler):
    def _is_authorized(self) -> bool:
        if not _get_active_pin():
            return True
        tok = _extract_session_token(self.headers.get("Cookie", ""))
        if not tok:
            return False
        exp = _PANEL_SESSIONS.get(tok, 0.0)
        if exp > time.time():
            return True
        _PANEL_SESSIONS.pop(tok, None)
        return False

    def _send_json(self, code: int, payload: dict, extra_headers: Optional[dict] = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed_path = urlparse(self.path).path
        if parsed_path in ("/", "/panel.html", "/scalp"):
            body = PANEL_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed_path == "/api/auth/status":
            pin_configured = bool(_get_active_pin())
            self._send_json(200, {
                "pin_configured": pin_configured,
                "locked": pin_configured and (not self._is_authorized()),
            })
            return

        if not self._is_authorized():
            self._send_json(401, {"ok": False, "locked": True, "error": "Kotak Neo PIN required"})
            return

        if parsed_path == "/api/state":
            import time as _t
            tok = kotak_token_status(auto_login=False)
            now_ts = _t.time()
            if tok.get("valid") and (now_ts - _BROKER_CACHE["ts"] >= 1.0):
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
                "scalp_monitors": _SCALP_MONITORS,
                "running_strategy": running_strategy,
                "risk_locked": locked,
                "paused": STOP_FILE.exists(),
                "env": env_safe,
                "portfolio": json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8")) if PORTFOLIO_PATH.exists() else {},
                "quotes": _get_latest_market_quotes(),
                "trades": _read_today_trades(),
                "api_orders": _read_today_api_orders(),
                "telemetry": _TELEMETRY_REF,
                "dual_gate": {"armed": _DUAL_REF.get("armed", {}), "confirmed": _DUAL_REF.get("confirmed")},
                "logs": _read_recent_logs(60),
                "checks": _build_health_checks(tok, locked),
            })
            return

        if self.path.startswith("/api/scalp/expiries"):
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            sym = str((qs.get("symbol") or ["SENSEX"])[0]).strip().upper()
            self._send_json(200, {"ok": True, "symbol": sym, "expiries": _get_available_expiries(sym)})
            return

        if self.path.startswith("/api/scalp/quote"):
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            sym = str((qs.get("symbol") or ["SENSEX"])[0]).strip().upper()
            expiry = str((qs.get("expiry") or [""])[0]).strip() or None
            try:
                strike = int(float((qs.get("strike") or ["84500"])[0]))
            except Exception:
                strike = 84500 if sym == "SENSEX" else 25950
            ce_q = get_live_option_ltp(sym, strike, "CE", expiry_date=expiry, bypass_cache=True)
            pe_q = get_live_option_ltp(sym, strike, "PE", expiry_date=expiry, bypass_cache=True)
            self._send_json(200, {
                "ok": bool(ce_q or pe_q),
                "symbol": sym,
                "strike": strike,
                "expiry": expiry,
                "ce": ce_q or {"symbol": f"{sym} {strike} CE", "ltp": 0.0},
                "pe": pe_q or {"symbol": f"{sym} {strike} PE", "ltp": 0.0},
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

        if self.path == "/api/auth/unlock":
            active_pin = _get_active_pin()
            if not active_pin:
                self._send_json(200, {"ok": True, "pin_configured": False})
                return
            client_ip = (self.client_address[0] if self.client_address else "unknown")
            fails, locked_until = _PIN_FAILS.get(client_ip, (0, 0.0))
            now_ts = time.time()
            if locked_until > now_ts:
                wait_s = int(locked_until - now_ts) + 1
                self._send_json(429, {"ok": False, "error": f"Too many attempts. Try again in {wait_s}s."})
                return
            pin = str(data.get("pin", "")).strip()
            if pin and hmac.compare_digest(pin, active_pin):
                _PIN_FAILS.pop(client_ip, None)
                tok = secrets.token_urlsafe(32)
                _PANEL_SESSIONS[tok] = now_ts + _SESSION_TTL_SEC
                cookie = f"tr_session={tok}; Path=/; HttpOnly; SameSite=Lax; Max-Age={_SESSION_TTL_SEC}"
                self._send_json(200, {"ok": True, "locked": False}, extra_headers={"Set-Cookie": cookie})
            else:
                fails += 1
                lock_until = (now_ts + 30.0) if fails >= 5 else 0.0
                _PIN_FAILS[client_ip] = (0 if fails >= 5 else fails, lock_until)
                self._send_json(401, {"ok": False, "error": "Incorrect Kotak Neo PIN."})
            return

        if self.path == "/api/auth/lock":
            tok = _extract_session_token(self.headers.get("Cookie", ""))
            if tok:
                _PANEL_SESSIONS.pop(tok, None)
            self._send_json(
                200,
                {"ok": True, "locked": bool(_get_active_pin())},
                extra_headers={"Set-Cookie": "tr_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"},
            )
            return

        if not self._is_authorized():
            self._send_json(401, {"ok": False, "locked": True, "error": "Kotak Neo PIN required"})
            return

        if self.path == "/api/settings":
            try:
                env_updates = data.get("env") or {}
                if env_updates:
                    clean_updates = {k: str(v) for k, v in env_updates.items() if str(v).strip() != ""}
                    _update_env_file(clean_updates)
                    if "STRIKE_MODE" in clean_updates:
                        for eng in _ENGINES_REF.values():
                            eng.strike_mode = clean_updates["STRIKE_MODE"].strip().lower()
                    if "SENSEX_STRIKE_MODE" in clean_updates and "sensex" in _ENGINES_REF:
                        _ENGINES_REF["sensex"].strike_mode = clean_updates["SENSEX_STRIKE_MODE"].strip().lower()
                    if "NIFTY_STRIKE_MODE" in clean_updates and "nifty" in _ENGINES_REF:
                        _ENGINES_REF["nifty"].strike_mode = clean_updates["NIFTY_STRIKE_MODE"].strip().lower()
                    if "SIZING_MODE" in clean_updates:
                        for eng in _ENGINES_REF.values():
                            eng.sizing_mode = clean_updates["SIZING_MODE"].strip().lower()
                    if "ACCOUNT_CAPITAL" in clean_updates:
                        try:
                            cap_val = float(clean_updates["ACCOUNT_CAPITAL"])
                            for eng in _ENGINES_REF.values():
                                eng.account_capital = cap_val
                        except ValueError:
                            pass
                    if "CAPITAL_PER_LOT" in clean_updates:
                        try:
                            cpl_val = float(clean_updates["CAPITAL_PER_LOT"])
                            for eng in _ENGINES_REF.values():
                                eng.capital_per_lot = cpl_val
                        except ValueError:
                            pass
                    if "MAX_LOTS_CAP" in clean_updates:
                        try:
                            mlc_val = int(clean_updates["MAX_LOTS_CAP"])
                            for eng in _ENGINES_REF.values():
                                eng.max_lots_cap = mlc_val
                        except ValueError:
                            pass
                    if "TRADING_LOTS" in clean_updates:
                        try:
                            new_lots = int(clean_updates["TRADING_LOTS"])
                            for eng in _ENGINES_REF.values():
                                eng.lots = new_lots
                        except ValueError:
                            pass

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

                port_updates = data.get("portfolio") or {}
                if port_updates and PORTFOLIO_PATH.exists():
                    try:
                        pcfg = json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8"))
                        pcfg.update(port_updates)
                        PORTFOLIO_PATH.write_text(json.dumps(pcfg, indent=2), encoding="utf-8")
                    except Exception as exc:
                        logger.warning(f"Failed to update portfolio_config.json: {exc}")

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
                for mon in list(_SCALP_MONITORS.values()):
                    if mon.get("sl_order_id"):
                        cancel_kotak_order(mon["sl_order_id"])
                    if mon.get("tp_order_id"):
                        cancel_kotak_order(mon["tp_order_id"])
                square_off_all_kotak_positions(open_pos, "WEB_PANEL_EMERGENCY_SQUARE_OFF")
                _SCALP_MONITORS.clear()
                _BROKER_CACHE["ts"] = 0.0
                self._send_json(200, {"ok": True, "message": f"Square-off sent for {len(open_pos)} open position(s)."})
            elif action == "exit_broker_position":
                sym = str(data.get("symbol", "")).strip()
                net_qty = int(data.get("net_qty", 0) or 0)
                if not sym or net_qty == 0:
                    self._send_json(400, {"ok": False, "message": "Invalid position symbol or quantity."})
                    return
                side = -1 if net_qty > 0 else 1
                try:
                    mon = _SCALP_MONITORS.pop(sym, None) or {}
                    entry_px = float(mon.get("entry_px") or data.get("avg_price") or 0.0)
                    if entry_px <= 0:
                        for p in (_BROKER_CACHE.get("positions") or []):
                            if p.get("symbol") == sym:
                                entry_px = float(p.get("avg_price") or 0.0)
                                break
                    if mon.get("sl_order_id"):
                        cancel_kotak_order(mon["sl_order_id"])
                    if mon.get("tp_order_id"):
                        cancel_kotak_order(mon["tp_order_id"])
                    oid, fill_px = place_kotak_order(
                        symbol=sym,
                        qty=abs(net_qty),
                        side=side,
                        product=str(data.get("product", "NRML")),
                        exchange_segment=data.get("exchange_segment") or None,
                    )
                    _BROKER_CACHE["ts"] = 0.0
                    _record_scalp_order_csv("SELL" if side == -1 else "BUY", sym, abs(net_qty), oid, fill_px, "MANUAL_EXIT")
                    _notify_scalp_exit(sym, abs(net_qty), net_qty > 0, fill_px, entry_px, "MANUAL_EXIT", oid)
                    self._send_json(200, {"ok": True, "message": f"Exited {sym} (Order ID: {oid} @ ₹{fill_px:.2f})."})
                except Exception as exc:
                    self._send_json(500, {"ok": False, "message": f"Exit failed for {sym}: {exc}"})
            else:
                self._send_json(400, {"ok": False, "message": "Unknown action"})
            return

        if self.path == "/api/scalp/order":
            locked, _ = is_risk_locked()
            if locked:
                self._send_json(200, {"ok": False, "message": "Trading halted: Risk Guardian lock active today."})
                return
            tok = kotak_token_status(auto_login=False)
            if not tok.get("valid"):
                self._send_json(200, {"ok": False, "message": "Kotak Neo session disconnected. Reconnect in Settings first."})
                return

            idx = str(data.get("index_symbol", "SENSEX")).strip().upper()
            opt_type = str(data.get("option_type", "CE")).strip().upper()
            expiry = str(data.get("expiry") or "").strip() or None
            try:
                strike = int(float(data.get("strike", 84500 if idx == "SENSEX" else 25950)))
                lots = max(1, int(data.get("lots", 1)))
                sl_pts = max(0.0, float(data.get("sl_pts") or 0.0))
                tp_pts = max(0.0, float(data.get("tp_pts") or 0.0))
            except Exception as exc:
                self._send_json(400, {"ok": False, "message": f"Invalid order parameters: {exc}"})
                return

            product = str(data.get("product", "NRML")).strip().upper()
            lot_sz = 20 if idx == "SENSEX" else 65
            qty = lots * lot_sz
            ex_seg = "bse_fo" if idx == "SENSEX" else "nse_fo"

            client = get_kotak_client()
            if not client:
                self._send_json(200, {"ok": False, "message": "Kotak Neo client unavailable."})
                return
            scrip = _get_or_search_scrip(client, idx, strike, opt_type, expiry_date=expiry)
            if not scrip or not scrip.get("symbol"):
                self._send_json(200, {"ok": False, "message": f"Contract not found for {idx} {strike} {opt_type}."})
                return

            opt_sym = scrip["symbol"]
            try:
                oid, fill_px = place_kotak_order(
                    symbol=opt_sym,
                    qty=qty,
                    side=1,
                    product=product,
                    exchange_segment=ex_seg,
                )
                if fill_px <= 0:
                    q = get_live_option_ltp(idx, strike, opt_type, expiry_date=expiry, bypass_cache=True)
                    fill_px = float((q or {}).get("ltp") or 0.0)
                _record_scalp_order_csv("BUY", opt_sym, qty, oid, fill_px, "MANUAL_SCALP", status="FILLED")
                sl_order_id = ""
                tp_order_id = ""
                if sl_pts > 0 and fill_px > 0:
                    try:
                        sl_order_id = place_kotak_sl_order(opt_sym, qty, max(0.05, fill_px - sl_pts), product=product, exchange_segment=ex_seg)
                    except Exception as sl_exc:
                        logger.warning(f"[SCALP] Native Kotak SL placement skipped: {sl_exc}")
                elif tp_pts > 0 and fill_px > 0:
                    try:
                        tp_order_id = place_kotak_target_order(opt_sym, qty, fill_px + tp_pts, product=product, exchange_segment=ex_seg)
                    except Exception as tp_exc:
                        logger.warning(f"[SCALP] Native Kotak Target placement skipped: {tp_exc}")

                if sl_pts > 0 or tp_pts > 0:
                    _SCALP_MONITORS[opt_sym] = {
                        "symbol": opt_sym,
                        "entry_px": fill_px,
                        "peak_px": fill_px,
                        "last_mod_peak": fill_px,
                        "qty": qty,
                        "product": product,
                        "exchange_segment": ex_seg,
                        "sl_pts": sl_pts,
                        "tp_pts": tp_pts,
                        "sl_order_id": sl_order_id,
                        "tp_order_id": tp_order_id,
                    }
                _BROKER_CACHE["ts"] = 0.0
                logger.info(f"[SCALP] FILLED BUY {qty}x {opt_sym} @ ₹{fill_px:.2f} (Order {oid} | TL {sl_pts} | TP {tp_pts})")
                if is_configured():
                    quotes = _get_latest_market_quotes()
                    notify_trade_entry(
                        symbol=idx,
                        stage="MANUAL_SCALP",
                        side="CALL" if opt_type == "CE" else "PUT",
                        option_str=f"{strike}{opt_type}",
                        spot_entry=None,
                        sl_pts=sl_pts,
                        initial_sl=None,
                        lots=lots,
                        qty=qty,
                        target_pts=tp_pts if tp_pts > 0 else None,
                        option_symbol=opt_sym,
                        option_entry_price=fill_px if fill_px > 0 else None,
                        vix_chg=quotes.get("vix_chg"),
                        order_id=oid,
                        mode="LIVE",
                        option_sl_px=max(0.05, fill_px - sl_pts) if (sl_pts > 0 and fill_px > 0) else None,
                    )
                self._send_json(200, {
                    "ok": True,
                    "order_id": oid,
                    "symbol": opt_sym,
                    "qty": qty,
                    "fill_price": fill_px,
                    "message": f"FILLED: BUY {qty}x {opt_sym} @ ₹{fill_px:.2f} (#{oid})",
                })
            except Exception as exc:
                _record_scalp_order_csv("BUY", opt_sym, qty, "", 0.0, str(exc), status="REJECTED")
                logger.error(f"[SCALP] REJECTED BUY {qty}x {opt_sym}: {exc}")
                if is_configured():
                    notify_system("Manual Scalp Order Error", f"Contract: {opt_sym}\nQty: {qty}\nError: {exc}", level="ERROR")
                self._send_json(200, {"ok": False, "message": f"REJECTED: {opt_sym} — {exc}"})
            return

        if self.path == "/api/scalp/update_tp_sl":
            sym = str(data.get("symbol", "")).strip()
            if not sym:
                self._send_json(400, {"ok": False, "message": "Symbol required"})
                return
            try:
                sl_pts = max(0.0, float(data.get("sl_pts") or 0.0))
                tp_pts = max(0.0, float(data.get("tp_pts") or 0.0))
                entry_px = float(data.get("entry_px") or 0.0)
            except Exception:
                self._send_json(400, {"ok": False, "message": "Invalid SL/TP values"})
                return
            existing = _SCALP_MONITORS.get(sym, {})
            qty = int(data.get("qty") or existing.get("qty") or 0)
            product = str(data.get("product") or existing.get("product") or "NRML")
            ex_seg = str(data.get("exchange_segment") or existing.get("exchange_segment") or "") or None
            base_px = entry_px or float(existing.get("entry_px") or 0.0)
            sl_oid = str(existing.get("sl_order_id") or "")
            tp_oid = str(existing.get("tp_order_id") or "")

            if sl_pts == 0 and sl_oid:
                cancel_kotak_order(sl_oid)
                sl_oid = ""
            if tp_pts == 0 and tp_oid:
                cancel_kotak_order(tp_oid)
                tp_oid = ""

            if sl_pts > 0 and base_px > 0 and qty > 0:
                if tp_oid:
                    cancel_kotak_order(tp_oid)
                    tp_oid = ""
                new_trig = max(0.05, base_px - sl_pts)
                if sl_oid:
                    modify_kotak_sl_order(sl_oid, qty, new_trig)
                else:
                    try:
                        sl_oid = place_kotak_sl_order(sym, qty, new_trig, product=product, exchange_segment=ex_seg)
                    except Exception as exc:
                        logger.warning(f"[SCALP] Native SL place failed: {exc}")
            elif tp_pts > 0 and base_px > 0 and qty > 0:
                new_tp = base_px + tp_pts
                if tp_oid:
                    modify_kotak_target_order(tp_oid, qty, new_tp)
                else:
                    try:
                        tp_oid = place_kotak_target_order(sym, qty, new_tp, product=product, exchange_segment=ex_seg)
                    except Exception as exc:
                        logger.warning(f"[SCALP] Native Target place failed: {exc}")

            if sl_pts == 0 and tp_pts == 0:
                _SCALP_MONITORS.pop(sym, None)
            else:
                _SCALP_MONITORS[sym] = {
                    "symbol": sym,
                    "entry_px": base_px,
                    "peak_px": float(existing.get("peak_px") or base_px),
                    "last_mod_peak": float(existing.get("last_mod_peak") or base_px),
                    "qty": qty,
                    "product": product,
                    "exchange_segment": ex_seg or "",
                    "sl_pts": sl_pts,
                    "tp_pts": tp_pts,
                    "sl_order_id": sl_oid,
                    "tp_order_id": tp_oid,
                }
            broker_note = f" (Kotak SL #{sl_oid})" if sl_oid else (f" (Kotak TP #{tp_oid})" if tp_oid else "")
            self._send_json(200, {"ok": True, "message": f"Updated {sym}: TL {sl_pts} pts · TP {tp_pts} pts{broker_note}"})
            return

        self._send_json(404, {"error": "Not found"})

    def log_message(self, fmt: str, *args) -> None:
        pass


def start_web_panel(
    stop_event: Optional[threading.Event] = None,
    engines: Optional[dict] = None,
    telemetry: Optional[dict] = None,
    dual_state: Optional[dict] = None,
) -> Optional[threading.Thread]:
    global _ENGINES_REF, _TELEMETRY_REF, _DUAL_REF
    if engines is not None:
        _ENGINES_REF = engines
    if telemetry is not None:
        _TELEMETRY_REF = telemetry
    if dual_state is not None:
        _DUAL_REF = dual_state
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
    w = threading.Thread(target=_scalp_sl_tp_watcher_loop, args=(stop_event,), daemon=True, name="scalp-watcher")
    w.start()
    return t
