-- ============================================================
-- Trading platform schema. Loaded ONCE on first DB init.
-- Schema changes after that need a migration (or volume wipe).
-- ============================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ============================================================
-- BARS (OHLCV) — core market data
-- ============================================================
-- `source` is part of the PK so we can store the same bar from multiple
-- providers (alpaca, yfinance, polygon...) and compare/cross-check. Strategies
-- MUST query through bars_canonical (defined below) to avoid double-counting.
CREATE TABLE IF NOT EXISTS bars (
    symbol      TEXT        NOT NULL,
    timeframe   TEXT        NOT NULL,    -- '1min','5min','15min','1hour','1day'
    ts          TIMESTAMPTZ NOT NULL,    -- bar close time, UTC always
    open        DOUBLE PRECISION NOT NULL,
    high        DOUBLE PRECISION NOT NULL,
    low         DOUBLE PRECISION NOT NULL,
    close       DOUBLE PRECISION NOT NULL,
    volume      DOUBLE PRECISION NOT NULL,
    vwap        DOUBLE PRECISION,
    trade_count INTEGER,
    source      TEXT        NOT NULL,
    PRIMARY KEY (symbol, timeframe, ts, source)
);

-- Hypertable: chunks by 7d windows. Tune chunk_time_interval if hot data
-- doesn't fit in memory (rule of thumb: a chunk should fit comfortably in RAM).
SELECT create_hypertable('bars', 'ts',
    if_not_exists => TRUE,
    chunk_time_interval => INTERVAL '7 days');

CREATE INDEX IF NOT EXISTS idx_bars_symbol_tf_ts
    ON bars (symbol, timeframe, ts DESC);

-- Compress old chunks: ~10x disk savings, modest read penalty. We don't query
-- 60-day-old 1-min bars and yesterday's bars in the same hot loop, so fine.
ALTER TABLE bars SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol, timeframe',
    timescaledb.compress_orderby = 'ts DESC'
);
SELECT add_compression_policy('bars', INTERVAL '30 days', if_not_exists => TRUE);

-- ============================================================
-- bars_canonical — the view strategies read.
-- ============================================================
-- Picks one row per (symbol, timeframe, ts) by source priority. Without this,
-- having both Alpaca and yfinance data for the same symbol silently doubles
-- query results. NEVER query the raw `bars` table from a strategy.
CREATE OR REPLACE VIEW bars_canonical AS
SELECT DISTINCT ON (symbol, timeframe, ts)
    symbol, timeframe, ts, open, high, low, close,
    volume, vwap, trade_count, source
FROM bars
ORDER BY symbol, timeframe, ts,
    -- Lower number = higher priority. Alpaca first (matches live feed),
    -- polygon next (consolidated SIP if added later), yfinance last (EOD only).
    CASE source
        WHEN 'alpaca'   THEN 1
        WHEN 'polygon'  THEN 2
        WHEN 'yfinance' THEN 3
        ELSE 99
    END;

