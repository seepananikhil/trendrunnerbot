"""
Single Source of Truth — Apex Duo Static Strategy Rules.

All entry, exit, stop-loss, and Dual-Cross confluence rules live here.
Both Backtest (`scripts/run_dual_confluence_master_backtest.py`) and
Live/Paper (`trend_runner_paper.py`, `app_server.py`) call these exact
functions so strategy behavior cannot drift across engines.
"""

from __future__ import annotations

from typing import Optional

# --- Static Engine Constants (Non-Tamperable) ---
BODY_STANDARD_TH: float = 0.70
BODY_CONVICTION_TH: float = 0.80
WICK_PCT_MAX: float = 0.30

NIFTY_WICK_TOL: float = 4.0
SENSEX_WICK_TOL: float = 15.0

NIFTY_INITIAL_SL_PTS: float = 55.0
SENSEX_INITIAL_SL_PTS: float = 220.0

NIFTY_EMA_BUFFER_PTS: float = 10.0
SENSEX_EMA_BUFFER_PTS: float = 30.0

EMA_TIMEFRAME: str = "1m"
EMA_PERIOD: int = 200

NIFTY_EMA_ARM_PTS: float = 50.0
SENSEX_EMA_ARM_PTS: float = 150.0

CALL_CUTOFF_TIME: str = "10:45"
REENTRY_CUTOFF_TIME: str = "13:30"
VIX_CALL_TH: float = 1.8
VIX_PUT_TH: float = 0.0
DUAL_ACTIVATION_WINDOW_SECS: int = 3600


def get_initial_sl_pts(symbol: str) -> float:
    return SENSEX_INITIAL_SL_PTS if symbol.upper() == "SENSEX" else NIFTY_INITIAL_SL_PTS


def get_ema_buffer_pts(symbol: str) -> float:
    return SENSEX_EMA_BUFFER_PTS if symbol.upper() == "SENSEX" else NIFTY_EMA_BUFFER_PTS


def get_ema_arm_pts(symbol: str) -> float:
    return SENSEX_EMA_ARM_PTS if symbol.upper() == "SENSEX" else NIFTY_EMA_ARM_PTS


def get_wick_tol_pts(symbol: str) -> float:
    return SENSEX_WICK_TOL if symbol.upper() == "SENSEX" else NIFTY_WICK_TOL


def evaluate_bo_candle(
    symbol: str,
    o: float,
    h: float,
    l: float,
    c: float,
    vix_chg: float,
    t_str: str,
    level_high: float,
    level_low: float,
    vwap: Optional[float] = None,
    trade_num: int = 1,
) -> Optional[str]:
    """
    Evaluate whether a candle is a valid Breakout (BO) entry candle.
    Returns 'CALL', 'PUT', or None.
    """
    rng = h - l
    body_ratio = abs(c - o) / rng if rng > 0 else 0.0
    upper_wick_pts = h - max(o, c)
    lower_wick_pts = min(o, c) - l
    upper_wick_pct = upper_wick_pts / rng if rng > 0 else 0.0
    lower_wick_pct = lower_wick_pts / rng if rng > 0 else 0.0

    wick_tol = get_wick_tol_pts(symbol)
    long_wick_ok = (upper_wick_pct <= WICK_PCT_MAX) or (upper_wick_pts <= wick_tol)
    short_wick_ok = (lower_wick_pct <= WICK_PCT_MAX) or (lower_wick_pts <= wick_tol)

    vwap_long_ok = True if vwap is None else (c > vwap)
    vwap_short_ok = True if vwap is None else (c < vwap)
    call_time_ok = (t_str <= CALL_CUTOFF_TIME) if trade_num == 1 else True

    valid_call_vix = (body_ratio >= BODY_CONVICTION_TH and vix_chg <= 0.0) or (
        body_ratio >= BODY_STANDARD_TH and vix_chg <= -VIX_CALL_TH
    )
    valid_put_vix = body_ratio >= BODY_STANDARD_TH and vix_chg >= VIX_PUT_TH

    if (c > level_high) and (c > o) and vwap_long_ok and long_wick_ok and valid_call_vix and call_time_ok:
        return "CALL"
    if (c < level_low) and (c < o) and vwap_short_ok and short_wick_ok and valid_put_vix:
        return "PUT"
    return None


def check_initial_sl(side: str, h: float, l: float, initial_sl: float) -> bool:
    """Return True if 1m High/Low breached the protective initial stop loss."""
    is_long = side.upper() in ("LONG", "CALL", "CE")
    return (l <= initial_sl) if is_long else (h >= initial_sl)


def is_ema20_armed(symbol: str, max_fav: float) -> bool:
    """Return True once trade favorable excursion reaches the arming threshold."""
    return max_fav >= get_ema_arm_pts(symbol)


