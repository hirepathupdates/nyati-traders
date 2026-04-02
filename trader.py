# =============================================================================
# trader.py  —  Order execution and open-position lifecycle management
# =============================================================================
# Responsibilities:
#   • Trade dataclass – snapshot of one open intraday position.
#   • Trader (QObject) – places BUY orders, monitors for SL/TP hits, and
#     squares off the position via a SELL order.
#
# Rules enforced:
#   • Only ONE trade open at a time (MIS intraday, NSE equity).
#   • Stop-loss  = entry × (1 − STOP_LOSS_PCT)
#   • Target     = entry × (1 + TARGET_PCT)
#   • All order activity is emitted as Qt log_message signals.
# =============================================================================

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from PyQt5.QtCore import QObject, pyqtSignal

from data import AngelOneClient
import config

logger = logging.getLogger(__name__)


# =============================================================================
# Trade dataclass
# =============================================================================

@dataclass
class Trade:
    """Immutable snapshot of a single open (or just-closed) intraday trade."""
    symbol:      str
    token:       str
    side:        str           # always "BUY" for long-only intraday
    quantity:    int
    entry_price: float
    stop_loss:   float
    target:      float
    order_id:    str
    entry_time:  datetime      = field(default_factory=datetime.now)

    # Populated on close
    exit_price:  Optional[float]    = None
    exit_time:   Optional[datetime] = None
    exit_reason: Optional[str]      = None   # "TARGET" | "STOP_LOSS" | "MANUAL"
    pnl:         float              = 0.0


# =============================================================================
# Trader
# =============================================================================

