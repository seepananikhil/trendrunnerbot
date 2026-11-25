"""Single-User Kotak Neo Trade API Integration.

Handles:
- Local credential loading from .env (with optional neo_config.json fallback)
- Automated 2-step TOTP + MPIN authentication (pyotp + NeoAPI)
- Local session token persistence (data/kotak/kotak_token.json)
- Live market/SL order placement, modification, cancellation, funds & positions
- Telegram alerts on authentication or session failures
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

# Prevent neo_api_client file lock issues on midnight rotation
os.environ["NEO_LOG_FILE_ENABLED"] = "false"

try:
    import pyotp
except ImportError:
    pyotp = None

try:
    from neo_api_client import NeoAPI
except ImportError:
    NeoAPI = None

from telegram_notifier import notify_system

logger = logging.getLogger("broker_kotak")

ROOT = Path(__file__).resolve().parent
KOTAK_DIR = ROOT / "data" / "kotak"
KOTAK_DIR.mkdir(parents=True, exist_ok=True)
KOTAK_TOKEN_FILE = KOTAK_DIR / "kotak_token.json"
IST = timezone(timedelta(hours=5, minutes=30))

_KOTAK_CLIENT: Optional[NeoAPI] = None
_LAST_KOTAK_LOGIN_ATTEMPT: float = 0.0


def reset_kotak_session() -> None:
    """Clear cached in-memory client and saved token file (e.g. after credential update)."""
    global _KOTAK_CLIENT, _LAST_KOTAK_LOGIN_ATTEMPT
    _KOTAK_CLIENT = None
    _LAST_KOTAK_LOGIN_ATTEMPT = 0.0
    if KOTAK_TOKEN_FILE.exists():
        try:
            KOTAK_TOKEN_FILE.unlink()
        except Exception:
            pass


def load_kotak_config() -> dict:
    """Load single-user Kotak Neo credentials from environment variables or neo_config.json."""
    cfg = {
        "consumer_key": os.getenv("NEO_CONSUMER_KEY", "").strip(),
        "mobile_number": os.getenv("NEO_MOBILE_NUMBER", "").strip(),
        "ucc": os.getenv("NEO_UCC", "").strip(),
        "mpin": os.getenv("NEO_MPIN", "").strip(),
        "totp_secret": (os.getenv("NEO_TOTP_SECRET", "") or os.getenv("NEO_TOTP_KEY", "")).strip(),
        "environment": os.getenv("NEO_ENVIRONMENT", "prod").strip() or "prod",
    }
    neo_config = ROOT / "neo_config.json"
    if neo_config.exists():
        try:
            file_cfg = json.loads(neo_config.read_text(encoding="utf-8"))
            for key in ("consumer_key", "mobile_number", "ucc", "mpin", "environment"):
                if not cfg[key] and file_cfg.get(key):
                    cfg[key] = str(file_cfg[key]).strip()
            if not cfg["totp_secret"]:
                cfg["totp_secret"] = str(file_cfg.get("totp_secret") or file_cfg.get("totp_key") or "").strip()
        except Exception:
            pass
    return cfg


def kotak_login(totp_code: Optional[str] = None, mpin: Optional[str] = None) -> dict:
    """Authenticate with Kotak Neo using 2-step TOTP + MPIN flow."""
    global _KOTAK_CLIENT
    if NeoAPI is None:
        return {"ok": False, "error": "kotakneoapi is not installed"}

    k_cfg = load_kotak_config()
    consumer_key = k_cfg["consumer_key"]
    mobile = k_cfg["mobile_number"]
    ucc = k_cfg["ucc"]
    totp_secret = k_cfg["totp_secret"]
    active_mpin = mpin or k_cfg["mpin"]

    if not consumer_key or not mobile or not ucc:
        return {
            "ok": False,
            "error": "Missing Kotak Neo credentials: NEO_CONSUMER_KEY, NEO_MOBILE_NUMBER, and NEO_UCC are required.",
        }
    if not active_mpin:
        return {"ok": False, "error": "NEO_MPIN is required"}

    active_totp = totp_code
    if not active_totp and totp_secret:
        if pyotp is None:
            return {"ok": False, "error": "pyotp library is not installed"}
        try:
            clean_secret = totp_secret.replace(" ", "").strip().upper()
            active_totp = pyotp.TOTP(clean_secret).now()
        except Exception as exc:
            err = f"Failed to generate TOTP from secret: {exc}"
            notify_system("Kotak Neo Auth Error", err, level="ERROR")
            return {"ok": False, "error": err}

    if not active_totp:
        return {"ok": False, "error": "TOTP code or NEO_TOTP_SECRET is required"}

    client = NeoAPI(consumer_key=consumer_key, environment=k_cfg["environment"])

    try:
        step1 = client.totp_login(mobile_number=mobile, ucc=ucc, totp=active_totp)
    except Exception as exc:
        err = f"Kotak TOTP login failed: {exc}"
        notify_system("Kotak Neo Login Failed", err, level="ERROR")
        return {"ok": False, "error": err}

    if not isinstance(step1, dict) or "data" not in step1:
        err = str(step1.get("error", "Invalid TOTP login response") if isinstance(step1, dict) else step1)
        notify_system("Kotak Neo Login Failed", err, level="ERROR")
        return {"ok": False, "error": err}

    view_data = step1["data"]
    view_token = view_data.get("token")
    sid = view_data.get("sid")

    try:
        step2 = client.totp_validate(mpin=active_mpin)
    except Exception as exc:
        err = f"Kotak MPIN validation failed: {exc}"
        notify_system("Kotak Neo MPIN Failed", err, level="ERROR")
        return {"ok": False, "error": err}

    if not isinstance(step2, dict) or "data" not in step2:
        err = str(step2.get("error", "Invalid MPIN response") if isinstance(step2, dict) else step2)
        notify_system("Kotak Neo MPIN Failed", err, level="ERROR")
        return {"ok": False, "error": err}

    trade_data = step2["data"]
    session_payload = {
        "ucc": ucc,
        "greeting_name": view_data.get("greetingName", ""),
        "view_token": view_token,
        "trade_token": trade_data.get("token"),
        "view_sid": sid,
        "edit_sid": trade_data.get("sid"),
        "edit_rid": trade_data.get("rid"),
        "sid": sid,
        "rid": trade_data.get("rid"),
        "base_url": trade_data.get("baseUrl"),
        "data_center": trade_data.get("dataCenter"),
        "logged_in_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        "date": datetime.now(IST).strftime("%Y-%m-%d"),
    }

    KOTAK_TOKEN_FILE.write_text(json.dumps(session_payload, indent=2), encoding="utf-8")
    _KOTAK_CLIENT = client
    logger.info(f"Kotak Neo authenticated for UCC {ucc} ({session_payload['greeting_name']})")
    return {"ok": True, "greeting_name": session_payload["greeting_name"], "ucc": ucc}


def kotak_token_status(auto_login: bool = True) -> dict:
    """Check today's session token validity, auto-logging in via TOTP if expired/missing."""
    global _LAST_KOTAK_LOGIN_ATTEMPT
    today = datetime.now(IST).strftime("%Y-%m-%d")
    if KOTAK_TOKEN_FILE.exists():
        try:
            data = json.loads(KOTAK_TOKEN_FILE.read_text(encoding="utf-8"))
            if data.get("date") == today and data.get("trade_token"):
                return {
                    "valid": True,
                    "ucc": data.get("ucc"),
                    "greeting_name": data.get("greeting_name"),
                    "logged_in_at": data.get("logged_in_at"),
                }
        except Exception:
            pass

    if auto_login:
        k_cfg = load_kotak_config()
        if k_cfg["totp_secret"] and k_cfg["mpin"]:
            now = time.time()
            if now - _LAST_KOTAK_LOGIN_ATTEMPT >= 120:
                _LAST_KOTAK_LOGIN_ATTEMPT = now
                res = kotak_login()
                if res.get("ok"):
                    return {
                        "valid": True,
                        "ucc": res.get("ucc"),
                        "greeting_name": res.get("greeting_name"),
                        "logged_in_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
                    }
                return {"valid": False, "reason": res.get("error", "Kotak auto-login failed")}

    return {"valid": False, "reason": "Token expired or missing"}


