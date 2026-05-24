"""Risk circuit breakers. These VETO entries — they cannot be overridden by
strategy logic. The chain of command is:

    risk_engine.veto_reason() -> if not None, no entry happens.

Three hard limits:
  1. DAILY_LOSS_CAP_PCT — if realized + unrealized PnL today is below this
     fraction of start-of-day equity, no new entries until UTC midnight.
  2. MAX_DRAWDOWN_PCT   — if equity has dropped this far from its all-time
     peak (tracked in risk_state table), enter LOCKDOWN until equity recovers
     to KILL_SWITCH_RECOVERY_PCT * peak. While locked, no new entries.
  3. POSITION_CONCENTRATION_PCT — single-symbol market_value cannot exceed
     this fraction of equity. Used to size-down new entries, not veto them.

State is persisted to a tiny `risk_state` table so a bot restart doesn't
reset the all-time peak or lockdown flag.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras


@dataclass
class RiskLimits:
    daily_loss_cap_pct: float = 0.03           # 3% of SOD equity
    max_drawdown_pct: float = 0.15             # 15% from peak triggers lockdown
    kill_switch_recovery_pct: float = 0.97     # unlock when equity >= 97% of peak
    position_concentration_pct: float = 0.10   # 10% of equity per symbol
    max_open_positions: int = 8

    @classmethod
    def from_env(cls) -> "RiskLimits":
        return cls(
            daily_loss_cap_pct=float(os.environ.get("DAILY_LOSS_CAP_PCT", "0.03")),
            max_drawdown_pct=float(os.environ.get("MAX_DRAWDOWN_PCT", "0.15")),
            kill_switch_recovery_pct=float(os.environ.get("KILL_SWITCH_RECOVERY_PCT", "0.97")),
            position_concentration_pct=float(os.environ.get("POSITION_CONCENTRATION_PCT", "0.10")),
            max_open_positions=int(os.environ.get("MAX_OPEN_POSITIONS", "8")),
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _db_conn():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", "5432")),
        user=os.environ.get("DB_USER", "trader"),
        password=os.environ.get("DB_PASSWORD") or os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ.get("DB_NAME", "trading"),
    )


def ensure_table():
    """Create risk_state and risk_events on first run. Idempotent."""
    with _db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS risk_state (
                id INTEGER PRIMARY KEY DEFAULT 1,
                peak_equity DOUBLE PRECISION NOT NULL DEFAULT 0,
                peak_equity_ts TIMESTAMPTZ,
                sod_equity DOUBLE PRECISION NOT NULL DEFAULT 0,
                sod_date DATE,
                lockdown BOOLEAN NOT NULL DEFAULT FALSE,
                lockdown_reason TEXT,
                lockdown_ts TIMESTAMPTZ,
                CHECK (id = 1)
            );
            INSERT INTO risk_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

            CREATE TABLE IF NOT EXISTS risk_events (
                id BIGSERIAL PRIMARY KEY,
                ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                kind TEXT NOT NULL,
                detail JSONB
            );
            CREATE INDEX IF NOT EXISTS idx_risk_events_ts ON risk_events (ts DESC);
        """)


def _get_state() -> dict:
    with _db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM risk_state WHERE id = 1")
        return dict(cur.fetchone() or {})


def _save_state(**kwargs) -> None:
    if not kwargs:
        return
    cols = ", ".join(f"{k} = %s" for k in kwargs)
    vals = list(kwargs.values())
    with _db_conn() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE risk_state SET {cols} WHERE id = 1", vals)


def _log_event(kind: str, detail: dict) -> None:
    import json
    with _db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO risk_events (kind, detail) VALUES (%s, %s)",
            (kind, json.dumps(detail, default=str)),
        )


# ---------------------------------------------------------------------------
# Checks — called by the bot before each entry
# ---------------------------------------------------------------------------

