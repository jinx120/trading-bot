"""Trading platform admin dashboard.

Three pages, focused on what matters:
  - Bot:         live Alpaca account, open positions, recent trades, run-tick buttons
  - Reflections: what the bot has learned from its own data, with approve buttons
  - Ingest:     pull historical bars from Alpaca for reflection backtests

Everything else previously here (Universe / Strategies / Backtests / Allocator /
Runner Controls / Test Trade / Live Signals) has been retired with the
research + multi-strategy runner.

Auth: NONE. Bind to localhost or tailnet only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app")

import pandas as pd
import requests
import streamlit as st
from sqlalchemy import text

from common.db import get_engine

st.set_page_config(page_title="Trading Bot", page_icon="📊", layout="wide")


@st.cache_resource
def engine():
    return get_engine()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
st.sidebar.title("Trading Bot")
st.sidebar.caption(f"UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
page = st.sidebar.radio("Page", ["Bot", "Symbols", "Reflections"])

_alpaca_ok = bool(os.environ.get("ALPACA_API_KEY"))
if _alpaca_ok:
    st.sidebar.success("Alpaca: paper")
else:
    st.sidebar.error("Alpaca: keys missing")


_HEADERS = {
    "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
    "APCA-API-SECRET-KEY": os.environ.get("ALPACA_API_SECRET", ""),
}
_PAPER = "https://paper-api.alpaca.markets"


def alpaca_account() -> dict:
    try:
        return requests.get(f"{_PAPER}/v2/account", headers=_HEADERS, timeout=8).json()
    except Exception as e:
        return {"error": str(e)}


def alpaca_positions() -> list[dict]:
    try:
        r = requests.get(f"{_PAPER}/v2/positions", headers=_HEADERS, timeout=8).json()
        return r if isinstance(r, list) else []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Page: Bot
# ---------------------------------------------------------------------------
if page == "Bot":
    st.title("S&R Paper Bot")
    st.caption(
        "Trades 5-bar S&R pivots on 1H+4H with confluence sizing and a "
        "regime filter. Paper account, $250 base notional per trade."
    )

    acct = alpaca_account()
    positions = alpaca_positions()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Equity", f"${float(acct.get('equity', 0)):,.2f}")
    c2.metric("Cash", f"${float(acct.get('cash', 0)):,.2f}")
    c3.metric("Buying Power", f"${float(acct.get('buying_power', 0)):,.2f}")
    c4.metric("Open Positions", len(positions))

    # Phase 1 telemetry: drawdown throttle + portfolio risk
    with engine().connect() as conn:
        try:
            rs = conn.execute(text(
                "SELECT peak_equity, lockdown, lockdown_reason FROM risk_state WHERE id = 1"
            )).fetchone()
        except Exception:
            rs = None
        equity_val = float(acct.get("equity", 0) or 0)
        peak = float(rs.peak_equity) if rs and rs.peak_equity else equity_val
        dd_pct = (1.0 - equity_val / peak) * 100 if peak > 0 else 0.0
        slope = float(os.environ.get("DD_THROTTLE_SLOPE", 4.0))
        floor = float(os.environ.get("DD_THROTTLE_FLOOR", 0.20))
        size_mult = max(floor, 1.0 - slope * max(0.0, dd_pct / 100))
        try:
            r = conn.execute(text("""
                SELECT
                  SUM(ABS(entry_price - (metadata->>'sl')::float) * quantity) AS open_dollar_risk
                FROM trades
                WHERE strategy = 'sr_paper_bot' AND exit_ts IS NULL
                  AND (metadata->>'sl') IS NOT NULL
            """)).fetchone()
            open_risk = float(r.open_dollar_risk or 0)
        except Exception:
            open_risk = 0.0
        max_port_risk = float(os.environ.get("MAX_PORTFOLIO_RISK_PCT", 0.02))
        open_risk_pct = (open_risk / equity_val) * 100 if equity_val > 0 else 0.0

    r1, r2, r3 = st.columns(3)
    r1.metric("Drawdown", f"{dd_pct:.2f}%",
              help=f"from peak ${peak:,.2f}; lockdown engages at "
                   f"{float(os.environ.get('MAX_DRAWDOWN_PCT', 0.15))*100:.0f}%")
    r2.metric("Size throttle", f"{size_mult:.2f}×",
              help=f"Vol-target sizing × this multiplier (slope={slope}, floor={floor})")
    r3.metric("Portfolio risk", f"{open_risk_pct:.2f}% / {max_port_risk*100:.0f}%",
              help=f"Sum of |entry - SL| × qty across open bot trades. "
                   f"New entries blocked when this is at cap.")
    if rs and rs.lockdown:
        st.error(f"⛔ LOCKDOWN: {rs.lockdown_reason} — no new entries until equity recovers")

    st.subheader("Open positions (live)")
    if positions:
        st.dataframe(pd.DataFrame([{
            "symbol": p["symbol"],
            "qty": float(p["qty"]),
            "avg_entry": float(p["avg_entry_price"]),
            "market_price": float(p["current_price"]),
            "market_value": float(p["market_value"]),
            "unrealized_pnl": float(p["unrealized_pl"]),
            "unrealized_pct": round(float(p["unrealized_plpc"]) * 100, 2),
        } for p in positions]), use_container_width=True, hide_index=True)
    else:
        st.info("No open positions.")

    # ---- Ensemble — Phase 4 ----
    st.subheader("Ensemble (latest composite per symbol)")
    st.caption(
        "Composite = weighted sum of sub-strategy scores. |composite| must clear "
        f"±{float(os.environ.get('ENSEMBLE_ENTRY_THRESHOLD', 0.40)):.2f} for the "
        "bot to enter. Disagreement → no trade."
    )
    with engine().connect() as conn:
        try:
            weights_df = pd.read_sql_query(text("""
                SELECT name, ROUND(weight::numeric, 3) AS weight,
                       enabled, last_updated
                FROM strategy_weights ORDER BY name
            """), conn)
            st.dataframe(weights_df, use_container_width=True, hide_index=True)
        except Exception as e:
            st.info(f"strategy_weights not populated: {e}")

        try:
            latest_scores = pd.read_sql_query(text("""
                WITH ranked AS (
                  SELECT symbol, strategy, score,
                         ROW_NUMBER() OVER (PARTITION BY symbol, strategy ORDER BY ts DESC) AS rn,
                         ts
                  FROM scores
                  WHERE ts > NOW() - INTERVAL '15 minutes'
                )
                SELECT symbol, strategy, ROUND(score::numeric, 3) AS score, ts
                FROM ranked WHERE rn = 1
                ORDER BY symbol, strategy
            """), conn)
            if not latest_scores.empty:
                # Pivot to per-symbol row, per-strategy column
                pivoted = latest_scores.pivot(index="symbol", columns="strategy",
                                              values="score").fillna(0.0)
                # Compute composite using current weights
                weights = dict(zip(weights_df["name"], weights_df["weight"])) \
                    if not weights_df.empty else {}
                enabled_set = set(weights_df[weights_df["enabled"]]["name"]) \
                    if not weights_df.empty else set()
                composite = sum(
                    pivoted.get(s, 0) * weights.get(s, 0)
                    for s in pivoted.columns if s in enabled_set
                )
                if isinstance(composite, pd.Series):
                    pivoted["composite"] = composite.round(3)
                threshold = float(os.environ.get("ENSEMBLE_ENTRY_THRESHOLD", 0.40))
                pivoted["entry_eligible"] = pivoted["composite"].abs() >= threshold
                st.dataframe(pivoted, use_container_width=True)
            else:
                st.info("No recent score rows yet — bot needs one tick.")
        except Exception as e:
            st.warning(f"score query failed: {e}")

    st.subheader("Recent bot trades")
    with engine().connect() as conn:
        bot_trades = pd.read_sql_query(text("""
            SELECT entry_ts, symbol, side,
                   ROUND(entry_price::numeric, 6) AS entry_price,
                   ROUND(quantity::numeric, 6) AS qty,
                   exit_ts,
                   ROUND(exit_price::numeric, 6) AS exit_price,
                   exit_reason,
                   ROUND((pnl_pct * 100)::numeric, 2) AS pnl_pct,
                   metadata->>'regime' AS regime,
                   metadata->>'confluence' AS confluence,
                   metadata->>'notional_mult' AS mult
            FROM trades
            WHERE strategy = 'sr_paper_bot'
            ORDER BY entry_ts DESC LIMIT 50
        """), conn)
    if bot_trades.empty:
        st.info("No bot trades yet.")
    else:
        st.dataframe(bot_trades, use_container_width=True, hide_index=True)

    st.subheader("Last 100 signals evaluated")
    st.caption("Every symbol the bot looked at — whether it traded or not. "
               "Feeds reflection.")
    with engine().connect() as conn:
        sig_df = pd.read_sql_query(text("""
            SELECT ts, symbol, side, took_trade, skip_reason,
                   ROUND(close::numeric, 6) AS close,
                   ROUND(trigger_level::numeric, 6) AS trigger_level,
                   confluence, regime,
                   ROUND(adx::numeric, 2) AS adx,
                   ROUND(approach_pct::numeric, 4) AS approach_pct
            FROM signals
            WHERE strategy = 'sr_paper_bot'
            ORDER BY ts DESC LIMIT 100
        """), conn)
    if sig_df.empty:
        st.info("Signals table will populate once the bot restarts with the "
                "logging extension.")
    else:
        st.dataframe(sig_df, use_container_width=True, hide_index=True)

    st.subheader("Controls")
    cc1, cc2 = st.columns(2)
    if cc1.button("▶ Run one tick (dry-run)"):
        r = subprocess.run(
            ["python", "/app/sr-bot/sr_paper_bot.py", "--once", "--dry-run"],
            capture_output=True, text=True, timeout=120,
        )
        st.code(r.stdout + ("\n" + r.stderr if r.stderr else ""), language="text")
    if cc2.button("▶ Run one tick LIVE", type="primary"):
        r = subprocess.run(
            ["python", "/app/sr-bot/sr_paper_bot.py", "--once"],
            capture_output=True, text=True, timeout=120,
        )
        st.code(r.stdout + ("\n" + r.stderr if r.stderr else ""), language="text")

    st.subheader("Config")
    cfg_path = "/app/sr-bot/.env"
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            st.code(f.read(), language="bash")


# ---------------------------------------------------------------------------
# Page: Reflections
# ---------------------------------------------------------------------------
elif page == "Reflections":
    st.title("Reflections")
    st.caption(
        "Autonomous self-improvement loop. The bot periodically analyzes its "
        "own trade history and **auto-applies** parameter changes to its "
        ".env. No human gate. Bot hot-reloads .env every few ticks."
    )

    # Risk state header
    with engine().connect() as conn:
        try:
            rs = conn.execute(text(
                "SELECT peak_equity, sod_equity, sod_date, lockdown, "
                "lockdown_reason, lockdown_ts FROM risk_state WHERE id = 1"
            )).fetchone()
        except Exception:
            rs = None
    if rs is not None:
        c1, c2, c3 = st.columns(3)
        c1.metric("Peak equity", f"${(rs.peak_equity or 0):,.2f}")
        c2.metric("SOD equity", f"${(rs.sod_equity or 0):,.2f}",
                  help=f"start-of-day baseline · date={rs.sod_date}")
        if rs.lockdown:
            c3.error(f"LOCKDOWN: {rs.lockdown_reason}")
        else:
            c3.success("Risk: OK")

    if st.button("▶ Run reflection now"):
        with st.spinner("Analyzing signals + trades + applying changes..."):
            r = subprocess.run(
                ["python", "/app/sr-bot/reflection.py"],
                capture_output=True, text=True, timeout=120,
            )
        st.code(r.stdout + ("\n" + r.stderr if r.stderr else ""), language="text")

    st.subheader("Recent reflections")
    with engine().connect() as conn:
        refl = pd.read_sql_query(text("""
            SELECT id, run_ts, n_trades_analyzed, n_signals_analyzed,
                   summary, proposed_changes, applied, applied_ts
            FROM reflections
            ORDER BY run_ts DESC LIMIT 20
        """), conn)

    if refl.empty:
        st.info("No reflections yet. The bot triggers one every "
                "REFLECTION_EVERY_TICKS ticks. You can also run one manually "
                "with the button above.")
    else:
        for _, row in refl.iterrows():
            badge = "✅ APPLIED" if row["applied"] else "—"
            with st.expander(
                f"#{row['id']} · {row['run_ts'].strftime('%Y-%m-%d %H:%M')} · "
                f"{row['n_trades_analyzed']} trades · {badge}"
            ):
                st.text(row["summary"])
                proposed = row["proposed_changes"]
                if isinstance(proposed, str):
                    proposed = json.loads(proposed) if proposed else {}
                if proposed:
                    st.json(proposed)
                else:
                    st.caption("No actionable changes proposed.")

    st.subheader("Recent risk events")
    with engine().connect() as conn:
        try:
            events = pd.read_sql_query(text("""
                SELECT ts, kind, detail FROM risk_events
                ORDER BY ts DESC LIMIT 30
            """), conn)
            if events.empty:
                st.caption("None — circuit breakers haven't fired.")
            else:
                st.dataframe(events, use_container_width=True, hide_index=True)
        except Exception:
            st.caption("(risk_events table not yet created — bot needs one tick.)")


# ---------------------------------------------------------------------------
# Page: Symbols — flashy GUI for managing the bot's trading universe
# ---------------------------------------------------------------------------
elif page == "Symbols":
    st.title("🎯 Symbols")
    st.caption(
        "What the bot watches and trades. Toggle a symbol off and it stops "
        "considering new entries for that ticker. Bot picks up changes within "
        "5 ticks (hot-reload). Existing positions are NOT auto-closed when "
        "you disable — they exit on their own SL/TP/trail."
    )

    ENV_PATH = "/app/sr-bot/.env"
    BOT_ENV_PATH_HOST = "/home/redji/sr-bot/.env"

    def _read_env() -> dict:
        env = {}
        if os.path.exists(ENV_PATH):
            for line in open(ENV_PATH):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
        return env

    def _write_env_key(key: str, value: str) -> None:
        for path in (ENV_PATH, BOT_ENV_PATH_HOST):
            if not os.path.exists(path):
                continue
            try:
                lines = open(path).read().splitlines()
                replaced = False
                for i, ln in enumerate(lines):
                    if ln.startswith(f"{key}="):
                        lines[i] = f"{key}={value}"
                        replaced = True
                        break
                if not replaced:
                    lines.append(f"{key}={value}")
                with open(path, "w") as f:
                    f.write("\n".join(lines) + "\n")
            except Exception as e:
                st.error(f"writing {path}: {e}")

    _env = _read_env()
    active_crypto = [s.strip() for s in _env.get(
        "SR_BOT_SYMBOLS", "BTC/USD,ETH/USD,SOL/USD,LINK/USD,AVAX/USD,DOGE/USD,AAVE/USD"
    ).split(",") if s.strip()]
    active_equity = [s.strip() for s in _env.get(
        "SR_BOT_EQUITIES", "DIA,SPY,QQQ"
    ).split(",") if s.strip()]

    # Build a pool of "known" symbols = currently active plus a curated list
    # so the user can quickly toggle popular tickers on/off.
    CRYPTO_POOL = sorted(set(active_crypto) | {
        "BTC/USD", "ETH/USD", "SOL/USD", "LINK/USD", "AVAX/USD",
        "DOGE/USD", "AAVE/USD", "ADA/USD", "DOT/USD", "MATIC/USD",
        "UNI/USD", "XRP/USD", "LTC/USD", "BCH/USD",
    })
    EQUITY_POOL = sorted(set(active_equity) | {
        "DIA", "SPY", "QQQ", "IWM", "TQQQ", "SOXL", "TLT",
        "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
    })

    # ---- Pull live prices (cached for 15s to avoid per-render Alpaca calls) ----
    @st.cache_data(ttl=15, show_spinner=False)
    def _fetch_prices(crypto_csv: str, equity_csv: str) -> tuple[dict, dict]:
        headers = {
            "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": os.environ.get("ALPACA_API_SECRET", ""),
        }
        cp, ep = {}, {}
        if crypto_csv:
            try:
                r = requests.get(
                    "https://data.alpaca.markets/v1beta3/crypto/us/latest/trades",
                    params={"symbols": crypto_csv},
                    headers=headers, timeout=8,
                )
                if r.ok:
                    for sym, t in r.json().get("trades", {}).items():
                        cp[sym] = {"price": float(t["p"])}
            except Exception:
                pass
        if equity_csv:
            try:
                r = requests.get(
                    "https://data.alpaca.markets/v2/stocks/trades/latest",
                    params={"symbols": equity_csv, "feed": "iex"},
                    headers=headers, timeout=8,
                )
                if r.ok:
                    for sym, t in r.json().get("trades", {}).items():
                        ep[sym] = {"price": float(t["p"])}
            except Exception:
                pass
        return cp, ep

    crypto_prices, equity_prices = _fetch_prices(
        ",".join(CRYPTO_POOL), ",".join(EQUITY_POOL),
    )

    # ---- Open positions lookup ----
    open_by_sym = {p["symbol"]: p for p in alpaca_positions()}

    def render_card_grid(pool: list[str], active: list[str], prices: dict,
                         label_emoji: str, env_key: str):
        """Render a grid of toggle cards. Uses st.container for stable layout
        instead of inline-styled divs (lighter on Streamlit's renderer)."""
        active_set = set(active)
        per_row = 4
        rows = (len(pool) + per_row - 1) // per_row
        new_active: list[str] = []
        for r in range(rows):
            cols = st.columns(per_row)
            for c in range(per_row):
                i = r * per_row + c
                if i >= len(pool):
                    continue
                sym = pool[i]
                px = prices.get(sym, {}).get("price")
                pos = open_by_sym.get(sym.replace("/", ""))
                with cols[c]:
                    # Stable, fixed-width text so font swap doesn't shift layout.
                    px_str = f"${px:,.4f}" if px is not None else "—".ljust(10)
                    pos_str = (f"📍 {float(pos['qty']):.4f}" if pos else "")
                    st.markdown(
                        f"**{label_emoji} {sym}**  \n"
                        f"`{px_str}`  \n"
                        f"<small>{pos_str}</small>",
                        unsafe_allow_html=True,
                    )
                    enabled = st.toggle(
                        "active" if sym in active_set else "inactive",
                        value=(sym in active_set),
                        key=f"toggle_{env_key}_{sym}", label_visibility="collapsed",
                    )
                    if enabled:
                        new_active.append(sym)
        return new_active

    st.subheader("🪙 Crypto (24/7)")
    new_crypto = render_card_grid(CRYPTO_POOL, active_crypto, crypto_prices,
                                  "🪙", "SR_BOT_SYMBOLS")
    if set(new_crypto) != set(active_crypto):
        _write_env_key("SR_BOT_SYMBOLS", ",".join(new_crypto))
        st.success(f"✓ Updated crypto universe ({len(new_crypto)} symbols). "
                   f"Bot hot-reloads within ~5 minutes.")

    st.divider()

    st.subheader("📈 Equities (US market hours only)")
    new_equity = render_card_grid(EQUITY_POOL, active_equity, equity_prices,
                                  "📈", "SR_BOT_EQUITIES")
    if set(new_equity) != set(active_equity):
        _write_env_key("SR_BOT_EQUITIES", ",".join(new_equity))
        st.success(f"✓ Updated equity universe ({len(new_equity)} symbols).")

    st.divider()

    # ---- Add custom symbol ----
    st.subheader("➕ Add custom symbol")
    cc1, cc2, cc3 = st.columns([2, 1, 1])
    with cc1:
        new_sym = st.text_input(
            "Symbol (e.g. SHIB/USD for crypto, NVDA for equity)",
            label_visibility="collapsed",
            placeholder="SHIB/USD or NVDA",
            key="add_symbol_input",
        )
    asset_class = cc2.selectbox(
        "Type", ["Crypto", "Equity"], label_visibility="collapsed",
        key="add_symbol_class",
    )
    if cc3.button("➕ Add", type="primary", use_container_width=True):
        sym = (new_sym or "").strip().upper()
        if not sym:
            st.error("Empty symbol.")
        elif asset_class == "Crypto" and "/" not in sym:
            st.error("Crypto symbols need a slash, e.g. SHIB/USD")
        elif asset_class == "Equity" and "/" in sym:
            st.error("Equity symbols don't have a slash.")
        else:
            key = "SR_BOT_SYMBOLS" if asset_class == "Crypto" else "SR_BOT_EQUITIES"
            current = new_crypto if asset_class == "Crypto" else new_equity
            if sym in current:
                st.info(f"{sym} already in universe.")
            else:
                current.append(sym)
                _write_env_key(key, ",".join(current))
                st.success(f"✓ Added {sym}. Bot will pick it up on next reload.")
                st.rerun()
