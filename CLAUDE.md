# CLAUDE.md — trading-platform

## START HERE, EVERY SESSION
**Read [DEVLOG.md](DEVLOG.md) before doing anything else.** It is the source of
truth for in-progress work and exists so an interrupted session can resume
without relying on ephemeral session memory. Keep its **Active initiative** and
**Progress** sections up to date as you work, and record decisions (with the
*why*) there — don't just update at the end of a session.

## What this is
Autonomous Support & Resistance paper-trading bot on Alpaca, with a
self-improvement loop. See [README.md](README.md) for the architecture and
[MANUAL.md](MANUAL.md) for operational detail.

## Layout
- `platform/sr-bot/` — the bot (main loop, reflection, risk engine, sizing, lab).
- `platform/api/main.py` — FastAPI backend for the new dashboard (`/api/*`, `/ws/live`).
- `platform/ui/` — React + Vite + TypeScript + Tailwind frontend (replacing Streamlit).
- `platform/admin.py` — legacy Streamlit admin (`tp-admin`, :8501), kept during migration.
- `data/schema.sql` — TimescaleDB schema.
- `docker-compose.yml` — `tp-timescaledb`, `tp-sr-bot`, `tp-lab`, `tp-admin`, `tp-ui`.

## Conventions
- DB timestamps are **UTC**; render UI in **America/Los_Angeles**.
- Alpaca position symbols have no slash (`BTCUSD`); `trades` uses `BTC/USD`.
- Don't submit real paper orders during verification without explicit user OK.

## Build / run
```bash
cd platform/ui && npm run build && cd ../..   # build SPA -> dist/
docker compose up -d ui                        # serves SPA + /api on :8000
```
