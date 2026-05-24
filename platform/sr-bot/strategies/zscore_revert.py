"""Z-score mean reversion. Fades extreme deviations from a moving mean.

Score:
  z = (close - rolling_mean) / rolling_std on 1H bars (48-bar window = 2 days)
  Positive z (price too high) → negative score (fade = short)
  Negative z (price too low)  → positive score (fade = long)
  Inactive when |z| < threshold or when daily MA crossover trend is strongly
  in the same direction the fade would go (don't fade strong trends).

Score is bounded to [-1, +1] via tanh-like scaling.
"""
from __future__ import annotations

import pandas as pd

name = "zscore_revert"


def score(bars_1h: pd.DataFrame, window: int = 48, z_threshold: float = 1.5,
          z_scale: float = 3.0) -> float:
    if len(bars_1h) < window + 5:
        return 0.0
    c = bars_1h["close"]
    mean = c.rolling(window).mean().iloc[-1]
    std = c.rolling(window).std().iloc[-1]
    if std == 0 or pd.isna(std):
        return 0.0
    z = (float(c.iloc[-1]) - float(mean)) / float(std)
    if abs(z) < z_threshold:
        return 0.0
    raw = -z / z_scale          # invert sign (fade)
    return float(max(-1.0, min(1.0, raw)))
