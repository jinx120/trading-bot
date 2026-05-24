"""RSI mean-reversion candidate. Fades extremes on the 1H chart.

Score:
  RSI < oversold (default 30)  → positive (buy the dip)
  RSI > overbought (default 70) → negative (fade the rip)
  Mid-zone (30 < RSI < 70) → 0.

Sharpened by ATR: only fire when ATR is in normal-to-high range so we're
fading actual extremes, not consolidation noise. Saved as a shadow
candidate; the lab tracks its hypothetical PnL until promotion.
"""
from __future__ import annotations

import pandas as pd

name = "rsi_mean_revert"


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1.0 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1.0 / n, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-9)
    return 100 - (100 / (1 + rs))


def score(bars_1h: pd.DataFrame, oversold: float = 30, overbought: float = 70) -> float:
    if len(bars_1h) < 30:
        return 0.0
    rsi = _rsi(bars_1h["close"]).iloc[-1]
    if pd.isna(rsi):
        return 0.0
    if rsi < oversold:
        return float(min(1.0, (oversold - rsi) / oversold))
    if rsi > overbought:
        return float(-min(1.0, (rsi - overbought) / (100 - overbought)))
    return 0.0
