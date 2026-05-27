# DEVLOG — session continuity notes

**Purpose:** this file is the source of truth for in-progress work so that an
interrupted session can be resumed without relying on local/ephemeral session
memory. CLAUDE.md instructs every new session to read this file first.

**Rules for whoever edits this (human or Claude):**
- Read this file at the start of every session before touching code.
- Keep the **Active initiative** + **Progress** sections current as you work —
  update them when you finish a step or change direction, not just at the end.
- When an initiative is fully shipped, move its entry to **History** (terse).
- Record decisions (and the *why*) in **Decisions** so we don't relitigate them.

---

## Active initiative — Streamlit → React/FastAPI dashboard migration

Replacing the legacy Streamlit admin (`platform/admin.py`, container `tp-admin`,
:8501) with a React + FastAPI dashboard for faster load times and richer UI.

- **Frontend:** React + Vite + TypeScript + Tailwind in `platform/ui/`.
  Pages so far: Bot, Symbols, Reflections (`platform/ui/src/pages/`).
- **Backend:** FastAPI in `platform/api/main.py` — REST under `/api/*` + a
  `/ws/live` WebSocket that pushes account/positions/scores every 5s. In prod it
  also serves the built SPA from `/app/ui/dist`.
- **Container:** `tp-ui` service in `docker-compose.yml` (:8000). Runs uvicorn,
  bind-mounts `./platform:/app`. The legacy `tp-admin` Streamlit stays up during
  the transition so we never lose visibility.

### Current task: manual order-entry tab (stop-limit + take-profit)
The migration was interrupted mid-way through adding a manual order-entry tab.
It did **not** exist on disk when resumed (no `Trade.tsx`, no nav entry, no order
endpoint, nothing stashed) — so it's being rebuilt from scratch.

Goal: a new "Trade" tab to place a manual order with a **stop-limit** stop-loss
leg and a **take-profit** leg.

### Progress
- [x] Audited current state (pages, API, bot order mechanics, schema).
- [x] `POST /api/order` endpoint in `platform/api/main.py` (+ `/api/clock`,
      `/api/quote` helpers).
- [x] Widen `/api/positions` join to include `strategy='manual'` (adds `source`).
- [x] Bot: `fetch_open_bot_trades` includes `'manual'` (+ returns `strategy`);
      `monitor_exits` honors SL/TP only for manual trades (no trailing / time exit).
- [x] `platform/ui/src/pages/Trade.tsx` + nav wiring in `App.tsx`.
- [x] `npm run build` (tsc clean) + `docker compose up -d ui`; SPA serves on
      :8000, new endpoints verified, Trade component present in bundle.
- [ ] **Not yet done:** browser smoke test of the Trade tab + a real end-to-end
      paper order (deferred — needs explicit user OK to place a paper order).

**Verified 2026-05-24:** `GET /` returns the SPA; `/api/clock`, `/api/quote`,
`/api/positions` (with `source`) all respond. Trade tab not yet visually
confirmed in a browser (no browser in the remote session).

**ACTION NEEDED before placing a manual CRYPTO order:** restart the bot so it
runs the updated `monitor_exits` (`docker compose restart sr-bot`). Equity
manual orders use Alpaca's native bracket and don't need this. Not done yet.

---

## Decisions

- **Exit handling for manual orders = Hybrid (matches the bot).**
  - Equities → native Alpaca **bracket** order: `stop_loss {stop_price,
    limit_price}` (stop-LIMIT) + `take_profit {limit_price}`. Alpaca manages it.
  - Crypto → Alpaca has **no** OCO/bracket for crypto, so place a plain
    market/limit entry and record SL/TP in the DB so the bot's `monitor_exits`
    loop closes it.
  - Manual trades are tagged `strategy='manual'` in the `trades` table. The bot
    gates new entries on open Alpaca positions (symbol-based), so it will not
    double-enter a symbol that has a manual position.
  - **Why:** mirrors how the bot already places orders (see
    `sr_paper_bot.py::place_bracket`), least surprising, no new exit machinery.
