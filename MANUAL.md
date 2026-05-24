# Trading Platform — Owner's Manual

Last updated: 2026-05-10

This is the complete reference for the platform. It covers what each component does, how to drive it day-to-day, what every dashboard panel means, and the statistical concepts you need to interpret results without fooling yourself.

If you're new, read sections in order. If you're returning, jump to **Common workflows** for the recipes.

---

## 1. What this is

A research-grade algorithmic trading platform that:

- Ingests historical and live OHLCV data into TimescaleDB.
- Runs strategy code as pure functions of (bars, params).
- Backtests with vectorbt + walk-forward cross-validation + multiple-testing-corrected statistics (Probabilistic / Deflated Sharpe).
- Gates strategies through structured promotion criteria before they trade.
- Trades paper money on Alpaca via a polling runner that reads its config from the database (so the admin UI can toggle it without restarting anything).
- Surfaces everything through two dashboards: Streamlit (write actions) and Grafana (read-only analytics).

It is **not** a money-printer. The platform is the easy part — finding strategies that clear the strict gate is months-to-years of research. Most strategies you try will fail the gate, and that's the gate's job.

It is **not** an LLM-driven trader. There is no model "predicting" prices. Every decision is a deterministic function of bars and parameters. This is by design.

---

## 2. TL;DR — daily ops

| Task | Command |
|---|---|
| Bring stack up | `docker compose up -d` |
| Bring stack down | `docker compose down` |
| Tail runner logs | `docker compose logs -f platform` |
| Tail admin UI logs | `docker compose logs -f admin` |
| psql into DB | `docker compose exec timescaledb psql -U trader -d trading` |
| Open Python shell in container | `docker compose exec platform python` |
| Run a CLI command | `docker compose exec platform python /app/cli.py <subcommand>` |

URLs (over Tailscale, accessible from any device on your tailnet):

- **Streamlit (control):** https://clawd.tail78f4cc.ts.net/
- **Grafana (analytics):** https://clawd.tail78f4cc.ts.net:8443/

URLs over plain LAN (the host itself or other LAN devices):

- Streamlit: http://localhost:8501
- Grafana: http://localhost:3000
- TimescaleDB: localhost:5432

The Tailscale serve setup uses HTTPS with auto-issued certs. NOT exposed to the public internet. If you ever want to expose to the internet (you almost certainly should not while the dashboards are unauthenticated), you'd use `tailscale funnel` instead — but that comes with a security warning, see Section 11.

---

## 3. Architecture

Four containers, one network, two volumes:

```
                  ┌──────────────────────────────────────┐
                  │ TimescaleDB (tp-timescaledb)         │
                  │ - bars / signals / trades / equity   │
                  │ - backtest_runs / strategy_status    │
                  │ - universe / runner_config           │
                  │ - bars_canonical view                │
                  └─────────▲───────────▲────────────────┘
                            │           │
        ┌───────────────────┘           └────────────────┐
        │                                                │
┌───────┴───────────┐  ┌─────────────────────┐  ┌────────┴────────┐
│ platform          │  │ admin               │  │ grafana         │
│ (tp-platform)     │  │ (tp-admin)          │  │ (tp-grafana)    │
│ - runner_loop.py  │  │ - Streamlit UI      │  │ - dashboards    │
│   reads config    │  │ - read+write API    │  │ - read-only     │
│   from DB each    │  │ - port 8501         │  │ - port 3000     │
│   tick            │  │                     │  │                 │
│ - executes orders │  │                     │  │                 │
│   via Alpaca      │  │                     │  │                 │
│ - all research    │  │                     │  │                 │
│   code lives here │  │                     │  │                 │
└───────────────────┘  └─────────────────────┘  └─────────────────┘
        │
        │ HTTPS
        ▼
   Alpaca paper API
```

**Container responsibilities:**

- **timescaledb**: PostgreSQL 16 + TimescaleDB extension. All persistent state.
- **platform**: Python 3.11 image with all research/strategy/live code. The runner loop is its main process; you also `docker compose exec` into it for CLI / scripting.
- **admin**: Same image as platform, different command — runs Streamlit on port 8501. Sidecar service.
- **grafana**: Stock Grafana 11 with provisioned TimescaleDB datasource and one dashboard.

**Volumes:** `timescale_data` (DB rows), `grafana_data` (Grafana state). Both Docker-managed, persistent across container recreation.

**Code mounts:** `./platform → /app` and `./data → /app/data` are bind-mounts. Edit code on the host; it's live in the container. No rebuild needed for code changes (but env-var or `requirements.txt` changes do need a rebuild).

---

## 4. Streamlit Dashboard (control center)

URL: **https://clawd.tail78f4cc.ts.net/**

