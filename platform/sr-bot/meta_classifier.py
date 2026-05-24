"""Phase 8: Triple-barrier meta-classifier.

Adapts López de Prado's Triple Barrier method (Advances in Financial Machine
Learning, 2018). For each historical bot trade, label = which barrier hit
first: TP_HIT (1), SL_HIT (0), TIME_OUT (treated as 0 — no edge).

A scikit-learn classifier learns to predict P(TP_HIT) from features
available at entry time:
  - per-strategy ensemble scores (sr_bounce, donchian_trend, ma_crossover, zscore_revert)
  - ensemble composite + confidence
  - regime label (one-hot: range / uptrend / downtrend / unknown)
  - ADX, ATR_pct, sma_dev_pct
  - hour of day, day of week (cyclic)

The bot calls predict_proba() at entry decision time. If P(TP_HIT) is below
META_MIN_PROBABILITY, the entry is vetoed even though the ensemble said go.

Training is intentionally lightweight (sklearn RandomForest, no GPU) and
re-runs during reflection cycles when N_NEW_TRADES_SINCE_LAST_TRAIN > 5.

Model is persisted to disk at /tmp/meta_classifier.pkl. Disk-only is fine —
on restart the bot re-trains from the trades table.

Honest constraints:
  - Need at least META_MIN_TRAIN_SAMPLES closed trades before training.
    Below that, predict_proba returns None and the bot proceeds without veto.
  - Trades from before ensemble mode (no breakdown) are excluded.
  - This is a meta-model on top of the ensemble, not a replacement for it.
"""
from __future__ import annotations

import json
import logging
import os
import pickle
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras


MODEL_PATH = Path(os.environ.get("META_MODEL_PATH", "/tmp/meta_classifier.pkl"))
META_MIN_TRAIN_SAMPLES = int(os.environ.get("META_MIN_TRAIN_SAMPLES", "30"))
META_RETRAIN_EVERY_N_TRADES = int(os.environ.get("META_RETRAIN_EVERY_N_TRADES", "5"))
META_MIN_PROBABILITY = float(os.environ.get("META_MIN_PROBABILITY", "0.50"))
META_ENABLED = os.environ.get("META_ENABLED", "true").lower() == "true"

REGIME_LABELS = ["range", "uptrend", "downtrend", "unknown"]
STRATEGY_NAMES = ["sr_bounce", "donchian_trend", "ma_crossover", "zscore_revert"]


def _db_conn():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", "5432")),
        user=os.environ.get("DB_USER", "trader"),
        password=os.environ.get("DB_PASSWORD") or os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ.get("DB_NAME", "trading"),
    )


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

@dataclass
class EntryFeatures:
    composite: float
    confidence: float
    sr_bounce: float
    donchian_trend: float
    ma_crossover: float
    zscore_revert: float
    regime_range: int
    regime_uptrend: int
    regime_downtrend: int
    regime_unknown: int
    adx: float
    atr_pct: float
    sma_dev_pct: float
    hour_sin: float
    hour_cos: float
    dow_sin: float
    dow_cos: float

    def to_array(self) -> np.ndarray:
        return np.array([
            self.composite, self.confidence,
            self.sr_bounce, self.donchian_trend, self.ma_crossover, self.zscore_revert,
            self.regime_range, self.regime_uptrend, self.regime_downtrend, self.regime_unknown,
            self.adx, self.atr_pct, self.sma_dev_pct,
            self.hour_sin, self.hour_cos, self.dow_sin, self.dow_cos,
        ], dtype=np.float32)

    @classmethod
    def feature_names(cls) -> list[str]:
        return [
            "composite", "confidence",
            "sr_bounce", "donchian_trend", "ma_crossover", "zscore_revert",
            "regime_range", "regime_uptrend", "regime_downtrend", "regime_unknown",
            "adx", "atr_pct", "sma_dev_pct",
            "hour_sin", "hour_cos", "dow_sin", "dow_cos",
        ]