class Trader(QObject):
    """
    Manages the full lifecycle of one intraday trade at a time.

    Signals emitted (connected by ui.py):
        trade_opened  – Trade opened successfully
        trade_closed  – Trade exited with P&L details
        order_error   – A critical order operation failed
        log_message   – Informational string for the UI log panel
    """

    # --- Signals -----------------------------------------------------------
    trade_opened = pyqtSignal(object)    # Trade instance
    trade_closed = pyqtSignal(object)    # Trade instance (with exit fields)
    order_error  = pyqtSignal(str)
    log_message  = pyqtSignal(str)

    # Exchange error codes that are permanent — never retry
    _PERMANENT_ERROR_CODES = {"AB4036"}   # cautionary listing, surveillance ban, etc.

    # Seconds to pause auto-trading after a transient failed BUY order
    _ORDER_FAIL_COOLDOWN = 60

    def __init__(self, client: AngelOneClient, parent=None) -> None:
        super().__init__(parent)
        self._client: AngelOneClient = client
        self.open_trade: Optional[Trade] = None
        self._last_buy_fail_time: float = 0.0   # epoch seconds of last BUY failure
        self._available_cash: float     = 0.0   # kept in sync via set_available_cash()
        self._blocked_symbols: set      = set() # permanently blocked for this session

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def has_open_trade(self) -> bool:
        return self.open_trade is not None

    def set_available_cash(self, amount: float) -> None:
        """Update the available cash used for dynamic quantity calculation."""
        self._available_cash = max(0.0, amount)

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

    def execute_buy(self, symbol: str, token: str, ltp: float) -> None:
        """
        Place a market BUY MIS order and register the position.

        Silently skips if a trade is already open (one-trade-at-a-time rule).
        Stop-loss and target are calculated automatically from config.
        """
        if self.has_open_trade:
            self.log_message.emit(
                f"⚠  BUY skipped — trade already open on {self.open_trade.symbol}."
            )
            return

        # Permanent block: exchange has flagged this symbol (e.g. cautionary listing)
        if symbol in self._blocked_symbols:
            self.log_message.emit(
                f"⛔  {symbol} is blocked (exchange cautionary listing). "
                "Change symbol to resume trading."
            )
            return

        # Cooldown: don't retry if a BUY failed recently (transient failure)
        secs_since_fail = time.time() - self._last_buy_fail_time
        if secs_since_fail < self._ORDER_FAIL_COOLDOWN:
            remaining = int(self._ORDER_FAIL_COOLDOWN - secs_since_fail)
            self.log_message.emit(
                f"⏸  BUY paused — order failed recently. Retrying in {remaining}s."
            )
            return

        # Calculate quantity dynamically from available funds and MIS margin
        quantity  = self._client.calculate_quantity(self._available_cash, ltp)
        stop_loss = round(ltp * (1.0 - config.STOP_LOSS_PCT), 2)
        target    = round(ltp * (1.0 + config.TARGET_PCT),    2)

        self.log_message.emit(
            f"→ Placing BUY: {symbol} ×{quantity} @ ≈₹{ltp:.2f}  "
            f"| SL ₹{stop_loss:.2f}  | Target ₹{target:.2f}"
        )

        order_id = self._client.place_order(symbol, token, "BUY", quantity)
        if order_id is None:
            err_code = self._client.last_error_code
            if err_code in self._PERMANENT_ERROR_CODES:
                # Permanent exchange block — add to session blocklist, no cooldown
                self._blocked_symbols.add(symbol)
                self.order_error.emit(
                    f"⛔ {symbol} BLOCKED by exchange (error {err_code}: cautionary listing). "
                    "Auto-trading disabled for this symbol."
                )
            else:
                # Transient failure — apply cooldown and retry later
                self._last_buy_fail_time = time.time()
                self.order_error.emit(
                    f"BUY order FAILED for {symbol}. "
                    f"Auto-trading paused for {self._ORDER_FAIL_COOLDOWN}s."
                )
            return

        trade = Trade(
            symbol=symbol,
            token=token,
            side="BUY",
            quantity=quantity,
            entry_price=ltp,
            stop_loss=stop_loss,
            target=target,
            order_id=order_id,
        )
        self.open_trade = trade
        self.log_message.emit(
            f"✓ BUY executed: {symbol} ×{quantity} @ ₹{ltp:.2f}  "
            f"[Order {order_id}]"
        )
        self.trade_opened.emit(trade)

    # ------------------------------------------------------------------
    # Exit monitoring
    # ------------------------------------------------------------------

    def check_exits(self, ltp: float) -> None:
        """
        Called on every data tick while a trade is open.
        Triggers a square-off if stop-loss or target has been reached.
        """
        if not self.has_open_trade:
            return

        trade = self.open_trade
        if ltp <= trade.stop_loss:
            self._square_off(trade, ltp, "STOP_LOSS")
        elif ltp >= trade.target:
            self._square_off(trade, ltp, "TARGET")

    def force_square_off(self, ltp: float) -> None:
        """
        Immediately square off any open position regardless of SL/TP.
        Intended for manual intervention or end-of-day wind-down.
        """
        if self.has_open_trade:
            self._square_off(self.open_trade, ltp, "MANUAL")
        else:
            self.log_message.emit("No open trade to square off.")

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _square_off(self, trade: Trade, ltp: float, reason: str) -> None:
        """Place a SELL order to close the long position and record P&L."""
        self.log_message.emit(
            f"→ Squaring off {trade.symbol} @ ₹{ltp:.2f}  (reason: {reason})"
        )

        order_id = self._client.place_order(
            trade.symbol, trade.token, "SELL", trade.quantity
        )
        if order_id is None:
            self.order_error.emit(
                f"EXIT SELL order FAILED for {trade.symbol} — manual action required!"
            )
            return

        # Populate exit fields
        trade.exit_price  = ltp
        trade.exit_time   = datetime.now()
        trade.exit_reason = reason
        trade.pnl         = round((ltp - trade.entry_price) * trade.quantity, 2)

        self.open_trade = None   # position is now flat

        emoji = "🟢" if trade.pnl >= 0 else "🔴"
        self.log_message.emit(
            f"{emoji} Trade closed: {trade.symbol}  entry ₹{trade.entry_price:.2f}  "
            f"exit ₹{ltp:.2f}  P&L ₹{trade.pnl:+.2f}  [Order {order_id}]"
        )
        self.trade_closed.emit(trade)
