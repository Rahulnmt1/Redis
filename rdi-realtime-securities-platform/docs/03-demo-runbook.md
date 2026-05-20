# Demo Runbook — Securities & Trading Firm RDI Demo

> This is the **operator-facing** companion to the talk track.
> Run through the pre-demo checklist 1 hour before the meeting,
> then keep this open during delivery.

---

## Pre-demo checklist (T-60 minutes)

### Hardware / environment

- [ ] Laptop has at least **8 GB free RAM** and **15 GB free disk**.
- [ ] Docker Desktop is running. Allocate **6 GB RAM, 4 CPUs minimum**.
- [ ] On WiFi capable of pulling Docker images (~2 GB pull on first run).
- [ ] External monitor / projector tested. Browser zoom set so dashboard
      renders cleanly at 1280×800 minimum.

### Run the stack & warm caches

```bash

# Clean any stale state
./scripts/teardown.sh 2>/dev/null || true

# Full bring-up
./scripts/setup.sh
```

Expect ~3 minutes. Watch for the "READY" banner at the end.

### Smoke tests (must all pass before the meeting)

```bash
# 1. Postgres healthy and seeded
docker exec sectrade-postgres psql -U postgres -d sectrade -c \
  "SELECT COUNT(*) AS customers FROM portfolio.customer;"
# expected: 10

# 2. Debezium running
docker logs sectrade-debezium 2>&1 | tail -20 | grep -i "Snapshot ended"
# expected: at least one match within a minute of start

# 3. RDI processor consuming
docker logs sectrade-rdi-processor 2>&1 | tail -20 | grep -c "JSON.SET\|HSET"
# expected: >20

# 4. Target Redis has data
docker exec sectrade-redis-enterprise redis-cli -p 12000 DBSIZE
# expected: >= 40

# 5. One known JSON key resolves
docker exec sectrade-redis-enterprise redis-cli -p 12000 \
  JSON.GET customer:HS0010001 \$
# expected: {"customer_id":10001,...}

# 6. Dashboard answers
curl -s http://localhost:5050/api/customers | head -c 200
# expected: JSON array starting with [{"client_code":"HS0010001",...

# 7. Latency endpoint
curl -s http://localhost:5050/api/latency/HS0010001
# expected: {"postgres_ms":..., "redis_ms":..., "speedup":...}

# 8. Mock RDI control-plane API up (drives Insight's RDI tab)
curl -sk https://localhost:8445/ | head -c 120
# expected: {"note":"Demo-only mock. ...","service":"sectrade-mock-rdi-api","version":"..."}
curl -sk https://localhost:8445/api/v1/monitoring/statistics \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("stream count =",len(d["data_streams"]["streams"]),"total =",d["data_streams"]["totals"]["total"])'
# expected: stream count = 5, total > 0
```

If any of these fail, see Troubleshooting at the bottom.

### Browser tabs to have open

In this order on the demo machine:

1. **Slide deck**: `docs/01-slide-deck.html` (or open via Finder)
2. **Portfolio dashboard**: <http://localhost:5050>
3. **Redis Insight**: <http://localhost:5540>
4. **Target Redis Enterprise UI**: <https://localhost:8443> (already logged in)
5. **GitHub Redis docs tab** for backup: <https://redis.io/docs/latest/integrate/redis-data-integration/>

### Terminals to have ready

Open three tabs in your terminal:

