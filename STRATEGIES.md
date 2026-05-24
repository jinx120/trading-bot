# Strategies — Cookbook

How to read the existing strategies in `platform/strategies/` and how to add your own. Keep this open alongside any of the existing strategy files (e.g., `atr_breakout.py`) — most patterns become obvious by example.

The platform's gate, walk-forward, and DSR machinery are doing the hard work. Your strategy just needs to emit signals correctly and deterministically.

---

## What a strategy IS

A subclass of `Strategy` (in `platform/strategies/base.py`) with two required class attributes and two required methods:

```python
class MyStrategy(Strategy):
    name = "my_strategy"          # unique identifier; used in DB rows + gate
    timeframe = "1day"            # '1min', '5min', '15min', '1hour', '1day'

    def _validate_params(self, params: dict) -> dict:
        """Fill defaults, raise ValueError on bad input, return final params."""

    def generate_signals(self, bars: pd.DataFrame, symbol: str) -> list[Signal]:
        """Bars in, list of Signals out. Pure function. No randomness."""
```

That's it. The framework calls `generate_signals` and the rest of the pipeline (gate, walk-forward, live runner) handles persistence, position management, stops, and divergence monitoring.

---

## The two invariants

**1. Pure function.** Same `(bars, params)` → same signals every time. No `datetime.now()`, no `random.random()`, no reading external state. The gate hashes `(name, params)` at promotion time; if your code isn't deterministic, the hash check is meaningless.

**2. No lookahead.** A signal at bar `t` may only depend on data from `bars[:t+1]` — i.e., up to and including bar `t`'s close. You don't get to peek at bar `t+1`. The standard pattern: compute indicators on the full series (vectorized), then `.shift(1)` them before comparing against the current bar.

```python
rsi_now = rsi(bars["close"], period=14)
rsi_prev = rsi_now.shift(1)
# At bar t, rsi_prev[t] is the RSI computed with data through bar t-1.
# Decide today's action from THAT — not rsi_now[t].
```

This is the trick that prevents you from accidentally building a strategy that "sees the future." If you skip the shift, your backtest will look incredible (and your live performance will be a disaster).

The platform has a built-in lookahead audit you can run on any strategy:

```python
full = strat.generate_signals(bars, symbol)
half = strat.generate_signals(bars.iloc[:len(bars)//2], symbol)
# Signals from the first half should match the prefix of full-window signals.
# If they don't, you have lookahead.
```

This is in the chunk-2 smoke test in this repo's history.

---

## Anatomy of a Signal

```python
@dataclass
class Signal:
    ts: pd.Timestamp           # the bar at which the signal fires
    symbol: str                # e.g. "BTC/USD"
    side: Literal["long", "short", "flat"]
    strength: float = 1.0      # optional: confidence in [0, 1]
    snapshot: dict = ...       # JSON-able dict of indicator values
```

`side` semantics in this platform:
- `"long"` = enter or maintain a long position. The runner places a buy order if no current position.
- `"flat"` = exit any current position. Runner closes the position.
- `"short"` = ignored for now (long-only platform).

The runner uses signal events (not boolean entry/exit arrays). Emit a `"long"` when you want to enter, a `"flat"` when you want to exit. The runner handles state — but YOUR code should track an internal `in_position` flag so you don't emit double-entries:

```python
in_position = False
for ts, row in bars.iterrows():
    ...
    if not in_position and entry_condition:
        signals.append(Signal(ts=ts, symbol=symbol, side="long", ...))
        in_position = True
    elif in_position and exit_condition:
        signals.append(Signal(ts=ts, symbol=symbol, side="flat", ...))
        in_position = False
```

The `snapshot` dict is critical. It lands in `signals.snapshot` (JSONB) and is what you'll use later to debug why a trade fired. Include the indicator values that drove the decision.

---

## Stops and targets

Don't compute stop-loss inside your strategy. Declare it as a param; the backtester applies it:

```python
defaults = {
    ...,
    "stop_loss_pct": 0.05,    # 5% below entry — backtester applies
    "take_profit_pct": None,
}
```

The backtester reads `strategy.stop_loss_pct()` (helper in the base class) and passes it to vectorbt's `sl_stop`. Same for `take_profit_pct` → `tp_stop`. Strategies stay focused on signal math; risk machinery is uniform across all of them.

---

## Worked example — RSI mean-reversion gated by 200-SMA regime

