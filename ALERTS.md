# Grafana Alerts — Setup Guide

Recipes for piping platform events to email / Discord / generic webhook. The Grafana UI does the heavy lifting; this doc gives you the click-by-click + the SQL each alert rule should use.

You'll do these via the Grafana UI at <https://clawd.tail78f4cc.ts.net:8443/> (login: `admin` / `${GRAFANA_PASSWORD}` from `.env`).

---

## Part 1 — Create a Contact Point

A "contact point" is where alerts go. Set up one for each notification channel you want.

### Email

Grafana 11 supports SMTP out of the box but needs SMTP server config. Two paths:

**Option A: Gmail SMTP via app password (recommended for personal use)**

1. Generate a Gmail App Password: <https://myaccount.google.com/apppasswords>. (You need 2FA enabled.) Save the 16-char password.
2. Edit `docker-compose.yml`. Add to the `grafana` service env:
   ```yaml
   GF_SMTP_ENABLED: "true"
   GF_SMTP_HOST: "smtp.gmail.com:587"
   GF_SMTP_USER: "your.email@gmail.com"
   GF_SMTP_PASSWORD: "abcd efgh ijkl mnop"   # the 16-char app password
   GF_SMTP_FROM_ADDRESS: "your.email@gmail.com"
   GF_SMTP_FROM_NAME: "Trading Platform Alerts"
   ```
3. `docker compose up -d grafana` to recreate.
4. In Grafana UI: **Alerting** → **Contact points** → **+ Add contact point**.
5. Name: `email-self`. Integration: `Email`. Addresses: your email.
6. Click **Test** to verify. You should get a test email within 30s.

**Option B: Skip email; use Discord (often simpler)**

### Discord (recommended — no SMTP fuss)

1. In Discord: server settings → Integrations → Webhooks → New Webhook. Copy the webhook URL.
2. In Grafana UI: **Alerting** → **Contact points** → **+ Add contact point**.
3. Name: `discord-self`. Integration: `Discord`. Webhook URL: paste.
4. Click **Test**. You should get a Discord message within 5s.

### Generic webhook (for n8n, Zapier, custom endpoints)

1. **Alerting** → **Contact points** → **+ Add contact point**.
2. Name: `webhook-generic`. Integration: `Webhook`. URL: your endpoint.
3. Default settings work for most receivers.

---

## Part 2 — Notification Policy (route alerts to the contact point)

Without a notification policy, alerts have nowhere to go.

1. **Alerting** → **Notification policies**.
2. The "Default policy" at the top routes ALL alerts that don't match a child policy. Click the pencil to edit.
3. **Default contact point**: pick `email-self` or `discord-self` (whichever you set up).
4. Save.

For most setups, that's enough — every alert fires through the default. If you ever want severity routing (warnings to email, criticals to phone), create child policies later.

---

## Part 3 — Alert Rules (the actual triggers)

In Grafana UI: **Alerting** → **Alert rules** → **+ New alert rule**.

Each rule below uses the `TimescaleDB` datasource (provisioned, UID `timescaledb`).

### Rule 1: "Strategy auto-paused by divergence monitor"

Fires when the divergence monitor (or anyone) writes an `auto-pause` reason to `strategy_status`. This is your "something went wrong" canary.

**Configuration:**
- Name: `Strategy auto-paused`
- Datasource: `TimescaleDB` (uid `timescaledb`)
- Query (rename to `A`):
  ```sql
  SELECT COUNT(*) AS n
  FROM strategy_status
  WHERE paused_reason LIKE 'auto-pause%'
    AND updated_at > NOW() - INTERVAL '1 hour'
  ```
- Expression: `B = $A` (Reduce → Last)
- Condition: when `last() of B > 0`
- Evaluation: every `5m`, for `0m` (fire immediately)
- Summary: `{{ $values.B }} strategies got auto-paused in the last hour`
- Pick contact point.

### Rule 2: "Ingestion error in last hour"

Fires when an ingestion job hits a non-`ok` status. Catches Alpaca outages, yfinance breakages, etc.