**Note on auth:** there is none. The dashboard is bound to localhost on the host and exposed via Tailscale serve to your tailnet only. Anyone on your tailnet can use it. If you add other people to your tailnet, they get full access including order placement.

### Sidebar
- Status indicator: shows whether the live runner is currently enabled
- Live runner config summary (poll interval, notional size, dry_run flag)
- Navigation: 7 pages

### Page: Status
Snapshot of the platform's current state.

- **Cash / Equity / Buying Power**: read live from your Alpaca paper account.
- **PDT?**: pattern day trader flag. If true, you're flagged for >3 day trades in a 5-day window. Doesn't matter for paper but reflects what would happen live.
- **Open Positions**: Alpaca positions table. The `manual_smoketest` row from chunk 5 might still show $9.78 of BTC.
- **Recent Activity (last 14d)**: union of recent backtests + trades. Useful for "what happened today/this week."

### Page: Universe
Manage the symbol list. The DB-backed `universe` table is the source of truth.

- **Add / update**: form. Pick a symbol like `LINK/USD` or `AAPL`, choose asset class, optionally enable for trading.
- **Existing symbols table**: shows ingestion + trading flags for each.
- **Per-symbol actions**: enable/disable trading, toggle ingestion, remove.

`enabled_for_trading` is the gate the live runner uses to filter strategy universes. A strategy's YAML lists *what it wants* to trade, but the runner intersects that with the universe table. Disabling a symbol here shuts off live orders without touching strategy code.

### Page: Strategies
Strategies that have been promoted via the gate (passed or failed).

- Table shows: enabled flag, promoted_run_id, config_hash (first 16 chars), promoted_at, paused_reason if rejected, walk-forward run count.
- **Toggle**: manually flip enabled (with reason capture for disable).

A strategy must first be promoted via `gate.promote.evaluate_gate` + `apply_gate_decision` before it appears here. Manual toggling here bypasses the gate — useful for emergencies (kill switch) but generally let the gate be authoritative.

### Page: Backtests
The leaderboard.

- Sort by: `dsr` (deflated Sharpe — what to actually trust), `sharpe` (raw — selection-biased), `max_dd` (smaller better), or `recent`.
- Each row shows the stats and notes.
- This is the same data Grafana's "Backtest leaderboard" panel shows; Streamlit lets you re-sort.

### Page: Backtest Launcher
Run a backtest from the UI.

- Pick strategy from registry (rsi_meanrev, donchian_swing, atr_breakout — extend by editing `STRATEGY_REGISTRY` in `live/runner.py`).
- Pick crypto/equity (decides Sharpe annualization: 365 vs 252 periods/year).
- Universe: multiselect from the Universe table, filtered by asset class.
- Date range, mode (full-period or walk-forward).
- Optional YAML override for params (otherwise reads the config file).
- Fees and slippage as decimal fractions (e.g. 0.0005 = 5 bps).
- **Run** button. Synchronous — the page locks while running. Most backtests complete in 30s–2min.

The result lands in `backtest_runs` and shows up immediately on the Backtests page, Grafana dashboard, and walk-forward summary panel.

### Page: Ingest Data
Pull bars from Alpaca or yfinance.

- Pick source (alpaca / yfinance), symbols (from universe or freetext), timeframe, years.
- **Start** button. Runs as a fire-and-forget background process inside the admin container.
- Recent ingestion activity table polls `ingestion_log` so you can see progress per chunk.

For long ingestions (multi-year minute bars across many pairs), expect 5–30+ minutes. The Streamlit page doesn't block — the subprocess runs independently. Refresh to see updated rows in the activity table.

### Page: Runner Controls
The live runner's config, exposed as a form.

- **Enabled**: master switch. False = idle (sleeps the runner; container stays alive).
- **Dry run**: log what would be traded but don't place orders. Useful when promoting a new strategy — turn this on for a day or two to see what signals would have fired.
- **Crypto mode**: TimeInForce.GTC for orders. Uncheck for equity-only strategies.
- **Notional per trade**: dollar amount per fresh signal. $50 is a sane default.
- **Poll interval (seconds)**: how often the runner ticks. 300 (5 min) is fine for daily strategies.

Save → runner reads new config on next tick (within 30s when disabled, within `poll_seconds` when enabled).

---

## 5. Grafana Dashboard (read-only analytics)

URL: **https://clawd.tail78f4cc.ts.net:8443/**
Login: `admin` / value of `GRAFANA_PASSWORD` in `.env`.

Dashboard URL: `/d/platform-overview/trading-platform-overview`

The dashboard auto-refreshes every 30 seconds.

