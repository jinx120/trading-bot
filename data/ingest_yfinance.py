"""
Ingest daily bars from yfinance for long-history backtesting.

yfinance gives you ~20 years of daily data free. Use it for daily-bar swing
strategies and for long-history regime testing (2008, 2020, etc).

Adjustment policy: `auto_adjust=True` returns split- AND dividend-adjusted
OHLC. Same reasoning as the Alpaca script — research code uses adjusted prices,
live trading uses unadjusted prices, and they're different code paths.

Usage:
    python ingest_yfinance.py --symbols AAPL,MSFT,SPY,QQQ --years 20
"""
import argparse
import os
import sys

import pandas as pd
import yfinance as yf
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.db import get_engine  # noqa: E402


def log_ingestion(symbol, rows, status, error=None):
    """Mirror of the Alpaca script's logger so both ingestion paths leave a
    trail in ingestion_log. Useful when 3am cron jobs silently fail."""
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO ingestion_log
                    (source, symbol, timeframe, rows, status, error)
                VALUES ('yfinance', :symbol, '1day', :rows, :status, :error)
            """),
            dict(symbol=symbol, rows=rows, status=status, error=error),
        )


def upsert_daily_bars(df: pd.DataFrame, symbol: str) -> int:
    if df.empty:
        return 0

    df = df.reset_index()
    df.columns = [c.lower() if isinstance(c, str) else c for c in df.columns]
    df = df.rename(columns={"date": "ts"})
    df["symbol"] = symbol
    df["timeframe"] = "1day"
    df["source"] = "yfinance"
    df["vwap"] = None
    df["trade_count"] = None

    # yfinance returns naive timestamps; treat as UTC end-of-day.
    if df["ts"].dt.tz is None:
        df["ts"] = df["ts"].dt.tz_localize("UTC")

    cols = [
        "symbol", "timeframe", "ts",
        "open", "high", "low", "close",
        "volume", "vwap", "trade_count", "source",
    ]
    df = df[cols]

    engine = get_engine()
    with engine.begin() as conn:
        # See ingest_alpaca.upsert_bars for the rationale on TEMP + ON COMMIT DROP.
        conn.execute(text("""
            CREATE TEMP TABLE bars_staging (
                symbol      TEXT,
                timeframe   TEXT,
                ts          TIMESTAMPTZ,
                open        DOUBLE PRECISION,
                high        DOUBLE PRECISION,
                low         DOUBLE PRECISION,
                close       DOUBLE PRECISION,
                volume      DOUBLE PRECISION,
                vwap        DOUBLE PRECISION,
                trade_count INTEGER,
                source      TEXT
            ) ON COMMIT DROP
        """))
        df.to_sql(
            "bars_staging", conn,
            if_exists="append", index=False, method="multi", chunksize=1000,
        )
        result = conn.execute(text("""
            INSERT INTO bars (symbol, timeframe, ts, open, high, low, close,
                              volume, vwap, trade_count, source)
            SELECT symbol, timeframe, ts, open, high, low, close,
                   volume, vwap, trade_count, source
            FROM bars_staging
            ON CONFLICT (symbol, timeframe, ts, source) DO UPDATE SET
                open   = EXCLUDED.open,
                high   = EXCLUDED.high,
                low    = EXCLUDED.low,
                close  = EXCLUDED.close,
                volume = EXCLUDED.volume
        """))
        return result.rowcount


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--years", type=int, default=20)
    args = ap.parse_args()

    period = f"{args.years}y"
    symbols = [s.strip() for s in args.symbols.split(",")]

    total = 0
    for sym in symbols:
        print(f"Fetching {sym} ({period})...")
        try:
            df = yf.download(
                sym,
                period=period,
                interval="1d",
                progress=False,
                # Adjusted OHLC. Without this, splits look like 50% crashes to
                # any backtest spanning the split date.
                auto_adjust=True,
            )
            # yfinance returns a column MultiIndex when downloading multiple
            # symbols, and sometimes for single symbols too in newer versions.
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            rows = upsert_daily_bars(df, sym)
            print(f"  {rows} rows")
            total += rows
            log_ingestion(sym, rows, "ok" if rows > 0 else "partial")
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            log_ingestion(sym, 0, "error", str(e))

    print(f"\nTotal: {total} rows.")


if __name__ == "__main__":
    main()
