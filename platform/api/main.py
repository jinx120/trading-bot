"""FastAPI backend for the trading bot UI.

Exposes JSON REST endpoints + WebSocket /ws for live updates.
Mounts the built React SPA at / for production serving.

Endpoints mirror the data shapes the React frontend consumes:
  GET  /api/account           Alpaca paper account snapshot
  GET  /api/positions         open Alpaca positions joined with bot SL/TP
  GET  /api/trades            recent bot trades
  GET  /api/signals           recent signals evaluated
  GET  /api/scores/latest     latest per-symbol per-strategy ensemble scores
  GET  /api/strategy-weights  active strategy weights
  GET  /api/shadow-scores     shadow candidate scores (lab)
  GET  /api/reflections       recent reflections
  GET  /api/risk-state        peak equity, sod, lockdown
  GET  /api/risk-events       recent risk events
  GET  /api/lab-events        recent lab events
  GET  /api/symbols           active universe + curated pool

  POST /api/symbols/{kind}    update SR_BOT_SYMBOLS or SR_BOT_EQUITIES
  POST /api/run-tick          fire one bot tick (dry-run or live)
  POST /api/run-reflection    fire reflection now

  WS   /ws/live               stream positions + scores updates every 5s
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text

sys.path.insert(0, "/app")
from common.db import get_engine


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Trading Bot API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_ALPACA = "https://paper-api.alpaca.markets"

# Curated pool for the Symbols page
CRYPTO_POOL = sorted({
    "BTC/USD", "ETH/USD", "SOL/USD", "LINK/USD", "AVAX/USD",
    "DOGE/USD", "AAVE/USD", "ADA/USD", "DOT/USD", "MATIC/USD",
    "UNI/USD", "XRP/USD", "LTC/USD", "BCH/USD",
})
EQUITY_POOL = sorted({
    "DIA", "SPY", "QQQ", "IWM", "TQQQ", "SOXL", "TLT",
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
})


def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_API_SECRET", ""),
    }


def _bot_env_path() -> Path:
    return Path(os.environ.get("SR_BOT_ENV_PATH", "/app/sr-bot/.env"))


def _read_env() -> dict:
    p = _bot_env_path()
    env: dict = {}
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def _write_env_key(key: str, value: str) -> None:
    p = _bot_env_path()
    lines = p.read_text().splitlines() if p.exists() else []
    replaced = False
    for i, ln in enumerate(lines):
        if ln.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}={value}")
    p.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Account / Positions
# ---------------------------------------------------------------------------

@app.get("/api/account")
def account():
    try:
        r = requests.get(f"{_ALPACA}/v2/account", headers=_alpaca_headers(), timeout=8)
        d = r.json()
        return {
            "ok": r.ok,
            "equity": float(d.get("equity", 0)),
            "cash": float(d.get("cash", 0)),
            "buying_power": float(d.get("buying_power", 0)),
            "status": d.get("status"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/positions")
def positions():
    try:
        r = requests.get(f"{_ALPACA}/v2/positions", headers=_alpaca_headers(), timeout=8)
        raw = r.json() if r.ok else []
        if isinstance(raw, dict):
            raw = []
    except Exception as e:
        return {"positions": [], "error": str(e)}

    # Join with bot trades to get SL/TP per symbol
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT symbol,
                   (metadata->>'sl')::float AS sl,
                   (metadata->>'tp')::float AS tp,
                   entry_ts,
                   side,
                   metadata->'snapshot'->>'dominant' AS dominant_strategy,
                   metadata->'snapshot'->>'composite' AS composite,
                   strategy
            FROM trades
            WHERE strategy IN ('sr_paper_bot', 'manual')
              AND exit_ts IS NULL
              AND (metadata->>'sl') IS NOT NULL
            ORDER BY entry_ts ASC
        """)).fetchall()
    by_normalized = {r.symbol.replace("/", ""): r for r in rows}

    out = []
    now = datetime.now(timezone.utc)
    for p in raw:
        sym = p["symbol"]
        bot = by_normalized.get(sym)
        cur = float(p["current_price"])
        entry = float(p["avg_entry_price"])
        sl = float(bot.sl) if bot else None
        tp = float(bot.tp) if bot else None
        held_seconds = None
        if bot and bot.entry_ts:
            ts = bot.entry_ts
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            held_seconds = int((now - ts).total_seconds())
        out.append({
            "symbol": sym,
            "qty": float(p["qty"]),
            "side": "long" if float(p["qty"]) > 0 else "short",
            "entry_price": entry,
            "current_price": cur,
            "market_value": float(p["market_value"]),
            "unrealized_pl": float(p["unrealized_pl"]),
            "unrealized_plpc": float(p["unrealized_plpc"]),
            "sl": sl,
            "tp": tp,
            "sl_dist_pct": ((cur - sl) / cur * 100) if sl else None,
            "tp_dist_pct": ((tp - cur) / cur * 100) if tp else None,
            "held_seconds": held_seconds,
            "dominant_strategy": bot.dominant_strategy if bot else None,
            "composite": float(bot.composite) if bot and bot.composite else None,
            "bot_managed": bot is not None,
            "source": (bot.strategy if bot else None),
        })
    return {"positions": out}