### Panel 1: "Strategies — current status"
**Query:** reads `strategy_status` for all strategies.
**Columns:** strategy, enabled (color-coded — green = ENABLED, red = DISABLED), promoted_run_id, promoted_at, paused_reason (truncated to 120 chars), config_hash (first 16 chars), updated_at.

**How to read:**
- Green ENABLED row → the live runner is allowed to trade this strategy.
- Red DISABLED row → either the gate rejected it, or someone (you, divergence monitor) manually paused.
- `paused_reason` tells you exactly why. e.g. `min_deflated_sharpe: 0.65 below threshold 0.95` means the strict gate rejected on DSR; `auto-pause: divergence ratio 0.32 < 0.5` means the divergence monitor flagged.
- `config_hash` is the SHA-256 of (name, params) at promotion time. If you tweak the YAML, the hash changes and the runner refuses to trade until re-promoted.

**Red flag:** an ENABLED row with a stale `promoted_at` (months old) and the strategy's YAML has been edited. Means the live runner is trading something that no longer matches what was validated. Check by comparing current `config_hash()` (from the strategy class) against this row.

### Panel 2: "Backtest leaderboard — by deflated Sharpe"
**Query:** top-50 backtest_runs sorted by deflated_sharpe DESC.
**Columns:** id, strategy, run_ts, wf (is_walkforward), sharpe, dsr (color-graded: red <0, yellow 0–0.5, green ≥0.95), max_dd_pct (color-graded), trades, total_return_pct, n_trials, passed_gate.

**How to read:**
- The DSR column is the headline. Higher = more confidence the strategy has real edge after correcting for the n_trials backtests done.
- A green DSR cell (≥0.95) is a strict-gate pass. Yellow is research-grade. Red is no edge or worse.
- `wf=t` rows are walk-forward folds — these are the rows that matter most because they're sequential out-of-sample.
- `n_trials > 1` means this run came from a parameter sweep; the DSR has been deflated for the multiple testing.

**Red flag:** lots of `wf=f` rows with high Sharpe but no `wf=t` rows for the same strategy. Means you've been running full-period backtests but never doing walk-forward. Headline Sharpe alone is meaningless.

**Red flag:** a strategy enabled in `strategy_status` whose top WF rows all have DSR <0.5. Means you promoted via the research gate (`gate_research.yaml`) — that's fine for paper observation but never use it as a real-money signal.

### Panel 3: "Recent trades"
**Query:** last 100 trades across modes (backtest / paper / live).
**Columns:** id, mode, strategy, symbol, side, entry_ts, entry_price, exit_ts, exit_price, qty, pnl (color: green positive, red negative), pnl_pct, exit_reason, backtest_run_id.

**How to read:**
- `mode='paper'` rows are real fills on your Alpaca paper account.
- `mode='backtest'` rows are simulated. They have a backtest_run_id.
- `pnl` and `pnl_pct` are realized — only filled for closed trades. Open positions show NULL.
- `exit_reason` tells you why the trade closed: `signal` (strategy said exit), `Closed` (vectorbt), `stop` (stop-loss), `target` (take-profit), `eod` (end-of-day forced close, future feature).

**Useful queries to run alongside this panel** (in psql):
```sql
-- live PnL summary by strategy
SELECT strategy, COUNT(*) AS trades,
       SUM(pnl) AS total_pnl,
       AVG(pnl_pct) AS avg_pct
FROM trades WHERE mode='paper'
GROUP BY strategy;

-- biggest winners and losers
SELECT * FROM trades WHERE mode='paper' ORDER BY pnl DESC NULLS LAST LIMIT 5;
SELECT * FROM trades WHERE mode='paper' ORDER BY pnl ASC NULLS LAST LIMIT 5;
```

### Panel 4: "Equity curve — most recent backtest run"
**Query:** equity table for `backtest_run_id = MAX(id)`.
**Type:** time-series chart, USD on Y-axis.

