"""Dynamic position sizing.

Phase 1 — vol-targeted (Carver-style) sizing with drawdown throttle and
portfolio-level risk cap.

Each trade risks a fixed FRACTION of equity ("target risk per trade") rather
than a fixed NOTIONAL. Result: a 10%-ATR symbol gets a smaller notional than
a 1%-ATR symbol such that their dollar-risk is equal.

Then size is scaled by:
  - dd_throttle      shrinks size linearly as drawdown deepens
  - performance_mult shrinks size on losing streaks, grows on winners
  - confluence_mult  legacy 1.5x boost when 4H+1H levels coincide
                     (will be deprecated in Phase 4 ensemble)

Final size is capped at MAX_PORTFOLIO_RISK_PCT of equity (combined open risk
across all positions) and clamped to [MIN_NOTIONAL, equity * MAX_CONCENTRATION].
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras


# Defaults — overridable via env (.env hot-reload picks them up).
TARGET_RISK_PER_TRADE_PCT = 0.005   # 0.5% of equity risked per trade
MAX_PORTFOLIO_RISK_PCT    = 0.02    # 2% of equity max combined open risk
DD_THROTTLE_FLOOR         = 0.20    # never size below this fraction of base
DD_THROTTLE_SLOPE         = 4.0     # at 5% dd -> 0.8x; at 10% -> 0.6x


def _db_conn():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", "5432")),
        user=os.environ.get("DB_USER", "trader"),
        password=os.environ.get("DB_PASSWORD") or os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ.get("DB_NAME", "trading"),
    )


def atr_pct(bars_1h: pd.DataFrame, window: int = 14) -> float:
    """Average True Range / price. Used to size relative to volatility."""
    if len(bars_1h) < window + 1:
        return 0.01
    high, low, c = bars_1h["high"], bars_1h["low"], bars_1h["close"]
    tr = pd.concat([
        (high - low),
        (high - c.shift()).abs(),
        (low - c.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / window, adjust=False).mean().iloc[-1]
    px = float(c.iloc[-1])
    return float(atr) / px if px > 0 else 0.01


def recent_performance_multiplier(
    lookback_n: int = 10,
    boost_per_win: float = 0.05,
    cut_per_loss: float = 0.10,
    floor: float = 0.3,
    ceiling: float = 1.6,
) -> float:
    """Confidence-weighted sizing.

    Look at the last N closed trades. Each winner increases size by
    boost_per_win, each loser decreases by cut_per_loss. Result clamped to
    [floor, ceiling]. After a 5-loss streak, size at ~0.6×. After a 5-win
    streak, size at ~1.25×.

    This is intentionally asymmetric (losses hurt more than wins help) —
    capital preservation > FOMO.
    """
    try:
        with _db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT pnl_pct FROM trades
                WHERE strategy = 'sr_paper_bot' AND exit_ts IS NOT NULL
                ORDER BY exit_ts DESC LIMIT %s
            """, (lookback_n,))
            rows = cur.fetchall()
    except Exception as e:
        logging.debug("recent_performance_multiplier: db read failed: %s", e)
        return 1.0
    if not rows:
        return 1.0
    mult = 1.0
    for r in rows:
        p = r["pnl_pct"]
        if p is None:
            continue
        if p > 0:
            mult += boost_per_win
        else:
            mult -= cut_per_loss
    return float(max(floor, min(ceiling, mult)))


def dd_throttle(current_equity: float, peak_equity: float,
                floor: Optional[float] = None,
                slope: Optional[float] = None) -> float:
    """Linear size reduction as drawdown deepens. Returns multiplier in [floor, 1.0].

    At 0% dd  -> 1.0x
    At 5% dd  -> 1 - slope*0.05 (default 0.8x)
    At 10% dd -> default 0.6x
    Clamped to `floor` so the bot still places (small) trades even in deep dd.
    Hard lockdown is a separate mechanism in risk_engine.
    """
    if peak_equity <= 0:
        return 1.0
    if floor is None:
        floor = float(os.environ.get("DD_THROTTLE_FLOOR", DD_THROTTLE_FLOOR))
    if slope is None:
        slope = float(os.environ.get("DD_THROTTLE_SLOPE", DD_THROTTLE_SLOPE))
    dd = max(0.0, 1.0 - current_equity / peak_equity)
    return max(floor, 1.0 - slope * dd)