# ---------------------------------------------------------------------------
# Trades / Signals
# ---------------------------------------------------------------------------

@app.get("/api/trades")
def trades(limit: int = 50):
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT id, entry_ts, exit_ts, symbol, side, entry_price, exit_price,
                   quantity, pnl_pct, exit_reason,
                   metadata->'snapshot'->>'composite' AS composite,
                   metadata->'snapshot'->>'confidence' AS confidence,
                   metadata->'snapshot'->>'dominant' AS dominant,
                   metadata->>'regime' AS regime,
                   metadata->>'confluence' AS confluence
            FROM trades
            WHERE strategy = 'sr_paper_bot'
            ORDER BY entry_ts DESC
            LIMIT :n
        """), {"n": limit}).fetchall()
    return {"trades": [dict(r._mapping) for r in rows]}


@app.get("/api/signals")
def signals(limit: int = 100):
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT id, ts, symbol, side, close, trigger_level, confluence,
                   regime, adx, took_trade, skip_reason
            FROM signals
            WHERE strategy = 'sr_paper_bot'
            ORDER BY ts DESC LIMIT :n
        """), {"n": limit}).fetchall()
    return {"signals": [dict(r._mapping) for r in rows]}


# ---------------------------------------------------------------------------
# Ensemble scores + weights
# ---------------------------------------------------------------------------

@app.get("/api/strategy-weights")
def strategy_weights():
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT name, weight, enabled, sharpe_30d, last_updated
            FROM strategy_weights ORDER BY name
        """)).fetchall()
    return {"weights": [dict(r._mapping) for r in rows]}


@app.get("/api/scores/latest")
def scores_latest():
    """Latest per-symbol per-strategy score from the last 30 minutes."""
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            WITH ranked AS (
              SELECT symbol, strategy, score, ts,
                     ROW_NUMBER() OVER (PARTITION BY symbol, strategy ORDER BY ts DESC) AS rn
              FROM scores WHERE ts > NOW() - INTERVAL '30 minutes'
            )
            SELECT symbol, strategy, score, ts FROM ranked WHERE rn = 1
            ORDER BY symbol, strategy
        """)).fetchall()
        weights = {w.name: float(w.weight)
                   for w in conn.execute(text(
                       "SELECT name, weight FROM strategy_weights WHERE enabled = TRUE"
                   )).fetchall()}

    by_symbol: dict = {}
    for r in rows:
        by_symbol.setdefault(r.symbol, {})
        by_symbol[r.symbol][r.strategy] = float(r.score)

    threshold = float(os.environ.get("ENSEMBLE_ENTRY_THRESHOLD", 0.40))
    result = []
    for sym, strat_scores in sorted(by_symbol.items()):
        composite = sum(strat_scores.get(s, 0) * weights.get(s, 0) for s in weights)
        result.append({
            "symbol": sym,
            "scores": strat_scores,
            "composite": round(composite, 4),
            "entry_eligible": abs(composite) >= threshold,
            "threshold": threshold,
        })
    return {"symbols": result, "weights": weights}


@app.get("/api/shadow-scores")
def shadow_scores(days: int = 7):
    with get_engine().connect() as conn:
        rows = conn.execute(text(f"""
            SELECT candidate,
                   COUNT(*) AS n,
                   AVG(score) AS avg_score,
                   AVG(fwd_return_4h) AS avg_fwd_ret,
                   SUM(CASE WHEN (score > 0 AND fwd_return_4h > 0)
                              OR (score < 0 AND fwd_return_4h < 0)
                            THEN 1 ELSE 0 END)::float
                     / NULLIF(COUNT(*) FILTER (WHERE fwd_return_4h IS NOT NULL), 0)
                       AS hit_rate
            FROM shadow_scores
            WHERE ts > NOW() - INTERVAL '{int(days)} days'
            GROUP BY candidate ORDER BY candidate
        """)).fetchall()
    return {"candidates": [dict(r._mapping) for r in rows]}