def check_ema20_cross(symbol: str, side: str, close_px: float, ema_px: float) -> bool:
    """Return True if 1m close crossed beyond 1m 200-EMA +/- static noise buffer (10p NIFTY / 30p SENSEX)."""
    buf = get_ema_buffer_pts(symbol)
    is_long = side.upper() in ("LONG", "CALL", "CE")
    return (close_px < (ema_px - buf)) if is_long else (close_px > (ema_px + buf))


def get_vix_trap_sl_dist(
    symbol: str,
    side: str,
    entry_px: float,
    rolling_low: float,
    rolling_high: float,
) -> float:
    """Return swing-anchored VIX Trap stop distance bounded by static floor/cap."""
    is_sx = symbol.upper() == "SENSEX"
    is_long = side.upper() in ("LONG", "CALL", "CE")
    if is_sx:
        swing_ref = (rolling_low if rolling_low < 900000 else entry_px - 60.0) if is_long else (rolling_high if rolling_high > 0 else entry_px + 60.0)
        raw = (entry_px - swing_ref + 10.0) if is_long else (swing_ref - entry_px + 10.0)
        return max(60.0, min(SENSEX_INITIAL_SL_PTS, raw))
    swing_ref = (rolling_low if rolling_low < 900000 else entry_px - 25.0) if is_long else (rolling_high if rolling_high > 0 else entry_px + 25.0)
    raw = (entry_px - swing_ref + 2.0) if is_long else (swing_ref - entry_px + 2.0)
    return max(25.0, min(80.0, raw))


def is_high_conviction_sizing(symbol: str, vix_chg: float, or_range: float, spot_px: float = 0.0) -> bool:
    """
    A+ Dynamic 2-Lot Conviction Gate:
    |VIX| >= 4.0% AND clean Opening Range (< 0.55% of spot; fallback < 140 pts NIFTY / < 450 pts SENSEX).
    """
    if spot_px > 0:
        clean_or = 0.0 < (or_range / spot_px) < 0.0055
    else:
        or_cap = 450.0 if symbol.upper() == "SENSEX" else 140.0
        clean_or = 0.0 < or_range < or_cap
    return abs(vix_chg) >= 4.0 and clean_or


def compute_auto_scaled_lots(
    symbol: str,
    sizing_mode: str,
    base_lots: int = 1,
    account_capital: float = 25000.0,
    capital_per_lot: float = 25000.0,
    vix_chg: float = 0.0,
    or_range: float = 0.0,
    max_lots_cap: int = 100,
    spot_px: float = 0.0,
) -> tuple[int, bool, str]:
    """
    Auto Step-Up Sizing Engine:
    - Fixed mode: always uses base_lots (Default Lots).
    - Auto Step-Up mode: stays at base_lots while capital < ₹50,000;
      once capital >= ₹50,000, steps up to floor(capital / capital_per_lot)
      (e.g. ₹50k -> 2 lots, ₹75k -> 3 lots, ₹1L -> 4 lots).
    - High-Conviction (|VIX| >= 4.0% & OR < 0.55% of spot): adds +1 lot when capital >= ₹50,000.
    """
    mode = str(sizing_mode or "auto_scale").strip().lower()
    default_lots = max(1, int(base_lots or 1))
    if mode in ("fixed", "off", "disabled"):
        return default_lots, False, f"Fixed {default_lots} lot(s)"

    cap = max(10000.0, float(account_capital or 25000.0))
    buffer = max(10000.0, float(capital_per_lot or 25000.0))
    step_threshold = max(50000.0, buffer * 2.0)

    if cap < step_threshold:
        stepped_lots = default_lots
    else:
        stepped_lots = max(default_lots, int(cap // buffer))

    is_momentum = is_high_conviction_sizing(symbol, vix_chg, or_range, spot_px)
    if is_momentum:
        if cap >= step_threshold:
            final_lots = min(max_lots_cap, stepped_lots + 1)
            reason = f"🔥 High-Conviction Setup: Stepped Up to {final_lots} Lots (₹{cap:,.0f})"
        else:
            final_lots = min(max_lots_cap, stepped_lots)
            reason = f"🔥 High-Conviction Setup: {final_lots} Lot (Default < ₹{step_threshold:,.0f})"
    else:
        final_lots = min(max_lots_cap, stepped_lots)
        if cap >= step_threshold:
            reason = f"⚡ Auto Step-Up: {final_lots} Lots (Capital ₹{cap:,.0f})"
        else:
            reason = f"⚡ Default: {final_lots} Lot(s) (Step-Up at ₹{step_threshold:,.0f})"

    return final_lots, is_momentum, reason
