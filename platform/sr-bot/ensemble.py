"""Ensemble voting across sub-strategies.

Each tick the bot collects scores in [-1, +1] from every enabled strategy.
The composite = weighted sum × per-strategy weights. The bot trades only
when |composite| ≥ ENTRY_THRESHOLD AND no veto fires. SL/TP are derived
from the dominant contributor's logic; ATR-based fallback otherwise.

Weights live in `strategy_weights` table (Phase 4 ships them as equal).
Phase 6 will let reflection adjust weights with floor/ceiling.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras


ENTRY_THRESHOLD = 0.40        # |composite| must clear this to enter
MIN_WEIGHT      = 0.05        # floor per enabled strategy
MAX_WEIGHT      = 0.50        # ceiling per enabled strategy


def _db_conn():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", "5432")),
        user=os.environ.get("DB_USER", "trader"),
        password=os.environ.get("DB_PASSWORD") or os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ.get("DB_NAME", "trading"),
    )


@dataclass
class EnsembleDecision:
    composite: float
    side: Optional[str]              # "buy", "sell", or None
    breakdown: dict                  # per-strategy score + weight + contribution
    dominant: Optional[str]          # strategy name with largest |contribution|
    confidence: float                # in [0, 1] — measures agreement
    entered: bool                    # did it clear ENTRY_THRESHOLD?
    reason: Optional[str] = None     # why we didn't enter, if applicable


def fetch_weights() -> dict[str, float]:
    """Return {strategy_name: weight} for enabled strategies, normalized to sum 1."""
    try:
        with _db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT name, weight FROM strategy_weights WHERE enabled = TRUE")
            rows = cur.fetchall()
    except Exception as e:
        logging.warning("fetch_weights failed: %s", e)
        return {"sr_bounce": 0.34, "donchian_trend": 0.33, "ma_crossover": 0.33}
    if not rows:
        return {"sr_bounce": 0.34, "donchian_trend": 0.33, "ma_crossover": 0.33}
    total = sum(float(r["weight"]) for r in rows) or 1.0
    return {r["name"]: float(r["weight"]) / total for r in rows}


def decide(scores: dict[str, float], threshold: Optional[float] = None) -> EnsembleDecision:
    """Combine sub-strategy scores into a single trade decision.

    scores: {strategy_name: score in [-1, 1]}.
    Returns EnsembleDecision with composite, side, breakdown, and entry flag.
    """
    if threshold is None:
        threshold = float(os.environ.get("ENSEMBLE_ENTRY_THRESHOLD", ENTRY_THRESHOLD))

    weights = fetch_weights()
    breakdown: dict = {}
    composite = 0.0
    sum_abs = 0.0
    dominant_name: Optional[str] = None
    dominant_abs = 0.0
    for name, score in scores.items():
        w = weights.get(name, 0.0)
        contrib = score * w
        breakdown[name] = {
            "score": round(score, 4),
            "weight": round(w, 4),
            "contribution": round(contrib, 4),
        }
        composite += contrib
        sum_abs += abs(score) * w
        if abs(contrib) > dominant_abs:
            dominant_abs = abs(contrib)
            dominant_name = name

    # Confidence = |composite| / sum_abs_weighted_scores. 1.0 = full agreement,
    # 0.0 = full disagreement.
    confidence = abs(composite) / sum_abs if sum_abs > 1e-9 else 0.0

    if abs(composite) < threshold:
        return EnsembleDecision(
            composite=round(composite, 4),
            side=None,
            breakdown=breakdown,
            dominant=dominant_name,
            confidence=round(confidence, 3),
            entered=False,
            reason=f"composite {composite:+.3f} below threshold ±{threshold}",
        )

    return EnsembleDecision(
        composite=round(composite, 4),
        side="buy" if composite > 0 else "sell",
        breakdown=breakdown,
        dominant=dominant_name,
        confidence=round(confidence, 3),
        entered=True,
        reason=None,
    )


def atr_pct(bars: pd.DataFrame, window: int = 14) -> float:
    if len(bars) < window + 1:
        return 0.01
    high, low, c = bars["high"], bars["low"], bars["close"]
    tr = pd.concat([
        (high - low),
        (high - c.shift()).abs(),
        (low - c.shift()).abs(),
    ], axis=1).max(axis=1)
    atr_v = tr.ewm(alpha=1.0 / window, adjust=False).mean().iloc[-1]
    px = float(c.iloc[-1])
    return float(atr_v) / px if px > 0 else 0.01


def derive_sl_tp(
    dominant: Optional[str],
    side: str,
    entry_price: float,
    bars_1h: pd.DataFrame,
    sr_levels: Optional[dict] = None,
    sl_atr_mult: float = 2.0,
    tp_atr_mult: float = 4.0,
) -> tuple[float, float]:
    """Choose SL/TP based on the dominant sub-strategy.

    - sr_bounce: use the nearest pivot level (level - SL_PCT) as SL.
    - donchian_trend / ma_crossover: ATR-based stops.
    - mixed/unknown: ATR-based fallback.
    """
    a = atr_pct(bars_1h)
    if dominant == "sr_bounce" and sr_levels:
        if side == "buy" and sr_levels.get("sup_1h"):
            sl = sr_levels["sup_1h"] * (1 - float(os.environ.get("STOP_LOSS_PCT", 0.01)))
            tp = entry_price * (1 + float(os.environ.get("TAKE_PROFIT_PCT", 0.02)))
            return sl, tp
        if side == "sell" and sr_levels.get("res_1h"):
            sl = sr_levels["res_1h"] * (1 + float(os.environ.get("STOP_LOSS_PCT", 0.01)))
            tp = entry_price * (1 - float(os.environ.get("TAKE_PROFIT_PCT", 0.02)))
            return sl, tp
    # ATR fallback
    if side == "buy":
        sl = entry_price * (1 - sl_atr_mult * a)
        tp = entry_price * (1 + tp_atr_mult * a)
    else:
        sl = entry_price * (1 + sl_atr_mult * a)
        tp = entry_price * (1 - tp_atr_mult * a)
    return float(sl), float(tp)
