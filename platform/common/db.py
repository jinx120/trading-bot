"""TimescaleDB connection + bar-loading helpers."""
import os
from datetime import datetime
from typing import Optional

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

_engine: Engine | None = None


def get_engine() -> Engine:
    """Lazy-initialized SQLAlchemy engine.

    Lazy so that `import` doesn't fail when env vars aren't set (e.g. running
    `--help` on an ingestion script). pool_pre_ping handles stale connections
    when TimescaleDB restarts under us during long ingests.
    """
    global _engine
    if _engine is None:
        url = (
            f"postgresql+psycopg2://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
            f"@{os.environ['DB_HOST']}:{os.environ.get('DB_PORT', 5432)}"
            f"/{os.environ['DB_NAME']}"
        )
        _engine = create_engine(
            url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
        )
    return _engine


def load_bars(
    symbol: str,
    timeframe: str,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> pd.DataFrame:
    """Load OHLCV bars from `bars_canonical` for a single (symbol, timeframe).

    Always queries the canonical VIEW, never raw `bars`. The raw table can
    contain duplicates across data sources (alpaca + yfinance for the same
    SPY day, for instance); the view applies source-priority deduplication.
    Strategy code MUST NOT bypass this — duplicate bars silently double-count
    every signal and trade.

    Returns a DataFrame indexed by ts (UTC tz-aware), columns:
      [open, high, low, close, volume, vwap, trade_count, source]
    Sorted ascending by ts. Empty DataFrame if no data.
    """
    sql = """
        SELECT ts, open, high, low, close, volume, vwap, trade_count, source
        FROM bars_canonical
        WHERE symbol = :symbol AND timeframe = :timeframe
    """
    params: dict = {"symbol": symbol, "timeframe": timeframe}
    if start is not None:
        sql += " AND ts >= :start"
        params["start"] = start
    if end is not None:
        sql += " AND ts <= :end"
        params["end"] = end
    sql += " ORDER BY ts ASC"

    df = pd.read_sql_query(
        text(sql), get_engine(), params=params, index_col="ts",
    )
    if not df.empty:
        # Ensure tz-aware UTC index even if the driver hands us naive datetimes.
        df.index = pd.to_datetime(df.index, utc=True)
    return df
