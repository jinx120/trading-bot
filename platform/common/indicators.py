"""Pure pandas/numpy indicators.

No TA-Lib dependency — TA-Lib's C build is a recurring source of Docker pain
and we only need a handful of indicators. Each function takes a 1D pandas
Series (or several, for multi-input indicators) and returns a Series of the
same length, NaN-padded at the head where there isn't enough history.

Convention: the returned Series at index t is computed using bars[:t+1]
(i.e. up to AND INCLUDING bar t's close). To use these without lookahead in
a strategy, .shift(1) the indicator before comparing against the current
bar — see strategies/rsi_meanrev.py for the pattern.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index, Wilder's smoothing.

    RSI = 100 - 100 / (1 + RS), where RS = avg_gain / avg_loss.

    Wilder's "smoothing" is an EMA with alpha = 1/period — NOT 2/(period+1)
    like a "standard" EMA. Most charting platforms (TradingView, MT4) call
    Wilder's version "RSI"; pandas' default ewm() uses the standard alpha,
    so we pass alpha explicitly.
    """
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    # Avoid div-by-zero when there are no losses in the window: that's RSI=100.
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    out = out.where(avg_loss != 0, 100)
    # Both gain and loss exactly zero (perfectly flat) → conventionally 50.
    out = out.where(~((avg_gain == 0) & (avg_loss == 0)), 50)
    return out


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Average True Range (Wilder).

    True range at bar t = max of:
      - high[t] - low[t]                  (intraday range)
      - |high[t] - close[t-1]|            (gap up)
      - |low[t] - close[t-1]|             (gap down)
    ATR is the Wilder-smoothed mean over `period` bars.
    """
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def donchian(
    high: pd.Series,
    low: pd.Series,
    period: int = 20,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Donchian channel: rolling N-bar high, low, midpoint.

    NOTE: the channel at bar t INCLUDES bar t itself (rolling window ends at t).
    For breakout strategies you want the PRIOR-N-bar high — apply .shift(1)
    on the strategy side. (Otherwise you compare today's close against a
    range that already includes today's high → trivially in-range, no signal.)
    """
    upper = high.rolling(period).max()
    lower = low.rolling(period).min()
    mid = (upper + lower) / 2
    return upper, lower, mid


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Standard exponential moving average (alpha = 2/(period+1)).

    Use this when you want a "trend filter" EMA. For RSI/ATR-style smoothing
    use Wilder's variant directly via .ewm(alpha=1/period, adjust=False).
    """
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def bollinger(
    close: pd.Series,
    period: int = 20,
    n_std: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands.

    Returns (upper, mid, lower):
      mid   = SMA(close, period)
      upper = mid + n_std * rolling_std(close, period)
      lower = mid - n_std * rolling_std(close, period)

    Standard parameters are (20, 2.0). The "bandwidth" — (upper - lower)/mid —
    is the volatility metric; squeeze strategies look for low bandwidth then
    enter on subsequent expansion.
    """
    mid = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    upper = mid + n_std * std
    lower = mid - n_std * std
    return upper, mid, lower


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD (Moving Average Convergence Divergence).

    Returns (macd_line, signal_line, histogram):
      macd_line   = EMA(close, fast) - EMA(close, slow)
      signal_line = EMA(macd_line, signal)
      histogram   = macd_line - signal_line

    Standard parameters (12, 26, 9). MACD line crossing ABOVE signal line is
    a bullish trigger; crossing BELOW is bearish.
    """
    ema_fast = close.ewm(span=fast, adjust=False, min_periods=fast).mean()
    ema_slow = close.ewm(span=slow, adjust=False, min_periods=slow).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def zscore(series: pd.Series, period: int = 20) -> pd.Series:
    """Rolling z-score: (x - rolling_mean) / rolling_std.

    Useful as a normalized "how unusual is this value vs recent history"
    metric. Mean-reversion entries fire when z < -2 (cheap relative to recent
    range); exits fire when z returns to 0 (back to mean).

    NaN until the rolling window is filled. Returns 0 when std is exactly 0
    (no movement) to avoid divide-by-zero.
    """
    mean = series.rolling(period).mean()
    std = series.rolling(period).std(ddof=0)
    z = (series - mean) / std.replace(0, np.nan)
    return z
