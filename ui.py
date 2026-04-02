# =============================================================================
# ui.py  —  Main application window (PyQt5 + pyqtgraph)
# =============================================================================
# Components:
#   CandlestickItem  – pyqtgraph GraphicsObject that draws OHLC candles using
#                      a pre-rendered QPicture for smooth, flicker-free updates.
#   TimeAxisItem     – custom AxisItem that displays HH:MM labels on the X-axis.
#   MainWindow       – QMainWindow that wires together the DataWorker thread,
#                      the Trader, the chart, and the log panel.
# =============================================================================

import logging
from datetime import datetime
from typing import List, Optional, Tuple

import pandas as pd
import pyqtgraph as pg
from PyQt5.QtCore import Qt, pyqtSlot
from PyQt5.QtGui import QColor, QFont, QPainter, QPicture, QPen, QBrush
from PyQt5.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from data import DataWorker
from strategy import generate_signal, get_sr_levels
from trader import Trader
import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# pyqtgraph global appearance
# ---------------------------------------------------------------------------
pg.setConfigOption("background", "#1e1e2e")   # Catppuccin Mocha base
pg.setConfigOption("foreground", "#cdd6f4")   # text / axes


# =============================================================================
# CandlestickItem
# =============================================================================

class CandlestickItem(pg.GraphicsObject):
    """
    Efficient OHLC candlestick renderer.

    Data format accepted by set_data():
        list of (x_index: int, open, high, low, close) tuples
        where x_index is simply the candle's position (0, 1, 2, …).

    The candles are drawn once into a QPicture and then just replayed by
    paint(), making real-time updates very fast.
    """

    _BULL_COLOR = QColor("#a6e3a1")   # green  – close >= open
    _BEAR_COLOR = QColor("#f38ba8")   # red    – close <  open
    _BODY_WIDTH = 0.35                # half-width of candle body

    def __init__(self) -> None:
        super().__init__()
        self._picture: QPicture = QPicture()
        self._data: List[Tuple] = []

    # ------------------------------------------------------------------

    def set_data(self, data: List[Tuple]) -> None:
        """Replace all candle data and trigger a repaint."""
        self._data = data
        self._render()
        self.prepareGeometryChange()
        self.informViewBoundsChanged()
        self.update()

    # ------------------------------------------------------------------
    # pyqtgraph overrides
    # ------------------------------------------------------------------

    def paint(self, painter: QPainter, *args) -> None:
        painter.drawPicture(0, 0, self._picture)

    def boundingRect(self) -> pg.QtCore.QRectF:
        return pg.QtCore.QRectF(self._picture.boundingRect())

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _render(self) -> None:
        """Pre-draw all candles into a QPicture for fast replay."""
        pic = QPicture()
        p = QPainter(pic)
        w = self._BODY_WIDTH

        for (t, o, h, l, c) in self._data:
            color  = self._BULL_COLOR if c >= o else self._BEAR_COLOR
            pen    = QPen(color, 1)
            brush  = QBrush(color)
            p.setPen(pen)
            p.setBrush(brush)

            # Wick (high/low line)
            p.drawLine(
                pg.QtCore.QPointF(t, float(l)),
                pg.QtCore.QPointF(t, float(h)),
            )
            # Body rectangle
            body_bottom = min(float(o), float(c))
            body_height = max(abs(float(c) - float(o)), 0.001)   # avoid zero-height
            p.drawRect(pg.QtCore.QRectF(t - w, body_bottom, w * 2, body_height))

        p.end()
        self._picture = pic


# =============================================================================
# TimeAxisItem
# =============================================================================