def build_features(
    ensemble_breakdown: dict,
    composite: float,
    confidence: float,
    regime_label: str,
    adx: float,
    atr_pct: float,
    sma_dev_pct: float,
    ts: datetime,
) -> EntryFeatures:
    """Pure function — build feature vector from entry context."""
    per_strat = {n: 0.0 for n in STRATEGY_NAMES}
    if ensemble_breakdown:
        for name, sb in ensemble_breakdown.items():
            if name in per_strat:
                try:
                    per_strat[name] = float(sb.get("score", 0))
                except (TypeError, ValueError, AttributeError):
                    pass
    regime_oh = {f"regime_{r}": 0 for r in REGIME_LABELS}
    key = f"regime_{regime_label}" if regime_label in REGIME_LABELS else "regime_unknown"
    regime_oh[key] = 1
    hour = ts.hour
    dow = ts.weekday()
    return EntryFeatures(
        composite=composite,
        confidence=confidence,
        sr_bounce=per_strat["sr_bounce"],
        donchian_trend=per_strat["donchian_trend"],
        ma_crossover=per_strat["ma_crossover"],
        zscore_revert=per_strat["zscore_revert"],
        regime_range=regime_oh["regime_range"],
        regime_uptrend=regime_oh["regime_uptrend"],
        regime_downtrend=regime_oh["regime_downtrend"],
        regime_unknown=regime_oh["regime_unknown"],
        adx=adx if adx is not None else 0.0,
        atr_pct=atr_pct if atr_pct is not None else 0.0,
        sma_dev_pct=sma_dev_pct if sma_dev_pct is not None else 0.0,
        hour_sin=np.sin(2 * np.pi * hour / 24),
        hour_cos=np.cos(2 * np.pi * hour / 24),
        dow_sin=np.sin(2 * np.pi * dow / 7),
        dow_cos=np.cos(2 * np.pi * dow / 7),
    )


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

