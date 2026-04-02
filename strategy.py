# =============================================================================
# strategy.py  —  Support / Resistance detection + trade-signal generation
# =============================================================================
# All functions are pure (no side-effects, no Qt dependencies).
# They operate exclusively on pandas DataFrames and plain Python values.
#
# Flow:
#   get_sr_levels()    – detect swing highs / lows → merge → rank by strength
#   generate_signal()  – compare LTP against levels → return BUY / SELL / HOLD
# =============================================================================

import logging
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)


# =============================================================================
# Swing-point detection
# =============================================================================

def find_swing_lows(df: pd.DataFrame, lookback: int = 2) -> List[float]:
    """
    Identify swing lows in a candle DataFrame.

    A candle at index *i* qualifies as a swing low iff:
        low[i] < low[i-k]  for every k in 1..lookback  (preceding candles)
        low[i] < low[i+k]  for every k in 1..lookback  (following candles)

    Returns a sorted list of unique low-price levels.
    """
    lows = df["low"].to_numpy(dtype=float)
    levels: List[float] = []

    for i in range(lookback, len(lows) - lookback):
        pivot = lows[i]
        if (pivot < lows[i - lookback: i]).all() and (pivot < lows[i + 1: i + lookback + 1]).all():
            levels.append(pivot)

    return sorted(set(levels))


def find_swing_highs(df: pd.DataFrame, lookback: int = 2) -> List[float]:
    """
    Identify swing highs in a candle DataFrame.

    A candle at index *i* qualifies as a swing high iff:
        high[i] > high[i-k]  for every k in 1..lookback
        high[i] > high[i+k]  for every k in 1..lookback

    Returns a sorted list of unique high-price levels.
    """
    highs = df["high"].to_numpy(dtype=float)
    levels: List[float] = []

    for i in range(lookback, len(highs) - lookback):
        pivot = highs[i]
        if (pivot > highs[i - lookback: i]).all() and (pivot > highs[i + 1: i + lookback + 1]).all():
            levels.append(pivot)

    return sorted(set(levels))


# =============================================================================
# Level post-processing
# =============================================================================

def _merge_nearby_levels(levels: List[float], tolerance_pct: float = 0.002) -> List[float]:
    """
    Collapse levels that lie within *tolerance_pct* of each other into a
    single averaged value, treating them as the same S/R zone.
    """
    if not levels:
        return []

    merged: List[float] = [sorted(levels)[0]]
    for lvl in sorted(levels)[1:]:
        if (lvl - merged[-1]) / merged[-1] < tolerance_pct:
            merged[-1] = (merged[-1] + lvl) / 2.0   # average into zone
        else:
            merged.append(lvl)
    return merged


def _count_touches(price: float, df: pd.DataFrame, tol_pct: float = 0.003) -> int:
    """
    Count how many candles 'touched' *price* (i.e. whose high or low came
    within *tol_pct* of the level).  Used to rank level strength.
    """
    tol = price * tol_pct
    touched = (
        ((df["low"]  >= price - tol) & (df["low"]  <= price + tol)) |
        ((df["high"] >= price - tol) & (df["high"] <= price + tol))
    )
    return int(touched.sum())


# =============================================================================
# Primary S/R interface
# =============================================================================

def get_sr_levels(
    df: pd.DataFrame,
    n_candles: int = 50,
    lookback: int = 2,
    max_levels: int = 5,
) -> Tuple[List[float], List[float]]:
    """
    Compute the strongest support and resistance levels from recent candles.

    Steps:
        1. Use the last *n_candles* candles only.
        2. Find swing lows  → support candidates.
        3. Find swing highs → resistance candidates.
        4. Merge levels that are within 0.2 % of each other.
        5. Rank by touch-count; return the top *max_levels* for each side.

    Returns:
        (supports, resistances)  – each a list sorted strongest-first.
    """
    if df is None or df.empty or len(df) < lookback * 2 + 1:
        return [], []

    window = df.tail(n_candles).reset_index(drop=True)

    raw_supports    = find_swing_lows(window,  lookback=lookback)
    raw_resistances = find_swing_highs(window, lookback=lookback)

    supports    = _merge_nearby_levels(raw_supports)
    resistances = _merge_nearby_levels(raw_resistances)

    # Rank by strength (number of historical touches)
    supports    = sorted(supports,    key=lambda x: _count_touches(x, window), reverse=True)[:max_levels]
    resistances = sorted(resistances, key=lambda x: _count_touches(x, window), reverse=True)[:max_levels]

    return supports, resistances


# =============================================================================
# Signal generation
# =============================================================================

def generate_signal(
    ltp: float,
    candles: pd.DataFrame,
    supports: List[float],
    resistances: List[float],
    proximity_pct: float = 0.005,
) -> Dict:
    """
    Evaluate current market conditions and return a structured signal dict.

    BUY  conditions  (both must be true):
        • LTP is within *proximity_pct* of a support level.
        • The last closed candle is bullish (close ≥ open).

    SELL conditions  (either):
        • LTP is within *proximity_pct* of a resistance level.

    Returns a dict with keys:
        action           : "BUY" | "SELL" | "HOLD"
        near_support     : float | None
        near_resistance  : float | None
        last_bullish     : bool
        reason           : str
    """
    result: Dict = {
        "action":          "HOLD",
        "near_support":    None,
        "near_resistance": None,
        "last_bullish":    False,
        "reason":          "No actionable signal.",
    }

    # Determine if the most-recent closed candle is bullish
    if candles is not None and not candles.empty:
        last = candles.iloc[-1]
        result["last_bullish"] = float(last["close"]) >= float(last["open"])

    # Find the nearest support within proximity
    for sup in sorted(supports, key=lambda x: abs(x - ltp)):
        if abs(ltp - sup) / sup <= proximity_pct:
            result["near_support"] = sup
            break

    # Find the nearest resistance within proximity
    for res in sorted(resistances, key=lambda x: abs(x - ltp)):
        if abs(ltp - res) / res <= proximity_pct:
            result["near_resistance"] = res
            break

    # --- Decision logic ---------------------------------------------------
    if result["near_support"] is not None and result["last_bullish"]:
        dist_pct = abs(ltp - result["near_support"]) / result["near_support"] * 100
        result["action"] = "BUY"
        result["reason"] = (
            f"Price ₹{ltp:.2f} is {dist_pct:.2f}% from support "
            f"₹{result['near_support']:.2f} with a bullish candle."
        )

    elif result["near_resistance"] is not None:
        dist_pct = abs(ltp - result["near_resistance"]) / result["near_resistance"] * 100
        result["action"] = "SELL"
        result["reason"] = (
            f"Price ₹{ltp:.2f} is {dist_pct:.2f}% from resistance "
            f"₹{result['near_resistance']:.2f}."
        )

    return result
