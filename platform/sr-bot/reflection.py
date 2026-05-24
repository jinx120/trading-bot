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
MIN_TRADES_PER_GROUP = int(os.environ.get("REFLECTION_MIN_TRADES", "20"))
STEP_FRACTION = float(os.environ.get("REFLECTION_STEP_FRACTION", "0.10"))

# Phase-0 guardrails -----------------------------------------------------
# Hard ranges. Reflection cannot push a param outside these regardless of
# what its rules suggest. Numbers are sane defaults for a 60s-tick S&R bot;
# adjust only with intent.
PARAM_RANGES: dict[str, tuple[float, float]] = {
    "APPROACH_PCT":         (0.001, 0.01),
    "STOP_LOSS_PCT":        (0.005, 0.03),
    "TAKE_PROFIT_PCT":      (0.005, 0.05),
    "COOLDOWN_MIN":         (30,    360),
    "CONFLUENCE_SIZE_MULT": (1.0,   2.0),
    "TRAIL_TRIGGER_PCT":    (0.002, 0.02),
    "TRAIL_DISTANCE_PCT":   (0.003, 0.025),
    "CLUSTER_PCT":          (0.001, 0.02),
    "CONFLUENCE_PCT":       (0.001, 0.02),
    "MAX_HOLD_HOURS":       (1,     168),
}

# Anti-runaway: if the same param has been adjusted in the same direction
# this many times in this window, freeze further changes to it.
ANTI_RUNAWAY_WINDOW_DAYS = 7
ANTI_RUNAWAY_SAME_DIR_COUNT = 3

# Revert: applied changes older than this with regressed performance get rolled back.
REVERT_AGE_DAYS = 7
REVERT_LOOKBACK_DAYS = 14
REVERT_MIN_TRADES = 5


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

    No human gate. Returns {env_key: {"old": ..., "new": ...}} so revert
    has the original values to restore later.
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


def _write_env_value(env_path: Path, env_key: str, value) -> None:
    """Single-key writer used by revert."""
    if not env_path.exists():
        return
    lines = env_path.read_text().splitlines()
    replaced = False
    for i, ln in enumerate(lines):
        if ln.startswith(f"{env_key}="):
            lines[i] = f"{env_key}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{env_key}={value}")
    env_path.write_text("\n".join(lines) + "\n")


def clamp_proposal(proposed: dict) -> tuple[dict, list[str]]:
    """Force every proposed value into its PARAM_RANGES bounds.

    Returns (clamped_dict, notes_list). Notes list contains a string per
    clamp action so the reflection summary can show what was bounded.
    """
    out: dict = {}
    notes: list[str] = []
    for k, v in proposed.items():
        env_k = k.upper()
        if env_k in PARAM_RANGES:
            lo, hi = PARAM_RANGES[env_k]
            try:
                vf = float(v)
            except (TypeError, ValueError):
                out[k] = v
                continue
            clamped = max(lo, min(hi, vf))
            if clamped != vf:
                notes.append(f"CLAMPED {env_k}: {vf} -> {clamped} (range [{lo}, {hi}])")
            # Preserve int-ness for things like COOLDOWN_MIN
            if isinstance(v, int) and not isinstance(v, bool):
                clamped = int(round(clamped))
            out[k] = clamped
        else:
            out[k] = v
    return out, notes


def frozen_params() -> dict[str, str]:
    """Find params modified in the same direction ANTI_RUNAWAY_SAME_DIR_COUNT
    times within ANTI_RUNAWAY_WINDOW_DAYS. Returns {env_key: reason}.
    """
    try:
        with db_conn() as conn:
            df = pd.read_sql_query(
                f"""
                SELECT applied_ts, applied_changes
                FROM reflections
                WHERE applied = TRUE
                  AND applied_ts >= NOW() - INTERVAL '{ANTI_RUNAWAY_WINDOW_DAYS} days'
                  AND applied_changes IS NOT NULL
                ORDER BY applied_ts ASC
                """,
                conn,
            )
    except Exception:
        return {}
    history: dict[str, list[int]] = {}  # env_key -> list of +1/-1
    for _, row in df.iterrows():
        ac = row["applied_changes"]
        if isinstance(ac, str):
            try:
                ac = json.loads(ac)
            except Exception:
                continue
        if not isinstance(ac, dict):
            continue
        for env_key, change in ac.items():
            try:
                old = float(change.get("old")) if change.get("old") is not None else None
                new = float(change.get("new"))
            except (TypeError, ValueError, AttributeError):
                continue
            if old is None:
                continue
            direction = 1 if new > old else -1 if new < old else 0
            if direction != 0:
                history.setdefault(env_key, []).append(direction)

    frozen: dict[str, str] = {}
    for env_key, dirs in history.items():
        last = dirs[-ANTI_RUNAWAY_SAME_DIR_COUNT:]
        if len(last) < ANTI_RUNAWAY_SAME_DIR_COUNT:
            continue
        if all(d == 1 for d in last) or all(d == -1 for d in last):
            sign = "increasing" if last[0] == 1 else "decreasing"
            frozen[env_key] = (
                f"{sign} for {ANTI_RUNAWAY_SAME_DIR_COUNT} consecutive applied "
                f"reflections in last {ANTI_RUNAWAY_WINDOW_DAYS}d — frozen"
            )
    return frozen