def fetch_peak_equity() -> float:
    """Read peak equity from risk_state. Returns 0 if not set."""
    try:
        with _db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT peak_equity FROM risk_state WHERE id = 1")
            row = cur.fetchone()
            return float(row[0]) if row and row[0] else 0.0
    except Exception:
        return 0.0


def sum_open_position_risk(equity: float) -> float:
    """Estimate total open dollar-risk across active bot trades, as a fraction of equity.

    For each open bot trade, risk = quantity * |entry_price - sl|. Returns
    sum / equity. Used to enforce MAX_PORTFOLIO_RISK_PCT.
    """
    if equity <= 0:
        return 0.0
    try:
        with _db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT entry_price, quantity, (metadata->>'sl')::float AS sl
                FROM trades
                WHERE strategy = 'sr_paper_bot' AND exit_ts IS NULL
            """)
            total = 0.0
            for entry, qty, sl in cur.fetchall():
                if sl is None or entry is None or qty is None:
                    continue
                total += abs(float(entry) - float(sl)) * float(qty)
            return total / equity
    except Exception:
        return 0.0


def compute_notional(
    equity: float,
    bars_1h: pd.DataFrame,
    stop_distance_pct: float,
    confluence_mult: float = 1.0,
    side: str = "buy",
    target_risk_per_trade_pct: Optional[float] = None,
    max_portfolio_risk_pct: Optional[float] = None,
) -> tuple[float, dict]:
    """Vol-targeted Carver sizing.

      dollar_risk_per_trade = equity * target_risk_per_trade_pct
      notional              = dollar_risk_per_trade / stop_distance_pct
      notional             *= dd_throttle * perf_mult * confluence_mult

    Then enforce portfolio-level cap: if adding this trade's risk would push
    sum-of-open-risk over MAX_PORTFOLIO_RISK_PCT of equity, shrink accordingly.

    Returns (notional, breakdown). breakdown is logged into signals + trades
    metadata for transparency.
    """
    if target_risk_per_trade_pct is None:
        target_risk_per_trade_pct = float(os.environ.get(
            "TARGET_RISK_PER_TRADE_PCT", TARGET_RISK_PER_TRADE_PCT))
    if max_portfolio_risk_pct is None:
        max_portfolio_risk_pct = float(os.environ.get(
            "MAX_PORTFOLIO_RISK_PCT", MAX_PORTFOLIO_RISK_PCT))

    stop_distance_pct = max(0.0005, stop_distance_pct)
    a = atr_pct(bars_1h)
    peak = fetch_peak_equity() or equity
    dd_mult = dd_throttle(equity, peak)
    perf_mult = recent_performance_multiplier()

    dollar_risk = equity * target_risk_per_trade_pct
    notional = dollar_risk / stop_distance_pct
    notional *= dd_mult * perf_mult * confluence_mult

    # Portfolio risk cap: leave headroom for this trade within MAX_PORTFOLIO_RISK_PCT.
    open_risk_pct = sum_open_position_risk(equity)
    remaining_pct = max_portfolio_risk_pct - open_risk_pct
    capped_by_portfolio = False
    if remaining_pct <= 0:
        # No headroom at all — refuse to add risk.
        notional = 0.0
        capped_by_portfolio = True
    else:
        # Make sure this trade's own risk fits the remainder.
        max_notional_for_remaining = remaining_pct * equity / stop_distance_pct
        if notional > max_notional_for_remaining:
            notional = max_notional_for_remaining
            capped_by_portfolio = True

    min_notional = float(os.environ.get("MIN_NOTIONAL", "20"))
    if 0 < notional < min_notional:
        notional = 0.0  # don't place dust trades; better to skip
    elif notional >= min_notional:
        notional = max(min_notional, notional)

    breakdown = {
        "equity": round(equity, 2),
        "peak_equity": round(peak, 2),
        "stop_distance_pct": round(stop_distance_pct, 5),
        "atr_pct": round(a, 5),
        "dollar_risk_per_trade": round(dollar_risk, 4),
        "dd_mult": round(dd_mult, 3),
        "perf_mult": round(perf_mult, 3),
        "conf_mult": round(confluence_mult, 3),
        "open_risk_pct_before": round(open_risk_pct, 4),
        "remaining_risk_pct": round(max(0.0, remaining_pct), 4),
        "capped_by_portfolio": capped_by_portfolio,
        "result": round(notional, 2),
    }
    return notional, breakdown
