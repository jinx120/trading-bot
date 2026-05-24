# Trading Bot

Autonomous Support & Resistance paper-trading bot on Alpaca, with self-improvement.

## Stack

- **TimescaleDB** — trade history, signals, reflections, risk state
- **sr_paper_bot.py** — main bot loop (60s ticks)
- **Streamlit admin** — `http://localhost:8501/` · 3 pages: Bot / Reflections / Ingest
- **Alpaca paper** — broker

## What the bot does each tick

1. Hot-reload `.env` (every 5 ticks)
2. Run exit monitor — SL / TP / trailing stop / time-based exit
3. Update peak equity + start-of-day baseline
4. Risk circuit breakers (lockdown, daily loss cap, max drawdown, concentration)
5. Signal generation — 5-bar S&R pivots on 1H/4H, regime filter (ADX > 25 vetoes counter-trend), cooldown after stop-out
6. Dynamic sizing — ATR-volatility × recent-performance × confluence multipliers
7. Place bracket order, write trade row, log every evaluation to `signals`
8. Periodic reflection — auto-applies parameter changes to `.env`, no human gate

## Layout

```
sr-bot/                  # actual bot code (synced from /home/redji/sr-bot/)
  sr_paper_bot.py        # main loop
  reflection.py          # self-improvement
  risk_engine.py         # circuit breakers
  sizing.py              # dynamic position sizing
  sentiment.py           # optional ScrapeMCP sentiment hook
  .env                   # NOT committed — see .gitignore

platform/                # admin dashboard + shared code
  admin.py               # Streamlit UI (Bot / Reflections / Ingest pages)
  common/db.py
  sr-bot/                # bind-mounted into tp-sr-bot container

data/                    # schema.sql + Alpaca ingestion scripts
docker-compose.yml       # tp-timescaledb + tp-sr-bot + tp-admin
```

## Run

```
docker compose up -d
```

Then **http://localhost:8501/** for the admin UI.

## Risk parameters (in `sr-bot/.env`)

| key | default | meaning |
|---|---|---|
| `DAILY_LOSS_CAP_PCT` | 0.03 | No new entries if day PnL drops below -3% |
| `MAX_DRAWDOWN_PCT` | 0.15 | Triggers lockdown at -15% from peak |
| `KILL_SWITCH_RECOVERY_PCT` | 0.97 | Lockdown lifts when equity recovers to 97% of peak |
| `POSITION_CONCENTRATION_PCT` | 0.10 | Single symbol cap as % of equity |
| `MAX_OPEN_POSITIONS` | 8 | Hard cap on open positions |
| `TRAIL_TRIGGER_PCT` | 0.005 | Start trailing once 0.5% in profit |
| `TRAIL_DISTANCE_PCT` | 0.008 | Trail stop this far behind peak |
| `MAX_HOLD_HOURS` | 24 | Force-close after this many hours |
| `REFLECTION_AUTO_APPLY` | true | Reflection writes `.env` directly (no human gate) |
| `REFLECTION_EVERY_TICKS` | 360 | Auto-reflect every N ticks (~6h at 60s poll) |

## Research

Daily digests of algotrading forum findings live in `research/`. Each file is one day's notes from the remote-agent scan — proposals for new entry rules, regime detectors, parameter changes, etc.
