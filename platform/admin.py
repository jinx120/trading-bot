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
page = st.sidebar.radio("Page", ["Bot", "Reflections", "Ingest"])

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
# Page: Ingest
# ---------------------------------------------------------------------------
elif page == "Ingest":
    st.title("Ingest historical bars")
    st.caption(
        "Pull historical OHLCV from Alpaca for reflection backtests. "
        "The bot itself pulls live data directly from Alpaca and does not "
        "need this table — but reflection does, to replay decisions."
    )

    syms = st.text_input("Symbols (comma-separated)",
                         value="BTC/USD,ETH/USD,SOL/USD,LINK/USD,AVAX/USD,DOGE/USD,AAVE/USD")
    c1, c2 = st.columns(2)
    timeframe = c1.selectbox("Timeframe", ["1hour", "1day", "15min"])
    is_crypto = c2.checkbox("Crypto endpoint", value=True,
                            help="Use crypto feed for BTC/USD etc. Uncheck for SPY/QQQ/DIA.")
    years = st.number_input("Years", min_value=1, max_value=10, value=2)

    if st.button("▶ Start ingestion", type="primary"):
        chosen = [s.strip() for s in syms.split(",") if s.strip()]
        if not chosen:
            st.error("No symbols.")
        else:
            cmd = ["python", "/app/data/ingest_alpaca.py",
                   "--symbols", ",".join(chosen),
                   "--timeframe", timeframe,
                   "--years", str(years)]
            if is_crypto:
                cmd.append("--crypto")
            log_path = f"/tmp/ingest_{int(datetime.now().timestamp())}.log"
            subprocess.Popen(cmd, stdout=open(log_path, "w"),
                             stderr=subprocess.STDOUT)
            st.success(f"Started: `{' '.join(cmd)}`")
            st.caption(f"Log: {log_path}")

    st.subheader("Recent ingestion activity")
    with engine().connect() as conn:
        try:
            ing = pd.read_sql_query(text("""
                SELECT ts, source, symbol, timeframe, rows, status,
                       SUBSTRING(error, 1, 80) AS error
                FROM ingestion_log ORDER BY ts DESC LIMIT 30
            """), conn)
            st.dataframe(ing, use_container_width=True, hide_index=True)
        except Exception as e:
            st.warning(f"ingestion_log not available: {e}")

    st.subheader("Bars in DB")
    with engine().connect() as conn:
        bars_summary = pd.read_sql_query(text("""
            SELECT symbol, timeframe, COUNT(*) AS n_bars,
                   MIN(ts) AS first_ts, MAX(ts) AS last_ts
            FROM bars
            GROUP BY symbol, timeframe
            ORDER BY symbol, timeframe
        """), conn)
    st.dataframe(bars_summary, use_container_width=True, hide_index=True)