| Tab | Purpose | Pre-typed command |
|---|---|---|
| 1 | psql session for live SQL | `docker exec -it sectrade-postgres psql -U postgres sectrade` |
| 2 | Simulation runner | `./scripts/run-simulation.sh` (don't execute yet) |
| 3 | Spare / logs | `docker logs -f sectrade-rdi-processor` |

---

## Demo flow (suggested 25 minutes)

Slide deck structure (matches `docs/01-slide-deck.pptx`):

| Slides | Purpose |
|---|---|
| 1–9   | Problem framing, RDI architecture, industry mapping, demo center |
| 10    | Six demo scenarios at a glance |
| 11–13 | TCO · Security · Next Steps |
| 14    | "Let's run the demo" — transition |
| 15–20 | One slide per scenario — keep on screen *during* the live run, speaker notes hold the exact CLI |
| 21    | Q&A wrap — three PoC KPIs |

Open the pptx in **Presenter View** before starting Scenario 1.
The audience sees the clean scenario slide (15…20); you see the
commands in the notes pane on the second screen. The blocks below
are the longer-form version of the same commands.

### Block A — Set the stage (2 min, on Slide 14)

Switch from slide deck to the dashboard. Briefly walk the layout:

> "Top header — what the components are. Left rail — list of the firm
> customers, all retrieved from Redis. Middle — the live portfolio.
> Right column — latency comparison, the RDI pipeline metrics, and
> what we have running underneath."

### Block B — Scenario 1: Snapshot already happened (3 min, on Slide 15)

**What we do** — pick three different customers on the dashboard and
let Redis serve every portfolio read.

**What you should see** — every portfolio renders in single-digit ms;
the *Refresh* KPI on the right card confirms it. Postgres is never
queried at runtime.

```bash
# pre-check, before opening the dashboard
docker exec sectrade-redis-enterprise redis-cli -p 12000 DBSIZE
# expected: >= 40   (snapshot already completed)
```

Then in the browser:

1. Open <http://localhost:5050>.
2. Pick **HS0010001 — Rajesh Kumar Sharma** (retail).
   Highlight *Refresh = single-digit ms* on the right.
3. Pick **HS0010002 — Priya Iyer** (HNI).
4. Pick **HS0010003 — Vikram Mehta** (UHNI).

> "Notice the segment tags — retail, HNI, UHNI. Each customer has a
> completely different portfolio mix. None of these reads ever touched
> Postgres. RDI prefetched everything before any user logged in."

**Fallback if the dashboard is empty:**

```bash
docker logs --tail 30 sectrade-rdi-processor   # look for JSON.SET
./scripts/verify-redis.sh
```

### Block C — Scenario 2: Live trade (4 min, on Slide 16)

**What we do** — insert a single BUY trade directly into Postgres
(no app code, no cache logic) and watch RDI propagate it.

**What you should see** — within ~1 second:

- The dashboard's **Recent trade stream** panel gets a new BUY row
  with `order_id = DEMO-LIVE-001`.
- The **RDI pipeline streams** card shows the
  `sectrade.portfolio.trade` event count went up by 1.
- *(Optional)* if you also run the holding UPDATE below, the
  RELIANCE quantity + invested value jump on the dashboard.

In terminal tab 1 (psql session), first show baseline:

```sql
SELECT trade_id, security_id, side, quantity, price, executed_at
FROM portfolio.trade
WHERE customer_id = 10001
ORDER BY executed_at DESC
LIMIT 3;
```

> "These are Mr Sharma's last 3 trades in Postgres."

Then place the new trade (moment of truth):

```sql
INSERT INTO portfolio.trade
  (trade_id, customer_id, security_id, side, quantity, price,
   trade_value, brokerage, order_id, exchange, executed_at)
VALUES (nextval('portfolio.trade_id_seq'), 10001, 1001, 'BUY',
        10, 2945.50, 29455.00, 14.73, 'DEMO-LIVE-001', 'NSE', now());
```

Switch to the dashboard tab (HS0010001 already selected).

Optional — also touches the holding row so the KPIs react:

```sql
UPDATE portfolio.holding
   SET quantity = quantity + 10,
       invested_value = (quantity + 10) * avg_buy_price,
       updated_at = now()
 WHERE customer_id = 10001 AND security_id = 1001;
```

> "One INSERT in Postgres. No `cache.invalidate()`, no message queue,
> no app code change. RDI's CDC saw the WAL entry, transformed it via
> `trade.yaml`, and wrote into Redis as a stream entry plus a JSON
> doc — in about a second."

**Fallback if nothing appears within 5s:**

```bash
docker logs --tail 20 sectrade-rdi-processor
docker logs --tail 20 sectrade-debezium
```

### Block D — Scenario 3: Live load (3 min, on Slide 17)

**What we do** — start the market-data simulator. It writes ~2
trades/sec and ~8 price ticks/sec to Postgres (broker-peak rate for a
10-customer slice). RDI keeps Redis in lockstep.

**What you should see** — on the dashboard:

- *Day %* column flickers across rows as LTPs update.
- **RDI pipeline streams** card increments visibly (price stream
  fastest, trade stream second).
- **Recent trade stream** gets multiple entries per minute (different
  customers, BUY/SELL mixed).
- Dashboard refresh latency stays sub-ms throughout.

Run the simulator in terminal tab 2:

```bash
./scripts/run-simulation.sh
# tunables (env):  TRADES_PER_SEC=5  PRICES_PER_SEC=10  DURATION=300
```

Let it run for ~60 seconds before commenting, then `Ctrl-C` in
terminal 2 (or leave it running — it's lightweight).

> "This is peak-hour for a mid-tier Indian broker, scaled down to 10
> customers. The RDI processor sustains ~10k records/sec/core in
> production — we are nowhere near saturation here."

Optional, only if asked:

```bash
docker exec sectrade-redis-rdi redis-cli -p 12001 XLEN \
  sectrade.portfolio.trade
```

### Block E — Scenario 4: Latency (3 min, on Slide 18)

**What we do** — run the same portfolio-summary query against
Postgres and against Redis, three times back-to-back, on the
dashboard's Latency panel.

**What you should see** — Postgres ~tens of ms, Redis sub-ms, a
4–8× speedup that holds across runs.

```
PostgreSQL :  4.8 ms
Redis Ent. :  0.9 ms
Redis is 5.3x faster for this query
```

On the dashboard, scroll to the **Latency** panel and click
**Run again** three times so the numbers stabilise.

> "These are end-to-end from the app's perspective — connection setup,
> query, deserialise. Redis is consistently 4–8x faster. On your
> production hardware the absolute numbers will be smaller, but the
> ratio is what stays — and ratios compound under load. Multiply this
> ratio by your peak QPS to see how many Oracle cores you no longer
> need to license."

**Do NOT run `./scripts/benchmark.sh` live** unless you have a deeply
technical audience and 10 minutes to explain it. The script uses
persistent Postgres connections (no setup cost), which on a laptop
with hot buffers can make Postgres look *faster* on a tiny dataset.
That's an honest data point — just not the right one to lead with.

If asked about it:

> "On a laptop with the entire dataset in Postgres shared buffers and
> a warm connection pool, Postgres can win small synthetic benchmarks.
> The numbers that matter come from production: Oracle CPU when 5,000
> users hit the portfolio screen at 9:15. Postgres queues, Redis
> scales. We will measure exactly that in the PoC."

### Block F — Scenario 5: Redis Insight (5 min, on Slide 19)

**What we do** — drive the demo from Redis Insight, the same tool
the customer's ops team will use in production. First show the data
view, then the Redis Data Integration tab with its pipeline editor
and live analytics.

**What you should see** —

- Target DB has `customer:*`, `holding:*`, `price:*`, `trades:*` keys
  with the right shapes (JSON / Stream / Hash).
- RDI state DB has 5 CDC streams under `sectrade.portfolio.*`.
- RDI tab pulls the actual `config.yaml` + `jobs/*.yaml` from the
  pipeline, shows green on **Test Connection**, and renders live
  event counts in Analytics.

#### Part 1 — Data view (~2 min)

Open <http://localhost:5540> and use the **Portfolio cache (target)**
connection.

| Filter | What to show |
|---|---|
| `holding:10001:*` → `holding:10001:1001` | JSON tree of one holding |
| `trades:10001` | STREAM view of one customer's trades |
| `price:*` → `price:1001` | HASH view of one security's tick |

Switch to the **RDI state DB** connection.

| Filter | What to show |
|---|---|
| `sectrade.portfolio.*` | 5 streams, one per source table |
| (click one) → expand an entry | Debezium CDC envelope (before/after/op/source/ts_ms) |

#### Part 2 — RDI tab (~3 min)

Click the **Redis Data Integration** icon in the left rail of Insight.
If you haven't added it yet:

1. **Add RDI endpoint**:

   | Field | Value |
   |---|---|
   | RDI Alias | `sectrade-rdi-demo` |
   | URL | `https://rdi-api` |
   | Username | `default` |
   | Password | `rdi_demo_pass` |

2. Click **Add Endpoint** → click into the new instance.

Walk three views:

- **Pipeline Management** — Insight has pulled `config.yaml` and the
  five `jobs/*.yaml` files straight from the demo. Click a job
  (`holding.yaml`) and scroll the YAML editor.
  > "This is what your platform team commits to git. Insight reads it
  > from RDI, writes it back via Deploy. No SSH, no manual file copies."

- **Analytics** — shows live throughput, the 5 CDC streams, processed
  counts, and snapshot status. Refresh interval = 5 s.
  > "Live event counts come straight from the RDI state DB. This is
  > the same panel your ops team would see in production."

- Click **Test Connection** in the editor footer → both target and
  source come back green.
  > "Insight is asking the RDI server to validate the config against
  > the real DBs before deploy. Zero risk of a bad config getting
  > pushed."

**What is faked vs real on the demo laptop**:

- *Real*: pipeline YAML, CDC streams, event counts, snapshot status,
  target & source test connections.
- *Faked*: Deploy/Start/Stop/Reset buttons. They return success but
  the pipeline is already running via docker-compose, so there's
  nothing to actually re-deploy. Mention this only if asked.

> "Same tool, same UI, in dev / staging / prod. Free with Redis
> Enterprise. Your engineers already know it. The pipeline is
> observable from the moment Debezium captures an event until it
> lands in the target — lag, throughput, DLQ, all here, all
> queryable."

### Block G — Scenario 6: Schema change (4 min, on Slide 20)

**What we do** — add a new column to the source table, then update
some rows so the WAL records it, then confirm Redis already has the
new field.

**What you should see** — `JSON.GET holding:10001:1001` returns the
familiar JSON plus a new `"strategy_tag":"LONG_TERM"` field. No
pipeline restart was needed, no application code change.

In terminal tab 1 (psql):

```sql
-- "We need to start tagging holdings as LONG_TERM / SHORT_TERM
-- for STT compliance"
ALTER TABLE portfolio.holding ADD COLUMN strategy_tag VARCHAR(40);

UPDATE portfolio.holding
   SET strategy_tag = 'LONG_TERM'
 WHERE customer_id = 10001;
```

> Postgres 16+ note: a bare `ALTER TABLE` doesn't enter the WAL by
> itself. The `UPDATE` is what makes CDC notice the new column.

Then in terminal tab 3:

```bash
docker exec sectrade-redis-enterprise redis-cli -p 12000 \
  JSON.GET holding:10001:1001 $
```

Expected: the JSON contains `"strategy_tag":"LONG_TERM"`.

> "No pipeline restart. No deploy. No code change. New columns auto-
> propagate. The opposite — removing or masking a column for
> compliance — is a 3-line YAML transform. We'll do that in the PoC
> against real PII columns like PAN and Aadhaar."

### Block H — Hand back to slides for close (3 min, on Slide 21)

Switch back to the deck for the **Q&A / Recap** slide (slide 21).
Three numbers to anchor the closing conversation:

- **P95 portfolio read latency** — CTO / VP Engineering
- **% Oracle CPU reduction** — CFO / IT Finance
- **Engineering hours / week reclaimed** — Engineering Manager

Ask for: a PoC technical lead name + email; a read-only Oracle clone
(or willingness to use Postgres for v1); a 2-week design-session
window. Commit to: SOC 2 + architecture docs by EOD, a TCO worksheet
template within 48h, design session scheduled within 5 working days.

Slides 11–13 (TCO, Security, Next Steps) can also be re-opened as
needed during Q&A.

---

## Live Q&A — be ready for

### Architecture

- *"What's the latency budget end-to-end source → Redis?"* — typically
  500 ms – 1 s under normal load; depends on source DB redo flush rate.

- *"Can RDI write to multiple target DBs?"* — yes, define multiple
  targets in `config.yaml`, reference them by name in `output:` blocks.

- *"What if the source DB has 600 tables?"* — only configure the ones
  you need in `tables:`. RDI is happy with selective capture.

### Operations

- *"How do you upgrade RDI without downtime?"* — RDI uses
  active/standby. Roll one node at a time.

- *"How does RDI survive a node restart?"* — RDI state lives on Redis
  Enterprise. Restart the RDI VM and it resumes from the last
  checkpoint in Redis without re-snapshotting Postgres.

- *"Throughput limits?"* — ~10k records/sec/core. Scale processor
  cores horizontally.

### Security

- *"Where are source DB credentials stored?"* — RDI secrets (file
  system or K8s secret). Never in plaintext config.

- *"Can RDI run air-gapped?"* — Yes. Offline installer. No outbound
  calls to Redis cloud.

- *"PII handling?"* — Use transformations to hash/mask sensitive columns
  before they hit Redis.

### Cost

- *"Pricing model?"* — Redis Enterprise + RDI is a single line item per
  shard for prod. Non-prod typically bundled. Quote follow-up.

- *"Oracle license offset?"* — Each Oracle EE read replica core you
  retire typically pays for several Redis Enterprise shards. Real
  ROI worksheet during PoC.

---

## Troubleshooting (during demo)

### Dashboard shows no customers

1. Wait 10 seconds, refresh.
2. `docker exec sectrade-redis-enterprise redis-cli -p 12000 DBSIZE`
   should be ≥ 40.
3. If 0: `docker logs sectrade-rdi-processor` — look for connection
   errors. Usually means Debezium hasn't snapshotted yet.

### Live trade doesn't appear within 5s

1. Check Debezium: `docker logs sectrade-debezium | tail -20` — should
   show stream activity.
2. Check processor: `docker logs sectrade-rdi-processor | tail -10` —
   should show recent JSON.SET / XADD.
3. Last-resort fallback: speak through what *should* be happening, then
   refresh dashboard manually.

### Postgres ALTER TABLE doesn't propagate

In Postgres 16 the new column lands in the WAL only when a row is
modified. Make sure to follow the ALTER with an UPDATE that touches the
column.

### Redis Enterprise UI never finishes loading

The first-time browser cert warning can leave the UI in a half-loaded
state. Open an incognito window and navigate fresh.

### Benchmark script reports `psycopg2.OperationalError`

The simulator may have lots of open transactions. `Ctrl-C` the simulator
and re-run benchmark.

### Insight RDI tab: `ECONNREFUSED ...:443`

The mock RDI API container isn't up. Recover with:

```bash
docker compose up -d rdi-api
docker logs --tail 20 sectrade-rdi-api
# expected last lines: "Running on https://0.0.0.0:443"
```

Then re-fill the **Add RDI endpoint** dialog with URL `https://rdi-api`
(NOT `https://redis-rdi` — that's the state DB, not the API).

### Insight RDI tab: "Failed to authenticate"

Username must be `default`, password `rdi_demo_pass`. If wrong, the
mock returns 401 just like the real API. There's no need to recreate
the endpoint — just click the endpoint, click *Edit*, fix credentials.

---

## Post-demo cleanup

```bash
./scripts/teardown.sh
```

If you want to keep the data and only stop containers:

```bash
docker compose stop
```
