# =============================================================================
# data.py  —  Angel One SmartAPI integration + background data-fetch thread
# =============================================================================
# Responsibilities:
#   • InstrumentCache  – downloads the NSE instrument master once, caches it
#                        on disk, and provides symbol → token lookup.
#   • AngelOneClient   – wraps SmartConnect with login, LTP, candle data, and
#                        order-placement helpers.
#   • DataWorker       – QThread that connects on startup then polls for fresh
#                        candle data + LTP on every REFRESH_INTERVAL tick,
#                        emitting Qt signals so the UI never blocks.
# =============================================================================

import os
import json
import time
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional

import pandas as pd
import pyotp
import requests
from PyQt5.QtCore import QThread, pyqtSignal
from SmartApi import SmartConnect

import config

logger = logging.getLogger(__name__)


# =============================================================================
# InstrumentCache
# =============================================================================

class InstrumentCache:
    """
    Downloads the Angel One OpenAPI instrument master once, saves it to disk,
    and offers a fast lookup from a plain NSE symbol name (e.g. "RELIANCE")
    to the numeric token required by the API (e.g. "2885").
    """

    def __init__(self) -> None:
        self._cache: Dict[str, str] = {}   # SYMBOL → token
        self._loaded: bool = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def ensure_loaded(self) -> None:
        """Load the instrument cache lazily (from disk or network)."""
        if self._loaded:
            return
        if not self._load_from_disk():
            self._download_and_cache()
        self._loaded = True

    def get_token(self, symbol: str) -> Optional[str]:
        """Return the Angel One token for *symbol* (uppercase NSE equity name)."""
        self.ensure_loaded()
        return self._cache.get(symbol.upper())

    @staticmethod
    def trading_symbol(symbol: str) -> str:
        """Return the Angel One trading-symbol string, e.g. 'RELIANCE-EQ'."""
        return symbol.upper() + "-EQ"

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_from_disk(self) -> bool:
        """Attempt to read the cached instrument list from the local file."""
        if not os.path.exists(config.INSTRUMENT_CACHE_FILE):
            return False
        try:
            with open(config.INSTRUMENT_CACHE_FILE, "r", encoding="utf-8") as fh:
                instruments = json.load(fh)
            self._build_lookup(instruments)
            logger.info(
                "Instrument cache loaded from disk (%d entries).", len(self._cache)
            )
            return True
        except Exception as exc:
            logger.warning("Could not load instrument cache from disk: %s", exc)
            return False

    def _download_and_cache(self) -> None:
        """Fetch the instrument master from Angel One and persist it locally."""
        logger.info("Downloading instrument master from Angel One…")
        resp = requests.get(config.INSTRUMENT_URL, timeout=30)
        resp.raise_for_status()
        instruments = resp.json()
        with open(config.INSTRUMENT_CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump(instruments, fh)
        self._build_lookup(instruments)
        logger.info(
            "Instrument master downloaded and cached (%d instruments total).",
            len(instruments),
        )

    def _build_lookup(self, instruments: list) -> None:
        """Build {SYMBOL: token} dict for NSE equities (symbol ends with '-EQ')."""
        self._cache.clear()
        for item in instruments:
            if (
                item.get("exch_seg") == config.EXCHANGE
                and str(item.get("symbol", "")).endswith("-EQ")
            ):
                base = item["symbol"].replace("-EQ", "").upper()
                self._cache[base] = str(item["token"])


# =============================================================================
# AngelOneClient
# =============================================================================

class AngelOneClient:
    """
    Thin wrapper around Angel One's SmartConnect library.

    Provides:
        connect()       – authenticate and establish a session
        get_ltp()       – fetch the last traded price for a symbol
        get_candles()   – fetch N one-minute OHLCV candles as a DataFrame
        place_order()   – submit an MIS market/limit order
    """

    def __init__(self) -> None:
        self._api: Optional[SmartConnect] = None
        self._connected: bool = False
        self.last_error_code: Optional[str] = None   # error code from last failed order

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """
        Login to Angel One using credentials from config.py.
        Raises ConnectionError if authentication fails.
        """
        totp_code = pyotp.TOTP(config.TOTP_SECRET).now()
        self._api = SmartConnect(api_key=config.API_KEY)
        session = self._api.generateSession(
            config.CLIENT_ID, config.PASSWORD, totp_code
        )
        if not session or session.get("status") is False:
            raise ConnectionError(
                f"Angel One login failed: {session.get('message', 'Unknown error')}"
            )
        self._connected = True
        logger.info("Authenticated as %s.", config.CLIENT_ID)

    # ------------------------------------------------------------------
    # Market Data
    # ------------------------------------------------------------------

    def get_ltp(self, symbol: str, token: str) -> Optional[float]:
        """
        Fetch the Last Traded Price for a given NSE equity.

        Returns the LTP as a float, or None on any error.
        """
        try:
            resp = self._api.ltpData(
                exchange=config.EXCHANGE,
                tradingsymbol=InstrumentCache.trading_symbol(symbol),
                symboltoken=token,
            )
            if resp and resp.get("status"):
                return float(resp["data"]["ltp"])
            logger.warning("ltpData returned non-OK for %s: %s", symbol, resp)
        except Exception as exc:
            logger.warning("LTP fetch error for %s: %s", symbol, exc)
        return None

    def get_candles(
        self, symbol: str, token: str, limit: int = 100
    ) -> Optional[pd.DataFrame]:
        """
        Fetch the last *limit* one-minute OHLCV candles for a symbol.

        Returns a DataFrame with columns:
            timestamp, open, high, low, close, volume
        or None if the request fails.
        """
        try:
            now     = datetime.now()
            from_dt = now - timedelta(minutes=limit + 45)   # extra buffer for gaps
            params  = {
                "exchange":    config.EXCHANGE,
                "symboltoken": token,
                "interval":    config.CANDLE_INTERVAL,
                "fromdate":    from_dt.strftime("%Y-%m-%d %H:%M"),
                "todate":      now.strftime("%Y-%m-%d %H:%M"),
            }
            resp = self._api.getCandleData(params)
            if not resp or not resp.get("status"):
                logger.warning("getCandleData failed for %s: %s", symbol, resp)
                return None

            raw = resp.get("data") or []
            if not raw:
                return None

            df = pd.DataFrame(
                raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
            )
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = (
                df.sort_values("timestamp")
                  .tail(limit)
                  .reset_index(drop=True)
            )
            return df

        except Exception as exc:
            logger.warning("Candle data error for %s: %s", symbol, exc)
            return None

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def place_order(
        self,
        symbol: str,
        token: str,
        transaction_type: str,   # "BUY" or "SELL"
        quantity: int,
        price: float = 0.0,
        order_type: str = "MARKET",
    ) -> Optional[str]:
        """
        Place an intraday (MIS) order on NSE.

        Returns the Angel One order ID string on success, or None on failure.
        """
        try:
            params = {
                "variety":         "NORMAL",
                "tradingsymbol":   InstrumentCache.trading_symbol(symbol),
                "symboltoken":     token,
                "transactiontype": transaction_type,
                "exchange":        config.EXCHANGE,
                "ordertype":       order_type,
                "producttype":     "INTRADAY",   # SmartAPI v2: INTRADAY (not MIS)
                "duration":        "DAY",
                "price":           str(round(price, 2)) if order_type == "LIMIT" else "0",
                "squareoff":       "0",
                "stoploss":        "0",
                "quantity":        str(quantity),
            }
            resp = self._api.placeOrder(params)

            # SmartAPI library versions differ in what placeOrder() returns:
            #   v1 / older : returns the order-ID string directly
            #   v2 / newer : returns {"status": True, "data": {"orderid": "..."}}
            #   some builds : returns {"status": True, "data": "orderid-string"}
            if isinstance(resp, str) and resp:
                # Older library — response IS the order ID
                order_id = resp
            elif isinstance(resp, dict) and resp.get("status"):
                data = resp.get("data") or {}
                if isinstance(data, dict):
                    order_id = data.get("orderid") or data.get("order_id", "")
                else:
                    order_id = str(data)   # data is the ID string itself
            else:
                # Capture the error code for the caller to inspect
                if isinstance(resp, dict):
                    self.last_error_code = resp.get("errorcode")
                    logger.error("placeOrder failed [%s]: %s",
                                 self.last_error_code, resp.get("message"))
                else:
                    self.last_error_code = None
                    logger.error("placeOrder failed: %s", resp)
                return None

            if order_id:
                logger.info(
                    "Order placed: %s %s ×%d → ID %s",
                    transaction_type, symbol, quantity, order_id,
                )
                return order_id
            logger.error("placeOrder returned empty order ID: %s", resp)
        except Exception as exc:
            logger.error("Order exception for %s: %s", symbol, exc)
        return None

    def calculate_quantity(self, available_cash: float, ltp: float) -> int:
        """
        Calculate the maximum buyable quantity for an intraday trade.

        Formula:
            margin_per_share = ltp × INTRADAY_MARGIN_PCT
            quantity = floor((available_cash × FUNDS_USAGE_PCT) / margin_per_share)

        Returns at least 1 so an order is always attempted when conditions are met.
        Falls back to 1 if price or cash is zero / unknown.
        """
        if ltp <= 0 or available_cash <= 0:
            return 1
        margin_per_share = ltp * config.INTRADAY_MARGIN_PCT
        quantity = int((available_cash * config.FUNDS_USAGE_PCT) / margin_per_share)
        return max(1, quantity)

    def get_funds(self) -> Optional[dict]:
        """
        Fetch RMS / margin data from Angel One via rmsLimit().

        Returns a dict with keys:
            available  – availablecash
            used       – utilizedamount
            net        – net balance
        or None on any failure.
        """
        try:
            resp = self._api.rmsLimit()
            if resp and resp.get("status"):
                data = resp.get("data") or {}
                return {
                    "available": float(data.get("availablecash",  0) or 0),
                    "used":      float(data.get("utilizedamount", 0) or 0),
                    "net":       float(data.get("net",            0) or 0),
                }
            logger.warning("rmsLimit returned non-OK: %s", resp)
        except Exception as exc:
            logger.warning("Funds fetch error: %s", exc)
        return None


# =============================================================================
# DataWorker  (QThread)
# =============================================================================

class DataWorker(QThread):
    """
    Background QThread that:
      1. Connects to Angel One when started.
      2. Every REFRESH_INTERVAL seconds, fetches the latest 1-minute candles
         and LTP for the currently active symbol.
      3. Emits Qt signals to hand data back to the UI thread safely.

    Public methods (callable from the main thread):
        set_symbol(symbol)  – change the active symbol
        stop()              – request a graceful shutdown
    """

    # --- Signals -----------------------------------------------------------
    connected        = pyqtSignal()           # successful login
    connection_error = pyqtSignal(str)        # login failed
    data_ready       = pyqtSignal(object, float)  # (DataFrame, ltp)
    fetch_error      = pyqtSignal(str)        # per-poll error
    log_message      = pyqtSignal(str)        # informational text
    funds_ready      = pyqtSignal(dict)       # {"available", "used", "net"}
    funds_error      = pyqtSignal(str)        # funds fetch failed

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.client           = AngelOneClient()
        self.instrument_cache = InstrumentCache()
        self._symbol: Optional[str] = None
        self._token:  Optional[str] = None
        self._running: bool = False
        self._funds_refresh_requested: bool = False
        self._last_funds_time: float = 0.0

    # ------------------------------------------------------------------
    # Public interface (main thread)
    # ------------------------------------------------------------------

    def set_symbol(self, symbol: str) -> bool:
        """
        Resolve *symbol* to an Angel One token and activate it.
        Returns True on success, False if the symbol is not in the cache.
        """
        self.instrument_cache.ensure_loaded()
        token = self.instrument_cache.get_token(symbol)
        if not token:
            return False
        self._symbol = symbol.upper()
        self._token  = token
        return True

    @property
    def active_token(self) -> Optional[str]:
        return self._token

    def stop(self) -> None:
        """Signal the run loop to exit cleanly."""
        self._running = False

    def request_funds_refresh(self) -> None:
        """Request an immediate funds refresh on the next polling tick."""
        self._funds_refresh_requested = True

    # ------------------------------------------------------------------
    # QThread.run()
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Entry point for the worker thread."""
        self._running = True

        # Step 1: authenticate
        try:
            self.log_message.emit("Connecting to Angel One…")
            self.client.connect()
            self.connected.emit()
            self.log_message.emit("✓ Connected to Angel One successfully.")
            self._fetch_and_emit_funds()   # initial balance snapshot on login
        except Exception as exc:
            self.connection_error.emit(str(exc))
            return

        # Step 2: polling loop
        while self._running:
            if self._symbol and self._token:
                self._fetch_and_emit()
            # Funds refresh: every FUNDS_INTERVAL seconds or on explicit request
            if self._funds_refresh_requested or (
                time.time() - self._last_funds_time >= config.FUNDS_INTERVAL
            ):
                self._fetch_and_emit_funds()
                self._funds_refresh_requested = False
            time.sleep(config.REFRESH_INTERVAL)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _fetch_and_emit(self) -> None:
        """Fetch data for the active symbol and emit data_ready."""
        df  = self.client.get_candles(self._symbol, self._token, limit=config.CANDLE_LIMIT)
        ltp = self.client.get_ltp(self._symbol, self._token)

        if df is not None and ltp is not None:
            self.data_ready.emit(df, float(ltp))
        else:
            msg = f"Data fetch incomplete for {self._symbol} (candles={'OK' if df is not None else 'FAIL'}, ltp={'OK' if ltp is not None else 'FAIL'})"
            self.fetch_error.emit(msg)

    def _fetch_and_emit_funds(self) -> None:
        """Fetch account funds/margin and emit funds_ready or funds_error."""
        self._last_funds_time = time.time()
        result = self.client.get_funds()
        if result is not None:
            self.funds_ready.emit(result)
        else:
            self.funds_error.emit("Funds data unavailable — retrying shortly.")