def check_reverts(env_path: Path) -> list[dict]:
    """Revert applied changes older than REVERT_AGE_DAYS whose performance
    has not improved vs the prior REVERT_LOOKBACK_DAYS window.

    Returns a list of revert actions for inclusion in the reflection summary.
    """
    actions: list[dict] = []
    try:
        with db_conn() as conn:
            refs = pd.read_sql_query(
                f"""
                SELECT id, applied_ts, applied_changes
                FROM reflections
                WHERE applied = TRUE
                  AND applied_ts < NOW() - INTERVAL '{REVERT_AGE_DAYS} days'
                  AND applied_ts > NOW() - INTERVAL '21 days'
                  AND reverted_at IS NULL
                  AND applied_changes IS NOT NULL
                ORDER BY applied_ts ASC
                """,
                conn,
            )
    except Exception as e:
        logging.warning("check_reverts: db read failed: %s", e)
        return actions

    for _, ref in refs.iterrows():
        ac = ref["applied_changes"]
        if isinstance(ac, str):
            try:
                ac = json.loads(ac)
            except Exception:
                continue
        if not isinstance(ac, dict) or not ac:
            continue
        applied_ts = ref["applied_ts"]
        try:
            with db_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                      AVG(CASE WHEN exit_ts <  %s THEN pnl_pct END) AS before_mean,
                      COUNT(*) FILTER (WHERE exit_ts <  %s)         AS before_n,
                      AVG(CASE WHEN exit_ts >= %s THEN pnl_pct END) AS after_mean,
                      COUNT(*) FILTER (WHERE exit_ts >= %s)         AS after_n
                    FROM trades
                    WHERE strategy = 'sr_paper_bot'
                      AND exit_ts IS NOT NULL
                      AND exit_ts >= %s - INTERVAL '{REVERT_LOOKBACK_DAYS} days'
                      AND exit_ts <  %s + INTERVAL '{REVERT_LOOKBACK_DAYS} days'
                    """,
                    (applied_ts, applied_ts, applied_ts, applied_ts,
                     applied_ts, applied_ts),
                )
                before_mean, before_n, after_mean, after_n = cur.fetchone()
        except Exception as e:
            logging.warning("check_reverts: pnl window query failed: %s", e)
            continue

        if not (before_n and after_n and
                before_n >= REVERT_MIN_TRADES and after_n >= REVERT_MIN_TRADES):
            continue

        before_mean = float(before_mean or 0)
        after_mean = float(after_mean or 0)
        # Revert when post-apply mean PnL is strictly worse than pre-apply.
        # Add a small noise threshold so we don't churn on tiny shifts.
        if after_mean >= before_mean - 0.001:
            continue

        reverted: dict = {}
        for env_key, change in ac.items():
            if change.get("old") in (None, ""):
                continue
            _write_env_value(env_path, env_key, change["old"])
            reverted[env_key] = change

        if not reverted:
            continue

        reason = (f"perf regressed: before_mean={before_mean:.4f} "
                  f"after_mean={after_mean:.4f} (n_before={before_n}, n_after={after_n})")
        try:
            with db_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE reflections SET reverted_at = NOW(), "
                    "revert_reason = %s WHERE id = %s",
                    (reason, int(ref["id"])),
                )
        except Exception as e:
            logging.warning("check_reverts: marking reverted failed: %s", e)

        logging.warning("REVERTED reflection #%d — %s", int(ref["id"]), reason)
        actions.append({
            "reflection_id": int(ref["id"]),
            "reverted": reverted,
            "before_mean": before_mean,
            "after_mean": after_mean,
            "n_before": int(before_n),
            "n_after": int(after_n),
        })
    return actions


def main() -> dict:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("reflection")

    trades = fetch_recent_trades(LOOKBACK_DAYS)
    signals = fetch_recent_signals(LOOKBACK_DAYS)
    log.info("loaded %d trades and %d signals from last %d days",
             len(trades), len(signals), LOOKBACK_DAYS)

    summary, proposed, metrics = analyze(trades, signals)
    env_path = Path(os.environ.get("SR_BOT_ENV_PATH", "/app/sr-bot/.env"))

    # Phase-0 gate 1: revert any stale applied changes whose performance regressed.
    reverts = check_reverts(env_path)
    if reverts:
        summary += f"\nREVERTED {len(reverts)} stale change(s): " \
                   f"{json.dumps([r['reverted'] for r in reverts])}"
        log.warning("reverted %d stale reflection(s) — see summary", len(reverts))

    # Phase-0 gate 2: clamp proposals to hard parameter ranges.
    if proposed:
        proposed, clamp_notes = clamp_proposal(proposed)
        if clamp_notes:
            summary += "\n" + "\n".join(clamp_notes)

    # Phase-0 gate 3: anti-runaway freeze. Drop proposed changes for params
    # that have been moved the same direction too many times recently.
    frozen = frozen_params() if proposed else {}
    if frozen:
        before = dict(proposed)
        proposed = {k: v for k, v in proposed.items() if k.upper() not in frozen}
        dropped = {k: before[k] for k in before if k not in proposed}
        if dropped:
            summary += f"\nFROZEN (anti-runaway) skipped {len(dropped)}: " + \
                       "; ".join(f"{k.upper()} — {frozen[k.upper()]}" for k in dropped)
            log.info("anti-runaway dropped %d proposed changes: %s", len(dropped), dropped)

    # Phase-8: retrain meta-classifier on the latest closed trades.
    if os.environ.get("META_ENABLED", "true").lower() == "true":
        try:
            from meta_classifier import train as meta_train
            meta_result = meta_train()
            summary += "\nMETA_CLASSIFIER: " + json.dumps({
                k: round(v, 4) if isinstance(v, float) else v
                for k, v in meta_result.items()
                if k in ("ok", "reason", "n_samples", "val_auc")
            })
        except Exception as e:
            log.warning("meta-classifier train failed: %s", e)
            summary += f"\nMETA_CLASSIFIER: ERROR — {e}"

    # Phase-6 retirement gate: disable sub-strategies with persistently
    # negative hit rate; re-enable ones that have recovered. Runs every
    # reflection cycle, regardless of whether a param proposal exists.
    if os.environ.get("STRATEGY_RETIREMENT_ENABLED", "true").lower() == "true":
        try:
            from strategy_health import evaluate_retirement, apply_retirement
            ret_result = evaluate_retirement()
            applied_count = apply_retirement(ret_result)
            if applied_count:
                summary += f"\nSTRATEGY_RETIREMENT: applied {applied_count} action(s) — " + \
                           json.dumps([{a['action']: a['name']} for a in ret_result["actions"]])
            else:
                summary += f"\nSTRATEGY_RETIREMENT: no actions ({len(ret_result.get('hit_rates_30d', {}))} strategies evaluated)"
        except Exception as e:
            log.warning("retirement gate failed: %s", e)
            summary += f"\nSTRATEGY_RETIREMENT: ERROR — {e}"

    # Phase-2 gate: walk-forward validation. Replay current vs proposed params
    # on last 30 days; only apply if 30d AND 7d-OOS Sharpe both improve.
    wf_result: dict = {}
    if proposed and os.environ.get("REFLECTION_WALKFORWARD_GATE", "true").lower() == "true":
        try:
            from walkforward import walk_forward_gate
            wf_result = walk_forward_gate(proposed)
            summary += "\nWALKFORWARD: " + json.dumps({
                k: v for k, v in wf_result.items()
                if k in ("passed", "reason", "n_trades_curr", "n_trades_prop",
                         "sharpe_curr_30d", "sharpe_prop_30d",
                         "sharpe_curr_oos", "sharpe_prop_oos")
            })
            if not wf_result.get("passed"):
                log.warning("walk-forward GATE REJECTED proposal: %s",
                            wf_result.get("reason"))
                proposed = {}
        except Exception as e:
            log.warning("walk-forward gate failed (proceeding without): %s", e)
            summary += f"\nWALKFORWARD: ERROR — {e}"

    # Auto-apply (no human gate). Disable by setting REFLECTION_AUTO_APPLY=false.
    auto = os.environ.get("REFLECTION_AUTO_APPLY", "true").lower() == "true"
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
                summary, proposed_changes, metrics, applied, applied_ts,
                applied_changes
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            "sr_paper_bot", LOOKBACK_DAYS, len(trades), len(signals),
            summary, json.dumps(proposed), json.dumps(metrics, default=str),
            bool(applied),
            datetime.now(timezone.utc) if applied else None,
            json.dumps(applied) if applied else None,
        ))
        ref_id = cur.fetchone()[0]

    log.info("reflection #%d saved.", ref_id)
    return {"reflection_id": ref_id, "summary": summary,
            "proposed": proposed, "applied": applied, "reverts": reverts}


if __name__ == "__main__":
    main()