We'll write a strategy that combines two ideas: only buy oversold conditions when the long-term trend is up. The intuition is that mean-reversion edge is real in uptrends but kills you in downtrends (you keep catching falling knives).

**File: `platform/strategies/rsi_with_regime.py`**

```python
"""RSI mean-reversion gated by 200-SMA regime filter.

Logic (long-only):
  - PRECONDITION: close > SMA(200)  (in uptrend regime)
  - ENTER long when RSI(14) crosses below 30 (oversold) AND precondition holds.
  - EXIT (flat) when RSI rises above 50 (back to neutral).

Theory: mean-reversion strategies fail catastrophically in downtrends. The
200-SMA regime gate restricts entries to bull/sideways markets where
oversold dips tend to recover.
"""
from __future__ import annotations

import pandas as pd

from common.indicators import rsi, sma

from .base import Signal, Strategy


class RSIWithRegime(Strategy):
    name = "rsi_with_regime"
    timeframe = "1day"

    def _validate_params(self, params: dict) -> dict:
        defaults = {
            "rsi_period": 14,
            "oversold": 30.0,
            "exit_threshold": 50.0,
            "regime_sma": 200,
            "stop_loss_pct": 0.05,
        }
        out = {**defaults, **params}
        if not (1 <= out["rsi_period"] <= 200):
            raise ValueError("rsi_period out of range")
        if not (0 < out["oversold"] < out["exit_threshold"] < 100):
            raise ValueError("need oversold < exit_threshold")
        if not (5 <= out["regime_sma"] <= 1000):
            raise ValueError("regime_sma out of range")
        if out["stop_loss_pct"] is not None and not (0 < out["stop_loss_pct"] < 1):
            raise ValueError("stop_loss_pct must be in (0, 1) or None")
        return out

    def generate_signals(self, bars: pd.DataFrame, symbol: str) -> list[Signal]:
        if bars.empty:
            return []
        p = self.params

        # Indicators on the FULL series, vectorized.
        rsi_now = rsi(bars["close"], period=p["rsi_period"])
        sma_now = sma(bars["close"], period=p["regime_sma"])

        # Lookahead guards — shift by 1 so we use yesterday's values for today.
        rsi_prev = rsi_now.shift(1)
        rsi_prev2 = rsi_now.shift(2)
        sma_prev = sma_now.shift(1)
        close_prev = bars["close"].shift(1)

        # Detect cross-below for entry.
        crossed_below = (rsi_prev2 >= p["oversold"]) & (rsi_prev < p["oversold"])
        # Detect cross-above for exit.
        crossed_above = (rsi_prev2 <= p["exit_threshold"]) & (rsi_prev > p["exit_threshold"])
        # Regime gate.
        in_regime = close_prev > sma_prev

        signals: list[Signal] = []
        in_position = False
        for ts, row in bars.iterrows():
            r_prev = rsi_prev.loc[ts]
            s_prev = sma_prev.loc[ts]
            if pd.isna(r_prev) or pd.isna(s_prev):
                continue   # warm-up

            if (not in_position
                    and crossed_below.loc[ts]
                    and bool(in_regime.loc[ts])):
                signals.append(Signal(
                    ts=ts, symbol=symbol, side="long", strength=1.0,
                    snapshot={
                        "rsi_prev": float(r_prev),
                        "sma200_prev": float(s_prev),
                        "close": float(row["close"]),
                        "regime": "up",
                    },
                ))
                in_position = True
            elif in_position and crossed_above.loc[ts]:
                signals.append(Signal(
                    ts=ts, symbol=symbol, side="flat", strength=1.0,
                    snapshot={
                        "rsi_prev": float(r_prev),
                        "close": float(row["close"]),
                        "exit_reason": "rsi_normalized",
                    },
                ))
                in_position = False

        return signals
```

**Config: `platform/strategies/configs/rsi_with_regime.yaml`**

```yaml
name: rsi_with_regime
description: |
  RSI(14) mean-reversion gated by 200-SMA regime filter. Long-only.
  Only enters oversold dips during uptrends.

universe:
  - BTC/USD
  - ETH/USD
  - SOL/USD

params:
  rsi_period: 14
  oversold: 30
  exit_threshold: 50
  regime_sma: 200
  stop_loss_pct: 0.05
```

**Register: `platform/live/runner.py`**

Add to imports:

```python
from strategies.rsi_with_regime import RSIWithRegime
```

Add to `STRATEGY_REGISTRY`:

