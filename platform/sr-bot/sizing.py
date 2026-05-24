"""Dynamic position sizing.

base_notional × performance_multiplier × volatility_multiplier × confluence_mult

Caps and floors enforced so a hot streak can't blow up sizing.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras


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


def compute_notional(
    base_notional: float,
    bars_1h: pd.DataFrame,
    confluence_mult: float,
    target_atr_pct: float = 0.01,
    atr_floor: float = 0.4,
    atr_ceiling: float = 1.5,
) -> tuple[float, dict]:
    """Return (notional, breakdown_dict).

    Order of multiplication matters only for logging; final value is the
    product, then clamped at MIN_NOTIONAL.
    """
    a = atr_pct(bars_1h)
    if a > 0:
        vol_mult = max(atr_floor, min(atr_ceiling, target_atr_pct / a))
    else:
        vol_mult = 1.0
    perf_mult = recent_performance_multiplier()
    notional = base_notional * vol_mult * perf_mult * confluence_mult
    min_notional = float(os.environ.get("MIN_NOTIONAL", "20"))
    notional = max(min_notional, notional)
    breakdown = {
        "base": base_notional,
        "vol_mult": round(vol_mult, 3),
        "perf_mult": round(perf_mult, 3),
        "conf_mult": round(confluence_mult, 3),
        "atr_pct": round(a, 5),
        "result": round(notional, 2),
    }
    return notional, breakdown