- **Manual trades get SL/TP exits only** — `monitor_exits` skips trailing-stop
  and `MAX_HOLD_HOURS` time-exit for `strategy='manual'`, so a manual position
  isn't force-closed at 24h or trailed out against the user's explicit levels.
- **Run/verify mode = Docker (`tp-ui`)**, not the Vite dev server. FastAPI serves
  the built SPA, so the React app must be **built** (`npm run build` → `dist/`)
  before `docker compose up -d ui` — the compose command only runs uvicorn.

---

## Run / verify commands

```bash
# from repo root: /home/redji/trading-platform
cd platform/ui && npm run build && cd ../..   # produces platform/ui/dist
docker compose up -d ui                        # FastAPI + SPA on :8000
docker compose logs -f ui                      # tail
# UI:        http://localhost:8000/
# legacy UI: http://localhost:8501/  (tp-admin Streamlit, kept during migration)
```

Do **not** submit a real paper order during verification without explicit
user confirmation — Alpaca paper still places live paper orders.

---

## Gotchas / learnings

- Alpaca **crypto** does not support bracket/OCO orders — must self-manage SL/TP
  (this is why the bot has `monitor_exits`).
- Alpaca position symbols have **no slash** (`BTCUSD`); the `trades` table uses
  `BTC/USD`. Join on `symbol.replace('/','')`.
- DB stores timestamps in **UTC**; the UI renders **America/Los_Angeles**.
- `trades.strategy` is free-text (`TEXT NOT NULL`, no CHECK) — `'manual'` is safe.
- Bot SL/TP for a trade live in `trades.metadata->>'sl'` / `->>'tp'`.

---

## Active initiative #2 — migrate stack to LXC (Docker-in-LXC)

**Goal (set by user 2026-05-24):** keep going until the dashboard is back up and
running inside an LXC container. Node/config left to my discretion.

Approach: single **LXC system container** running Docker + docker-compose; the
compose stack moves in unchanged. Decided 2026-05-24.

**Host facts (2026-05-24):** LXD 5.21.4 LTS (snap); user `redji` in `lxd`+`docker`
groups; **LXD not yet initialized** (no storage pool / network). Disk: 54G free
on `/`. RAM: 7.7G total, ~5.3G free. `timescale_data` volume ~257M (small → fast
`pg_dump`). Unrelated host container `xray-reality` is NOT part of this stack.

**Constraints / risks:**
- Only ONE `sr-bot`+`lab` may run at a time — both hit the same Alpaca paper
  account, so two would double-trade. Stop host bot/lab before starting LXC ones.
- RAM is tight (7.7G) — don't run both full stacks at once; stop host services
  during cutover.

**DB migration method:** physical **volume copy** (not pg_dump) — both sides run
the identical `timescale/timescaledb:2.17.2-pg16` image, so copying the
`trading-platform_timescale_data` volume preserves hypertables exactly. Copy via
a throwaway alpine container (no sudo needed): tar the volume → `lxc file push`
→ extract into the LXC volume before first `compose up`. `schema.sql` only runs
on first init, which a pre-populated volume skips.

**Container:** `tp-stack` (Ubuntu 24.04, LXD), nesting on. Docker 29.5.2 +
compose v5.1.4 inside. Repo at `/root/trading-platform`.

**!! NETWORKING — critical, hard-won (2026-05-25) !!**
The host blackholes `lxdbr0`-forwarded traffic to censored IPs (Alpaca
`35.194.67.18`, Google/Cloudflare DNS) — Tailscale fwmark policy routing marks
the *host's own* egress (rules 5210–5250) but forwarded container traffic isn't
marked and is dropped on the host before `enp6s18`. Proven via tcpdump: the
host's own Docker containers reach Alpaca fine; the LXC container's SYN dies at
`lxdbr0`. (Non-censored hosts like pypi/debian/docker DID work over lxdbr0,
which masked this at first.)
**Fix = dual NIC on `tp-stack`:**
- `eth0` = lxdbr0 (LXD default profile) — host/management access only,
  **no default route** (`use-routes: false` in netplan so it keeps just the
  10.200.175.0/24 link route).