def get_kotak_client(token_data: Optional[dict] = None) -> Optional[NeoAPI]:
    """Return an authenticated NeoAPI client, restoring from token file or auto-logging in."""
    global _KOTAK_CLIENT
    if NeoAPI is None:
        return None
    k_cfg = load_kotak_config()
    consumer_key = k_cfg["consumer_key"]
    if not consumer_key:
        return None

    status = kotak_token_status(auto_login=True)
    if _KOTAK_CLIENT is not None and status.get("valid"):
        return _KOTAK_CLIENT

    if token_data is None and KOTAK_TOKEN_FILE.exists():
        try:
            token_data = json.loads(KOTAK_TOKEN_FILE.read_text(encoding="utf-8"))
        except Exception:
            token_data = None

    client = NeoAPI(consumer_key=consumer_key, environment=k_cfg["environment"])
    if token_data and "trade_token" in token_data:
        client.configuration.edit_token = token_data.get("trade_token")
        client.configuration.edit_sid = token_data.get("edit_sid") or token_data.get("sid")
        client.configuration.edit_rid = token_data.get("edit_rid") or token_data.get("rid")
        client.configuration.view_token = token_data.get("view_token")
        client.configuration.view_sid = token_data.get("view_sid") or token_data.get("sid")
        if hasattr(client.configuration, "base_url") and token_data.get("base_url"):
            client.configuration.base_url = token_data.get("base_url")
        if hasattr(client.configuration, "data_center") and token_data.get("data_center"):
            client.configuration.data_center = token_data.get("data_center")
        _KOTAK_CLIENT = client
        return client
    return None


