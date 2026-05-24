"""Support & Resistance paper trading bot — small-scale, real Alpaca paper trades.

Self-contained. Does not depend on the trading-platform's strategy ABC, DSR
sweeps, or gate machinery. The goal is to make trades, not to research them.

Flow per tick (default 60s):
  1. For each symbol: pull last ~30d of 1H bars from Alpaca
  2. Resample 1H -> 4H, drop final partial 4H bar
  3. Detect 5-bar pivot highs/lows on both timeframes
  4. Cluster pivots that lie within cluster_pct of each other
  5. If current price is within approach_pct of a 1H support => long signal
     If current price is within approach_pct of a 1H resistance => short signal (margin only)
  6. If a 4H level coincides with the 1H level within confluence_pct => size up 1.5x
  7. Place bracket order: market entry + stop loss + take profit
  8. Persist trade to TimescaleDB `trades` table for dashboard visibility

Run:
  python3 sr_paper_bot.py            # uses .env in same dir
  python3 sr_paper_bot.py --once     # single tick then exit (smoke test)
  python3 sr_paper_bot.py --dry-run  # detect signals + log, no orders
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras
import requests

try:
    from sentiment import should_skip_for_sentiment
except Exception:  # noqa: BLE001 — sentiment is optional
    def should_skip_for_sentiment(symbol, side, threshold=-0.5):
        return False, None

from risk_engine import RiskLimits, ensure_table as ensure_risk_table, veto_reason
from sizing import compute_notional, atr_pct as compute_atr_pct


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULTS = {
    "POLL_SECONDS": 60,
    "SYMBOLS": "BTC/USD,ETH/USD,SOL/USD",  # crypto = 24/7, will actually trade
    "EQUITIES": "DIA,SPY,QQQ",             # added during US market hours
    "PIVOT_WINDOW": 5,
    "CLUSTER_PCT": 0.003,
    "CONFLUENCE_PCT": 0.005,
    "CONFLUENCE_SIZE_MULT": 1.5,
    "APPROACH_PCT": 0.0015,
    "STOP_LOSS_PCT": 0.01,
    "TAKE_PROFIT_PCT": 0.02,
    "NOTIONAL_PER_TRADE": 250.0,          # small scale, real proof of concept
    "MAX_OPEN_POSITIONS": 5,
    "BAR_LOOKBACK_HOURS": 720,            # 30 days of 1H bars
    "COOLDOWN_MIN": 60,                   # minutes to skip a symbol after stop-out
    "TRAIL_TRIGGER_PCT": 0.005,           # start trailing once position is +0.5% in profit
    "TRAIL_DISTANCE_PCT": 0.008,          # trail stop this far behind peak
    "MAX_HOLD_HOURS": 24,                 # force-close after this many hours
    "REFLECTION_EVERY_TICKS": 360,        # auto-run reflection every N ticks (~6h at 60s)
    "ENV_RELOAD_EVERY_TICKS": 5,          # re-read .env every N ticks for hot config
}


def _env(name: str, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if isinstance(default, bool):
        return raw.lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


def load_env_file(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


# ---------------------------------------------------------------------------
# Alpaca client (thin REST wrapper — keeps deps minimal)
# ---------------------------------------------------------------------------

PAPER_TRADING_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"


class Alpaca:
    def __init__(self, key: str, secret: str):
        self.h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self.s = requests.Session()
        self.s.headers.update(self.h)

    def account(self) -> dict:
        return self.s.get(f"{PAPER_TRADING_URL}/v2/account", timeout=10).json()

    def positions(self) -> list[dict]:
        return self.s.get(f"{PAPER_TRADING_URL}/v2/positions", timeout=10).json()

    def clock(self) -> dict:
        return self.s.get(f"{PAPER_TRADING_URL}/v2/clock", timeout=10).json()

    def crypto_bars(self, symbol: str, start: datetime, end: datetime, timeframe: str = "1Hour") -> pd.DataFrame:
        params = {
            "symbols": symbol,
            "timeframe": timeframe,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": 10000,
        }
        r = self.s.get(f"{DATA_URL}/v1beta3/crypto/us/bars", params=params, timeout=15)
        r.raise_for_status()
        data = r.json().get("bars", {}).get(symbol, [])
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        df["ts"] = pd.to_datetime(df["t"], utc=True)
        df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        return df.set_index("ts")[["open", "high", "low", "close", "volume"]]

    def stock_bars(self, symbol: str, start: datetime, end: datetime, timeframe: str = "1Hour") -> pd.DataFrame:
        params = {
            "symbols": symbol,
            "timeframe": timeframe,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": 10000,
            "adjustment": "split",
            "feed": "iex",
        }
        r = self.s.get(f"{DATA_URL}/v2/stocks/bars", params=params, timeout=15)
        r.raise_for_status()
        data = r.json().get("bars", {}).get(symbol, [])
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        df["ts"] = pd.to_datetime(df["t"], utc=True)
        df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        return df.set_index("ts")[["open", "high", "low", "close", "volume"]]

    def latest_quote_crypto(self, symbol: str) -> Optional[float]:
        r = self.s.get(f"{DATA_URL}/v1beta3/crypto/us/latest/trades", params={"symbols": symbol}, timeout=10)
        if not r.ok:
            return None
        trades = r.json().get("trades", {}).get(symbol)
        return float(trades["p"]) if trades else None

    def latest_quote_stock(self, symbol: str) -> Optional[float]:
        r = self.s.get(f"{DATA_URL}/v2/stocks/trades/latest", params={"symbols": symbol, "feed": "iex"}, timeout=10)
        if not r.ok:
            return None
        trades = r.json().get("trades", {}).get(symbol)
        return float(trades["p"]) if trades else None

    def close_position(self, symbol: str) -> dict:
        """Close an entire position at market. Works for crypto + equity."""
        url_symbol = symbol.replace("/", "")  # Alpaca position symbols have no slash
        r = self.s.delete(f"{PAPER_TRADING_URL}/v2/positions/{url_symbol}", timeout=15)
        if not r.ok:
            return {"error": r.text, "status": r.status_code}
        return r.json()

    def place_bracket(
        self,
        symbol: str,
        side: str,
        notional: float,
        stop_price: float,
        take_profit_price: float,
        is_crypto: bool,
    ) -> dict:
        """Crypto on Alpaca doesn't support bracket OCO — we place a simple
        market order for crypto and rely on the bot's monitoring loop for SL/TP.
        For equities (and during RTH only), we use a true bracket order."""
        if is_crypto:
            payload = {
                "symbol": symbol,
                "notional": str(round(notional, 2)),
                "side": side,
                "type": "market",
                "time_in_force": "gtc",
            }
        else:
            payload = {
                "symbol": symbol,
                "notional": str(round(notional, 2)),
                "side": side,
                "type": "market",
                "time_in_force": "day",
                "order_class": "bracket",
                "stop_loss": {"stop_price": str(round(stop_price, 2))},
                "take_profit": {"limit_price": str(round(take_profit_price, 2))},
            }
        r = self.s.post(f"{PAPER_TRADING_URL}/v2/orders", json=payload, timeout=15)
        if not r.ok:
            return {"error": r.text, "status": r.status_code}
        return r.json()


# ---------------------------------------------------------------------------
# S&R logic — pure functions, easy to unit test
# ---------------------------------------------------------------------------

@dataclass
class Level:
    price: float
    kind: str          # "support" or "resistance"
    confirm_ts: pd.Timestamp


def detect_pivots(bars: pd.DataFrame, window: int) -> list[Level]:
    """5-bar pivot: bar i is a pivot high iff its high == max of window centered
    at i. confirm_ts = ts of bar i + (window//2) bars (when it becomes knowable).
    """
    if len(bars) < window:
        return []
    half = window // 2
    highs = bars["high"].values
    lows = bars["low"].values
    idx = bars.index
    out: list[Level] = []
    for i in range(half, len(bars) - half):
        lo = i - half
        hi = i + half + 1
        if highs[i] == highs[lo:hi].max() and (highs[lo:hi] == highs[i]).sum() == 1:
            out.append(Level(float(highs[i]), "resistance", idx[i + half]))
        if lows[i] == lows[lo:hi].min() and (lows[lo:hi] == lows[i]).sum() == 1:
            out.append(Level(float(lows[i]), "support", idx[i + half]))
    return out


def cluster_levels(levels: list[Level], pct: float) -> list[Level]:
    """Greedy 1D clustering by price. Returns merged levels — kind from majority,
    price from mean, confirm_ts from earliest member."""
    if not levels:
        return []
    levels = sorted(levels, key=lambda l: l.price)
    clusters: list[list[Level]] = [[levels[0]]]
    for lev in levels[1:]:
        ref = clusters[-1][0].price
        if abs(lev.price - ref) / ref < pct:
            clusters[-1].append(lev)
        else:
            clusters.append([lev])
    merged: list[Level] = []
    for c in clusters:
        kinds = [x.kind for x in c]
        kind = max(set(kinds), key=kinds.count)
        merged.append(Level(
            price=float(np.mean([x.price for x in c])),
            kind=kind,
            confirm_ts=min(x.confirm_ts for x in c),
        ))
    return merged


def nearest_above_below(price: float, levels: list[Level]) -> tuple[Optional[Level], Optional[Level]]:
    below = [l for l in levels if l.price < price]
    above = [l for l in levels if l.price > price]
    sup = max(below, key=lambda l: l.price) if below else None
    res = min(above, key=lambda l: l.price) if above else None
    return sup, res


def is_confluent(a: Optional[Level], b: Optional[Level], pct: float) -> bool:
    if a is None or b is None:
        return False
    return abs(a.price - b.price) / max(a.price, b.price) < pct


def resample_4h(bars_1h: pd.DataFrame) -> pd.DataFrame:
    return bars_1h.resample("4h").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna().iloc[:-1]  # drop partial trailing 4H bar


def regime(bars_1h: pd.DataFrame, sma_window: int = 200, adx_window: int = 14) -> dict:
    """Classify the market regime so the bot can skip S&R bounce trades in trends.

    Returns dict with:
      sma_slope_pct  — last bar's % deviation from the 200-bar SMA (signed)
      adx            — Wilder's ADX over adx_window bars
      di_plus, di_minus
      label          — 'uptrend' | 'downtrend' | 'range'
    """
    if len(bars_1h) < max(sma_window, adx_window * 4):
        return {"label": "unknown", "sma_slope_pct": 0.0, "adx": 0.0,
                "di_plus": 0.0, "di_minus": 0.0}

    close = bars_1h["close"]
    sma = close.rolling(sma_window).mean()
    sma_dev = (close.iloc[-1] / sma.iloc[-1] - 1.0) if pd.notna(sma.iloc[-1]) else 0.0

    high, low, c = bars_1h["high"], bars_1h["low"], bars_1h["close"]
    plus_dm = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    # only the larger of the two counts on a given bar
    mask_plus = plus_dm > minus_dm
    mask_minus = minus_dm > plus_dm
    plus_dm = plus_dm.where(mask_plus, 0.0)
    minus_dm = minus_dm.where(mask_minus, 0.0)
    tr = pd.concat([
        (high - low),
        (high - c.shift()).abs(),
        (low - c.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / adx_window, adjust=False).mean()
    di_plus = 100 * plus_dm.ewm(alpha=1.0 / adx_window, adjust=False).mean() / atr
    di_minus = 100 * minus_dm.ewm(alpha=1.0 / adx_window, adjust=False).mean() / atr
    dx = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)
    adx = dx.ewm(alpha=1.0 / adx_window, adjust=False).mean()

    a = float(adx.iloc[-1]) if pd.notna(adx.iloc[-1]) else 0.0
    p = float(di_plus.iloc[-1]) if pd.notna(di_plus.iloc[-1]) else 0.0
    m = float(di_minus.iloc[-1]) if pd.notna(di_minus.iloc[-1]) else 0.0

    if a >= 25 and m > p and sma_dev < -0.005:
        label = "downtrend"
    elif a >= 25 and p > m and sma_dev > 0.005:
        label = "uptrend"
    else:
        label = "range"

    return {"label": label, "sma_slope_pct": float(sma_dev),
            "adx": a, "di_plus": p, "di_minus": m}


# ---------------------------------------------------------------------------
# Signal generator
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    symbol: str
    side: str          # "buy" or "sell"
    price: float
    trigger_level: float
    trigger_tf: str
    sl: float
    tp: float
    notional_mult: float
    confluence: bool
    snapshot: dict = field(default_factory=dict)


def generate_signal(
    symbol: str,
    bars_1h: pd.DataFrame,
    cfg: dict,
    live_price: Optional[float] = None,
) -> Optional[Signal]:
    """Build a signal from S&R pivots and the *current* market price.

    live_price is the latest trade price from Alpaca. The 1H bar close is up
    to ~60 min stale, which in fast-moving crypto lets price drop well below
    a support while the bar close still hugs it — producing fake "approaching
    support from above" signals that immediately stop out. Always check
    proximity against live_price when provided."""
    if len(bars_1h) < cfg["PIVOT_WINDOW"] * 4:
        return None

    pivots_1h = cluster_levels(detect_pivots(bars_1h, cfg["PIVOT_WINDOW"]), cfg["CLUSTER_PCT"])
    bars_4h = resample_4h(bars_1h)
    pivots_4h = cluster_levels(detect_pivots(bars_4h, cfg["PIVOT_WINDOW"]), cfg["CLUSTER_PCT"])

    if not pivots_1h:
        return None

    bar_close = float(bars_1h["close"].iloc[-1])
    close = float(live_price) if live_price is not None else bar_close

    # Regime filter: S&R bounces fail in strong trends — skip longs in
    # downtrends and shorts in uptrends. Range markets are fine for both.
    reg = regime(bars_1h)
    block_long = reg["label"] == "downtrend"
    block_short = reg["label"] == "uptrend"
    sup_1h, res_1h = nearest_above_below(close, pivots_1h)
    sup_4h, res_4h = nearest_above_below(close, pivots_4h)

    snap = {
        "close": close,
        "bar_close": bar_close,
        "regime": reg["label"],
        "adx": round(reg["adx"], 2),
        "sma_dev_pct": round(reg["sma_slope_pct"] * 100, 3),
        "sup_1h": sup_1h.price if sup_1h else None,
        "res_1h": res_1h.price if res_1h else None,
        "sup_4h": sup_4h.price if sup_4h else None,
        "res_4h": res_4h.price if res_4h else None,
        "n_levels_1h": len(pivots_1h),
        "n_levels_4h": len(pivots_4h),
    }

    # LONG: price approaching support from above (close > sup, within approach_pct)
    if sup_1h and 0 < (close - sup_1h.price) / close < cfg["APPROACH_PCT"]:
        if block_long:
            return None
        confluence = is_confluent(sup_1h, sup_4h, cfg["CONFLUENCE_PCT"])
        mult = cfg["CONFLUENCE_SIZE_MULT"] if confluence else 1.0
        sl = sup_1h.price * (1 - cfg["STOP_LOSS_PCT"])
        tp = close * (1 + cfg["TAKE_PROFIT_PCT"])
        return Signal(symbol, "buy", close, sup_1h.price, "1H", sl, tp, mult, confluence, snap)

    # SHORT: price approaching resistance from below (close < res, within approach_pct)
    if res_1h and 0 < (res_1h.price - close) / close < cfg["APPROACH_PCT"]:
        if block_short:
            return None
        confluence = is_confluent(res_1h, res_4h, cfg["CONFLUENCE_PCT"])
        mult = cfg["CONFLUENCE_SIZE_MULT"] if confluence else 1.0
        sl = res_1h.price * (1 + cfg["STOP_LOSS_PCT"])
        tp = close * (1 - cfg["TAKE_PROFIT_PCT"])
        return Signal(symbol, "sell", close, res_1h.price, "1H", sl, tp, mult, confluence, snap)

    return None


# ---------------------------------------------------------------------------
# Persistence — write to existing TimescaleDB trades table
# ---------------------------------------------------------------------------

def db_conn():
    return psycopg2.connect(
        host=os.environ.get("PG_HOST") or os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("PG_PORT") or os.environ.get("DB_PORT", "5432")),
        user=os.environ.get("PG_USER") or os.environ.get("DB_USER", "trader"),
        password=os.environ.get("DB_PASSWORD") or os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ.get("PG_DB") or os.environ.get("DB_NAME", "trading"),
    )


def recent_stopouts(cooldown_min: int) -> set[str]:
    """Symbols that hit SL within the last `cooldown_min` minutes — skip re-entry."""
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT symbol FROM trades
                WHERE strategy = 'sr_paper_bot'
                  AND exit_reason = 'sl_hit'
                  AND exit_ts >= NOW() - (%s || ' minutes')::interval
            """, (cooldown_min,))
            return {row[0] for row in cur.fetchall()}
    except Exception as e:
        logging.warning("recent_stopouts failed: %s", e)
        return set()


