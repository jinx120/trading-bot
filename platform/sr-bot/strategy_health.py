"""Strategy retirement gate.

Phase 6: each reflection cycle, evaluate every enabled sub-strategy's
predictive accuracy and disable strategies with persistent negative edge.

Methodology:
  1. For each closed ensemble trade in the lookback window, read its score
     breakdown from `trades.metadata->>'snapshot'->'ensemble'`.
  2. For each strategy that contributed, check if its score's sign matched
     the trade's pnl_pct sign. (Score >0 + winning long = hit. Score <0 +
     losing long = hit. etc.)
  3. Compute hit rate per strategy over 30d and 60d windows.
  4. Disable a strategy when:
        n_30d_contributions >= MIN_CONTRIBS_30D AND
        n_60d_contributions >= MIN_CONTRIBS_60D AND
        hit_rate_30d < THRESHOLD AND
        hit_rate_60d < THRESHOLD
  5. Re-enable a previously-disabled strategy when 14d hit_rate >= 0.55.

Disabling sets enabled=FALSE on strategy_weights. The bot's ensemble.fetch_weights
only loads enabled rows, so the strategy stops contributing immediately on the
next tick after the next env reload.

The gate has guardrails:
  - Never disables the last enabled strategy (would leave bot with no signal).
  - Conservative thresholds (0.45 disable, 0.55 re-enable) prevent thrashing.
  - Re-evaluates every reflection cycle so dead strategies get a second
    chance once the regime that broke them flips.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

import pandas as pd
import psycopg2
import psycopg2.extras


HIT_RATE_DISABLE = float(os.environ.get("STRAT_HIT_DISABLE", "0.45"))
HIT_RATE_ENABLE  = float(os.environ.get("STRAT_HIT_ENABLE", "0.55"))
MIN_CONTRIBS_30D = int(os.environ.get("STRAT_MIN_30D", "10"))
MIN_CONTRIBS_60D = int(os.environ.get("STRAT_MIN_60D", "15"))


def _db_conn():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", "5432")),
        user=os.environ.get("DB_USER", "trader"),
        password=os.environ.get("DB_PASSWORD") or os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ.get("DB_NAME", "trading"),
    )


def _hit_rates_for_window(days: int) -> dict[str, dict]:
    """Compute per-strategy hit rate over the last `days` days.

    A "hit" is when the strategy's score sign at entry matched the trade's
    pnl sign. Score 0 counts as no contribution (excluded).
    """
    with _db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT pnl_pct, side,
                   metadata->'snapshot'->'ensemble' AS ensemble,
                   exit_ts
            FROM trades
            WHERE strategy = 'sr_paper_bot'
              AND exit_ts IS NOT NULL
              AND exit_ts >= NOW() - INTERVAL '{int(days)} days'
              AND pnl_pct IS NOT NULL
            """
        )
        rows = cur.fetchall()
    counts: dict[str, dict] = {}
    for row in rows:
        ens = row["ensemble"]
        if not ens:
            continue
        if isinstance(ens, str):
            try:
                ens = json.loads(ens)
            except Exception:
                continue
        if not isinstance(ens, dict):
            continue
        pnl = float(row["pnl_pct"])
        for strat_name, sb in ens.items():
            try:
                score = float(sb.get("score", 0))
            except (TypeError, ValueError, AttributeError):
                continue
            if score == 0:
                continue
            counts.setdefault(strat_name, {"hits": 0, "n": 0})
            counts[strat_name]["n"] += 1
            # Long trade (side='long' / 'buy') with positive score & positive pnl → hit
            # Short trade with negative score & positive pnl → hit
            # We treat pnl sign vs score sign directly (already direction-aware via score)
            if (score > 0 and pnl > 0) or (score < 0 and pnl < 0):
                counts[strat_name]["hits"] += 1
    out: dict[str, dict] = {}
    for name, c in counts.items():
        out[name] = {
            "n": c["n"],
            "hits": c["hits"],
            "hit_rate": round(c["hits"] / c["n"], 3) if c["n"] else 0.0,
        }
    return out


def evaluate_retirement() -> dict:
    """Decide enable/disable changes for sub-strategies. Returns action summary.

    Conservative — never disables the last surviving strategy, requires
    enough trade samples in BOTH windows, and surfaces 14d for re-enable.
    """
    h30 = _hit_rates_for_window(30)
    h60 = _hit_rates_for_window(60)
    h14 = _hit_rates_for_window(14)

    # Current state
    with _db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT name, weight, enabled FROM strategy_weights")
        current = {r["name"]: dict(r) for r in cur.fetchall()}

    enabled_now = [n for n, s in current.items() if s["enabled"]]
    actions: list[dict] = []

    # Disable check
    for name, state in current.items():
        if not state["enabled"]:
            continue
        s30 = h30.get(name, {"n": 0, "hit_rate": 0.0})
        s60 = h60.get(name, {"n": 0, "hit_rate": 0.0})
        if s30["n"] < MIN_CONTRIBS_30D or s60["n"] < MIN_CONTRIBS_60D:
            continue
        if s30["hit_rate"] < HIT_RATE_DISABLE and s60["hit_rate"] < HIT_RATE_DISABLE:
            if len(enabled_now) <= 1:
                logging.warning(
                    "retirement gate WANTED to disable %s but it's the last "
                    "enabled strategy — refusing to leave bot mute", name,
                )
                continue
            actions.append({
                "action": "disable",
                "name": name,
                "hit_rate_30d": s30["hit_rate"],
                "hit_rate_60d": s60["hit_rate"],
                "n_30d": s30["n"], "n_60d": s60["n"],
                "reason": f"hit_rate 30d={s30['hit_rate']:.0%}, "
                          f"60d={s60['hit_rate']:.0%} (both < {HIT_RATE_DISABLE:.0%})",
            })
            enabled_now.remove(name)

    # Re-enable check (uses 14d so dead strategies aren't stuck forever)
    for name, state in current.items():
        if state["enabled"]:
            continue
        s14 = h14.get(name, {"n": 0, "hit_rate": 0.0})
        if s14["n"] < 5:
            continue
        if s14["hit_rate"] >= HIT_RATE_ENABLE:
            actions.append({
                "action": "enable",
                "name": name,
                "hit_rate_14d": s14["hit_rate"],
                "n_14d": s14["n"],
                "reason": f"hit_rate 14d={s14['hit_rate']:.0%} >= {HIT_RATE_ENABLE:.0%}",
            })

    return {
        "actions": actions,
        "hit_rates_30d": h30,
        "hit_rates_60d": h60,
        "hit_rates_14d": h14,
    }


def apply_retirement(result: dict) -> int:
    """Persist the disable/enable actions to strategy_weights. Returns count."""
    actions = result.get("actions", [])
    if not actions:
        return 0
    applied = 0
    with _db_conn() as conn, conn.cursor() as cur:
        for a in actions:
            new_enabled = (a["action"] == "enable")
            cur.execute(
                "UPDATE strategy_weights SET enabled = %s, last_updated = NOW(), "
                "metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb "
                "WHERE name = %s",
                (new_enabled, json.dumps({"retirement_log": [{
                    "action": a["action"], "reason": a["reason"],
                    "ts": pd.Timestamp.utcnow().isoformat(),
                }]}), a["name"]),
            )
            applied += 1
            logging.warning("retirement gate: %s %s — %s",
                            a["action"].upper(), a["name"], a["reason"])
    return applied


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    result = evaluate_retirement()
    print(json.dumps(result, indent=2, default=str))
    applied = apply_retirement(result)
    print(f"\napplied {applied} action(s)")
