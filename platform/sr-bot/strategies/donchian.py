"""Donchian channel breakout — the Turtle rule. Trend follower.

Score:
  +score on close > previous-bar upper band (long breakout)
  -score on close < previous-bar lower band (short breakout)
  0 inside the channel.

Uses yesterday's bands (iloc[-2]) so the breakout isn't its own trigger.
"""
from __future__ import annotations

import pandas as pd

name = "donchian_trend"


def score(bars_1d: pd.DataFrame, window: int = 20, scale: float = 50.0) -> float:
    if len(bars_1d) < window + 5:
        return 0.0
    upper = bars_1d["high"].rolling(window).max().iloc[-2]
    lower = bars_1d["low"].rolling(window).min().iloc[-2]
    close = float(bars_1d["close"].iloc[-1])
    if upper <= 0 or lower <= 0:
        return 0.0
    if close > upper:
        return float(min(1.0, (close - upper) / upper * scale))
    if close < lower:
        return float(max(-1.0, (close - lower) / lower * scale))
    return 0.0