-- ============================================================
-- BACKTEST RUNS — every backtest gets a row, with full stats
-- ============================================================
-- Defined BEFORE signals/trades/equity so they can reference it.
-- Not a hypertable: low row count (one per backtest), full FK support needed.
CREATE TABLE IF NOT EXISTS backtest_runs (
    id              BIGSERIAL PRIMARY KEY,
    run_ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    strategy        TEXT NOT NULL,
    params          JSONB NOT NULL,
    universe        TEXT[] NOT NULL,
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,
    timeframe       TEXT NOT NULL,
    is_walkforward  BOOLEAN NOT NULL DEFAULT FALSE,
    stats           JSONB NOT NULL,
    deflated_sharpe DOUBLE PRECISION,
    n_trials        INTEGER,         -- for multiple-testing correction
    passed_gate     BOOLEAN,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_bt_strategy_runts
    ON backtest_runs (strategy, run_ts DESC);

-- ============================================================
-- SIGNALS — every signal a strategy emits, even unactioned
-- ============================================================
-- Hypertable requirement: PK must include the time partitioning column. So
-- the PK is (id, ts) rather than just (id). `id` alone is still effectively
-- unique (BIGSERIAL never collides), the (id, ts) form just satisfies
-- TimescaleDB's invariant.
--
-- backtest_run_id is NULL for live/paper signals, populated for backtest runs.
-- This is what lets you say "show me signals from run #42" without grep.
-- No FK constraint: TimescaleDB hypertables can't be FK targets, and a FK
-- from a hypertable to a regular table (backtest_runs) works but blocks some
-- compression operations — kept as a logical reference instead.
CREATE TABLE IF NOT EXISTS signals (
    id              BIGSERIAL,
    ts              TIMESTAMPTZ NOT NULL,
    strategy        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    side            TEXT NOT NULL,           -- 'long','short','flat'
    strength        DOUBLE PRECISION,
    snapshot        JSONB NOT NULL,          -- indicator values at signal time
    acted_on        BOOLEAN NOT NULL DEFAULT FALSE,
    backtest_run_id BIGINT,                  -- NULL when mode != 'backtest'
    PRIMARY KEY (id, ts)
);
SELECT create_hypertable('signals', 'ts',
    if_not_exists => TRUE,
    chunk_time_interval => INTERVAL '30 days');

CREATE INDEX IF NOT EXISTS idx_signals_strategy_ts
    ON signals (strategy, ts DESC);
CREATE INDEX IF NOT EXISTS idx_signals_run
    ON signals (backtest_run_id) WHERE backtest_run_id IS NOT NULL;
-- Live runner queries unacted signals on the hot path.
CREATE INDEX IF NOT EXISTS idx_signals_unacted
    ON signals (strategy, ts) WHERE NOT acted_on;

-- ============================================================
-- TRADES — every executed trade (backtest, paper, live)
-- ============================================================
-- entry_signal_id: logical reference to signals.id, not enforced as FK
-- (signals is a hypertable; FKs to hypertables aren't supported).
CREATE TABLE IF NOT EXISTS trades (
    id              BIGSERIAL PRIMARY KEY,
    strategy        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,             -- 'long','short'
    entry_ts        TIMESTAMPTZ NOT NULL,
    entry_price     DOUBLE PRECISION NOT NULL,
    exit_ts         TIMESTAMPTZ,
    exit_price      DOUBLE PRECISION,
    quantity        DOUBLE PRECISION NOT NULL,
    pnl             DOUBLE PRECISION,
    pnl_pct         DOUBLE PRECISION,
    fees            DOUBLE PRECISION DEFAULT 0,
    slippage        DOUBLE PRECISION DEFAULT 0,
    entry_signal_id BIGINT,                    -- logical ref → signals.id
    exit_reason     TEXT,                      -- 'signal','stop','target','eod'
    mode            TEXT NOT NULL,             -- 'backtest','paper','live'
    backtest_run_id BIGINT REFERENCES backtest_runs(id),  -- NULL for paper/live
    metadata        JSONB
);
CREATE INDEX IF NOT EXISTS idx_trades_strategy_entry
    ON trades (strategy, entry_ts DESC);
CREATE INDEX IF NOT EXISTS idx_trades_mode
    ON trades (mode, strategy);
CREATE INDEX IF NOT EXISTS idx_trades_run
    ON trades (backtest_run_id) WHERE backtest_run_id IS NOT NULL;

-- ============================================================
-- EQUITY — equity curve snapshots
-- ============================================================
-- Hypertable: high-frequency writes (every minute live, every bar in
-- backtest). PK can't include nullable backtest_run_id, so we use a UNIQUE
-- INDEX with NULLS NOT DISTINCT (PG15+) — treats two NULL run_ids on the
-- same (ts, strategy, mode) as a duplicate. This requires backtests to
-- always populate backtest_run_id; live/paper writes leave it NULL.
CREATE TABLE IF NOT EXISTS equity (
    ts              TIMESTAMPTZ NOT NULL,
    strategy        TEXT NOT NULL,             -- 'portfolio' for aggregate
    mode            TEXT NOT NULL,
    backtest_run_id BIGINT REFERENCES backtest_runs(id),  -- NULL for paper/live
    cash            DOUBLE PRECISION NOT NULL,
    positions_value DOUBLE PRECISION NOT NULL,
    total_equity    DOUBLE PRECISION NOT NULL
);
SELECT create_hypertable('equity', 'ts',
    if_not_exists => TRUE,
    chunk_time_interval => INTERVAL '30 days');

CREATE UNIQUE INDEX IF NOT EXISTS idx_equity_unique
    ON equity (ts, strategy, mode, backtest_run_id) NULLS NOT DISTINCT;

-- ============================================================
-- STRATEGY STATUS — which strategies are allowed to trade live
-- ============================================================
CREATE TABLE IF NOT EXISTS strategy_status (
    strategy        TEXT PRIMARY KEY,
    enabled         BOOLEAN NOT NULL DEFAULT FALSE,
    promoted_run_id BIGINT REFERENCES backtest_runs(id),
    promoted_at     TIMESTAMPTZ,
    paused_reason   TEXT,
    -- Hash of the strategy YAML at time of promotion. If config drifts,
    -- the strategy must re-pass the gate before trading again.
    config_hash     TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- INGESTION LOG — track what we've pulled, gaps, errors
-- ============================================================
CREATE TABLE IF NOT EXISTS ingestion_log (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source      TEXT NOT NULL,
    symbol      TEXT,
    timeframe   TEXT,
    range_start TIMESTAMPTZ,
    range_end   TIMESTAMPTZ,
    rows        INTEGER,
    status      TEXT NOT NULL,                 -- 'ok','partial','error'
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_ingestion_log_ts
    ON ingestion_log (ts DESC);
