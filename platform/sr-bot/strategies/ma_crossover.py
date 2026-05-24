"""Slow EMA crossover regime indicator. Cheap and robust.

Score:
  sign(EMA_fast - EMA_slow) × min(1, |slope_fast| × scale)
  Slope is the 5-day change of EMA_fast as a fraction of EMA_fast.

Long bias when fast EMA above slow EMA AND trending up; symmetric for shorts.
"""
from __future__ import annotations

import pandas as pd

name = "ma_crossover"


def score(bars_1d: pd.DataFrame, fast: int = 50, slow: int = 200,
          slope_scale: float = 1000.0) -> float:
    if len(bars_1d) < slow + 10:
        return 0.0
    c = bars_1d["close"]
    fast_ema = c.ewm(span=fast, adjust=False).mean()
    slow_ema = c.ewm(span=slow, adjust=False).mean()
    diff = fast_ema.iloc[-1] - slow_ema.iloc[-1]
    sign = 1 if diff > 0 else -1
    slope = fast_ema.diff(5).iloc[-1] / fast_ema.iloc[-1] if fast_ema.iloc[-1] else 0.0
    return float(sign * min(1.0, abs(slope) * slope_scale))