def log_score(symbol: str, strategy: str, score: float, metadata: Optional[dict] = None) -> None:
    """Phase 3: persist sub-strategy scores to `scores` table for ensemble
    analysis. Live bot writes these every tick; ensemble in Phase 4 reads them
    to compute composite voting weights."""
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO scores (symbol, strategy, score, metadata) "
                "VALUES (%s, %s, %s, %s)",
                (symbol, strategy, float(score),
                 json.dumps(metadata) if metadata else None),
            )
    except Exception as e:
        logging.debug("log_score failed: %s", e)


def log_signal(
    symbol: str,
    sig: Optional["Signal"],
    bars_close: float,
    took_trade: bool,
    skip_reason: Optional[str],
    extras: Optional[dict] = None,
) -> None:
    """Record every evaluation — feeds reflection. One row per (tick, symbol)
    regardless of whether a trade fired. extras dict captures regime / adx /
    nearest-level info from the strategy."""
    extras = extras or {}
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO signals (
                    strategy, symbol, side, close, trigger_level, trigger_tf,
                    sl, tp, confluence, regime, adx, approach_pct,
                    notional_mult, took_trade, skip_reason, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s)
            """, (
                "sr_paper_bot",
                symbol,
                sig.side if sig else None,
                float(sig.price) if sig else bars_close,
                float(sig.trigger_level) if sig else None,
                sig.trigger_tf if sig else None,
                float(sig.sl) if sig else None,
                float(sig.tp) if sig else None,
                bool(sig.confluence) if sig else None,
                extras.get("regime"),
                extras.get("adx"),
                extras.get("approach_pct"),
                float(sig.notional_mult) if sig else 1.0,
                took_trade,
                skip_reason,
                json.dumps(extras),
            ))
    except Exception as e:
        logging.debug("log_signal failed: %s", e)


def fetch_open_bot_trades() -> list[dict]:
    """Return open sr_paper_bot trade rows (no exit yet) joined with their sl/tp."""
    try:
        with db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, symbol, side, entry_price, quantity, entry_ts,
                       (metadata->>'sl')::float AS sl,
                       (metadata->>'tp')::float AS tp,
                       (metadata->>'is_crypto')::bool AS is_crypto
                FROM trades
                WHERE strategy = 'sr_paper_bot' AND exit_ts IS NULL
                ORDER BY entry_ts ASC
            """)
            return list(cur.fetchall())
    except Exception as e:
        logging.warning("fetch_open_bot_trades failed: %s", e)
        return []


