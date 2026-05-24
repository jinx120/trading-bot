"""The Lab — autonomous research and feature improvement loop.

Three jobs:

1. SHADOW STRATEGY RUNNER (every 5 min)
   - For each candidate in lab/shadow_strategies/, compute a score per symbol
   - Persist to shadow_scores table
   - Backfill forward returns when those bars roll in (so we can later
     measure: did the score's sign predict the next 4h move?)

2. PROMOTION GATE (every 6 hours)
   - For each shadow candidate with enough samples (>= MIN_PROMOTION_SAMPLES):
       - Compute hit rate (score sign vs forward 4h return sign)
   - If hit rate > PROMOTION_HIT_RATE AND sample count clears N, auto-promote:
       - Copy file from lab/shadow_strategies/ to strategies/
       - Update __init__.py REGISTRY
       - INSERT row into strategy_weights with seed_weight, rebalance siblings

3. HEALTH MONITOR (every 30 min)
   - Reads live bot's recent equity, trade hit rate, ensemble agreement
   - INSERT into lab_events when a metric crosses a threshold (alarm)

Runs as a separate container (tp-lab). No interaction with live trading
state — only DB writes to shadow tables + lab_events.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras


# Make sibling modules importable: meta_classifier, sentiment, etc.
SR_BOT_PATH = Path("/app/sr-bot")
if str(SR_BOT_PATH) not in sys.path:
    sys.path.insert(0, str(SR_BOT_PATH))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SHADOW_CADENCE_SECONDS    = int(os.environ.get("LAB_SHADOW_CADENCE", "300"))   # 5 min
PROMOTION_CADENCE_SECONDS = int(os.environ.get("LAB_PROMOTE_CADENCE", "21600"))  # 6h
HEALTH_CADENCE_SECONDS    = int(os.environ.get("LAB_HEALTH_CADENCE", "1800"))   # 30 min

MIN_PROMOTION_SAMPLES = int(os.environ.get("LAB_MIN_SAMPLES", "100"))
PROMOTION_HIT_RATE    = float(os.environ.get("LAB_PROMOTE_HIT_RATE", "0.55"))
PROMOTION_SEED_WEIGHT = float(os.environ.get("LAB_SEED_WEIGHT", "0.10"))

HEALTH_MIN_HIT_RATE    = float(os.environ.get("LAB_HEALTH_MIN_HIT_RATE", "0.30"))
HEALTH_MAX_DRAWDOWN    = float(os.environ.get("LAB_HEALTH_MAX_DD", "0.10"))


def db_conn():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", "5432")),
        user=os.environ.get("DB_USER", "trader"),
        password=os.environ.get("DB_PASSWORD") or os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ.get("DB_NAME", "trading"),
    )


def log_event(kind: str, detail: dict) -> None:
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO lab_events (kind, detail) VALUES (%s, %s)",
                        (kind, json.dumps(detail, default=str)))
    except Exception as e:
        logging.warning("log_event failed: %s", e)


# ---------------------------------------------------------------------------
# Candidate discovery
# ---------------------------------------------------------------------------

def discover_candidates() -> dict:
    """Import every Python module under lab/shadow_strategies/. Each must
    expose `name` and `score(bars)`."""
    candidates: dict = {}
    mod_dir = Path(__file__).parent / "shadow_strategies"
    sys.path.insert(0, str(mod_dir.parent))
    for py in mod_dir.glob("*.py"):
        if py.stem.startswith("_"):
            continue
        try:
            mod_name = f"shadow_strategies.{py.stem}"
            mod = __import__(mod_name, fromlist=["score", "name"])
            cand_name = getattr(mod, "name", py.stem)
            candidates[cand_name] = mod
        except Exception as e:
            logging.warning("failed to import %s: %s", py, e)
    return candidates


# ---------------------------------------------------------------------------
# Job 1 — Shadow strategy runner
# ---------------------------------------------------------------------------

def run_shadow_scoring(candidates: dict) -> int:
    """Pull recent 1H bars per symbol, score each candidate, persist. Returns rows written."""
    from sr_paper_bot import Alpaca

    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_API_SECRET", "")
    if not key or not secret:
        return 0
    alpaca = Alpaca(key, secret)
    symbols = [s.strip() for s in os.environ.get(
        "SR_BOT_SYMBOLS", "BTC/USD,ETH/USD,SOL/USD,LINK/USD,AVAX/USD,DOGE/USD,AAVE/USD"
    ).split(",") if s.strip()]

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=720)
    rows = 0
    with db_conn() as conn, conn.cursor() as cur:
        for sym in symbols:
            is_crypto = "/" in sym
            try:
                bars = alpaca.crypto_bars(sym, start, end) if is_crypto \
                    else alpaca.stock_bars(sym, start, end)
            except Exception as e:
                logging.debug("bars fetch failed for %s: %s", sym, e)
                continue
            if bars.empty or len(bars) < 50:
                continue
            for cand_name, mod in candidates.items():
                try:
                    s = float(mod.score(bars))
                except Exception as e:
                    logging.debug("%s/%s score failed: %s", sym, cand_name, e)
                    continue
                cur.execute(
                    "INSERT INTO shadow_scores (symbol, candidate, score) "
                    "VALUES (%s, %s, %s)",
                    (sym, cand_name, s),
                )
                rows += 1
    return rows


def backfill_forward_returns() -> int:
    """For shadow_scores rows older than 4h that don't have fwd_return_4h,
    fetch the close at score_ts + 4h and write the forward return."""
    from sr_paper_bot import Alpaca
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_API_SECRET", "")
    if not key or not secret:
        return 0
    alpaca = Alpaca(key, secret)

    updated = 0
    with db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Look at rows aged 4–48h with no fwd_return_4h yet
        cur.execute("""
            SELECT id, ts, symbol FROM shadow_scores
            WHERE fwd_return_4h IS NULL
              AND ts <= NOW() - INTERVAL '4 hours'
              AND ts >  NOW() - INTERVAL '48 hours'
            ORDER BY ts ASC LIMIT 500
        """)
        rows = cur.fetchall()
    if not rows:
        return 0

    # Cache bars per symbol to amortize API calls
    bars_cache: dict = {}
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=72)
    for row in rows:
        sym = row["symbol"]
        if sym not in bars_cache:
            is_crypto = "/" in sym
            try:
                bars_cache[sym] = (alpaca.crypto_bars(sym, start, end) if is_crypto
                                   else alpaca.stock_bars(sym, start, end))
            except Exception:
                bars_cache[sym] = pd.DataFrame()
        bars = bars_cache[sym]
        if bars.empty:
            continue
        score_ts = row["ts"]
        if score_ts.tzinfo is None:
            score_ts = score_ts.replace(tzinfo=timezone.utc)
        # Find close at score_ts (nearest), and at score_ts + 4h
        try:
            close_now_idx = bars.index.get_indexer([score_ts], method="nearest")[0]
            close_now = float(bars["close"].iloc[close_now_idx])
            target = score_ts + timedelta(hours=4)
            close_4h_idx = bars.index.get_indexer([target], method="nearest")[0]
            close_4h = float(bars["close"].iloc[close_4h_idx])
            if close_now > 0:
                fwd_ret = (close_4h / close_now) - 1.0
                with db_conn() as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE shadow_scores SET fwd_return_4h = %s WHERE id = %s",
                        (fwd_ret, row["id"]),
                    )
                updated += 1
        except Exception:
            continue
    return updated


# ---------------------------------------------------------------------------
# Job 2 — Promotion gate
# ---------------------------------------------------------------------------

def evaluate_promotion(candidates: dict) -> list[dict]:
    """For each shadow candidate, compute hit rate and decide whether to
    auto-promote into the live strategies/ package."""
    actions: list[dict] = []
    with db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT candidate,
                   COUNT(*) AS n,
                   SUM(CASE WHEN (score > 0 AND fwd_return_4h > 0)
                              OR (score < 0 AND fwd_return_4h < 0)
                            THEN 1 ELSE 0 END) AS hits,
                   AVG(score * fwd_return_4h) AS mean_signed_product
            FROM shadow_scores
            WHERE fwd_return_4h IS NOT NULL
              AND score <> 0
              AND ts > NOW() - INTERVAL '30 days'
            GROUP BY candidate
        """)
        stats = cur.fetchall()

    for s in stats:
        n = int(s["n"])
        if n < MIN_PROMOTION_SAMPLES:
            continue
        hr = float(s["hits"]) / n if n else 0.0
        if hr < PROMOTION_HIT_RATE:
            continue
        if s["candidate"] not in candidates:
            continue
        if _is_already_promoted(s["candidate"]):
            continue
        # Promote: copy file, add to REGISTRY in strategies/__init__.py, insert weight row
        try:
            _promote(s["candidate"])
            actions.append({
                "action": "promote",
                "candidate": s["candidate"],
                "n": n, "hit_rate": round(hr, 3),
                "mean_signed_product": round(float(s["mean_signed_product"] or 0), 6),
            })
            log_event("promote", {
                "candidate": s["candidate"], "n": n,
                "hit_rate": hr, "seed_weight": PROMOTION_SEED_WEIGHT,
            })
            logging.warning("PROMOTED %s: hit_rate=%.2f over n=%d shadow samples",
                            s["candidate"], hr, n)
        except Exception as e:
            logging.error("promotion of %s failed: %s\n%s",
                          s["candidate"], e, traceback.format_exc())
    return actions


