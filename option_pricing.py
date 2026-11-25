"""Live Option Pricing Module for Kotak Neo.

Fetches real-time LTP and nearest weekly trading symbol for NIFTY/SENSEX options
directly from the Kotak Neo exchange feed.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from broker_kotak import get_kotak_client

logger = logging.getLogger("option_pricing")

_SCRIP_CACHE: Dict[str, Dict[str, Any]] = {}
_QUOTE_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_QUOTE_TTL_SEC: float = 2.5


def clear_scrip_cache() -> None:
    """Clear daily scrip token cache (e.g., on midnight rollover)."""
    _SCRIP_CACHE.clear()
    _QUOTE_CACHE.clear()


def _get_or_search_scrip(
    client: Any,
    symbol_name: str,
    strike: int,
    opt_type: str,
    expiry_date: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    opt_type = opt_type.upper()
    cache_key = f"{symbol_name}_{strike}_{opt_type}_{expiry_date or 'nearest'}"
    scrip_info = _SCRIP_CACHE.get(cache_key)
    if scrip_info:
        return scrip_info

    try:
        exch_seg = "bse_fo" if symbol_name.upper() == "SENSEX" else "nse_fo"
        res = client.search_scrip(
            exchange_segment=exch_seg,
            symbol=symbol_name,
            strike_price=str(strike),
            option_type=opt_type,
        )
        if isinstance(res, list):
            now = datetime.now()
            valid = []
            for item in res:
                if str(item.get("pSymbolName", "")).upper() != symbol_name.upper():
                    continue
                if not str(item.get("pTrdSymbol", "")).startswith(symbol_name.upper()):
                    continue
                exp_str = item.get("pExpiryDate")
                try:
                    exp_dt = datetime.strptime(exp_str, "%d%b%Y")
                    if exp_dt.date() >= now.date():
                        if expiry_date and exp_dt.strftime("%Y-%m-%d") == expiry_date:
                            valid.insert(0, (exp_dt, item))
                        else:
                            valid.append((exp_dt, item))
                except Exception:
                    pass
            if not expiry_date:
                valid.sort(key=lambda x: x[0])
            if valid:
                nearest_exp, nearest_item = valid[0]
                scrip_info = {
                    "token": str(nearest_item.get("pSymbol")),
                    "symbol": str(nearest_item.get("pTrdSymbol")),
                    "expiry": nearest_exp.strftime("%Y-%m-%d"),
                    "exchange_segment": exch_seg,
                }
                _SCRIP_CACHE[cache_key] = scrip_info
                return scrip_info
    except Exception as exc:
        logger.warning(f"Error searching scrip for {cache_key}: {exc}")
    return None


def get_live_option_ltp(
    symbol_name: str = "NIFTY",
    strike: int = 22500,
    opt_type: str = "CE",
    expiry_date: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch real-time market quote for nearest or specified weekly option contract."""
    opt_type = opt_type.upper()
    cache_key = f"{symbol_name}_{strike}_{opt_type}_{expiry_date or 'nearest'}"

    now_ts = time.time()
    cached = _QUOTE_CACHE.get(cache_key)
    if cached and (now_ts - cached[0] < _QUOTE_TTL_SEC):
        return cached[1]

    client = get_kotak_client()
    if not client:
        return None

    scrip_info = _get_or_search_scrip(client, symbol_name, strike, opt_type, expiry_date)
    if not scrip_info:
        return None

    seg = scrip_info.get("exchange_segment", "nse_fo")
    try:
        q = client.quotes(
            [{"instrument_token": scrip_info["token"], "exchange_segment": seg}],
            quote_type="ltp",
        )
        if q and isinstance(q, list) and len(q) > 0:
            ltp = float(q[0].get("ltp") or 0.0)
            if ltp > 0:
                result = {
                    "symbol": scrip_info["symbol"],
                    "token": scrip_info["token"],
                    "expiry": scrip_info["expiry"],
                    "exchange_segment": seg,
                    "ltp": ltp,
                    "bid": ltp,
                    "ask": ltp,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                _QUOTE_CACHE[cache_key] = (now_ts, result)
                return result
    except Exception as exc:
        logger.warning(f"Quote error for {scrip_info['symbol']}: {exc}")

    if cached and (now_ts - cached[0] < 30.0):
        return cached[1]
    return None