def update_trade_exit(trade_id: int, exit_price: float, exit_reason: str, side: str,
                      entry_price: float, qty: float) -> None:
    direction = 1 if side == "long" else -1
    pnl = (exit_price - entry_price) * qty * direction
    pnl_pct = ((exit_price / entry_price) - 1.0) * direction
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                UPDATE trades
                SET exit_ts = NOW(), exit_price = %s, exit_reason = %s,
                    pnl = %s, pnl_pct = %s
                WHERE id = %s
            """, (exit_price, exit_reason, pnl, pnl_pct, trade_id))
    except Exception as e:
        logging.warning("update_trade_exit failed (trade %s): %s", trade_id, e)


def _trail_state_update(trade_id: int, peak: float) -> None:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE trades SET metadata = jsonb_set(metadata, '{trail_peak}', %s::jsonb) "
            "WHERE id = %s",
            (json.dumps(peak), trade_id),
        )


def monitor_exits(alpaca: Alpaca, cfg: dict, dry_run: bool = False) -> None:
    """Check open bot trades against current price; close + record on:
      - SL hit
      - TP hit
      - Trailing stop hit (only active once position has been TRAIL_TRIGGER_PCT in profit)
      - Max hold time exceeded

    Required because Alpaca crypto doesn't support OCO bracket orders — the bot
    stores levels in metadata and enforces them here. For equities with native
    bracket orders, Alpaca handles exits but this still records them in DB.
    """
    open_trades = fetch_open_bot_trades()
    if not open_trades:
        return
    try:
        positions = {p["symbol"]: p for p in alpaca.positions()}
    except Exception as e:
        logging.warning("positions fetch failed in monitor: %s", e)
        return

    trail_trigger = float(cfg.get("TRAIL_TRIGGER_PCT", 0.005))
    trail_dist = float(cfg.get("TRAIL_DISTANCE_PCT", 0.008))
    max_hold_hours = float(cfg.get("MAX_HOLD_HOURS", 24))
    now = datetime.now(timezone.utc)

    # Fetch trail_peak metadata once per trade for hot path
    for t in open_trades:
        sym = t["symbol"]
        alp_sym = sym.replace("/", "")
        sl, tp, side = t["sl"], t["tp"], t["side"]
        if sl is None or tp is None:
            # Pre-existing trade without bot-managed levels (e.g. older manual entries).
            # Skip; we won't try to manage exits for trades we didn't create.
            continue
        is_crypto = t["is_crypto"] if t["is_crypto"] is not None else "/" in sym

        # If Alpaca shows no position for this symbol, the equity bracket
        # already closed it via Alpaca-side SL/TP. Record exit from last trade
        # price if available, otherwise just mark closed with current quote.
        if alp_sym not in positions:
            try:
                cur_px = alpaca.latest_quote_crypto(sym) if is_crypto else alpaca.latest_quote_stock(sym)
            except Exception:
                cur_px = None
            if cur_px is None:
                continue
            reason = "tp_hit" if (
                (side == "long" and cur_px >= tp) or (side == "short" and cur_px <= tp)
            ) else "sl_hit" if (
                (side == "long" and cur_px <= sl) or (side == "short" and cur_px >= sl)
            ) else "alpaca_closed"
            logging.info("%s: position no longer open — recording exit (%s @ %.4f)", sym, reason, cur_px)
            update_trade_exit(t["id"], cur_px, reason, side, t["entry_price"], t["quantity"])
            continue

        cur_px = float(positions[alp_sym]["current_price"])
        entry = float(t["entry_price"])
        hit_tp = (side == "long" and cur_px >= tp) or (side == "short" and cur_px <= tp)
        hit_sl = (side == "long" and cur_px <= sl) or (side == "short" and cur_px >= sl)

        # Trailing stop. Track running peak in metadata.trail_peak (best price
        # since entry). Once unrealized PnL crosses trail_trigger, enforce the
        # trail at trail_distance behind the peak.
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT metadata FROM trades WHERE id = %s", (t["id"],))
            row = cur.fetchone()
        meta = row[0] if row else {}
        trail_peak = meta.get("trail_peak") if isinstance(meta, dict) else None

        if side == "long":
            new_peak = max(cur_px, trail_peak) if trail_peak else cur_px
        else:
            new_peak = min(cur_px, trail_peak) if trail_peak else cur_px

        if (trail_peak is None) or (
            (side == "long" and new_peak > trail_peak) or
            (side == "short" and new_peak < trail_peak)
        ):
            _trail_state_update(t["id"], new_peak)

        in_profit_pct = (
            (new_peak - entry) / entry if side == "long"
            else (entry - new_peak) / entry
        )
        trail_active = in_profit_pct >= trail_trigger
        trail_hit = False
        if trail_active:
            if side == "long":
                trail_level = new_peak * (1 - trail_dist)
                trail_hit = cur_px <= trail_level
            else:
                trail_level = new_peak * (1 + trail_dist)
                trail_hit = cur_px >= trail_level

        # Time-based exit
        entry_ts = t["entry_ts"]
        if entry_ts.tzinfo is None:
            entry_ts = entry_ts.replace(tzinfo=timezone.utc)
        held_hours = (now - entry_ts).total_seconds() / 3600.0
        time_exit = held_hours >= max_hold_hours

        if not (hit_tp or hit_sl or trail_hit or time_exit):
            continue

        reason = (
            "tp_hit" if hit_tp else
            "trail_stop" if trail_hit else
            "time_exit" if time_exit else
            "sl_hit"
        )
        logging.info(
            "%s: %s — closing at market (px=%.4f sl=%.4f tp=%.4f peak=%.4f held=%.1fh)",
            sym, reason.upper(), cur_px, sl, tp, new_peak, held_hours,
        )
        if dry_run:
            continue
        result = alpaca.close_position(sym)
        if "error" in result:
            logging.error("%s: close_position failed: %s", sym, result["error"])
            continue
        update_trade_exit(t["id"], cur_px, reason, side, t["entry_price"], t["quantity"])


def persist_trade(sig: Signal, order: dict, qty_filled: float, is_crypto: bool, mode: str = "paper") -> None:
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO trades (
                    strategy, symbol, side, entry_ts, entry_price,
                    quantity, mode, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    "sr_paper_bot",
                    sig.symbol,
                    "long" if sig.side == "buy" else "short",
                    datetime.now(timezone.utc),
                    sig.price,
                    qty_filled,
                    mode,
                    json.dumps({
                        "trigger_level": sig.trigger_level,
                        "trigger_tf": sig.trigger_tf,
                        "confluence": sig.confluence,
                        "notional_mult": sig.notional_mult,
                        "sl": sig.sl,
                        "tp": sig.tp,
                        "is_crypto": is_crypto,
                        "alpaca_order_id": order.get("id"),
                        "snapshot": sig.snapshot,
                    }),
                ),
            )
    except Exception as e:
        logging.warning("persist_trade failed: %s", e)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def tick(alpaca: Alpaca, cfg: dict, dry_run: bool = False) -> None:
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=cfg["BAR_LOOKBACK_HOURS"])

    # Step 1: enforce SL/TP/trail/time exits on open positions
    monitor_exits(alpaca, cfg, dry_run=dry_run)

    # gate by open positions
    try:
        open_positions = alpaca.positions()
    except Exception as e:
        logging.warning("could not fetch positions: %s", e)
        open_positions = []
    open_symbols = {p["symbol"] for p in open_positions}

    # Risk engine: get current equity + check circuit breakers up front
    try:
        acct = alpaca.account()
        current_equity = float(acct.get("equity", 0))
    except Exception as e:
        logging.warning("could not fetch account in tick: %s", e)
        current_equity = 0.0
    limits = RiskLimits.from_env()
    # Always update peak/SOD baselines so drawdown tracking is current even
    # on ticks where no signals fire.
    try:
        from risk_engine import update_peak_and_sod
        update_peak_and_sod(current_equity)
    except Exception as e:
        logging.warning("risk state update failed: %s", e)
    if len(open_symbols) >= limits.max_open_positions:
        logging.info("at max open positions (%d) — skipping new entries", len(open_symbols))
        return

    is_market_open = False
    try:
        is_market_open = bool(alpaca.clock().get("is_open"))
    except Exception:
        pass

    symbols = [s.strip() for s in cfg["SYMBOLS"].split(",") if s.strip()]
    if is_market_open:
        symbols += [s.strip() for s in cfg["EQUITIES"].split(",") if s.strip()]

    on_cooldown = recent_stopouts(int(cfg.get("COOLDOWN_MIN", 60)))

    # Phase 3: fetch daily bars once per tick for trend sub-strategies.
    # Daily bars are cheap (10 bars per day × 250 days = 2500 per symbol).
    daily_lookback_days = 365
    daily_start = end - timedelta(days=daily_lookback_days)

    for symbol in symbols:
        is_crypto = "/" in symbol
        if symbol in on_cooldown:
            logging.info("%s: skipped (in cooldown after recent stop)", symbol)
            log_signal(symbol, None, 0.0, took_trade=False,
                       skip_reason="cooldown", extras={})
            continue
        try:
            bars = alpaca.crypto_bars(symbol, start, end) if is_crypto else alpaca.stock_bars(symbol, start, end)
        except Exception as e:
            logging.warning("%s: bar fetch failed: %s", symbol, e)
            continue
        if bars.empty:
            logging.info("%s: no bars returned", symbol)
            continue

        # Phase 4 ensemble: collect scores from every enabled sub-strategy,
        # combine into a composite vote, decide side + SL/TP from the dominant
        # contributor. Set ENSEMBLE_MODE=false to fall back to legacy S&R-only.
        ensemble_mode = os.environ.get("ENSEMBLE_MODE", "true").lower() == "true"
        ensemble_scores: dict[str, float] = {}
        ensemble_sr_levels: dict = {}
        daily_bars = pd.DataFrame()
        try:
            from strategies import REGISTRY as _STRAT_REGISTRY
            daily_bars = (
                alpaca.crypto_bars(symbol, daily_start, end, timeframe="1Day")
                if is_crypto else
                alpaca.stock_bars(symbol, daily_start, end, timeframe="1Day")
            )
            for strat_name, mod in _STRAT_REGISTRY.items():
                try:
                    if strat_name in ("donchian_trend", "ma_crossover"):
                        s = mod.score(daily_bars) if not daily_bars.empty else 0.0
                    else:
                        # sr_bounce, zscore_revert operate on 1H bars
                        s = mod.score(bars)
                    ensemble_scores[strat_name] = float(s)
                    log_score(symbol, strat_name, s)
                except Exception as e:
                    logging.debug("%s/%s score failed: %s", symbol, strat_name, e)
        except Exception as e:
            logging.debug("%s: ensemble score loop failed: %s", symbol, e)

        try:
            live_px = alpaca.latest_quote_crypto(symbol) if is_crypto else alpaca.latest_quote_stock(symbol)
        except Exception:
            live_px = None
        bar_close = float(bars["close"].iloc[-1])

        # ----- Ensemble path (Phase 4) -----
        if ensemble_mode and ensemble_scores:
            from ensemble import decide, derive_sl_tp
            decision = decide(ensemble_scores)
            entry_price = float(live_px) if live_px is not None else bar_close
            extras = {
                "ensemble_composite": decision.composite,
                "ensemble_confidence": decision.confidence,
                "ensemble_breakdown": decision.breakdown,
                "dominant": decision.dominant,
                "live_price": live_px,
            }
            if not decision.entered:
                # Still build a tiny no-signal log so reflection sees the data
                logging.info(
                    "%s: ensemble composite=%+.3f conf=%.2f — %s",
                    symbol, decision.composite, decision.confidence, decision.reason,
                )
                log_signal(symbol, None, bar_close, took_trade=False,
                           skip_reason=f"ensemble:{decision.reason}", extras=extras)
                continue

            # Need SR levels for SL/TP derivation if SR is dominant
            sr_sig = generate_signal(symbol, bars, cfg, live_price=live_px)
            if sr_sig:
                ensemble_sr_levels = {
                    "sup_1h": sr_sig.snapshot.get("sup_1h"),
                    "res_1h": sr_sig.snapshot.get("res_1h"),
                    "sup_4h": sr_sig.snapshot.get("sup_4h"),
                    "res_4h": sr_sig.snapshot.get("res_4h"),
                }
            sl, tp = derive_sl_tp(
                decision.dominant, decision.side, entry_price, bars,
                sr_levels=ensemble_sr_levels or None,
            )
            # Build a Signal-compatible object so the rest of the pipeline works
            sig = Signal(
                symbol=symbol, side=decision.side, price=entry_price,
                trigger_level=ensemble_sr_levels.get("sup_1h" if decision.side == "buy" else "res_1h", entry_price) or entry_price,
                trigger_tf=("1H_sr" if decision.dominant == "sr_bounce" else "ATR"),
                sl=float(sl), tp=float(tp),
                notional_mult=1.0,                       # ensemble owns sizing now
                confluence=False,                        # legacy field, deprecated by composite
                snapshot={"ensemble": decision.breakdown,
                          "composite": decision.composite,
                          "confidence": decision.confidence,
                          "dominant": decision.dominant},
            )
            extras["sl"] = sl
            extras["tp"] = tp

        # ----- Legacy S&R path (fallback when ENSEMBLE_MODE=false or no scores) -----
        else:
            sig = generate_signal(symbol, bars, cfg, live_price=live_px)
            extras = {
                "regime": sig.snapshot.get("regime") if sig else None,
                "adx": sig.snapshot.get("adx") if sig else None,
                "approach_pct": cfg.get("APPROACH_PCT"),
                "live_price": live_px,
            }
            if sig is None:
                logging.info("%s: no signal (close=%.4f)", symbol, bar_close)
                log_signal(symbol, None, bar_close, took_trade=False,
                           skip_reason="no_signal", extras=extras)
                continue

        normalized = symbol.replace("/", "")
        if normalized in open_symbols or symbol in open_symbols:
            logging.info("%s: signal but already in position — skip", symbol)
            log_signal(symbol, sig, bar_close, took_trade=False,
                       skip_reason="already_in_position", extras=extras)
            continue

        # Vol-targeted sizing: dollar risk per trade is fixed, notional varies
        # inversely with stop distance. Carver §15.
        stop_distance_pct = abs(sig.price - sig.sl) / max(1e-9, sig.price)
        notional, breakdown = compute_notional(
            equity=current_equity,
            bars_1h=bars,
            stop_distance_pct=stop_distance_pct,
            confluence_mult=sig.notional_mult,
            side=sig.side,
        )
        extras["sizing"] = breakdown

        if notional <= 0:
            logging.info(
                "%s: SKIP — sizing returned $0 (open_risk=%.2f%% remaining=%.2f%% capped=%s)",
                symbol, breakdown["open_risk_pct_before"] * 100,
                breakdown["remaining_risk_pct"] * 100,
                breakdown["capped_by_portfolio"],
            )
            log_signal(symbol, sig, bar_close, took_trade=False,
                       skip_reason="portfolio_risk_cap", extras=extras)
            continue

        # Risk circuit breakers — last check before placing the order
        veto = veto_reason(limits, current_equity, open_positions, notional, symbol)
        if veto:
            logging.warning("%s: VETO — %s (would be $%.2f)", symbol, veto, notional)
            log_signal(symbol, sig, bar_close, took_trade=False,
                       skip_reason=f"risk_veto:{veto}", extras=extras)
            continue

        msg = (
            f"{symbol}: SIGNAL side={sig.side} px={sig.price:.4f} "
            f"trigger={sig.trigger_level:.4f} sl={sig.sl:.4f} tp={sig.tp:.4f} "
            f"confluence={sig.confluence} notional=${notional:.2f} "
            f"(stop={stop_distance_pct*100:.2f}% dd_mult={breakdown['dd_mult']:.2f} "
            f"perf={breakdown['perf_mult']:.2f} open_risk={breakdown['open_risk_pct_before']*100:.2f}%)"
        )
        logging.info(msg)

        skip, sent = should_skip_for_sentiment(symbol, sig.side)
        if sent is not None:
            logging.info("%s: scrapemcp_sentiment=%.2f", symbol, sent)
            extras["sentiment"] = sent
        if skip:
            logging.info("%s: SKIP — sentiment %.2f opposes %s entry", symbol, sent, sig.side)
            log_signal(symbol, sig, bar_close, took_trade=False,
                       skip_reason="sentiment_block", extras=extras)
            continue

        if dry_run:
            log_signal(symbol, sig, bar_close, took_trade=False,
                       skip_reason="dry_run", extras=extras)
            continue

        order = alpaca.place_bracket(symbol, sig.side, notional, sig.sl, sig.tp, is_crypto)
        if "error" in order:
            logging.error("%s: order rejected: %s", symbol, order["error"])
            log_signal(symbol, sig, bar_close, took_trade=False,
                       skip_reason=f"order_rejected:{order.get('error','')[:80]}",
                       extras=extras)
            continue
        try:
            qty = float(order["qty"]) if order.get("qty") else notional / sig.price
        except (TypeError, ValueError):
            qty = notional / sig.price
        logging.info("%s: ORDER PLACED id=%s qty=%.6f", symbol, order.get("id"), qty)
        persist_trade(sig, order, qty, is_crypto, mode="paper")
        log_signal(symbol, sig, bar_close, took_trade=True, skip_reason=None,
                   extras={**extras, "alpaca_order_id": order.get("id")})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="single tick then exit")
    parser.add_argument("--dry-run", action="store_true", help="detect signals but don't place orders")
    parser.add_argument("--env", default="/home/redji/sr-bot/.env")
    args = parser.parse_args()

    load_env_file(args.env)
    # also fall back to trading-platform .env for db creds & alpaca keys
    load_env_file("/home/redji/trading-platform/.env")
    setup_logging()

    cfg = {k: _env(k, v) for k, v in DEFAULTS.items()}
    cfg["SYMBOLS"] = os.environ.get("SR_BOT_SYMBOLS", cfg["SYMBOLS"])
    cfg["EQUITIES"] = os.environ.get("SR_BOT_EQUITIES", cfg["EQUITIES"])

    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_API_SECRET", "")
    if not key or not secret:
        logging.error("ALPACA_API_KEY / ALPACA_API_SECRET missing — set in .env")
        sys.exit(2)

    alpaca = Alpaca(key, secret)
    try:
        acct = alpaca.account()
    except Exception as e:
        logging.error("could not reach Alpaca: %s", e)
        sys.exit(2)
    logging.info("alpaca paper: equity=$%s cash=$%s status=%s", acct["equity"], acct["cash"], acct["status"])
    logging.info("cfg: %s", {k: cfg[k] for k in ("POLL_SECONDS","SYMBOLS","EQUITIES","NOTIONAL_PER_TRADE","CONFLUENCE_SIZE_MULT","APPROACH_PCT")})

    ensure_risk_table()

    stop = {"flag": False}
    def _handle(*_):
        stop["flag"] = True
        logging.info("shutdown signal received — finishing current tick")
    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    tick_count = 0
    env_path = args.env
    while not stop["flag"]:
        # Hot-reload .env every ENV_RELOAD_EVERY_TICKS so reflection
        # auto-apply takes effect without restart.
        if tick_count > 0 and tick_count % int(cfg.get("ENV_RELOAD_EVERY_TICKS", 5)) == 0:
            try:
                _reload_env(env_path)
                cfg = {k: _env(k, v) for k, v in DEFAULTS.items()}
                cfg["SYMBOLS"] = os.environ.get("SR_BOT_SYMBOLS", cfg["SYMBOLS"])
                cfg["EQUITIES"] = os.environ.get("SR_BOT_EQUITIES", cfg["EQUITIES"])
            except Exception as e:
                logging.warning("env hot-reload failed: %s", e)

        try:
            tick(alpaca, cfg, dry_run=args.dry_run)
        except Exception as e:
            logging.exception("tick error: %s", e)

        # Auto-reflection: run periodically, no human gate
        reflect_every = int(cfg.get("REFLECTION_EVERY_TICKS", 360))
        if reflect_every > 0 and tick_count > 0 and tick_count % reflect_every == 0:
            try:
                _run_reflection_inline()
            except Exception as e:
                logging.warning("auto-reflection failed: %s", e)

        tick_count += 1
        if args.once:
            break
        for _ in range(cfg["POLL_SECONDS"]):
            if stop["flag"]:
                break
            time.sleep(1)

    logging.info("exiting")


def _reload_env(path: str) -> None:
    """Re-read .env and OVERWRITE os.environ for any keys present (not setdefault).
    This is what gives reflection auto-apply its hot effect."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()


def _run_reflection_inline() -> None:
    """Run reflection.main() in-process so a successful run side-effects the DB
    (and, with auto-apply, writes the new .env). Subsequent hot-reload picks it up."""
    try:
        import importlib
        import reflection as _refl
        importlib.reload(_refl)
        out = _refl.main()
        logging.info("auto-reflection #%s done; proposed=%s",
                     out.get("reflection_id"), out.get("proposed"))
    except Exception as e:
        logging.warning("reflection import/run failed: %s", e)


if __name__ == "__main__":
    main()