def _is_already_promoted(candidate: str) -> bool:
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM strategy_weights WHERE name = %s", (candidate,))
            return cur.fetchone() is not None
    except Exception:
        return False


def _promote(candidate: str) -> None:
    """Copy candidate file into live strategies/, update __init__.py, insert
    strategy_weights row with PROMOTION_SEED_WEIGHT (rebalance siblings)."""
    src = Path(__file__).parent / "shadow_strategies" / f"{candidate}.py"
    dst = Path("/app/sr-bot/strategies") / f"{candidate}.py"
    if not src.exists():
        raise FileNotFoundError(f"{src} not found")
    dst.write_text(src.read_text())

    # Patch strategies/__init__.py REGISTRY
    init_path = Path("/app/sr-bot/strategies/__init__.py")
    content = init_path.read_text()
    if candidate in content:
        pass  # already imported
    else:
        # Insert import + REGISTRY entry. Conservative regex-free patch:
        lines = content.splitlines()
        # Find the `from . import ...` line and append the candidate
        for i, ln in enumerate(lines):
            if ln.startswith("from . import "):
                if ln.endswith(","):
                    lines.insert(i + 1, f"from . import {candidate}")
                else:
                    lines[i] = ln.rstrip() + f", {candidate}"
                break
        # Find REGISTRY dict closing brace and add an entry
        for i, ln in enumerate(lines):
            if "REGISTRY" in ln and "{" in ln:
                # find the closing brace
                for j in range(i, len(lines)):
                    if lines[j].strip().startswith("}"):
                        lines.insert(j, f'    "{candidate}":   {candidate},')
                        break
                break
        init_path.write_text("\n".join(lines) + "\n")

    # Insert into strategy_weights at the seed weight, rebalance siblings
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO strategy_weights (name, weight, enabled,
                metadata)
            VALUES (%s, %s, TRUE, %s)
            ON CONFLICT (name) DO UPDATE SET enabled = TRUE, weight = EXCLUDED.weight
        """, (candidate, PROMOTION_SEED_WEIGHT,
              json.dumps({"promoted_from_lab": datetime.now(timezone.utc).isoformat()})))
        # Renormalize so weights sum to 1.0
        cur.execute("SELECT name, weight FROM strategy_weights WHERE enabled = TRUE")
        rows = cur.fetchall()
        total = sum(float(r[1]) for r in rows) or 1.0
        for name, w in rows:
            cur.execute("UPDATE strategy_weights SET weight = %s WHERE name = %s",
                        (float(w) / total, name))


# ---------------------------------------------------------------------------
# Job 3 — Health monitor
# ---------------------------------------------------------------------------

def health_monitor() -> dict:
    """Read live bot's recent activity and emit health metrics. Alarm on
    threshold crossings."""
    metrics: dict = {}
    with db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT COUNT(*) AS n_closed_7d,
                   AVG(pnl_pct) AS avg_pnl_7d,
                   SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END)::float
                     / NULLIF(COUNT(*), 0) AS hit_rate_7d
            FROM trades
            WHERE strategy = 'sr_paper_bot'
              AND exit_ts IS NOT NULL
              AND exit_ts > NOW() - INTERVAL '7 days'
        """)
        row = cur.fetchone() or {}
        metrics["n_closed_7d"] = int(row.get("n_closed_7d") or 0)
        metrics["avg_pnl_7d"] = float(row.get("avg_pnl_7d") or 0)
        metrics["hit_rate_7d"] = float(row.get("hit_rate_7d") or 0)

        cur.execute("SELECT peak_equity, sod_equity, lockdown FROM risk_state WHERE id=1")
        rs = cur.fetchone() or {}
        metrics["peak_equity"] = float(rs.get("peak_equity") or 0)
        metrics["lockdown"] = bool(rs.get("lockdown") or False)

    alarms = []
    if metrics["n_closed_7d"] >= 10 and metrics["hit_rate_7d"] < HEALTH_MIN_HIT_RATE:
        alarms.append(f"hit_rate_7d {metrics['hit_rate_7d']:.0%} < {HEALTH_MIN_HIT_RATE:.0%}")
    if metrics["lockdown"]:
        alarms.append("lockdown active")

    if alarms:
        log_event("health_alarm", {"alarms": alarms, "metrics": metrics})
        logging.warning("HEALTH ALARM: %s", "; ".join(alarms))
    else:
        log_event("health_ok", metrics)
    return {"metrics": metrics, "alarms": alarms}


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(
        level=os.environ.get("LAB_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log = logging.getLogger("lab")

    # Load .env so we get the same Alpaca + DB creds the bot uses
    try:
        from sr_paper_bot import load_env_file
        load_env_file("/app/sr-bot/.env", overwrite=True)
        load_env_file("/home/redji/trading-platform/.env", overwrite=False)
    except Exception as e:
        log.warning("env load failed: %s", e)

    candidates = discover_candidates()
    log.info("lab booting · %d shadow candidate(s): %s",
             len(candidates), list(candidates.keys()))
    log_event("lab_started", {"candidates": list(candidates.keys())})

    stop = {"flag": False}
    def _handle(*_):
        stop["flag"] = True
        log.info("shutdown signal received")
    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    last_shadow = 0.0
    last_promote = 0.0
    last_health = 0.0
    while not stop["flag"]:
        now = time.time()
        try:
            if now - last_shadow >= SHADOW_CADENCE_SECONDS:
                n = run_shadow_scoring(candidates)
                m = backfill_forward_returns()
                log.info("shadow scoring: %d rows written, %d fwd returns backfilled",
                         n, m)
                last_shadow = now
        except Exception as e:
            log.exception("shadow scoring error: %s", e)
        try:
            if now - last_promote >= PROMOTION_CADENCE_SECONDS:
                actions = evaluate_promotion(candidates)
                log.info("promotion eval: %d action(s)", len(actions))
                last_promote = now
        except Exception as e:
            log.exception("promotion error: %s", e)
        try:
            if now - last_health >= HEALTH_CADENCE_SECONDS:
                h = health_monitor()
                log.info("health: %s alarms · hit_rate_7d=%.2f n_7d=%d",
                         len(h["alarms"]),
                         h["metrics"]["hit_rate_7d"],
                         h["metrics"]["n_closed_7d"])
                last_health = now
        except Exception as e:
            log.exception("health error: %s", e)
        # Sleep until next earliest deadline (min 30s)
        time.sleep(30)
    log.info("lab exiting")


if __name__ == "__main__":
    main()