# ---------------------------------------------------------------------------
# Reflections / Risk
# ---------------------------------------------------------------------------

@app.get("/api/reflections")
def reflections(limit: int = 20):
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT id, run_ts, n_trades_analyzed, n_signals_analyzed,
                   summary, proposed_changes, applied, applied_ts,
                   applied_changes, reverted_at, revert_reason
            FROM reflections
            ORDER BY run_ts DESC LIMIT :n
        """), {"n": limit}).fetchall()
    return {"reflections": [dict(r._mapping) for r in rows]}


@app.get("/api/risk-state")
def risk_state():
    with get_engine().connect() as conn:
        row = conn.execute(text("""
            SELECT peak_equity, peak_equity_ts, sod_equity, sod_date,
                   lockdown, lockdown_reason, lockdown_ts
            FROM risk_state WHERE id = 1
        """)).fetchone()
        if row is None:
            return {"state": None}
        return {"state": dict(row._mapping)}


@app.get("/api/risk-events")
def risk_events(limit: int = 30):
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT ts, kind, detail FROM risk_events
            ORDER BY ts DESC LIMIT :n
        """), {"n": limit}).fetchall()
    return {"events": [dict(r._mapping) for r in rows]}


@app.get("/api/lab-events")
def lab_events(limit: int = 30):
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT ts, kind, detail FROM lab_events
            ORDER BY ts DESC LIMIT :n
        """), {"n": limit}).fetchall()
    return {"events": [dict(r._mapping) for r in rows]}


# ---------------------------------------------------------------------------
# Symbols (universe management)
# ---------------------------------------------------------------------------

@app.get("/api/symbols")
def symbols():
    env = _read_env()
    active_crypto = [s.strip() for s in env.get(
        "SR_BOT_SYMBOLS", "BTC/USD,ETH/USD,SOL/USD,LINK/USD,AVAX/USD,DOGE/USD,AAVE/USD"
    ).split(",") if s.strip()]
    active_equity = [s.strip() for s in env.get(
        "SR_BOT_EQUITIES", "DIA,SPY,QQQ"
    ).split(",") if s.strip()]
    # Fetch prices
    crypto_pool = sorted(set(active_crypto) | set(CRYPTO_POOL))
    equity_pool = sorted(set(active_equity) | set(EQUITY_POOL))
    cp, ep = {}, {}
    try:
        r = requests.get(
            "https://data.alpaca.markets/v1beta3/crypto/us/latest/trades",
            params={"symbols": ",".join(crypto_pool)},
            headers=_alpaca_headers(), timeout=8,
        )
        if r.ok:
            for s, t in r.json().get("trades", {}).items():
                cp[s] = float(t["p"])
    except Exception:
        pass
    try:
        r = requests.get(
            "https://data.alpaca.markets/v2/stocks/trades/latest",
            params={"symbols": ",".join(equity_pool), "feed": "iex"},
            headers=_alpaca_headers(), timeout=8,
        )
        if r.ok:
            for s, t in r.json().get("trades", {}).items():
                ep[s] = float(t["p"])
    except Exception:
        pass

    return {
        "crypto": [
            {"symbol": s, "active": s in active_crypto, "price": cp.get(s)}
            for s in crypto_pool
        ],
        "equity": [
            {"symbol": s, "active": s in active_equity, "price": ep.get(s)}
            for s in equity_pool
        ],
    }


class SymbolUpdate(BaseModel):
    active: list[str]


@app.post("/api/symbols/{kind}")
def update_symbols(kind: str, body: SymbolUpdate):
    if kind not in ("crypto", "equity"):
        raise HTTPException(400, "kind must be crypto or equity")
    key = "SR_BOT_SYMBOLS" if kind == "crypto" else "SR_BOT_EQUITIES"
    _write_env_key(key, ",".join(body.active))
    return {"ok": True, "key": key, "n": len(body.active)}


# ---------------------------------------------------------------------------
# Manual orders (Trade tab)
# ---------------------------------------------------------------------------

def _latest_price(symbol: str, is_crypto: bool) -> Optional[float]:
    try:
        if is_crypto:
            r = requests.get(
                "https://data.alpaca.markets/v1beta3/crypto/us/latest/trades",
                params={"symbols": symbol}, headers=_alpaca_headers(), timeout=8,
            )
            if r.ok:
                t = r.json().get("trades", {}).get(symbol)
                return float(t["p"]) if t else None
        else:
            r = requests.get(
                "https://data.alpaca.markets/v2/stocks/trades/latest",
                params={"symbols": symbol, "feed": "iex"},
                headers=_alpaca_headers(), timeout=8,
            )
            if r.ok:
                t = r.json().get("trades", {}).get(symbol)
                return float(t["p"]) if t else None
    except Exception:
        return None
    return None


@app.get("/api/clock")
def clock():
    """Alpaca market clock — lets the UI warn when equities are outside RTH."""
    try:
        r = requests.get(f"{_ALPACA}/v2/clock", headers=_alpaca_headers(), timeout=8)
        d = r.json()
        return {"ok": r.ok, "is_open": bool(d.get("is_open")),
                "next_open": d.get("next_open"), "next_close": d.get("next_close")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/quote")
def quote(symbol: str):
    is_crypto = "/" in symbol
    return {"symbol": symbol, "is_crypto": is_crypto,
            "price": _latest_price(symbol, is_crypto)}


class OrderRequest(BaseModel):
    symbol: str
    side: str                                  # 'buy' | 'sell'
    qty: Optional[float] = None
    notional: Optional[float] = None
    entry_type: str = "market"                 # 'market' | 'limit'
    limit_price: Optional[float] = None        # required when entry_type == 'limit'
    stop_price: float                          # stop-loss trigger
    stop_limit_price: Optional[float] = None   # set -> SL leg is a stop-LIMIT
    take_profit_price: float                   # take-profit limit


def _validate_order(o: OrderRequest, is_crypto: bool, ref_px: Optional[float]) -> None:
    if o.side not in ("buy", "sell"):
        raise HTTPException(400, "side must be 'buy' or 'sell'")
    if (o.qty is None) == (o.notional is None):
        raise HTTPException(400, "provide exactly one of qty or notional")
    if o.entry_type not in ("market", "limit"):
        raise HTTPException(400, "entry_type must be 'market' or 'limit'")
    if o.entry_type == "limit" and not o.limit_price:
        raise HTTPException(400, "limit_price required for a limit entry")
    if o.stop_price <= 0 or o.take_profit_price <= 0:
        raise HTTPException(400, "stop_price and take_profit_price must be > 0")
    # Equity bracket orders on Alpaca require whole-share qty (no notional/fractional).
    if not is_crypto:
        if o.qty is None:
            raise HTTPException(400, "equity bracket orders require qty (not notional)")
        if o.qty != int(o.qty):
            raise HTTPException(400, "equity bracket orders require whole-share qty")
    # SL/TP must straddle the entry on the correct sides.
    anchor = o.limit_price if (o.entry_type == "limit" and o.limit_price) else ref_px
    if anchor:
        if o.side == "buy":
            if not (o.stop_price < anchor < o.take_profit_price):
                raise HTTPException(400, "for a long: stop_price < entry < take_profit_price")
            if o.stop_limit_price and o.stop_limit_price > o.stop_price:
                raise HTTPException(400, "long stop-limit price should be <= stop_price")
        else:
            if not (o.take_profit_price < anchor < o.stop_price):
                raise HTTPException(400, "for a short: take_profit_price < entry < stop_price")
            if o.stop_limit_price and o.stop_limit_price < o.stop_price:
                raise HTTPException(400, "short stop-limit price should be >= stop_price")


@app.post("/api/order")
def place_order(o: OrderRequest):
    is_crypto = "/" in o.symbol
    ref_px = _latest_price(o.symbol, is_crypto)
    _validate_order(o, is_crypto, ref_px)

    if is_crypto:
        # No OCO/bracket for crypto — plain entry; the bot's monitor_exits loop
        # enforces the SL/TP we record below.
        payload: dict = {
            "symbol": o.symbol,
            "side": o.side,
            "type": o.entry_type,
            "time_in_force": "gtc",
        }
    else:
        payload = {
            "symbol": o.symbol,
            "side": o.side,
            "type": o.entry_type,
            "time_in_force": "day",
            "order_class": "bracket",
            "take_profit": {"limit_price": str(round(o.take_profit_price, 2))},
            "stop_loss": {"stop_price": str(round(o.stop_price, 2))},
        }
        if o.stop_limit_price:
            payload["stop_loss"]["limit_price"] = str(round(o.stop_limit_price, 2))
    if o.entry_type == "limit":
        payload["limit_price"] = str(o.limit_price)
    if o.qty is not None:
        payload["qty"] = str(o.qty)
    else:
        payload["notional"] = str(round(o.notional, 2))

    r = requests.post(f"{_ALPACA}/v2/orders", json=payload,
                      headers=_alpaca_headers(), timeout=15)
    if not r.ok:
        return {"ok": False, "status": r.status_code, "error": r.text}
    order = r.json()

    # Record the trade so the bot's monitor manages crypto SL/TP (and so manual
    # positions show SL/TP in the Bot/Trade tabs). Tagged 'manual'.
    entry_px = (float(o.limit_price) if o.entry_type == "limit" and o.limit_price
                else ref_px)
    qty = None
    try:
        qty = float(order.get("filled_qty") or order.get("qty") or 0) or None
    except (TypeError, ValueError):
        qty = None
    if qty is None and o.qty is not None:
        qty = float(o.qty)
    try:
        with get_engine().begin() as conn:
            conn.execute(text("""
                INSERT INTO trades (strategy, symbol, side, entry_ts, entry_price,
                                    quantity, mode, metadata)
                VALUES ('manual', :symbol, :side, NOW(), :entry_price,
                        :qty, 'paper', CAST(:meta AS JSONB))
            """), {
                "symbol": o.symbol,
                "side": "long" if o.side == "buy" else "short",
                "entry_price": entry_px,
                "qty": qty or 0,
                "meta": json.dumps({
                    "sl": o.stop_price,
                    "tp": o.take_profit_price,
                    "stop_limit_price": o.stop_limit_price,
                    "is_crypto": is_crypto,
                    "entry_type": o.entry_type,
                    "alpaca_order_id": order.get("id"),
                    "source": "manual_ui",
                }),
            })
    except Exception as e:
        # Order is already live on Alpaca; surface the bookkeeping failure but
        # don't pretend the order didn't happen.
        return {"ok": True, "order": order, "db_warning": str(e)}

    return {"ok": True, "order": order, "ref_price": ref_px}


# ---------------------------------------------------------------------------
# Action triggers
# ---------------------------------------------------------------------------

@app.post("/api/run-tick")
def run_tick(dry_run: bool = True):
    cmd = ["python", "/app/sr-bot/sr_paper_bot.py", "--once"]
    if dry_run:
        cmd.append("--dry-run")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return {"ok": True, "stdout": r.stdout, "stderr": r.stderr, "code": r.returncode}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/run-reflection")
def run_reflection():
    try:
        r = subprocess.run(
            ["python", "/app/sr-bot/reflection.py"],
            capture_output=True, text=True, timeout=180,
            env={**os.environ, "SR_BOT_ENV_PATH": str(_bot_env_path())},
        )
        return {"ok": True, "stdout": r.stdout, "stderr": r.stderr, "code": r.returncode}
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# WebSocket — live positions + scores fan-out
# ---------------------------------------------------------------------------

class WSManager:
    def __init__(self):
        self.clients: list[WebSocket] = []
        self.lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self.lock:
            self.clients.append(ws)

    async def disconnect(self, ws: WebSocket):
        async with self.lock:
            if ws in self.clients:
                self.clients.remove(ws)

    async def broadcast(self, msg: dict):
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)


ws_manager = WSManager()


async def _live_loop():
    """Push positions + latest scores every 5 seconds to all WS clients."""
    while True:
        try:
            payload = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "account": account(),
                "positions": positions().get("positions", []),
                "scores": scores_latest(),
            }
            await ws_manager.broadcast(payload)
        except Exception as e:
            print("live_loop error:", e, file=sys.stderr)
        await asyncio.sleep(5)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_live_loop())


@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            # Keep-alive — clients don't need to send anything but we wait
            # in case they want to ping or filter later.
            await websocket.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)
    except Exception:
        await ws_manager.disconnect(websocket)


# ---------------------------------------------------------------------------
# Static frontend (mounted last so /api/* takes precedence)
# ---------------------------------------------------------------------------

UI_DIST = Path("/app/ui/dist")
if UI_DIST.exists():
    app.mount("/assets", StaticFiles(directory=UI_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        # SPA fallback — serve index.html for any non-/api path
        index = UI_DIST / "index.html"
        if index.exists():
            return FileResponse(index)
        return JSONResponse({"error": "ui not built"}, status_code=503)
else:
    @app.get("/")
    def root():
        return {"status": "API running. UI not built yet. POST /api/* for data."}
