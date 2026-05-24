"""
Ingest historical bars from Alpaca into TimescaleDB.

Examples:
    # Daily equities (works fine on free tier — daily bars are EOD-vendor data)
    python ingest_alpaca.py --symbols SPY,QQQ,AAPL --timeframe 1day --years 5

    # Crypto minute bars (Alpaca crypto is a real consolidated feed, free tier OK)
    python ingest_alpaca.py --symbols BTC/USD,ETH/USD --timeframe 1min --years 2 --crypto

    # Equity minute bars: WORKS on free tier but the data is IEX-only
    # (~3% of consolidated volume). Backtest results built on this won't
    # generalize to real fills. The script will warn loudly.
    python ingest_alpaca.py --symbols AAPL --timeframe 1min --years 2

Adjustment policy: we request `adjustment="all"` so historical prices are
back-adjusted for splits AND dividends. Without this, splits create artificial
gaps in the price series that look like crashes to a strategy. Live trading
uses unadjusted prices by definition; that's a separate code path.
"""
import argparse
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Iterable

import pandas as pd
from alpaca.data.historical import (
    StockHistoricalDataClient,
    CryptoHistoricalDataClient,
)
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from sqlalchemy import text
from tenacity import retry, stop_after_attempt, wait_exponential

# Make platform/common importable when running from /app/data/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.db import get_engine  # noqa: E402


TIMEFRAME_MAP = {
    "1min":  TimeFrame(1,  TimeFrameUnit.Minute),
    "5min":  TimeFrame(5,  TimeFrameUnit.Minute),
    "15min": TimeFrame(15, TimeFrameUnit.Minute),
    "1hour": TimeFrame(1,  TimeFrameUnit.Hour),
    "1day":  TimeFrame(1,  TimeFrameUnit.Day),
}

INTRADAY_TIMEFRAMES = {"1min", "5min", "15min", "1hour"}


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30))
def fetch_stock_bars(client, symbols, tf, start, end):
    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=tf,
        start=start,
        end=end,
        # "all" applies both split and dividend adjustments — required for
        # multi-year backtests. Strategies trained on raw prices break across
        # any split in the window.
        adjustment="all",
    )
    return client.get_stock_bars(req).df


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30))
def fetch_crypto_bars(client, symbols, tf, start, end):
    req = CryptoBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=tf,
        start=start,
        end=end,
    )
    return client.get_crypto_bars(req).df


def chunked_date_ranges(
    start: datetime, end: datetime, days: int = 30
) -> Iterable[tuple[datetime, datetime]]:
    """Yield (start, end) windows. Alpaca handles big ranges, but chunking
    gives progress visibility and partial recovery if one window 500s."""
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=days), end)
        yield cur, nxt
        cur = nxt


def upsert_bars(df: pd.DataFrame, timeframe: str, source: str) -> int:
    """Upsert bars to DB via session-scoped TEMP staging table.

    TEMP + ON COMMIT DROP means the staging table:
      - is invisible to other connections (no parallel-ingestion conflicts),
      - vanishes at the end of the transaction (no debris if we crash mid-way).
    """
    if df.empty:
        return 0

    # Alpaca returns a multi-index (symbol, timestamp); flatten it.
    df = df.reset_index()
    df = df.rename(columns={"timestamp": "ts"})
    df["timeframe"] = timeframe
    df["source"] = source

    # Some bars come back without vwap/trade_count (esp. low-liquidity windows).
    for col in ("vwap", "trade_count"):
        if col not in df.columns:
            df[col] = None

    cols = [
        "symbol", "timeframe", "ts",
        "open", "high", "low", "close",
        "volume", "vwap", "trade_count", "source",
    ]
    df = df[cols]

    engine = get_engine()
    with engine.begin() as conn:
        # Create the temp table BEFORE pandas appends. ON COMMIT DROP makes it
        # session-scoped and self-cleaning — no risk of stale `bars_staging`
        # left behind by a crashed run.
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
                open        = EXCLUDED.open,
                high        = EXCLUDED.high,
                low         = EXCLUDED.low,
                close       = EXCLUDED.close,
                volume      = EXCLUDED.volume,
                vwap        = EXCLUDED.vwap,
                trade_count = EXCLUDED.trade_count
        """))
        return result.rowcount


def log_ingestion(source, symbol, timeframe, start, end, rows, status, error=None):
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO ingestion_log
                    (source, symbol, timeframe, range_start, range_end,
                     rows, status, error)
                VALUES
                    (:source, :symbol, :tf, :start, :end,
                     :rows, :status, :error)
            """),
            dict(source=source, symbol=symbol, tf=timeframe,
                 start=start, end=end, rows=rows, status=status, error=error),
        )


def warn_intraday_iex(timeframe: str, is_crypto: bool) -> None:
    """Loudly warn when ingesting equity intraday bars on free Alpaca.

    Free-tier Alpaca minute/hour equity bars are built from IEX trades only —
    roughly 3% of consolidated volume. Volumes will look tiny, prices can drift
    from the SIP, and any strategy with a volume filter will misbehave on real
    fills. Crypto and daily bars are unaffected.
    """
    if is_crypto or timeframe not in INTRADAY_TIMEFRAMES:
        return
    print(
        "\n"
        "==============================================================\n"
        "  WARNING: ingesting EQUITY INTRADAY bars from Alpaca.\n"
        "  On the free tier these bars are IEX-only (~3% of volume).\n"
        "  Backtests on this data may not survive real fills.\n"
        "  Consider --timeframe 1day for equities until you upgrade\n"
        "  to SIP data (Alpaca Algo Trader Plus or a Polygon plan).\n"
        "==============================================================\n",
        file=sys.stderr,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--symbols", required=True,
        help="Comma-separated, e.g. AAPL,MSFT or BTC/USD,ETH/USD",
    )
    ap.add_argument(
        "--timeframe", default="1min", choices=list(TIMEFRAME_MAP.keys()),
    )
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument(
        "--crypto", action="store_true", help="Use the crypto endpoint",
    )
    args = ap.parse_args()

    warn_intraday_iex(args.timeframe, args.crypto)

    symbols = [s.strip() for s in args.symbols.split(",")]
    tf = TIMEFRAME_MAP[args.timeframe]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(365 * args.years))

    api_key = os.environ["ALPACA_API_KEY"]
    api_secret = os.environ["ALPACA_API_SECRET"]

    if args.crypto:
        client = CryptoHistoricalDataClient(api_key, api_secret)
        fetch = fetch_crypto_bars
    else:
        client = StockHistoricalDataClient(api_key, api_secret)
        fetch = fetch_stock_bars

    total_rows = 0
    for chunk_start, chunk_end in chunked_date_ranges(start, end, days=30):
        try:
            df = fetch(client, symbols, tf, chunk_start, chunk_end)
            rows = upsert_bars(df, args.timeframe, source="alpaca")
            total_rows += rows
            print(f"[{chunk_start.date()} -> {chunk_end.date()}] {rows} rows")
            log_ingestion(
                "alpaca", ",".join(symbols), args.timeframe,
                chunk_start, chunk_end, rows, "ok",
            )
        except Exception as e:
            print(
                f"ERROR {chunk_start.date()}->{chunk_end.date()}: {e}",
                file=sys.stderr,
            )
            log_ingestion(
                "alpaca", ",".join(symbols), args.timeframe,
                chunk_start, chunk_end, 0, "error", str(e),
            )

    print(f"\nTotal: {total_rows} rows ingested.")


if __name__ == "__main__":
    main()
