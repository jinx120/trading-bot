"""S&R bounce — wraps the existing logic in sr_paper_bot as a sub-strategy.

Score reflects how close price is to a confirmed pivot and the regime
compatibility. This is the current bot's core logic, just adapted to the
ensemble interface.
"""
from __future__ import annotations

import pandas as pd

name = "sr_bounce"


def score(bars_1h: pd.DataFrame, approach_pct: float = 0.003,
          cluster_pct: float = 0.003, pivot_window: int = 5,
          confluence_pct: float = 0.005) -> float:
    from sr_paper_bot import (
        detect_pivots, cluster_levels, nearest_above_below,
        is_confluent, resample_4h, regime,
    )
    if len(bars_1h) < 200:
        return 0.0
    close = float(bars_1h["close"].iloc[-1])
    piv_1h = cluster_levels(detect_pivots(bars_1h, pivot_window), cluster_pct)
    bars_4h = resample_4h(bars_1h)
    piv_4h = cluster_levels(detect_pivots(bars_4h, pivot_window), cluster_pct) \
        if len(bars_4h) >= pivot_window + 4 else []
    if not piv_1h:
        return 0.0
    sup_1h, res_1h = nearest_above_below(close, piv_1h)
    sup_4h, res_4h = nearest_above_below(close, piv_4h) if piv_4h else (None, None)
    reg = regime(bars_1h)
    block_long = reg["label"] == "downtrend"
    block_short = reg["label"] == "uptrend"

    # Long score: distance to nearest support, normalized to approach_pct
    long_score = 0.0
    if sup_1h and not block_long:
        dist = (close - sup_1h.price) / close
        if 0 < dist < approach_pct:
            base = 1.0 - dist / approach_pct  # closer = stronger
            if is_confluent(sup_1h, sup_4h, confluence_pct):
                base = min(1.0, base * 1.3)
            long_score = base

    short_score = 0.0
    if res_1h and not block_short:
        dist = (res_1h.price - close) / close
        if 0 < dist < approach_pct:
            base = 1.0 - dist / approach_pct
            if is_confluent(res_1h, res_4h, confluence_pct):
                base = min(1.0, base * 1.3)
            short_score = -base

    # Pick the stronger side (can't be both)
    return long_score if abs(long_score) >= abs(short_score) else short_score
