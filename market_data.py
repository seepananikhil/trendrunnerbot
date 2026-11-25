"""Live Market Data Streamer (Kotak Neo).

Streams official 1-minute OHLC candles and 3-second quotes for:
- NIFTY 50 (nse_cm|Nifty 50)
- SENSEX (bse_cm|SENSEX)
- INDIA VIX (nse_cm|INDIA VIX)

Writes to data/live/ with automatic daily rollover and zero external dependencies.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from broker_kotak import get_kotak_client
from telegram_notifier import notify_system

logger = logging.getLogger("market_data")

ROOT = Path(__file__).resolve().parent
LIVE_DIR = ROOT / "data" / "live"
LIVE_DIR.mkdir(parents=True, exist_ok=True)
IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 50)
TS_FMT = "%Y-%m-%d %H:%M:%S"


def now_ist() -> datetime:
    return datetime.now(IST)


def ensure_continuous_1min_candles(df: pd.DataFrame, day: str) -> pd.DataFrame:
    """Ensure an unbroken sequence of 1-minute OHLC bars from 09:15 to current minute."""
    if df.empty or "timestamp" not in df.columns:
        return df
    try:
        df = df.copy()
        df["dt"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df = df.dropna(subset=["dt"]).drop_duplicates("dt", keep="last").set_index("dt").sort_index()
        if df.empty:
            return pd.DataFrame()

        start_dt = pd.to_datetime(f"{day} 09:15:00")
        now_t = now_ist().replace(second=0, microsecond=0, tzinfo=None)
        market_end = pd.to_datetime(f"{day} 15:50:00")

        if day == now_ist().strftime("%Y-%m-%d"):
            now_closed = (now_t - pd.Timedelta(minutes=1)) if now_t >= start_dt else start_dt
            end_dt = min(df.index.max(), now_closed, market_end)
        else:
            end_dt = min(max(df.index.max(), start_dt), market_end)

        if end_dt < start_dt:
            end_dt = start_dt

        full_grid = pd.date_range(start_dt, end_dt, freq="1min")
        df_reindexed = df.reindex(full_grid)
        df_reindexed["close"] = df_reindexed["close"].ffill().bfill()
        df_reindexed["open"] = df_reindexed["open"].fillna(df_reindexed["close"])
        df_reindexed["high"] = df_reindexed["high"].fillna(df_reindexed["close"])
        df_reindexed["low"] = df_reindexed["low"].fillna(df_reindexed["close"])
        vol = df_reindexed["volume"].fillna(1000.0) if "volume" in df_reindexed.columns else 1000.0

        return pd.DataFrame({
            "timestamp": full_grid.strftime("%Y-%m-%d %H:%M:00"),
            "open": df_reindexed["open"].values,
            "high": df_reindexed["high"].values,
            "low": df_reindexed["low"].values,
            "close": df_reindexed["close"].values,
            "volume": vol.values if hasattr(vol, "values") else vol,
        })
    except Exception as exc:
        logger.warning(f"Error in ensure_continuous_1min_candles: {exc}")
        return df


class MarketDataStreamer:
    """Autonomous live market data streamer for NIFTY, SENSEX, and INDIA VIX."""

    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.day = now_ist().strftime("%Y-%m-%d")
        self._auth_alerted = False

    def _sync_instrument_candles(self, client, seg: str, tok: str, target_file: Path, day: str) -> bool:
        try:
            res = client.historical_data(
                neosymbol=f"{seg}|{tok}",
                interval="1min",
                from_date=day,
                to_date=day,
            )
            raw = res.get("data", {}).get("candles", []) if isinstance(res, dict) else []
            if not raw:
                return False

            rows = []
            for r in raw:
                if len(r) >= 6:
                    ts = r[0][:19].replace("T", " ")
                    rows.append({
                        "timestamp": ts,
                        "open": float(r[1]),
                        "high": float(r[2]),
                        "low": float(r[3]),
                        "close": float(r[4]),
                        "volume": float(r[5]),
                    })

            if rows:
                df = pd.DataFrame(rows)
                df = df.drop_duplicates("timestamp", keep="last").sort_values("timestamp")
                df = ensure_continuous_1min_candles(df, day)
                df.to_csv(target_file, index=False)
                return True
        except Exception as exc:
            if "429" in str(exc):
                time.sleep(10)
            logger.warning(f"Candle fetch error for {seg}|{tok}: {exc}")
        return False

    def fetch_candles(self, day: str) -> bool:
        client = get_kotak_client()
        if not client:
            if not self._auth_alerted:
                self._auth_alerted = True
                notify_system("Market Data Error", "Kotak Neo client unavailable for candle sync.", level="ERROR")
            return False
        self._auth_alerted = False
        ok_nifty = self._sync_instrument_candles(client, "nse_cm", "Nifty 50", LIVE_DIR / f"nifty_1min_{day}.csv", day)
        time.sleep(1.0)
        ok_sensex = self._sync_instrument_candles(client, "bse_cm", "SENSEX", LIVE_DIR / f"sensex_1min_{day}.csv", day)
        return ok_nifty or ok_sensex

    def poll_quotes_once(self, day: str) -> None:
        client = get_kotak_client()
        if not client:
            return
        quote_req = [
            {"instrument_token": "Nifty 50", "exchange_segment": "nse_cm"},
            {"instrument_token": "SENSEX", "exchange_segment": "bse_cm"},
            {"instrument_token": "INDIA VIX", "exchange_segment": "nse_cm"},
        ]
        ts = now_ist()
        try:
            quotes = client.quotes(quote_req)
            quotes_data = {}
            if isinstance(quotes, list):
                for item in quotes:
                    if not isinstance(item, dict):
                        continue
                    tok = str(item.get("exchange_token", ""))
                    try:
                        ltp = float(item.get("ltp") or 0.0)
                    except (ValueError, TypeError):
                        ltp = 0.0
                    if ltp <= 0:
                        continue

                    payload = {
                        "ltp": ltp,
                        "change": float(item.get("change") or 0.0),
                        "per_change": float(item.get("per_change") or 0.0),
                        "prev_close": float((item.get("ohlc") or {}).get("close") or 0.0),
                        "time": ts.strftime(TS_FMT),
                    }
                    if "vix" in tok.lower():
                        quotes_data["vix"] = payload
                    elif "nifty" in tok.lower():
                        quotes_data["nifty"] = payload
                    elif "sensex" in tok.lower():
                        quotes_data["sensex"] = payload

                if quotes_data:
                    quotes_file = LIVE_DIR / f"quotes_{day}.json"
                    quotes_file.write_text(json.dumps(quotes_data), encoding="utf-8")
        except Exception as exc:
            logger.debug(f"Quote poll error: {exc}")

    def run_forever(self) -> None:
        """Continuously stream market data during market hours, sleeping cleanly off-hours."""
        logger.info("MarketDataStreamer started (NIFTY, SENSEX, INDIA VIX).")
        last_candle_sync = 0.0
        while not self.stop_event.is_set():
            now = now_ist()
            day = now.strftime("%Y-%m-%d")
            if day != self.day:
                self.day = day
                self._auth_alerted = False

            if now.weekday() >= 5:
                time.sleep(60)
                continue

            t_hm = (now.hour, now.minute)
            if t_hm < MARKET_OPEN or t_hm > MARKET_CLOSE:
                time.sleep(15)
                continue

            self.poll_quotes_once(day)
            now_ts = time.time()
            if now_ts - last_candle_sync >= 15.0:
                self.fetch_candles(day)
                last_candle_sync = now_ts

            time.sleep(3)
