"""VWAP-distance reversion candidate. Fades excursions from session VWAP.

Score:
  Price > VWAP by k × ATR  → negative (price stretched, fade down)
  Price < VWAP by k × ATR  → positive (stretched the other way)

Approximates session VWAP with a rolling 24-bar VWAP since we don't have
session-bound data. For crypto 24h sessions this is reasonable; for
equities it'll be a noisier proxy.
"""
from __future__ import annotations

import pandas as pd

name = "vwap_distance"


def score(bars_1h: pd.DataFrame, window: int = 24, k_atr: float = 1.5) -> float:
    if len(bars_1h) < window + 20:
        return 0.0
    typical = (bars_1h["high"] + bars_1h["low"] + bars_1h["close"]) / 3
    vol = bars_1h["volume"].replace(0, 1)
    vwap = (typical * vol).rolling(window).sum() / vol.rolling(window).sum()
    px = float(bars_1h["close"].iloc[-1])
    vwap_now = float(vwap.iloc[-1])
    # ATR estimate
    tr = pd.concat([
        bars_1h["high"] - bars_1h["low"],
        (bars_1h["high"] - bars_1h["close"].shift()).abs(),
        (bars_1h["low"] - bars_1h["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = float(tr.ewm(alpha=1.0 / 14, adjust=False).mean().iloc[-1])
    if atr <= 0 or pd.isna(vwap_now):
        return 0.0
    deviation = px - vwap_now
    z = deviation / (k_atr * atr)
    return float(max(-1.0, min(1.0, -z)))   # fade direction