class TimeAxisItem(pg.AxisItem):
    """
    X-axis that maps integer candle indices back to HH:MM timestamp strings.
    Call update_timestamps() whenever the candle list changes.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._timestamps: List = []

    def update_timestamps(self, timestamps: List) -> None:
        self._timestamps = timestamps

    def tickStrings(self, values, scale, spacing) -> List[str]:
        result = []
        for v in values:
            idx = int(round(v))
            if 0 <= idx < len(self._timestamps):
                ts = self._timestamps[idx]
                label = ts.strftime("%H:%M") if hasattr(ts, "strftime") else str(ts)
            else:
                label = ""
            result.append(label)
        return result


# =============================================================================
# MainWindow
# =============================================================================

class MainWindow(QMainWindow):
    """
    Top-level application window.

    Layout:
        [Control bar]
        ──────────────────────────────────────────
        [ Chart (pyqtgraph) ] | [ Log panel     ]
        ──────────────────────────────────────────
        [Status bar]
    """

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Nyati Traders — Intraday Analysis")
        self.resize(1440, 860)
        self._apply_stylesheet()

        # ── Runtime state ─────────────────────────────────────────────
        self._symbol: Optional[str]           = None
        self._candles: Optional[pd.DataFrame] = None
        self._ltp: float                      = 0.0
        self._supports: List[float]           = []
        self._resistances: List[float]        = []
        self._trading_enabled: bool           = False
        self._sr_lines: List[pg.InfiniteLine] = []   # keeps refs so we can remove them
        self._available_cash: float           = 0.0  # updated on every funds_ready signal

        # ── Worker + trader ───────────────────────────────────────────
        self._worker = DataWorker()
        self._trader: Optional[Trader] = None
        self._wire_worker_signals()

        # ── Build UI ──────────────────────────────────────────────────
        self._build_ui()

        # ── Start background thread ───────────────────────────────────
        self._worker.start()

    # ==================================================================
    # UI construction
    # ==================================================================

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        layout.addWidget(self._build_funds_bar())
        layout.addWidget(self._build_pnl_bar())
        layout.addWidget(self._build_control_bar())

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_chart_panel())
        splitter.addWidget(self._build_log_panel())
        splitter.setSizes([1040, 380])
        layout.addWidget(splitter, stretch=1)

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("Connecting to Angel One…")

    # ------------------------------------------------------------------

    def _build_control_bar(self) -> QGroupBox:
        box = QGroupBox("Controls")
        row = QHBoxLayout(box)
        row.setSpacing(10)

        # Symbol input
        row.addWidget(QLabel("Symbol:"))
        self._symbol_input = QLineEdit()
        self._symbol_input.setPlaceholderText("e.g. RELIANCE")
        self._symbol_input.setFixedWidth(150)
        self._symbol_input.returnPressed.connect(self._on_start_analyzing)
        row.addWidget(self._symbol_input)

        # Action buttons
        self._btn_analyze = QPushButton("▶  Start Analyzing")
        self._btn_analyze.setEnabled(False)   # enabled after connect
        self._btn_analyze.clicked.connect(self._on_start_analyzing)
        row.addWidget(self._btn_analyze)

        self._btn_trade = QPushButton("⚡  Start Trading")
        self._btn_trade.setCheckable(True)
        self._btn_trade.setEnabled(False)
        self._btn_trade.clicked.connect(self._on_toggle_trading)
        row.addWidget(self._btn_trade)

        self._btn_squareoff = QPushButton("✕  Square Off")
        self._btn_squareoff.setEnabled(False)
        self._btn_squareoff.clicked.connect(self._on_square_off)
        row.addWidget(self._btn_squareoff)

        row.addStretch()

        # LTP badge
        self._ltp_label = QLabel("LTP: —")
        self._ltp_label.setFont(QFont("Consolas", 15, QFont.Bold))
        self._ltp_label.setStyleSheet("color: #cba6f7; min-width: 160px;")
        row.addWidget(self._ltp_label)

        # Signal badge
        self._signal_label = QLabel("Signal: HOLD")
        self._signal_label.setFont(QFont("Consolas", 12, QFont.Bold))
        self._signal_label.setStyleSheet("color: #6c7086; min-width: 140px;")
        row.addWidget(self._signal_label)

        # Trade status
        self._trade_status = QLabel("Position: Flat")
        self._trade_status.setFont(QFont("Consolas", 10))
        self._trade_status.setStyleSheet("color: #a6adc8;")
        row.addWidget(self._trade_status)

        return box

    # ------------------------------------------------------------------

    def _build_funds_bar(self) -> QFrame:
        """
        Build the account-funds top bar.

        Displays three fields sourced from Angel One rmsLimit():
            • Available Funds  – green
            • Used Margin      – red
            • Net Balance      – default white
        Updated every FUNDS_INTERVAL seconds via DataWorker.funds_ready signal.
        """
        bar = QFrame()
        bar.setFrameShape(QFrame.StyledPanel)
        bar.setObjectName("fundsBar")
        bar.setFixedHeight(52)
        bar.setStyleSheet(
            "#fundsBar { background: #181825; "
            "border: 1px solid #45475a; border-radius: 6px; }"
        )

        outer = QHBoxLayout(bar)
        outer.setContentsMargins(20, 4, 20, 4)
        outer.setSpacing(0)

        bold_font = QFont("Consolas", 11, QFont.Bold)
        tiny_font = QFont("Segoe UI", 8)

        def _make_fund_field(title: str, value_color: str):
            """Return (QVBoxLayout, value_label) for one funds column."""
            col = QVBoxLayout()
            col.setSpacing(1)

            lbl_title = QLabel(title)
            lbl_title.setFont(tiny_font)
            lbl_title.setStyleSheet("color: #6c7086;")

            lbl_value = QLabel("—")
            lbl_value.setFont(bold_font)
            lbl_value.setStyleSheet(f"color: {value_color};")

            col.addWidget(lbl_title)
            col.addWidget(lbl_value)
            return col, lbl_value

        col1, self._funds_available_lbl = _make_fund_field("Available Funds", "#a6e3a1")
        col2, self._funds_used_lbl      = _make_fund_field("Used Margin",     "#f38ba8")
        col3, self._funds_net_lbl       = _make_fund_field("Net Balance",     "#cdd6f4")

        # Vertical separator helper
        def _sep():
            line = QFrame()
            line.setFrameShape(QFrame.VLine)
            line.setStyleSheet("color: #45475a;")
            return line

        outer.addLayout(col1)
        outer.addWidget(_sep())
        outer.addSpacing(24)
        outer.addLayout(col2)
        outer.addWidget(_sep())
        outer.addSpacing(24)
        outer.addLayout(col3)
        outer.addStretch()

        # Last-updated timestamp label (right-aligned)
        self._funds_updated_lbl = QLabel("")
        self._funds_updated_lbl.setFont(tiny_font)
        self._funds_updated_lbl.setStyleSheet("color: #45475a;")
        outer.addWidget(self._funds_updated_lbl, alignment=Qt.AlignRight | Qt.AlignVCenter)

        return bar

    # ------------------------------------------------------------------

    def _build_pnl_bar(self) -> QFrame:
        """
        Live P&L panel — shown directly below the funds bar.

        Displays a real-time snapshot of the open intraday position:
            Entry Price | Quantity | Current Price | P&L (green/red)
        Shows dashes when no trade is open.
        Updated on every LTP tick via _refresh_pnl_bar().
        """
        bar = QFrame()
        bar.setFrameShape(QFrame.StyledPanel)
        bar.setObjectName("pnlBar")
        bar.setFixedHeight(52)
        bar.setStyleSheet(
            "#pnlBar { background: #11111b; "
            "border: 1px solid #45475a; border-radius: 6px; }"
        )

        outer = QHBoxLayout(bar)
        outer.setContentsMargins(20, 4, 20, 4)
        outer.setSpacing(0)

        bold_font = QFont("Consolas", 11, QFont.Bold)
        tiny_font = QFont("Segoe UI", 8)

        def _make_pnl_field(title: str, color: str = "#cdd6f4"):
            col = QVBoxLayout()
            col.setSpacing(1)
            lbl_title = QLabel(title)
            lbl_title.setFont(tiny_font)
            lbl_title.setStyleSheet("color: #585b70;")
            lbl_value = QLabel("\u2014")
            lbl_value.setFont(bold_font)
            lbl_value.setStyleSheet(f"color: {color};")
            col.addWidget(lbl_title)
            col.addWidget(lbl_value)
            return col, lbl_value

        def _sep():
            line = QFrame()
            line.setFrameShape(QFrame.VLine)
            line.setStyleSheet("color: #313244;")
            return line

        col1, self._pnl_entry_lbl   = _make_pnl_field("Entry Price",   "#89b4fa")
        col2, self._pnl_qty_lbl     = _make_pnl_field("Quantity",      "#cdd6f4")
        col3, self._pnl_current_lbl = _make_pnl_field("Current Price", "#cba6f7")
        col4, self._pnl_value_lbl   = _make_pnl_field("P&L",           "#6c7086")

        # Make P&L column wider and value slightly larger
        self._pnl_value_lbl.setFont(QFont("Consolas", 13, QFont.Bold))

        outer.addLayout(col1)
        outer.addWidget(_sep())
        outer.addSpacing(24)
        outer.addLayout(col2)
        outer.addWidget(_sep())
        outer.addSpacing(24)
        outer.addLayout(col3)
        outer.addWidget(_sep())
        outer.addSpacing(24)
        outer.addLayout(col4)
        outer.addStretch()

        # Status label on the right (shows trade exit reason or "LIVE")
        self._pnl_status_lbl = QLabel("No Position")
        self._pnl_status_lbl.setFont(tiny_font)
        self._pnl_status_lbl.setStyleSheet("color: #585b70;")
        outer.addWidget(self._pnl_status_lbl, alignment=Qt.AlignRight | Qt.AlignVCenter)

        return bar

    # ------------------------------------------------------------------

    def _build_chart_panel(self) -> QGroupBox:
        box = QGroupBox("1-Minute Candlestick Chart")
        layout = QVBoxLayout(box)

        self._time_axis = TimeAxisItem(orientation="bottom")
        self._plot = pg.PlotWidget(axisItems={"bottom": self._time_axis})
        self._plot.showGrid(x=True, y=True, alpha=0.12)
        self._plot.setLabel("left", "Price (₹)")
        self._plot.getAxis("left").setStyle(tickFont=QFont("Consolas", 9))
        self._plot.getAxis("bottom").setStyle(tickFont=QFont("Consolas", 9))

        # Candlestick item
        self._candle_item = CandlestickItem()
        self._plot.addItem(self._candle_item)

        # Dashed LTP line
        self._ltp_line = pg.InfiniteLine(
            angle=0,
            pen=pg.mkPen(color="#cba6f7", width=1.2, style=Qt.DashLine),
        )
        self._plot.addItem(self._ltp_line)

        layout.addWidget(self._plot)
        return box

    # ------------------------------------------------------------------

    def _build_log_panel(self) -> QGroupBox:
        box = QGroupBox("Trade Log")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(4, 4, 4, 4)

        self._log_text = QTextEdit()
        self._log_text.setReadOnly(True)
        self._log_text.setFont(QFont("Consolas", 9))
        self._log_text.setStyleSheet("background:#181825; border:none;")
        layout.addWidget(self._log_text)

        return box

    # ==================================================================
    # Signal wiring
    # ==================================================================

    def _wire_worker_signals(self) -> None:
        self._worker.connected.connect(self._on_connected)
        self._worker.connection_error.connect(self._on_connection_error)
        self._worker.data_ready.connect(self._on_data_ready)
        self._worker.fetch_error.connect(self._on_fetch_error)
        self._worker.log_message.connect(self._append_log)
        self._worker.funds_ready.connect(self.update_funds_ui)
        self._worker.funds_error.connect(self._on_funds_error)

    def _wire_trader_signals(self, trader: Trader) -> None:
        trader.trade_opened.connect(
            lambda t: self._append_log(
                f"<span style='color:#a6e3a1;'>OPENED</span> "
                f"{t.symbol} ×{t.quantity} @ ₹{t.entry_price:.2f}  "
                f"SL ₹{t.stop_loss:.2f}  Target ₹{t.target:.2f}"
            )
        )
        # Refresh funds immediately after a BUY order fills
        trader.trade_opened.connect(lambda _: self._worker.request_funds_refresh())
        trader.trade_closed.connect(self._on_trade_closed)
        trader.order_error.connect(self._on_order_error)
        trader.log_message.connect(self._append_log)

    # ==================================================================
    # Slots
    # ==================================================================

    @pyqtSlot()
    def _on_connected(self) -> None:
        self._status_bar.showMessage("✓ Connected to Angel One")
        self._btn_analyze.setEnabled(True)
        self._trader = Trader(self._worker.client)
        self._trader.set_available_cash(self._available_cash)
        self._wire_trader_signals(self._trader)

    @pyqtSlot(str)
    def _on_connection_error(self, message: str) -> None:
        self._status_bar.showMessage(f"Connection failed: {message}")
        QMessageBox.critical(
            self, "Angel One — Connection Error",
            f"Could not connect to Angel One:\n\n{message}\n\n"
            "Verify your credentials in config.py and restart.",
        )

    @pyqtSlot()
    def _on_start_analyzing(self) -> None:
        symbol = self._symbol_input.text().strip().upper()
        if not symbol:
            QMessageBox.warning(self, "Input Error", "Please enter a stock symbol.")
            return

        self._append_log(f"Looking up instrument token for {symbol}…")
        ok = self._worker.set_symbol(symbol)
        if not ok:
            QMessageBox.warning(
                self, "Symbol Not Found",
                f"'{symbol}' was not found in the NSE instrument master.\n\n"
                "Try deleting instruments_cache.json and restarting, or check the symbol name.",
            )
            return

        self._symbol = symbol
        self.setWindowTitle(f"Nyati Traders — {symbol}")
        self._btn_trade.setEnabled(True)
        self._btn_squareoff.setEnabled(True)
        self._append_log(
            f"Tracking <b>{symbol}</b>. Chart refreshes every {config.REFRESH_INTERVAL}s."
        )

    @pyqtSlot(bool)
    def _on_toggle_trading(self, checked: bool) -> None:
        self._trading_enabled = checked
        if checked:
            self._btn_trade.setText("■  Stop Trading")
            self._append_log("⚡ Auto-trading <b>ENABLED</b>.")
        else:
            self._btn_trade.setText("⚡  Start Trading")
            self._append_log("Auto-trading <b>DISABLED</b>.")

    @pyqtSlot()
    def _on_square_off(self) -> None:
        if self._trader and self._trader.has_open_trade:
            self._trader.force_square_off(self._ltp)
        else:
            self._append_log("No open trade to square off.")

    @pyqtSlot(object, float)
    def _on_data_ready(self, candles: pd.DataFrame, ltp: float) -> None:
        """
        Main update handler — called on every data refresh from the worker thread.
        Runs entirely on the Qt main thread (connected via auto connection).
        """
        self._candles = candles
        self._ltp     = ltp

        # Update LTP display
        self._ltp_label.setText(f"LTP: ₹{ltp:.2f}")

        # Recompute S/R levels
        self._supports, self._resistances = get_sr_levels(
            candles,
            n_candles=config.SR_CANDLE_WINDOW,
            lookback=config.SWING_LOOKBACK,
            max_levels=config.SR_MAX_LEVELS,
        )

        # Refresh chart
        self._refresh_chart(candles, ltp)

        # Evaluate signal
        signal = generate_signal(
            ltp, candles,
            self._supports, self._resistances,
            proximity_pct=config.PROXIMITY_PCT,
        )
        self._update_signal_badge(signal)

        # Auto-trade
        if self._trading_enabled and self._trader:
            self._evaluate_trade(signal, ltp)

        # Check exits for open position
        if self._trader:
            self._trader.check_exits(ltp)

        # Update position label
        self._refresh_position_label()
        self._refresh_pnl_bar(ltp)

    @pyqtSlot(str)
    def _on_fetch_error(self, message: str) -> None:
        self._append_log(f"<span style='color:#fab387;'>⚠ {message}</span>")
        self._status_bar.showMessage(message)

    @pyqtSlot(object)
    def _on_trade_closed(self, trade) -> None:
        color = "#a6e3a1" if trade.pnl >= 0 else "#f38ba8"
        self._append_log(
            f"<span style='color:{color};'>CLOSED</span> "
            f"{trade.symbol} | entry ₹{trade.entry_price:.2f} "
            f"exit ₹{trade.exit_price:.2f} | "
            f"<b>P&amp;L ₹{trade.pnl:+.2f}</b>"
        )
        self._refresh_position_label()
        self._refresh_pnl_bar(self._ltp)   # reset bar immediately on close
        # Refresh funds after SELL fills so balance reflects the closed trade
        self._worker.request_funds_refresh()

    @pyqtSlot(dict)
    def update_funds_ui(self, data: dict) -> None:
        """
        Update the three funds-bar labels with fresh RMS data from Angel One.
        Also stores available cash and pushes it to the Trader for dynamic
        quantity calculation.
        """
        self._funds_available_lbl.setText(f"\u20b9{data['available']:,.2f}")
        self._funds_used_lbl.setText(f"\u20b9{data['used']:,.2f}")
        self._funds_net_lbl.setText(f"\u20b9{data['net']:,.2f}")
        ts = datetime.now().strftime("%H:%M:%S")
        self._funds_updated_lbl.setText(f"Updated {ts}")

        # Keep available cash in sync for dynamic quantity calculation
        self._available_cash = data["available"]
        if self._trader:
            self._trader.set_available_cash(self._available_cash)

    @pyqtSlot(str)
    def _on_funds_error(self, message: str) -> None:
        """Show error state in the funds bar when the rmsLimit API call fails."""
        err_style = "color: #f38ba8; font-style: italic;"
        self._funds_available_lbl.setText("Error")
        self._funds_available_lbl.setStyleSheet(err_style)
        self._funds_used_lbl.setText("Error")
        self._funds_used_lbl.setStyleSheet(err_style)
        self._funds_net_lbl.setText("Error")
        self._funds_net_lbl.setStyleSheet(err_style)
        self._funds_updated_lbl.setText("Fetch failed — retrying…")
        self._append_log(f"<span style='color:#fab387;'>\u26a0 Funds: {message}</span>")

    @pyqtSlot(str)
    def _on_order_error(self, message: str) -> None:
        """
        Handle order errors from the Trader.

        If the message signals a permanent exchange block (⛔), auto-trading is
        turned off and the button is reset so the user must re-enable it manually
        on a different symbol.  Transient errors just log in red as before.
        """
        is_permanent = message.startswith("⛔")
        color = "#ff5555" if is_permanent else "#f38ba8"
        self._append_log(
            f"<span style='color:{color};font-weight:bold;'>ORDER ERR</span> {message}"
        )
        if is_permanent:
            # Force trading off — user must pick a different symbol
            self._trading_enabled = False
            self._btn_trade.setChecked(False)
            self._btn_trade.setText("⚡  Start Trading")
            self._status_bar.showMessage(
                f"⛔ Auto-trading DISABLED — {self._symbol} is a cautionary listing."
            )

    # ==================================================================
    # Chart rendering
    # ==================================================================

    def _refresh_chart(self, candles: pd.DataFrame, ltp: float) -> None:
        """Rebuild the candlestick chart and S/R lines from the latest data."""

        # --- Candles ---
        candle_data = [
            (i, row.open, row.high, row.low, row.close)
            for i, row in enumerate(candles.itertuples(index=False))
        ]
        self._candle_item.set_data(candle_data)

        # --- Time axis ---
        self._time_axis.update_timestamps(list(candles["timestamp"]))

        # --- LTP dashed line ---
        self._ltp_line.setPos(ltp)

        # --- Remove old S/R lines ---
        for line in self._sr_lines:
            self._plot.removeItem(line)
        self._sr_lines.clear()

        # --- Draw support lines (green) ---
        for lvl in self._supports:
            line = pg.InfiniteLine(
                angle=0, pos=lvl,
                pen=pg.mkPen(color="#a6e3a1", width=1.2),
                label=f"S {lvl:.2f}",
                labelOpts={"color": "#a6e3a1", "position": 0.97, "anchors": [(0, 1), (0, 1)]},
            )
            self._plot.addItem(line)
            self._sr_lines.append(line)

        # --- Draw resistance lines (red) ---
        for lvl in self._resistances:
            line = pg.InfiniteLine(
                angle=0, pos=lvl,
                pen=pg.mkPen(color="#f38ba8", width=1.2),
                label=f"R {lvl:.2f}",
                labelOpts={"color": "#f38ba8", "position": 0.97, "anchors": [(0, 0), (0, 0)]},
            )
            self._plot.addItem(line)
            self._sr_lines.append(line)

        # --- Auto-fit Y axis ---
        if candle_data:
            all_lows  = [d[3] for d in candle_data]
            all_highs = [d[2] for d in candle_data]
            pad = (max(all_highs) - min(all_lows)) * 0.06
            self._plot.setYRange(min(all_lows) - pad, max(all_highs) + pad, padding=0)
            self._plot.setXRange(-0.5, len(candle_data) - 0.5, padding=0)

    # ==================================================================
    # Signal / position badges
    # ==================================================================

    def _update_signal_badge(self, signal: dict) -> None:
        action = signal.get("action", "HOLD")
        colors = {"BUY": "#a6e3a1", "SELL": "#f38ba8", "HOLD": "#6c7086"}
        self._signal_label.setText(f"Signal: {action}")
        self._signal_label.setStyleSheet(
            f"color: {colors.get(action, '#cdd6f4')}; font-weight: bold;"
        )

    def _refresh_position_label(self) -> None:
        if self._trader and self._trader.has_open_trade:
            t = self._trader.open_trade
            pnl = (self._ltp - t.entry_price) * t.quantity
            color = "#a6e3a1" if pnl >= 0 else "#f38ba8"
            self._trade_status.setText(
                f"Position: LONG {t.symbol} ×{t.quantity} "
                f"@ ₹{t.entry_price:.2f}  "
                f"<span style='color:{color}'>₹{pnl:+.2f}</span>"
            )
            self._trade_status.setTextFormat(Qt.RichText)
        else:
            self._trade_status.setText("Position: Flat")
    def _refresh_pnl_bar(self, ltp: float) -> None:
        """
        Update the live P&L bar on every LTP tick.
        Shows dashes when no trade is open; colors P&L green / red when live.
        """
        if self._trader and self._trader.has_open_trade:
            t = self._trader.open_trade
            pnl = (ltp - t.entry_price) * t.quantity
            pnl_color = "#a6e3a1" if pnl >= 0 else "#f38ba8"

            self._pnl_entry_lbl.setText(f"\u20b9{t.entry_price:,.2f}")
            self._pnl_entry_lbl.setStyleSheet("color: #89b4fa; font-weight: bold;")

            self._pnl_qty_lbl.setText(str(t.quantity))
            self._pnl_qty_lbl.setStyleSheet("color: #cdd6f4; font-weight: bold;")

            self._pnl_current_lbl.setText(f"\u20b9{ltp:,.2f}")
            self._pnl_current_lbl.setStyleSheet("color: #cba6f7; font-weight: bold;")

            self._pnl_value_lbl.setText(f"\u20b9{pnl:+,.2f}")
            self._pnl_value_lbl.setStyleSheet(
                f"color: {pnl_color}; font-weight: bold; font-size: 13px;"
            )
            self._pnl_status_lbl.setText("\u25cf LIVE")
            self._pnl_status_lbl.setStyleSheet("color: #a6e3a1; font-weight: bold;")
        else:
            flat_style = "color: #45475a; font-weight: bold;"
            for lbl in (self._pnl_entry_lbl, self._pnl_qty_lbl,
                        self._pnl_current_lbl, self._pnl_value_lbl):
                lbl.setText("\u2014")
                lbl.setStyleSheet(flat_style)
            self._pnl_status_lbl.setText("No Position")
            self._pnl_status_lbl.setStyleSheet("color: #585b70;")
    # ==================================================================
    # Trading decision (called on every data update)
    # ==================================================================

    def _evaluate_trade(self, signal: dict, ltp: float) -> None:
        """Execute a BUY if the signal says so and no position is open."""
        if not self._trader or self._trader.has_open_trade:
            return
        if signal.get("action") == "BUY":
            self._append_log(
                f"<span style='color:#a6e3a1;'>BUY SIGNAL</span> — "
                f"{signal.get('reason')}"
            )
            self._trader.execute_buy(
                self._symbol,
                self._worker.active_token,
                ltp,
            )

    # ==================================================================
    # Logging
    # ==================================================================

    @pyqtSlot(str)
    def _append_log(self, message: str) -> None:
        """Append a time-stamped HTML line to the log panel."""
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_text.append(
            f"<span style='color:#45475a;'>[{ts}]</span>&nbsp;{message}"
        )
        # Auto-scroll to bottom
        sb = self._log_text.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ==================================================================
    # Stylesheet
    # ==================================================================

    def _apply_stylesheet(self) -> None:
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #1e1e2e;
                color: #cdd6f4;
                font-family: "Segoe UI", "Noto Sans", sans-serif;
                font-size: 11px;
            }
            QGroupBox {
                border: 1px solid #45475a;
                border-radius: 6px;
                margin-top: 8px;
                padding-top: 8px;
                font-weight: bold;
                color: #cba6f7;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
            }
            QLineEdit {
                background: #181825;
                border: 1px solid #45475a;
                border-radius: 4px;
                padding: 4px 8px;
                color: #cdd6f4;
            }
            QLineEdit:focus { border-color: #cba6f7; }
            QPushButton {
                background: #313244;
                border: 1px solid #45475a;
                border-radius: 4px;
                padding: 5px 16px;
                color: #cdd6f4;
                font-weight: bold;
                min-width: 100px;
            }
            QPushButton:hover  { background: #45475a; }
            QPushButton:pressed{ background: #585b70; }
            QPushButton:checked {
                background: #f38ba8;
                color: #1e1e2e;
                border-color: #f38ba8;
            }
            QPushButton:disabled { color: #45475a; border-color: #313244; }
            QTextEdit { background: #181825; color: #a6adc8; border: none; }
            QSplitter::handle { background: #45475a; width: 2px; }
            QStatusBar {
                background: #181825;
                color: #6c7086;
                font-size: 10px;
            }
            QScrollBar:vertical { background: #181825; width: 8px; }
            QScrollBar::handle:vertical { background: #45475a; border-radius: 4px; }
        """)

    # ==================================================================
    # Window lifecycle
    # ==================================================================

    def closeEvent(self, event) -> None:
        """Gracefully stop the background thread before closing."""
        self._worker.stop()
        self._worker.wait(4000)
        event.accept()