```sql
SELECT COUNT(*) AS n
FROM ingestion_log
WHERE status != 'ok'
  AND ts > NOW() - INTERVAL '1 hour'
```

Same shape as Rule 1: condition `last() > 0`, every 5m.

### Rule 3: "Big drawdown on a recent backtest"

Fires when any backtest run in the last 24h shows >30% max drawdown. Catches accidentally bad strategies before you promote them.

```sql
SELECT MAX((stats->>'max_drawdown')::float) AS max_dd
FROM backtest_runs
WHERE run_ts > NOW() - INTERVAL '24 hours'
```

Condition: `last() > 0.30`. Evaluation: every 30m.

### Rule 4: "Live runner is enabled but hasn't placed a trade in 7 days"

Catches the case where the runner is supposedly active but isn't trading — could mean strategies stuck in flat state, or a silent bug.

```sql
SELECT
  CASE WHEN (SELECT enabled FROM runner_config WHERE id = 1) THEN 1 ELSE 0 END AS runner_on,
  (SELECT COUNT(*) FROM trades
   WHERE mode = 'paper' AND entry_ts > NOW() - INTERVAL '7 days') AS recent_trades
```

Condition: when `runner_on > 0 AND recent_trades < 1`. Note: alert evaluator's UI varies; you may need two queries (A and B) and a math expression. The principle is the same.

### Rule 5: "Position underwater more than 20%"

Catches an open position whose unrealized loss has exceeded 20%. Signals you should review or close.

```sql
SELECT MIN(pnl_pct) AS worst_pct
FROM trades
WHERE mode = 'paper' AND exit_ts IS NULL
```

Condition: `last() < -0.20`. Evaluation: every 30m.

(This requires open paper positions to be persisted with running pnl_pct, which the current schema does only for closed trades. You'd need to compute live unrealized PnL via Alpaca's positions endpoint and write it to a `live_positions` table for this rule to be meaningful. Skip it for now if you haven't built that.)

---

## Part 4 — Test the pipeline

Pick the simplest rule (e.g., Rule 1 or Rule 2). Trigger it artificially:

```bash
docker compose exec timescaledb psql -U trader -d trading -c "
INSERT INTO ingestion_log (source, symbol, timeframe, rows, status, error)
VALUES ('test', 'TEST/USD', '1day', 0, 'error', 'manually injected for alert test');
"
```

Wait up to 5 minutes. You should get a notification.

Clean up:
```bash
docker compose exec timescaledb psql -U trader -d trading -c "
DELETE FROM ingestion_log WHERE source='test';
"
```

The alert will resolve automatically on the next evaluation.

---

## Part 5 — Tuning

**Too noisy?** Increase the `for` duration on the rule (alert fires only after the condition is true for N minutes — filters transient blips).

**Not noisy enough?** Decrease the evaluation interval, or lower the threshold.

**Pages going to a phone?** Use a Discord webhook + the Discord mobile app. It's the cheapest reliable mobile alerting path. Real PagerDuty integration via webhook is also straightforward but overkill here.

**Alert dashboard:** Grafana → Alerting → Alert rules shows current state. The rules go through `Normal` → `Pending` → `Firing` → back to `Normal` as conditions change.

---

## What I left out

- **SMS via Twilio**: real cost, real account, real maintenance. Use Discord instead.
- **PagerDuty/Opsgenie**: enterprise-tier alerting; both are overkill for a personal platform but Grafana supports them via integration.
- **n8n routing**: if you want elaborate fan-out (alert → Slack AND email AND ticket), point Grafana's webhook contact point at n8n. n8n is well-documented; out of scope here.

---

## When you do this

1. Pick ONE contact point first (Discord recommended; cheapest and most reliable).
2. Create the notification policy pointing to it.
3. Create Rules 1, 2, and 3 above.
4. Test with a manual injection (Part 4).
5. Add Rule 4 + others as needed.

If the test injection doesn't fire: check the Alerting → Alert rules state, and check the alert state evaluation log (each rule has a "State history" tab).