def _load_training_set() -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Pull closed ensemble trades + their entry features + labels.

    Label = 1 if exit_reason == 'tp_hit', else 0 (sl_hit, trail_stop, time_exit
    are all "no edge realized" for the meta-model's purposes).
    """
    with _db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT entry_ts, exit_reason, pnl_pct,
                   metadata->'snapshot'->'ensemble' AS ensemble,
                   metadata->'snapshot'->>'composite' AS composite,
                   metadata->'snapshot'->>'confidence' AS confidence,
                   metadata->>'regime' AS regime,
                   metadata->>'adx' AS adx,
                   metadata->>'atr_pct' AS atr_pct
            FROM trades
            WHERE strategy = 'sr_paper_bot'
              AND exit_ts IS NOT NULL
              AND metadata->'snapshot' ? 'ensemble'
            ORDER BY entry_ts ASC
        """)
        rows = cur.fetchall()
    X, y = [], []
    feat_names = EntryFeatures.feature_names()
    for row in rows:
        ens = row["ensemble"]
        if isinstance(ens, str):
            try:
                ens = json.loads(ens)
            except Exception:
                continue
        if not isinstance(ens, dict):
            continue
        try:
            composite = float(row["composite"] or 0)
            confidence = float(row["confidence"] or 0)
            adx_v = float(row["adx"] or 0)
            atr_v = float(row["atr_pct"] or 0)
        except (TypeError, ValueError):
            composite = confidence = adx_v = atr_v = 0.0
        ts = row["entry_ts"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        feats = build_features(
            ensemble_breakdown=ens,
            composite=composite, confidence=confidence,
            regime_label=row["regime"] or "unknown",
            adx=adx_v, atr_pct=atr_v,
            sma_dev_pct=0.0,  # not stored in old trades — recoverable later
            ts=ts,
        )
        X.append(feats.to_array())
        y.append(1 if row["exit_reason"] == "tp_hit" else 0)
    if not X:
        return np.empty((0, len(feat_names))), np.empty(0, dtype=int), feat_names
    return np.vstack(X), np.array(y, dtype=int), feat_names


def train() -> dict:
    """Train (or retrain) the meta-classifier. Returns a status dict."""
    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.metrics import roc_auc_score
    except ImportError:
        return {"ok": False, "reason": "sklearn not installed"}

    X, y, feat_names = _load_training_set()
    n = len(y)
    if n < META_MIN_TRAIN_SAMPLES:
        return {"ok": False, "reason": f"not enough samples ({n} < {META_MIN_TRAIN_SAMPLES})"}
    if len(set(y)) < 2:
        return {"ok": False, "reason": f"only one class in labels (all={y[0]})"}

    # Hold last 20% as time-series validation
    cutoff = int(n * 0.8)
    X_train, X_val = X[:cutoff], X[cutoff:]
    y_train, y_val = y[:cutoff], y[cutoff:]

    clf = RandomForestClassifier(
        n_estimators=200, max_depth=6, min_samples_leaf=3,
        class_weight="balanced", random_state=42, n_jobs=-1,
    )
    clf.fit(X_train, y_train)

    val_auc = float("nan")
    if len(set(y_val)) >= 2 and len(y_val) >= 4:
        val_auc = float(roc_auc_score(y_val, clf.predict_proba(X_val)[:, 1]))

    # Persist model + meta
    payload = {
        "clf": clf,
        "feature_names": feat_names,
        "n_train": int(cutoff),
        "n_val": int(n - cutoff),
        "val_auc": val_auc,
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump(payload, f)
    feat_imp = dict(zip(feat_names, [float(v) for v in clf.feature_importances_]))
    logging.info("meta-classifier trained: n=%d val_auc=%.3f top_features=%s",
                 n, val_auc, sorted(feat_imp.items(), key=lambda x: -x[1])[:5])
    return {"ok": True, "n_samples": n, "val_auc": val_auc,
            "feature_importance": feat_imp}


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------

_MODEL_CACHE: Optional[dict] = None
_MODEL_CACHE_MTIME: Optional[float] = None


def _load_model() -> Optional[dict]:
    global _MODEL_CACHE, _MODEL_CACHE_MTIME
    if not MODEL_PATH.exists():
        return None
    mtime = MODEL_PATH.stat().st_mtime
    if _MODEL_CACHE is not None and _MODEL_CACHE_MTIME == mtime:
        return _MODEL_CACHE
    try:
        with open(MODEL_PATH, "rb") as f:
            _MODEL_CACHE = pickle.load(f)
            _MODEL_CACHE_MTIME = mtime
        return _MODEL_CACHE
    except Exception as e:
        logging.warning("meta-classifier load failed: %s", e)
        return None


def predict_proba(features: EntryFeatures) -> Optional[float]:
    """Return P(TP_HIT) in [0, 1], or None when no model is available."""
    if not META_ENABLED:
        return None
    payload = _load_model()
    if payload is None:
        return None
    clf = payload["clf"]
    try:
        p = clf.predict_proba(features.to_array().reshape(1, -1))[0, 1]
        return float(p)
    except Exception as e:
        logging.warning("meta-classifier predict failed: %s", e)
        return None


def should_veto(features: EntryFeatures) -> tuple[bool, Optional[float], str]:
    """Top-level veto check. Returns (veto, p_tp, reason)."""
    if not META_ENABLED:
        return False, None, "disabled"
    p = predict_proba(features)
    if p is None:
        return False, None, "no_model"
    if p < META_MIN_PROBABILITY:
        return True, p, f"P(TP)={p:.2f} < {META_MIN_PROBABILITY}"
    return False, p, f"P(TP)={p:.2f} OK"


# ---------------------------------------------------------------------------
# Standalone training entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    result = train()
    print(json.dumps(result, indent=2, default=str))