- `eth1` = **macvlan on `enp6s18`** (`lxc config device add tp-stack eth1 nic
  nictype=macvlan parent=enp6s18`) — DHCP from the LAN, **sole default route**.
  macvlan puts frames straight on the physical LAN, bypassing the host's
  lxdbr0/nftables/Tailscale drop. The container egresses like the host does.
- Nested Docker must route via eth1 too — it does once eth1 is the *only*
  default route (metric-based priority was unreliable: DHCP renew reset eth1's
  metric, so forwarded traffic fell back to eth0). Don't re-add an eth0 default.
- Nested Docker DNS: `/etc/docker/daemon.json` → `{"dns":["192.168.1.1","8.8.8.8"]}`
  (127.0.0.53 stub isn't reachable from the docker bridge netns).
- Verified: both the LXC container AND a nested `docker run` reach Alpaca + DNS.

**Dashboard access after cutover:** docker publishes :8000 on 0.0.0.0 inside the
container → reachable at `192.168.1.98:8000` (LAN/Tailscale) and
`10.200.175.25:8000` (from the host). Can also add an LXD proxy device on the
host's freed :8000 → container 127.0.0.1:8000 to preserve `localhost:8000`.

**Plan / progress — COMPLETE (2026-05-25):**
- [x] Init LXD: dir-backed storage pool `default` + `lxdbr0` wired to profile.
- [x] Launch `tp-stack` (Ubuntu 24.04) with `security.nesting=true`.
- [x] Install Docker + compose plugin inside the container.
- [x] Copy repo + `.env` files into the container (`/root/trading-platform`).
- [x] Build platform image + pull timescaledb in LXC.
- [x] **Resolve networking** (dual-NIC macvlan — see NETWORKING note above).
- [x] Cutover (volume copy): stopped host stack; exported DB volume (43M) →
      pushed → extracted into LXC volume; `compose up -d` in LXC.
- [x] Verified: SPA HTTP 200; `/api/account` live (equity ~$99.8k, Alpaca
      reachable); DB restored (11,562 trades / 3,219 signals / 6 reflections);
      `tp-sr-bot` ticking (single instance, host bot stopped → no double-trade).

**LIVE ENVIRONMENT IS NOW THE LXC CONTAINER `tp-stack`.**
- Dashboard access: `http://localhost:8000` (host, via LXD proxy device
  `ui8000`), `http://192.168.1.205:8000` (host LAN IP, same proxy), and
  `http://192.168.1.98:8000` (container macvlan IP, from LAN/Tailscale peers).
  Note: the host can't reach the container's macvlan IP directly (macvlan
  parent↔child limitation) — that's why the LXD proxy device exists.
- Legacy Streamlit admin also runs in LXC on :8501 (publishes inside container).
- **Rollback:** the host Docker stack is **stopped, not deleted** — its
  `trading-platform_timescale_data` volume is intact. To roll back: in LXC
  `docker compose stop sr-bot lab`, then on host `cd ~/trading-platform &&
  docker compose up -d`. NEVER run both bots at once (same Alpaca paper acct).
- Container egress depends on eth1 macvlan being the sole default route; if the
  bot loses Alpaca after a reboot, check `ip route show default` is via eth1.

**Cutover commands (when build done):**
```bash
# host: stop writers + db (also prevents double-trade)
cd /home/redji/trading-platform && docker compose stop sr-bot lab admin ui timescaledb
# host: export DB volume (root-free, via throwaway container)
docker run --rm -v trading-platform_timescale_data:/v -v /tmp:/out alpine \
  tar czf /out/tsdata.tgz -C /v .
lxc file push /tmp/tsdata.tgz tp-stack/root/tsdata.tgz
# LXC: populate volume, then bring up
lxc exec tp-stack -- bash -c "docker volume create trading-platform_timescale_data && \
  docker run --rm -v trading-platform_timescale_data:/v -v /root:/in alpine \
    sh -c 'rm -rf /v/* && tar xzf /in/tsdata.tgz -C /v' && \
  cd /root/trading-platform && docker compose up -d"
# expose dashboard: lxc config device add tp-stack ui8000 proxy \
#   listen=tcp:0.0.0.0:8000 connect=tcp:127.0.0.1:8000  (host :8000 is freed once host ui is stopped)
```

## History
_(none yet)_