**How to read:**
- Shows portfolio total equity over the backtest period for the latest run.
- Steep up = strategy made money. Flat = no trades or breakeven. Drawdowns visible as dips.
- This is the AGGREGATED portfolio (sum of per-symbol equities). For per-symbol breakdown, query `equity` directly with `strategy = 'atr_breakout:BTC/USD'` etc. (Per-symbol rows aren't currently persisted; this is a documented chunk-3 simplification.)

**Limitation:** "most recent backtest" includes single-fold WF runs and one-off launches. The MAX(id) heuristic is naive. To see a specific run, change the query in the Grafana panel editor or filter by `notes LIKE '%target_phrase%'`.

### Panel 5: "Ingestion (last 30d)"
**Query:** `ingestion_log` grouped by source + status.

**How to read:**
- Should be mostly `status='ok'` rows.
- Any `status='error'` count >0 → check the actual error in the table:
  ```sql
  SELECT ts, source, symbol, error FROM ingestion_log WHERE status='error' ORDER BY ts DESC LIMIT 10;
  ```
- `partial` status means some bars came back but not the full requested range.

### Panel 6: "Bars in DB by symbol & timeframe"
**Query:** `bars` grouped by symbol, timeframe, source.

**How to read:**
- Inventory of what you can backtest on. Symbols missing here can't be used.
- The `first` / `last` dates show the range — if `last` is more than a few days old, ingestion has fallen behind.
- Multiple `source` rows for the same (symbol, timeframe) = both Alpaca and yfinance have data. The `bars_canonical` view dedupes this for strategies; you don't need to worry about it unless you hit it directly.

**Red flag:** a symbol you expect to be tradable has no rows at the timeframe your strategy uses. Either ingest, or the strategy will silently no-op on that symbol.

### Panel 7: "Walk-forward fold-stability summary"
**Query:** aggregates `is_walkforward=TRUE` runs grouped by strategy.
**Columns:** strategy, n_folds, mean_sharpe, std_sharpe, mean_dsr, frac_positive_sharpe (color-graded — red <0.4, yellow 0.4–0.7, green ≥0.7).

**How to read:** this is the most diagnostic panel.
- `frac_positive_sharpe`: fraction of WF folds with positive Sharpe. <0.5 = coin flip. 0.7+ is healthy.
- `std_sharpe`: dispersion of Sharpe across folds. >1.5 = the strategy is regime-dependent and untrustworthy.
- `mean_dsr`: average DSR across folds. <0.5 → no detectable edge after correction.

**The rule of thumb:** a strategy worth promoting has frac+ ≥ 0.7 AND std_sharpe ≤ 1.5 AND mean_dsr ≥ 0.95. None of our reference strategies clear that bar. atr_breakout (mean_dsr ~0.77) is the closest, which is why it's enabled under the research gate.

---

## 6. Statistical concepts (read this once)

Three concepts you must internalize. The platform's value comes from doing these correctly.

### Sharpe ratio
$$ \text{SR}_\text{annual} = \frac{\overline{r}}{\sigma_r} \cdot \sqrt{N} $$

Where $\overline{r}$ is mean per-period return, $\sigma_r$ is per-period stdev, and $N$ is periods per year (252 for daily equities, 365 for daily crypto, 8760 for hourly crypto).

- A point estimate. Tells you nothing about confidence.
- Sharpe of 2.0 over 30 days is almost meaningless. Sharpe of 1.0 over 5 years is real.
- Annualized correctly: getting `N` wrong by an order of magnitude is the single most common stat-bug in retail backtests. The platform handles this for you (`is_crypto=True` flag etc.).

### Probabilistic Sharpe Ratio (Bailey & López de Prado, 2012)

$$ \text{PSR}(\text{SR}^*) = \Phi\!\left[\frac{(\hat{\text{SR}} - \text{SR}^*)\sqrt{T-1}}{\sqrt{1 - \gamma_3 \hat{\text{SR}} + \frac{\gamma_4 - 1}{4}\hat{\text{SR}}^2}}\right] $$

Where $\gamma_3$ is skewness, $\gamma_4$ is kurtosis, $T$ is number of return observations.

- Probability that the TRUE Sharpe exceeds the benchmark `SR*`, given the observed sample.
- PSR(0) > 0.95 = "95% confident this strategy has true positive Sharpe" (without correcting for multiple testing).
- Implemented in `research/stats.py:probabilistic_sharpe_ratio`.

### Deflated Sharpe Ratio (López de Prado, 2014)
$$ \text{DSR} = \text{PSR}(\text{SR}_0) \quad\text{where}\quad \text{SR}_0 = \sqrt{V[\text{SR}]} \cdot \mathbb{E}[\max\text{ of } N\,\mathcal{N}(0,1)] $$

- The PSR but with `SR*` set to the EXPECTED MAXIMUM Sharpe under H0 of N random strategies.
- Probability that the TRUE Sharpe exceeds zero, given that this strategy was selected as the best of `n_trials` tries.
- This is what kills retail "I tried 100 configs and the best had Sharpe 2.0!" — DSR with `n_trials=100` deflates that 2.0 by ~2.5 standard deviations of fake edge.
- Implemented in `research/stats.py:deflated_sharpe_ratio`. Used throughout backtest persistence.

**The platform's promotion gate uses DSR, not Sharpe.** The strict gate is `min_deflated_sharpe ≥ 0.95`. The research gate is 0.65.

### Walk-forward cross-validation
- Split the date range into rolling test windows (default: 12-month test, 6-month step).
- Run the strategy as-is (no parameter fitting) on each window.
- The fold-stability — variance of per-fold Sharpes, fraction of positive folds — is the diagnostic.
- A strategy with great IS Sharpe but inconsistent OOS folds is regime-dependent and not trustworthy.
- For TRUE walk-forward optimization (search params on each train fold, evaluate on test fold), our platform doesn't yet do that. Single-config WF is "rolling OOS evaluation across regimes."

### Multiple-testing correction
- If you sweep N parameter combinations and report the best Sharpe, you have selection bias.
- The "best of N" is biased upward by ~$\sqrt{2\ln N}$ standard deviations under H0.
- For N=100 random strategies, the expected max Sharpe is ~2.5 σ of fake edge — looks like a real result, isn't.
- The DSR formula corrects for this when you pass `n_trials = N`.
- Our `research/sweep.py` does this automatically. Importantly, it uses the EMPIRICAL cross-trial variance (not the conservative 1.0 default) since sweep configs are highly correlated.

---

## 7. CLI reference

The CLI is at `platform/cli.py`. Run inside the platform container:

```bash
docker compose exec platform python /app/cli.py <command>
```

### Universe management

```bash
# List
python /app/cli.py universe list
python /app/cli.py universe list --asset-class crypto
python /app/cli.py universe list --trading-only

# Add
python /app/cli.py universe add SHIB/USD --asset-class crypto
python /app/cli.py universe add MATIC/USD --asset-class crypto --enable-trading --notes "L2 polygon"

# Remove
python /app/cli.py universe remove SHIB/USD

# Toggle
python /app/cli.py universe enable-trading BTC/USD
python /app/cli.py universe disable-trading BTC/USD
python /app/cli.py universe enable-ingestion ADA/USD
python /app/cli.py universe disable-ingestion ADA/USD
```

### Strategy management

```bash
python /app/cli.py strategy list
python /app/cli.py strategy enable atr_breakout
python /app/cli.py strategy disable atr_breakout --reason "investigating divergence"
```

### Status

```bash
python /app/cli.py status
```

Prints a one-screen snapshot: universe counts, bar counts, backtest count, trade counts by mode, strategy enabled flags.

### Running a parameter sweep (Python, not CLI subcommand)

```python
# Inside the platform container, in a Python session
import sys; sys.path.insert(0, '/app')
from datetime import datetime, timezone
from strategies.atr_breakout import ATRBreakout
from research.sweep import parameter_sweep

grid = {
    'atr_period':         [10, 14, 20],
    'entry_atr_mult':     [1.5, 2.0, 2.5, 3.0],
    'exit_atr_mult':      [2.5, 3.0, 4.0],
    'max_bars_in_trade':  [20, 30, 60],
}
universe = ['BTC/USD', 'ETH/USD', 'SOL/USD', 'AVAX/USD', 'LINK/USD', 'DOGE/USD', 'AAVE/USD']
df = parameter_sweep(
    strategy_class=ATRBreakout,
    param_grid=grid,
    universe=universe,
    start=datetime(2022, 6, 1, tzinfo=timezone.utc),
    end=datetime(2026, 5, 9, tzinfo=timezone.utc),
    is_crypto=True,
    persist_top_k=3,  # writes top-3 to backtest_runs with proper n_trials
)
```

The sweep prints top-5 by DSR and by raw Sharpe, illustrating the selection-bias gap. The `persist_top_k=3` flag writes the top 3 winners to `backtest_runs` with `n_trials = grid size` baked in.

---

## 8. Common workflows

### Add a new symbol to track + trade

1. **Streamlit → Universe → Add / update**: enter symbol, pick asset class, optionally enable for trading.
2. **Streamlit → Ingest Data**: pick the symbol, timeframe, years. Hit Start.
3. Wait for ingestion to complete (watch the activity table on the page).
4. Optional: backtest your strategies against the new symbol via Backtest Launcher.

### Run a backtest

1. **Streamlit → Backtest Launcher**.
2. Pick strategy, universe, dates, mode.
3. Hit Run. Wait 30s–2min.
4. Result shows up here, on Backtests page, and on Grafana.

### Run a parameter sweep with proper DSR

CLI only for now (Streamlit launcher is chunk-8 work):
```bash
docker compose exec platform python -c "
import sys; sys.path.insert(0, '/app')
from datetime import datetime, timezone
from strategies.atr_breakout import ATRBreakout
from research.sweep import parameter_sweep

grid = {'atr_period': [10, 14, 20], 'entry_atr_mult': [1.5, 2.0, 2.5]}
universe = ['BTC/USD', 'ETH/USD', 'SOL/USD']
parameter_sweep(ATRBreakout, grid, universe,
    datetime(2023, 1, 1, tzinfo=timezone.utc),
    datetime(2026, 5, 1, tzinfo=timezone.utc),
    is_crypto=True, persist_top_k=3)
"
```

### Promote a strategy through the gate

1. Run a walk-forward backtest first (Backtest Launcher → Mode = Walk-forward).
2. In Python:
   ```python
   from strategies.atr_breakout import ATRBreakout
   from gate.promote import evaluate_gate, apply_gate_decision, print_gate_result

   strat = ATRBreakout.from_yaml('/app/strategies/configs/atr_breakout.yaml')

   # Strict gate — for real-money confidence
   result = evaluate_gate(strat)  # uses gate_config.yaml
   print_gate_result(result)
   if result.passed:
       apply_gate_decision(result)

   # Research gate — for paper-trading observation
   result_r = evaluate_gate(strat, gate_config_path='/app/gate/gate_research.yaml')
   if result_r.passed:
       apply_gate_decision(result_r)
   ```
3. The strategy now appears in Streamlit's Strategies page and the Grafana strategy_status panel.
4. Streamlit → Runner Controls → Enabled = TRUE if you want it actively trading.

### Add a brand-new strategy

1. Create `platform/strategies/my_strategy.py`. Subclass `Strategy`. Implement `_validate_params` (fill defaults, raise on bad input) and `generate_signals` (return list of `Signal`).
2. Register in `platform/live/runner.py:STRATEGY_REGISTRY`:
   ```python
   from strategies.my_strategy import MyStrategy
   STRATEGY_REGISTRY = {
       ...,
       "my_strategy": MyStrategy,
   }
   ```
3. Drop a YAML at `platform/strategies/configs/my_strategy.yaml` with the matching `name:`, `description`, `universe`, `params`.
4. Restart the admin container so the registry change is picked up: `docker compose restart admin platform`.
5. Now backtest via Streamlit, then promote through the gate.

### Disable a strategy in an emergency

- Streamlit → Strategies → select strategy → Disable with reason.
- Or CLI: `python /app/cli.py strategy disable my_strategy --reason "kill switch"`.
- The runner picks up the change on its next tick (within `poll_seconds`).

### Stop the runner without disabling strategies

- Streamlit → Runner Controls → uncheck Enabled, Save.
- The runner sleeps. Strategies remain configured. Re-enable to resume.

### Close the BTC test position from chunk 5

```python
# In Python
from live.broker import AlpacaBroker
b = AlpacaBroker()
order_id = b.close_position('BTC/USD', is_crypto=True)
print(f"close order: {order_id}")
```

Or do it via the Alpaca dashboard: <https://app.alpaca.markets/paper/dashboard/positions>.

---

## 9. File layout

```
trading-platform/
├── docker-compose.yml          # 4 services + 2 volumes
├── .env                        # secrets — gitignored
├── .env.example                # template
├── .gitignore
├── MANUAL.md                   # this file
├── data/
│   ├── schema.sql              # initial DB schema (runs once on volume init)
│   ├── ingest_alpaca.py        # Alpaca historical bars → DB
│   └── ingest_yfinance.py      # yfinance daily → DB
├── platform/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── cli.py                  # admin CLI
│   ├── admin.py                # Streamlit dashboard entrypoint
│   ├── common/
│   │   ├── db.py               # SQLAlchemy engine + load_bars helper
│   │   └── indicators.py       # pure-numpy RSI, ATR, Donchian, SMA, EMA
│   ├── strategies/
│   │   ├── base.py             # Strategy ABC + Signal dataclass
│   │   ├── rsi_meanrev.py      # reference: RSI mean-reversion
│   │   ├── donchian_swing.py   # reference: Donchian breakout
│   │   ├── atr_breakout.py     # reference: ATR volatility breakout
│   │   └── configs/            # YAMLs
│   ├── research/
│   │   ├── stats.py            # Sharpe, PSR, DSR, drawdown, etc.
│   │   ├── backtest.py         # vectorbt runner with DB persistence
│   │   ├── walkforward.py      # rolling-window WF CV
│   │   └── sweep.py            # parameter sweep with DSR deflation
│   ├── gate/
│   │   ├── promote.py          # evaluate + apply gate decisions
│   │   ├── gate_config.yaml    # strict gate (real-money bar)
│   │   └── gate_research.yaml  # research gate (paper observation bar)
│   └── live/
│       ├── broker.py           # Alpaca wrapper (paper-only)
│       ├── runner.py           # LiveRunner — one-tick logic
│       ├── runner_loop.py      # the platform container's main process
│       └── monitor.py          # divergence detector + auto-pause
└── grafana/
    └── provisioning/
        ├── datasources/timescaledb.yaml
        └── dashboards/
            ├── dashboards.yaml
            └── platform_overview.json
```

### Key DB tables

- `bars` (hypertable) + `bars_canonical` (view): OHLCV market data.
- `signals` (hypertable): one row per emitted signal, tagged with `backtest_run_id` (NULL for live).
- `trades`: one row per closed trade. Modes: `backtest`, `paper`, `live`.
- `equity` (hypertable): equity curve snapshots, tagged with `backtest_run_id`.
- `backtest_runs`: one row per backtest. Has `stats` JSONB blob with full results.
- `strategy_status`: one row per known strategy. Drives the live runner.
- `universe`: symbol metadata + ingestion/trading flags.
- `runner_config`: single-row table holding live runner state.
- `ingestion_log`: ingestion progress + errors.

---

## 10. Tailscale access

Setup already done:
```bash
sudo tailscale serve --bg --https=443 http://localhost:8501   # Streamlit
sudo tailscale serve --bg --https=8443 http://localhost:3000  # Grafana
```

Adding a new device to your tailnet (laptop, phone) gets it both URLs automatically.

To check current serve config:
```bash
sudo tailscale serve status
```

To turn off serve (e.g. when traveling and don't want it accessible):
```bash
sudo tailscale serve --https=443 off
sudo tailscale serve --https=8443 off
```

To bring back:
```bash
sudo tailscale serve --bg --https=443 http://localhost:8501
sudo tailscale serve --bg --https=8443 http://localhost:3000
```

**Why not `funnel` (public internet exposure):** Streamlit has no auth and operates a paper trading account. If you funnel it, anyone with the URL can place orders, change strategy state, and see your account state. Don't.

If you ever want broader access:
- Add basic auth via a reverse proxy (Caddy with `basic_auth` directive).
- Or run a real auth proxy (oauth2-proxy with GitHub or Google).
- Then funnel is defensible, but only with auth in front.

---

## 11. Troubleshooting

### Container won't start
```bash
docker compose logs <service>
```
Common causes: env var missing in `.env`, port collision (8501/3000/5432 already in use), volume permissions.

### Streamlit shows "Database connection error"
- Check the timescaledb container is healthy: `docker compose ps`.
- Check `.env` has `POSTGRES_PASSWORD` matching what timescaledb container thinks.

### Runner is enabled but never trades
1. Check Streamlit → Strategies. Is anything `enabled=TRUE`?
2. Tail `docker compose logs -f platform`. Are ticks happening?
3. For each tick, look at the outcome line per symbol. Likely `[no_signal]` — strategy isn't currently in a fresh-signal state.
4. If you see `[config_drift]`, the YAML changed since promotion. Re-evaluate gate or revert YAML.
5. If you see `[disabled]`, the strategy is paused. Check Strategies page for reason.

### Backtest fails with "no bars"
- Check Universe → ingestion is enabled for that symbol.
- Check Grafana panel "Bars in DB" — does the symbol+timeframe combo have rows?
- If not, run Ingest Data for that symbol first.

### Grafana panels show "No data"
- Datasource health: visit Grafana → Connections → Data sources → TimescaleDB → Test. Should say "Database Connection OK."
- If broken: check `POSTGRES_PASSWORD` is exposed to the grafana container in `docker-compose.yml`.

### Runner is placing orders I don't want
1. Streamlit → Runner Controls → Enabled = false. Save.
2. Or: Streamlit → Strategies → disable specific strategy.
3. Or hard stop: `docker compose stop platform`.

### Position lookup fails for crypto
- Alpaca uses `BTC/USD` for orders but `BTCUSD` for positions. The broker normalizes via `_position_symbol()`. If you're hitting alpaca-py directly, strip the slash.

---

## 12. What NOT to build (lessons learned)

These have been pushed back on twice. Re-listed here so future-you doesn't relitigate.

### 1. "Online learning that re-tunes strategy params from live PnL"
- 6 months of paper gives ~50–200 trades. Tuning on that = fitting noise.
- Strategies will converge on what worked yesterday → catastrophic regime shift.
- The realistic version is what we built: parameter sweeps in research, with DSR deflation, off-line.

### 2. "Microswing strategies for small-amplitude moves"
- 0.5% target move - 0.30% round-trip cost = 0.20% gross at best.
- After slippage and spreads on retail-tier infrastructure, edge is gone.
- Hourly RSI mean-reversion already showed this: DSR 0.02, -36% return, 693 trades. Costs > signal.
- Microswing trading requires sub-millisecond infra, direct exchange access, and modeled execution costs we don't have.

### 3. "LLM-generated trading signals or sentiment-as-signal"
- LLM-driven trading has zero validated edge in published academic results.
- Sentiment from social media correlates with retail FOMO peaks, not future returns.
- By the time something is "talked about," the move is largely over.
- The platform's job is to TEST hypotheses, not GENERATE them.

### Acceptable adjacent work (chunk 8+ candidates)

- **Capital allocator** across promoted strategies (weight by rolling realized Sharpe vs backtest expectation). Bounded, slow, defensible.
- **Per-symbol breakdown** of WF results — see if a strategy's edge is diversified or concentrated in one pair.
- **Real execution-cost modeling** — fees + spread + slippage that scales with trade size and book depth.
- **Tighter alert wiring** — Grafana contact points for: divergence auto-pause, ingestion errors, gate decisions, big drawdowns.
- **Discovery feed** (NOT signal): a small daemon that pulls trending tickers from finance forums (Reddit's API, RSS feeds) into a `discovery_candidates` table, for you to manually review and add to universe. Strictly enumeration, never trading.

---

## 13. Webscraping / sentiment scraping — my recommendation

You asked: "should we plan to integrate some sort of webscraping to dynamically search internet forums and social medias … to query for tickers and strategies being talked about?"

**My answer: not as a trading signal, possibly as a discovery feed only.**

The trading-signal version is the same trap as LLM guesswork. Sentiment leads price in research papers only when measured at fine timescales with social-listening infrastructure no retail trader has. By the time something is on r/cryptocurrency front page or trending on X, the move has happened — you'd be the exit liquidity.

The defensible version is much narrower: a **discovery feed**. A small periodic job that pulls trending finance discussion (Reddit's official API for /r/algotrading, /r/cryptocurrency, /r/wallstreetbets; finance RSS aggregators) into a `discovery_candidates` table. Each row is `(ticker, source, mention_count, last_seen, sample_post_url)`. You browse this table once a week and pick interesting candidates to add to your `universe` table for proper backtesting.

The platform's gate stays the same. The discovery feed is a top-of-funnel for symbol candidates, not a strategy. It's NOT used in live trading decisions, NOT consumed by any model, NOT input to any signal.

If you want this, it's a small chunk-8+ module:
1. A `data/discovery_scrape.py` that queries Reddit + RSS endpoints with throttling.
2. A `discovery_candidates` table.
3. A Grafana panel + Streamlit page showing trending tickers.
4. NO connection whatsoever to the runner or strategies.

Don't build it as a strategy input. Build it as a research-prompt, if at all.

---

## 14. Future roadmap (chunks 8 and beyond)

In rough priority order:

1. **Capital allocator** — weight notional across promoted strategies based on rolling Sharpe matching their backtest expectations. Slow, bounded, conservative. The legitimate "system gets smarter" loop.
2. **Per-symbol WF breakdown** — let the gate criteria be per-symbol so a strategy can be promoted on the symbols it works on, paused on the ones it doesn't.
3. **Streamlit sweep launcher** — synchronous-with-progress version of `parameter_sweep` accessible via UI.
4. **Real fee modeling** — Alpaca's tiered crypto fees (0.15–0.25% per side), spread modeling, depth-aware slippage.
5. **Alert pipeline** — Grafana → email/discord/sms on divergence auto-pause, ingestion errors, big drawdowns.
6. **Discovery feed** (above) — Reddit + RSS pull, NOT a signal.
7. **Strategy templates** — scaffold script for `python /app/cli.py strategy new <name>` to drop the boilerplate.
8. **DB-driven strategy params** (instead of YAML) — would let Streamlit edit params live, with config_hash drift detection unchanged.

None of these are needed to do real research. They're all quality-of-life. The platform as-is is sufficient.

---

## 15. Where the actual hard part starts

Everything above is infrastructure. None of it makes money.

Making money requires finding strategies that:
- Pass the strict gate (DSR ≥ 0.95) on properly walk-forwarded backtests
- Survive on out-of-sample data after the backtest period
- Continue to work in real fills (not just on backtest assumptions)
- Survive regime shifts

This is months-to-years of work. Most strategies you write will fail. The reference strategies all failed. ATR breakout (the best one we found) only clears the research gate, not strict.

What you do now:
1. Open Streamlit. Watch the runner do its (mostly nothing) thing.
2. Read finance literature. Pick a hypothesis (e.g. "momentum breakout works after high-vol days"). Implement it as a strategy.
3. Backtest, walk-forward, gate. Almost certainly: fails. Move on.
4. Repeat 50+ times. Eventually find something that holds up.
5. Promote, observe, monitor divergence. If divergence is excessive, the strategy doesn't survive real fills — back to step 2.

The platform's job is to make step 3 fast and honest. The gate is honest because it can't be tuned away from rejecting bad strategies — that's why it's strict by default.

Good luck. The gate doesn't care how much you want a strategy to pass.