def available_kotak_funds() -> dict:
    """Fetch available trading margin from Kotak Neo."""
    client = get_kotak_client()
    if client is None:
        return {"ok": False, "error": "Kotak Neo client not initialized. Configure .env credentials."}
    status = kotak_token_status()
    if not status.get("valid"):
        return {"ok": False, "error": "Kotak Neo token is expired or missing."}

    try:
        limits = client.limits()
        if isinstance(limits, dict):
            if "error" in limits or "Error Message" in limits:
                err = limits.get("error") or limits.get("Error Message") or limits.get("message")
                return {"ok": False, "error": str(err)}
            net = limits.get("Net") or limits.get("AvailableCash") or limits.get("cashAvailable") or limits.get("availableBalance")
            if net is not None:
                return {"ok": True, "available_equity_funds": float(net)}
            data = limits.get("data")
            if isinstance(data, list) and data:
                for row in data:
                    amt = row.get("Net") or row.get("AvailableCash") or row.get("marginAvailable")
                    if amt is not None:
                        return {"ok": True, "available_equity_funds": float(amt)}
        return {"ok": True, "available_equity_funds": 0.0, "raw": limits}
    except Exception as exc:
        return {"ok": False, "error": f"Unable to fetch Kotak Neo funds: {exc}"}


def place_kotak_order(
    symbol: str,
    qty: int,
    side: int,  # 1 = BUY, -1 = SELL
    product: str = "NRML",
    exchange_segment: Optional[str] = None,
) -> Tuple[str, float]:
    """Place a live market order on Kotak Neo with automatic relogin on expired session."""
    client = get_kotak_client()
    if client is None:
        raise RuntimeError("Kotak Neo client not configured")

    status = kotak_token_status()
    if not status.get("valid"):
        kotak_login()
        client = get_kotak_client()
        if client is None:
            raise RuntimeError("Kotak Neo session invalid and auto-login failed")

    ex_seg = exchange_segment or ("bse_fo" if "SENSEX" in symbol.upper() else "nse_fo")
    tt = "B" if side == 1 else "S"
    resp = client.place_order(
        exchange_segment=ex_seg,
        product=product,
        price="0",
        order_type="MKT",
        quantity=str(qty),
        validity="DAY",
        trading_symbol=symbol,
        transaction_type=tt,
        amo="NO",
    )

    err_str = str(resp)
    if isinstance(resp, dict) and (resp.get("stCode") == 100008 or "unauthorized" in err_str.lower()):
        kotak_login()
        client = get_kotak_client()
        resp = client.place_order(
            exchange_segment=ex_seg,
            product=product,
            price="0",
            order_type="MKT",
            quantity=str(qty),
            validity="DAY",
            trading_symbol=symbol,
            transaction_type=tt,
            amo="NO",
        )

    if isinstance(resp, dict) and product == "MIS" and (resp.get("stat") == "Not_Ok" or resp.get("error")):
        product = "NRML"
        resp = client.place_order(
            exchange_segment=ex_seg,
            product=product,
            price="0",
            order_type="MKT",
            quantity=str(qty),
            validity="DAY",
            trading_symbol=symbol,
            transaction_type=tt,
            amo="NO",
        )

    if not isinstance(resp, dict):
        raise RuntimeError(f"Unexpected response from Kotak Neo: {resp}")

    if resp.get("error") or resp.get("stat") == "Not_Ok":
        err_msg = resp.get("errMsg") or resp.get("message") or resp.get("error")
        raise RuntimeError(f"Kotak Neo order rejected: {err_msg}")

    order_id = str(resp.get("nOrdNo") or resp.get("order_id") or resp.get("result", ""))
    if not order_id:
        raise RuntimeError(f"Kotak Neo did not return an order ID: {resp}")

    traded_price = 0.0
    last_status = "pending"
    last_rej = ""
    for _ in range(15):
        time.sleep(0.5)
        try:
            hist = client.order_history(order_id=order_id)
            if isinstance(hist, dict):
                data = hist.get("data") or hist.get("result") or []
                if isinstance(data, list) and data:
                    ord_info = data[-1]
                    status_str = str(ord_info.get("ordSt", "")).lower()
                    last_status = status_str
                    last_rej = str(ord_info.get("rejRsn", "") or "")
                    if status_str in ("trd", "complete", "traded", "filled"):
                        traded_price = float(ord_info.get("avgPrc") or ord_info.get("prc") or 0.0)
                        return order_id, traded_price
                    if status_str in ("can", "rej", "rejected", "cancelled"):
                        rej_reason = ord_info.get("rejRsn") or "Order rejected/cancelled"
                        raise RuntimeError(f"Kotak order {order_id} failed: {rej_reason}")
        except Exception as exc:
            if "failed:" in str(exc):
                raise

    if last_status in ("can", "rej", "rejected", "cancelled"):
        raise RuntimeError(f"Kotak order {order_id} rejected: {last_rej or last_status}")

    return order_id, traded_price


def cancel_kotak_order(order_id: str) -> dict:
    client = get_kotak_client()
    if client is None:
        return {"ok": False, "error": "Kotak Neo client not configured"}
    try:
        resp = client.cancel_order(order_id=str(order_id))
        return {"ok": True, "response": resp}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
