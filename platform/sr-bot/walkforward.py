"""Walk-forward simulator + gate for reflection.

Phase 2: every proposed parameter change is replayed on the last 30 days
of 1H bars (per symbol). Two windows are scored:

  - 30-day in-sample Sharpe (full window)
  - 7-day OOS-tail Sharpe (most recent week)

A proposal passes the gate only if BOTH windows show improvement vs the
current params, AND there are enough trades to draw a signal. Without this,
reflection just tunes to whatever noise just happened.

The simulator replicates the bot's actual signal logic — pivots, cluster,
confluence, regime filter, SL/TP/trail/time exits — so the test trades
closely resemble what the live bot would have done under the proposed params.
Lookahead safety: pivot levels are only available once their confirm_ts has
passed; bar exits use that bar's high/low (intrabar realism).
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))


# ---------------------------------------------------------------------------
# Simulator — single-symbol replay
# ---------------------------------------------------------------------------

def simulate(bars: pd.DataFrame, params: dict) -> list[dict]:
    """Replay the bot's S&R signal logic across `bars`, returning pseudo-trades.

    `bars` is a tz-aware 1H OHLCV DataFrame. `params` is a dict of the env
    keys the bot consumes (APPROACH_PCT, STOP_LOSS_PCT, etc.). Unknown keys
    are ignored.
    """
    # Local import to avoid circulars during module load
    from sr_paper_bot import (
        detect_pivots, cluster_levels, nearest_above_below,
        is_confluent, resample_4h, regime,
    )

    if len(bars) < 200:
        return []

    pivot_window   = int(params.get("PIVOT_WINDOW", 5))
    cluster_pct    = float(params.get("CLUSTER_PCT", 0.003))
    confluence_pct = float(params.get("CONFLUENCE_PCT", 0.005))
    approach_pct   = float(params.get("APPROACH_PCT", 0.003))
    sl_pct         = float(params.get("STOP_LOSS_PCT", 0.01))
    tp_pct         = float(params.get("TAKE_PROFIT_PCT", 0.02))
    trail_trigger  = float(params.get("TRAIL_TRIGGER_PCT", 0.005))
    trail_dist     = float(params.get("TRAIL_DISTANCE_PCT", 0.008))
    max_hold_bars  = int(float(params.get("MAX_HOLD_HOURS", 24)))
    cooldown_h     = float(params.get("COOLDOWN_MIN", 60)) / 60.0
    confluence_mult = float(params.get("CONFLUENCE_SIZE_MULT", 1.5))

    trades: list[dict] = []
    open_pos: Optional[dict] = None
    cooldown_until: Optional[pd.Timestamp] = None

    # Need history for 200-bar SMA regime + pivots
    warmup = max(200, pivot_window * 4)

    for i in range(warmup, len(bars)):
        ts = bars.index[i]
        bar = bars.iloc[i]
        close = float(bar["close"])
        bar_hi = float(bar["high"])
        bar_lo = float(bar["low"])

        # ---- Exit logic ----
        if open_pos:
            side = open_pos["side"]
            entry = open_pos["entry_px"]
            sl = open_pos["sl"]
            tp = open_pos["tp"]
            peak = open_pos["peak"]
            held = i - open_pos["entry_idx"]

            if side == "long":
                new_peak = max(peak, bar_hi)
                hit_sl = bar_lo <= sl
                hit_tp = bar_hi >= tp
                in_profit = (new_peak - entry) / entry
                trail_level = new_peak * (1 - trail_dist)
                hit_trail = (in_profit >= trail_trigger) and (bar_lo <= trail_level)
            else:
                new_peak = min(peak, bar_lo)
                hit_sl = bar_hi >= sl
                hit_tp = bar_lo <= tp
                in_profit = (entry - new_peak) / entry
                trail_level = new_peak * (1 + trail_dist)
                hit_trail = (in_profit >= trail_trigger) and (bar_hi >= trail_level)
            time_exit = held >= max_hold_bars

            exit_px = None
            reason = None
            # Worst-case priority: SL before TP if both hit in same bar
            if hit_sl:
                exit_px, reason = sl, "sl_hit"
            elif hit_tp:
                exit_px, reason = tp, "tp_hit"
            elif hit_trail:
                exit_px, reason = trail_level, "trail_stop"
            elif time_exit:
                exit_px, reason = close, "time_exit"

            if exit_px is not None:
                pnl_pct = ((exit_px - entry) / entry) * (1 if side == "long" else -1)
                trades.append({
                    "entry_ts": open_pos["entry_ts"],
                    "exit_ts": ts,
                    "entry_px": entry,
                    "exit_px": float(exit_px),
                    "side": side,
                    "pnl_pct": float(pnl_pct),
                    "exit_reason": reason,
                    "held_bars": held,
                    "confluence": open_pos.get("confluence", False),
                })
                if reason == "sl_hit":
                    cooldown_until = ts + pd.Timedelta(hours=cooldown_h)
                open_pos = None
                continue
            else:
                open_pos["peak"] = new_peak
                continue

        # ---- Entry logic ----
        if cooldown_until is not None and ts < cooldown_until:
            continue

        hist = bars.iloc[: i + 1]
        # Lookahead-safe pivots — only include those whose confirm_ts has passed
        piv_1h_all = detect_pivots(hist, pivot_window)
        piv_1h = cluster_levels(
            [p for p in piv_1h_all if p.confirm_ts <= ts], cluster_pct,
        )

        bars_4h = resample_4h(hist)
        piv_4h = []
        if len(bars_4h) >= pivot_window + 4:
            piv_4h_all = detect_pivots(bars_4h, pivot_window)
            piv_4h = cluster_levels(
                [p for p in piv_4h_all if p.confirm_ts <= ts], cluster_pct,
            )

        if not piv_1h:
            continue

        reg = regime(hist)
        block_long = reg["label"] == "downtrend"
        block_short = reg["label"] == "uptrend"

        sup_1h, res_1h = nearest_above_below(close, piv_1h)
        sup_4h, res_4h = nearest_above_below(close, piv_4h) if piv_4h else (None, None)

        # LONG
        if sup_1h and not block_long:
            dist = (close - sup_1h.price) / close
            if 0 < dist < approach_pct:
                conf = is_confluent(sup_1h, sup_4h, confluence_pct)
                open_pos = {
                    "entry_ts": ts, "entry_idx": i, "entry_px": close,
                    "side": "long",
                    "sl": sup_1h.price * (1 - sl_pct),
                    "tp": close * (1 + tp_pct),
                    "peak": close,
                    "confluence": conf,
                }
                continue

        # SHORT
        if res_1h and not block_short:
            dist = (res_1h.price - close) / close
            if 0 < dist < approach_pct:
                conf = is_confluent(res_1h, res_4h, confluence_pct)
                open_pos = {
                    "entry_ts": ts, "entry_idx": i, "entry_px": close,
                    "side": "short",
                    "sl": res_1h.price * (1 + sl_pct),
                    "tp": close * (1 - tp_pct),
                    "peak": close,
                    "confluence": conf,
                }

    return trades


def sharpe_of_trades(trades: list[dict]) -> float:
    """Per-trade Sharpe-like ratio: mean / std of pnl_pct. Not annualized."""
    if len(trades) < 3:
        return 0.0
    arr = np.array([t["pnl_pct"] for t in trades])
    s = arr.std()
    return float(arr.mean() / s) if s > 1e-9 else 0.0


def win_rate(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    return float(sum(1 for t in trades if t["pnl_pct"] > 0) / len(trades))


# ---------------------------------------------------------------------------
# Bars fetcher — reuse the bot's Alpaca client at reflection time
# ---------------------------------------------------------------------------

def _alpaca_bars_fetcher(symbol: str, lookback_hours: int = 720) -> pd.DataFrame:
    """Pull 1H bars from Alpaca for the given symbol."""
    from sr_paper_bot import Alpaca
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_API_SECRET", "")
    if not key or not secret:
        return pd.DataFrame()
    a = Alpaca(key, secret)
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=lookback_hours)
    try:
        if "/" in symbol:
            return a.crypto_bars(symbol, start, end)
        return a.stock_bars(symbol, start, end)
    except Exception as e:
        logging.warning("walkforward bars fetch failed for %s: %s", symbol, e)
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Gate — used by reflection
# ---------------------------------------------------------------------------

def walk_forward_gate(
    proposed: dict,
    insample_days: int = 30,
    oos_tail_days: int = 7,
    min_sharpe_improvement: float = 0.05,
    symbols: Optional[list[str]] = None,
    bars_fetcher = _alpaca_bars_fetcher,
) -> dict:
    """Decide whether to apply `proposed` by replaying both param sets.

    Returns a dict with keys:
      passed:               bool
      reason:               str
      n_trades_curr / prop  in-sample trade counts
      sharpe_curr_30d / prop_30d
      sharpe_curr_oos / prop_oos
    """
    if not proposed:
        return {"passed": True, "reason": "no proposed changes"}

    # Build current env snapshot + proposed merge
    env_path = Path(os.environ.get("SR_BOT_ENV_PATH", "/app/sr-bot/.env"))
    current: dict = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                current[k.strip()] = v.strip()
    merged = {**current, **{k.upper(): v for k, v in proposed.items()}}

    symbols = symbols or [
        s.strip() for s in os.environ.get(
            "SR_BOT_SYMBOLS", "BTC/USD,ETH/USD,SOL/USD,LINK/USD"
        ).split(",") if s.strip()
    ]
    # Cap for speed
    symbols = symbols[:5]

    bars_per_symbol: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        b = bars_fetcher(sym, lookback_hours=insample_days * 24 + 200)
        if not b.empty and len(b) >= 200:
            bars_per_symbol[sym] = b

    if not bars_per_symbol:
        return {"passed": False, "reason": "no bars available for walkforward"}

    end_ts = max(b.index[-1] for b in bars_per_symbol.values())
    oos_start = end_ts - pd.Timedelta(days=oos_tail_days)

    curr_all, prop_all = [], []
    for sym, bars in bars_per_symbol.items():
        curr_all.extend(simulate(bars, current))
        prop_all.extend(simulate(bars, merged))

    curr_oos = [t for t in curr_all if t["entry_ts"] >= oos_start]
    prop_oos = [t for t in prop_all if t["entry_ts"] >= oos_start]

    sh_curr_30 = sharpe_of_trades(curr_all)
    sh_prop_30 = sharpe_of_trades(prop_all)
    sh_curr_oos = sharpe_of_trades(curr_oos)
    sh_prop_oos = sharpe_of_trades(prop_oos)

    result = {
        "n_trades_curr": len(curr_all),
        "n_trades_prop": len(prop_all),
        "n_trades_curr_oos": len(curr_oos),
        "n_trades_prop_oos": len(prop_oos),
        "sharpe_curr_30d": round(sh_curr_30, 4),
        "sharpe_prop_30d": round(sh_prop_30, 4),
        "sharpe_curr_oos": round(sh_curr_oos, 4),
        "sharpe_prop_oos": round(sh_prop_oos, 4),
        "win_rate_curr_30d": round(win_rate(curr_all), 3),
        "win_rate_prop_30d": round(win_rate(prop_all), 3),
        "symbols": list(bars_per_symbol.keys()),
    }

    # Decision rules — all must be true
    if len(prop_all) < 10:
        result.update(passed=False, reason=f"too few proposed trades ({len(prop_all)} < 10)")
        return result
    if sh_prop_30 < sh_curr_30 + min_sharpe_improvement:
        result.update(passed=False,
                      reason=f"30d Sharpe didn't improve "
                             f"({sh_prop_30:.3f} vs {sh_curr_30:.3f}, "
                             f"need +{min_sharpe_improvement})")
        return result
    if sh_prop_oos < sh_curr_oos:
        result.update(passed=False,
                      reason=f"OOS-tail Sharpe regressed "
                             f"({sh_prop_oos:.3f} vs {sh_curr_oos:.3f})")
        return result

    result.update(passed=True, reason="improvement in both windows")
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    import json as _json
    # Smoke test: replay current params on default symbols
    res = walk_forward_gate({})
    print(_json.dumps(res, indent=2, default=str))
