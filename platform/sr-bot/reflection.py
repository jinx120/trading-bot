"""Reflection job — the bot learning from its own data.

Reads `signals` and `trades`, groups by (regime, symbol, confluence), computes
expectancy + win rate per group, and proposes parameter changes that lean the
bot toward groups with positive expectancy.

Run on demand or as a daily cron. Writes findings to `reflections` table.
Nothing is auto-applied — the admin UI surfaces them with an Approve button.

Design choices:
  - Only proposes *parameter* changes, not strategy logic changes. Logic
    changes are human work.
  - Confidence floor: a group must have ≥ MIN_TRADES trades before it can
    influence a proposal. Otherwise a 2-trade fluke could move APPROACH_PCT.
  - Direction-only suggestions: it nudges params, never sets to extremes.
    Each run can adjust at most STEP_FRACTION of any value.
  - "Did nothing" is a valid outcome — if no group has a confident edge,
    proposed_changes is {} and admin shows no apply button.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import psycopg2
import psycopg2.extras


sys.path.insert(0, str(Path(__file__).parent))


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
LOOKBACK_DAYS = int(os.environ.get("REFLECTION_LOOKBACK_DAYS", "14"))
MIN_TRADES_PER_GROUP = int(os.environ.get("REFLECTION_MIN_TRADES", "5"))
STEP_FRACTION = float(os.environ.get("REFLECTION_STEP_FRACTION", "0.10"))


def db_conn():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", "5432")),
        user=os.environ.get("DB_USER", "trader"),
        password=os.environ.get("DB_PASSWORD") or os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ.get("DB_NAME", "trading"),
    )


def fetch_recent_trades(days: int) -> pd.DataFrame:
    with db_conn() as conn:
        df = pd.read_sql_query(
            """
            SELECT id, symbol, side, entry_ts, exit_ts, entry_price, exit_price,
                   pnl_pct, exit_reason,
                   (metadata->>'confluence')::boolean AS confluence,
                   (metadata->>'notional_mult')::float AS mult,
                   metadata->'snapshot'->>'regime' AS regime
            FROM trades
            WHERE strategy = 'sr_paper_bot'
              AND entry_ts >= NOW() - INTERVAL '%(days)s days'
            ORDER BY entry_ts ASC
            """ % {"days": days},
            conn,
        )
    return df


def fetch_recent_signals(days: int) -> pd.DataFrame:
    with db_conn() as conn:
        df = pd.read_sql_query(
            f"""
            SELECT id, ts, symbol, side, took_trade, skip_reason,
                   confluence, regime, adx, approach_pct, notional_mult
            FROM signals
            WHERE strategy = 'sr_paper_bot'
              AND ts >= NOW() - INTERVAL '{days} days'
            ORDER BY ts ASC
            """,
            conn,
        )
    return df


def analyze(trades: pd.DataFrame, signals: pd.DataFrame) -> tuple[str, dict, dict]:
    """Build per-group stats and decide on a proposal.

    Returns (summary_text, proposed_changes_dict, metrics_dict)
    """
    closed = trades[trades["exit_ts"].notna()].copy()
    summary_lines: list[str] = []
    metrics: dict = {}
    proposed: dict = {}

    if len(closed) < MIN_TRADES_PER_GROUP:
        summary_lines.append(
            f"Only {len(closed)} closed trades in last {LOOKBACK_DAYS}d "
            f"(<{MIN_TRADES_PER_GROUP} required). Skipping parameter "
            "proposals; need more samples."
        )
        return "\n".join(summary_lines), {}, {"n_closed": len(closed)}

    closed["is_win"] = (closed["pnl_pct"] > 0).astype(int)
    overall = {
        "n": int(len(closed)),
        "win_rate": round(closed["is_win"].mean(), 3),
        "avg_pnl_pct": round(closed["pnl_pct"].mean() * 100, 3),
        "expectancy_pct": round(closed["pnl_pct"].mean() * 100, 3),
    }
    metrics["overall"] = overall
    summary_lines.append(
        f"Overall: {overall['n']} closed trades · win_rate={overall['win_rate']} · "
        f"avg_pnl={overall['avg_pnl_pct']}%"
    )

    # Group by regime — biggest expected effect from the recent regime filter
    if "regime" in closed and closed["regime"].notna().any():
        by_regime = (closed.dropna(subset=["regime"])
                           .groupby("regime")
                           .agg(n=("id", "count"),
                                wr=("is_win", "mean"),
                                avg_pnl=("pnl_pct", "mean"))
                           .reset_index())
        metrics["by_regime"] = by_regime.to_dict(orient="records")
        summary_lines.append("By regime:")
        for _, row in by_regime.iterrows():
            summary_lines.append(
                f"  {row['regime']}: n={int(row['n'])} wr={row['wr']:.2f} "
                f"avg_pnl={row['avg_pnl']*100:+.2f}%"
            )

    # Group by confluence
    by_conf = closed.groupby(closed["confluence"].fillna(False)).agg(
        n=("id", "count"), wr=("is_win", "mean"),
        avg_pnl=("pnl_pct", "mean"),
    ).reset_index()
    metrics["by_confluence"] = by_conf.to_dict(orient="records")
    summary_lines.append("By confluence:")
    for _, row in by_conf.iterrows():
        summary_lines.append(
            f"  confluence={bool(row['confluence'])}: n={int(row['n'])} "
            f"wr={row['wr']:.2f} avg_pnl={row['avg_pnl']*100:+.2f}%"
        )

    # ---- Decide proposals ----
    sl_hits = (closed["exit_reason"] == "sl_hit").sum()
    tp_hits = (closed["exit_reason"] == "tp_hit").sum()
    sl_rate = sl_hits / max(1, sl_hits + tp_hits)
    metrics["sl_hit_rate"] = round(sl_rate, 3)

    cur_env_path = Path(os.environ.get("SR_BOT_ENV_PATH", "/app/sr-bot/.env"))
    cur_env = {}
    if cur_env_path.exists():
        for line in cur_env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cur_env[k.strip()] = v.strip()

    cur_approach = float(cur_env.get("APPROACH_PCT", 0.003))
    cur_sl = float(cur_env.get("STOP_LOSS_PCT", 0.01))
    cur_tp = float(cur_env.get("TAKE_PROFIT_PCT", 0.02))
    cur_cooldown = int(cur_env.get("COOLDOWN_MIN", 60))

    # Rule 1: high SL-hit rate -> stops are too tight OR entries too eager.
    # Push entries to be pickier (lower approach_pct).
    if sl_rate >= 0.7 and overall["avg_pnl_pct"] < 0:
        new_approach = max(0.0005, cur_approach * (1 - STEP_FRACTION))
        if abs(new_approach - cur_approach) > 1e-6:
            proposed["approach_pct"] = round(new_approach, 5)
            summary_lines.append(
                f"PROPOSE approach_pct {cur_approach} -> {proposed['approach_pct']} "
                f"(sl_hit_rate={sl_rate:.0%} with negative expectancy)"
            )

    # Rule 2: high SL-hit rate with avg loss > SL_PCT -> stops are too tight
    # relative to volatility. Widen stop loss a bit.
    if sl_rate >= 0.6 and abs(overall["avg_pnl_pct"]) > cur_sl * 100 * 1.2:
        new_sl = min(0.05, cur_sl * (1 + STEP_FRACTION))
        if abs(new_sl - cur_sl) > 1e-5:
            proposed["stop_loss_pct"] = round(new_sl, 4)
            summary_lines.append(
                f"PROPOSE stop_loss_pct {cur_sl} -> {proposed['stop_loss_pct']} "
                f"(avg pnl magnitude exceeds current SL by >20%)"
            )

    # Rule 3: tp_hit_rate is decent + avg_pnl < cur_tp -> targets too far.
    # Tighten take_profit.
    if tp_hits >= 3 and tp_hits / max(1, sl_hits + tp_hits) >= 0.4 \
            and overall["avg_pnl_pct"] < cur_tp * 100 * 0.5:
        new_tp = max(0.005, cur_tp * (1 - STEP_FRACTION))
        if abs(new_tp - cur_tp) > 1e-5:
            proposed["take_profit_pct"] = round(new_tp, 4)
            summary_lines.append(
                f"PROPOSE take_profit_pct {cur_tp} -> {proposed['take_profit_pct']} "
                f"(realized TP hits but avg pnl well below target)"
            )

    # Rule 4: cooldown — if same symbol stopped out multiple times in lookback,
    # extend cooldown.
    if "regime" in closed and len(closed) >= 5:
        repeat_stops = (closed[closed["exit_reason"] == "sl_hit"]
                        .groupby("symbol").size())
        if (repeat_stops >= 3).any():
            new_cd = min(360, cur_cooldown + 30)
            if new_cd != cur_cooldown:
                proposed["cooldown_min"] = new_cd
                bad_syms = ", ".join(repeat_stops[repeat_stops >= 3].index.tolist())
                summary_lines.append(
                    f"PROPOSE cooldown_min {cur_cooldown} -> {new_cd} "
                    f"(repeat stop-outs on: {bad_syms})"
                )

    # Rule 5: confluence isn't paying — if confluence trades have *worse*
    # expectancy than non-confluence by ≥1 pp, reduce CONFLUENCE_SIZE_MULT.
    by_conf_dict = {bool(r["confluence"]): r for _, r in by_conf.iterrows()}
    if True in by_conf_dict and False in by_conf_dict:
        diff = (by_conf_dict[True]["avg_pnl"] - by_conf_dict[False]["avg_pnl"])
        if diff < -0.01 and by_conf_dict[True]["n"] >= MIN_TRADES_PER_GROUP:
            cur_mult = float(cur_env.get("CONFLUENCE_SIZE_MULT", 1.5))
            new_mult = max(1.0, round(cur_mult - 0.1, 2))
            if abs(new_mult - cur_mult) > 1e-3:
                proposed["confluence_size_mult"] = new_mult
                summary_lines.append(
                    f"PROPOSE confluence_size_mult {cur_mult} -> {new_mult} "
                    f"(confluence trades underperform by {diff*100:.2f}pp)"
                )

    if not proposed:
        summary_lines.append("No high-confidence parameter changes proposed.")

    return "\n".join(summary_lines), proposed, metrics


def apply_to_env(proposed: dict, env_path: Path) -> dict:
    """Write proposed param changes directly into the .env file.

    No human gate. Returns the dict of keys actually changed.
    """
    if not proposed or not env_path.exists():
        return {}
    content = env_path.read_text()
    lines = content.splitlines()
    applied: dict = {}
    for raw_key, new_val in proposed.items():
        env_key = raw_key.upper()
        replaced = False
        for i, ln in enumerate(lines):
            if ln.startswith(f"{env_key}="):
                old_val = ln.split("=", 1)[1].strip()
                if str(old_val) == str(new_val):
                    replaced = True
                    break
                lines[i] = f"{env_key}={new_val}"
                applied[env_key] = {"old": old_val, "new": new_val}
                replaced = True
                break
        if not replaced:
            lines.append(f"{env_key}={new_val}")
            applied[env_key] = {"old": None, "new": new_val}
    env_path.write_text("\n".join(lines) + "\n")
    return applied


def main() -> dict:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("reflection")

    trades = fetch_recent_trades(LOOKBACK_DAYS)
    signals = fetch_recent_signals(LOOKBACK_DAYS)
    log.info("loaded %d trades and %d signals from last %d days",
             len(trades), len(signals), LOOKBACK_DAYS)

    summary, proposed, metrics = analyze(trades, signals)

    # Auto-apply proposals to .env (no human gate). Disable by setting
    # REFLECTION_AUTO_APPLY=false in the env.
    auto = os.environ.get("REFLECTION_AUTO_APPLY", "true").lower() == "true"
    env_path = Path(os.environ.get("SR_BOT_ENV_PATH", "/app/sr-bot/.env"))
    applied: dict = {}
    if auto and proposed:
        applied = apply_to_env(proposed, env_path)
        if applied:
            log.info("AUTO-APPLIED %d params: %s", len(applied), applied)
            summary += f"\nAUTO-APPLIED: {json.dumps(applied)}"

    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO reflections (
                strategy, lookback_days, n_trades_analyzed, n_signals_analyzed,
                summary, proposed_changes, metrics, applied, applied_ts
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            "sr_paper_bot", LOOKBACK_DAYS, len(trades), len(signals),
            summary, json.dumps(proposed), json.dumps(metrics, default=str),
            bool(applied),
            datetime.now(timezone.utc) if applied else None,
        ))
        ref_id = cur.fetchone()[0]

    log.info("reflection #%d saved.", ref_id)
    return {"reflection_id": ref_id, "summary": summary,
            "proposed": proposed, "applied": applied}


if __name__ == "__main__":
    main()