def update_peak_and_sod(current_equity: float) -> dict:
    """Tick housekeeping: update peak equity + start-of-day baseline.

    Returns the post-update state for use in veto checks.
    """
    state = _get_state()
    today = datetime.now(timezone.utc).date()
    updates: dict = {}

    if state.get("peak_equity", 0) == 0 or current_equity > state["peak_equity"]:
        updates["peak_equity"] = current_equity
        updates["peak_equity_ts"] = datetime.now(timezone.utc)

    if state.get("sod_date") != today:
        updates["sod_equity"] = current_equity
        updates["sod_date"] = today

    if updates:
        _save_state(**updates)
        state.update(updates)
    return state


def veto_reason(
    limits: RiskLimits,
    current_equity: float,
    open_positions: list[dict],
    new_position_notional: float,
    symbol: str,
) -> Optional[str]:
    """Return a string reason if the trade must be blocked, else None.

    Called per-entry. Cheap.
    """
    state = update_peak_and_sod(current_equity)

    # 1) Persistent lockdown after max drawdown
    if state.get("lockdown"):
        peak = state.get("peak_equity") or current_equity
        if current_equity >= peak * limits.kill_switch_recovery_pct:
            _save_state(lockdown=False, lockdown_reason=None, lockdown_ts=None)
            _log_event("lockdown_lifted", {
                "equity": current_equity, "peak": peak,
                "recovery_pct": limits.kill_switch_recovery_pct,
            })
            logging.warning("RISK: lockdown lifted, equity recovered to %.2f", current_equity)
        else:
            return f"lockdown ({state.get('lockdown_reason', 'unknown')})"

    # 2) Trigger lockdown on max drawdown
    peak = state.get("peak_equity") or current_equity
    dd = 1.0 - (current_equity / peak) if peak > 0 else 0.0
    if dd >= limits.max_drawdown_pct:
        _save_state(lockdown=True,
                    lockdown_reason=f"max_drawdown {dd:.2%}",
                    lockdown_ts=datetime.now(timezone.utc))
        _log_event("lockdown_triggered", {
            "equity": current_equity, "peak": peak, "drawdown_pct": dd,
            "limit": limits.max_drawdown_pct,
        })
        logging.error("RISK LOCKDOWN: drawdown %.2f%% exceeds %.2f%%",
                      dd * 100, limits.max_drawdown_pct * 100)
        return f"max_drawdown {dd:.2%}"

    # 3) Daily loss cap
    sod = state.get("sod_equity") or current_equity
    daily_pnl_pct = (current_equity / sod) - 1.0 if sod > 0 else 0.0
    if daily_pnl_pct <= -limits.daily_loss_cap_pct:
        _log_event("daily_loss_cap_hit", {
            "equity": current_equity, "sod": sod, "daily_pnl_pct": daily_pnl_pct,
            "limit": limits.daily_loss_cap_pct,
        })
        return f"daily_loss_cap {daily_pnl_pct:.2%}"

    # 4) Max open positions
    if len(open_positions) >= limits.max_open_positions:
        return f"max_open_positions {len(open_positions)}/{limits.max_open_positions}"

    # 5) Per-symbol concentration. Existing position + new notional must not
    # exceed cap.
    sym_keys = {symbol, symbol.replace("/", "")}
    existing = sum(
        abs(float(p.get("market_value", 0)))
        for p in open_positions if p.get("symbol") in sym_keys
    )
    proj = (existing + new_position_notional) / max(1e-6, current_equity)
    if proj > limits.position_concentration_pct:
        return (f"concentration {proj:.1%} > {limits.position_concentration_pct:.1%} "
                f"on {symbol}")

    return None


def scale_for_volatility(
    base_notional: float,
    atr_pct: float,
    target_atr_pct: float = 0.01,
    floor: float = 0.4,
    ceiling: float = 1.5,
) -> float:
    """Scale base_notional inversely to ATR. Used by sizing.py."""
    if atr_pct <= 0 or target_atr_pct <= 0:
        return base_notional
    ratio = target_atr_pct / atr_pct
    ratio = max(floor, min(ceiling, ratio))
    return base_notional * ratio