```python
"rsi_with_regime": RSIWithRegime,
```

**Test it:**

```bash
docker compose restart admin platform
docker compose exec platform python -c "
import sys; sys.path.insert(0, '/app')
from common.db import load_bars
from strategies.rsi_with_regime import RSIWithRegime
strat = RSIWithRegime.from_yaml('/app/strategies/configs/rsi_with_regime.yaml')
sigs = strat.generate_signals(load_bars('BTC/USD', '1day'), 'BTC/USD')
print(f'{len(sigs)} signals')
for s in sigs[:5]:
    print(' ', s.ts.date(), s.side, s.snapshot)
"
```

**Backtest + walk-forward + gate:**

Use the Streamlit Backtest Launcher OR:

```bash
docker compose exec platform python /app/research/promote_zoo.py \
    --strategies rsi_with_regime --apply
```

If it passes the research gate, it ends up enabled. The runner will pick it up next tick.

---

## Common patterns

### Indicator + threshold (simplest)
```python
ind_prev = indicator(bars["close"], period=14).shift(1)
if not in_position and ind_prev.loc[ts] < threshold:
    # enter long
```
RSI mean-rev, z-score reversion, regime filter all follow this shape.

### Crossover (state change)
```python
fast_prev = fast.shift(1); fast_prev2 = fast.shift(2)
slow_prev = slow.shift(1); slow_prev2 = slow.shift(2)
crossed_up = (fast_prev2 <= slow_prev2) & (fast_prev > slow_prev)
crossed_down = (fast_prev2 >= slow_prev2) & (fast_prev < slow_prev)
```
MA crossover, MACD signal.

### Multi-condition AND
```python
condition_a = (close_prev > sma_prev)
condition_b = (rsi_prev > 50)
favorable = condition_a & condition_b
```
Combine arbitrarily many filters with `&` and `|`.

### Path-dependent (squeeze, trail-stop)
```python
streak = 0
for ts, row in bars.iterrows():
    if condition:
        streak += 1
    else:
        streak = 0
    # use streak in your decision
```
Bollinger squeeze, ATR trail-stop tracking.

---

## Indicators available

In `platform/common/indicators.py`:

| Function | Returns | Notes |
|---|---|---|
| `rsi(close, period=14)` | Series | Wilder smoothing |
| `atr(high, low, close, period=14)` | Series | Wilder smoothing |
| `donchian(high, low, period=20)` | (upper, lower, mid) | Range channel |
| `sma(series, period)` | Series | Simple moving avg |
| `ema(series, period)` | Series | Standard EMA, α=2/(p+1) |
| `bollinger(close, period=20, n_std=2.0)` | (upper, mid, lower) | |
| `macd(close, fast=12, slow=26, signal=9)` | (macd_line, signal_line, hist) | |
| `zscore(series, period=20)` | Series | Rolling normalization |

If you need something else (Ichimoku, Heiken Ashi, Keltner, Stoch, etc.), add it to `indicators.py` first. Keep new indicators pure-numpy/pandas, no TA-Lib.

---

## What kills strategy authors

In rough order of frequency:

1. **Lookahead bias.** Forgot to `.shift(1)`. Backtest looks great; live is a disaster.
2. **Selection bias.** "I tried 50 configs and the best had Sharpe 2.0!" — that's expected under H0. Use parameter sweep with `n_trials` set correctly.
3. **In-sample optimization without OOS.** Skipping walk-forward. Use `walk_forward()` from `research/walkforward.py`.
4. **Surviving fees.** Strategy with 100 trades/year and 0.5% per round-trip nets you -50% on fees alone if your edge is <50 bps per trade. Test with realistic fees.
5. **Regime sensitivity.** Strategy works in 2023 (bull) but not 2022 (bear). Walk-forward fold-stability surfaces this.
6. **Confirmation bias on params.** "If I just tweak the threshold to 28 instead of 30, it works." Yes, on this dataset. Re-tune means re-evaluate.

The gate exists to catch you on each of these.

---

## When you're done

1. Run via `python /app/research/promote_zoo.py --strategies my_strategy --apply`.
2. Check the Backtests page in Streamlit and the Grafana leaderboard.
3. If passed, the runner picks it up next tick — see Live Signals page in Streamlit.
4. Watch for first paper trades. Watch the divergence monitor.

If the gate rejected your strategy: don't tweak params to make it pass. That's the failure mode the gate exists to prevent. Move on.
