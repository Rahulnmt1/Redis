# RDI for Securities & Trading Firm — Talk Track

This document is the standalone, presenter-facing script. Keep it open on a
second screen during the meeting. It mirrors the slide deck and adds
extra context, anticipated objections, and recovery lines if a demo step
hiccups.

**Total time budget**: ~45 min (10 min slides + 25 min demo + 10 min Q&A).

**Slide deck — 21 slides**

| Block | Slides | What it is |
|---|---|---|
| Frame the problem  | 1–3   | Cover · Portfolio screen · Why cache-aside fails |
| What & how         | 4–8   | RDI in one slide · Deployment · Lifecycle · industry mapping · 10-line YAML |
| Public reference   | 9     | Redis demo center video |
| Demo overview      | 10    | The 6 scenarios at a glance |
| Business close     | 11–13 | TCO · Security · Next Steps |
| Transition         | 14    | "Let's run the demo" |
| **Live demo**      | **15–20** | One slide per scenario; speaker notes hold exact CLI |
| Q&A wrap           | 21    | Three PoC KPIs to anchor the close |

Open the deck in **Presenter View** before slide 15. The audience sees
the clean scenario slide; you see the commands and talking points in
the notes pane.

---

## Opening (2 min)

> "Thanks for the time today. Last time we spoke, the recurring theme was
> portfolio data — how to serve it fast, fresh, and at the scale your
> customers expect during market hours. So instead of a generic Redis
> pitch, I built a demo on this laptop that mirrors your environment:
> a relational system of record on one side, Redis Enterprise on the
> other, and Redis Data Integration keeping them in sync in real time.
>
> The agenda is short: ~10 minutes of slides to frame the problem and
> the architecture, ~25 minutes of live demo, and then I'd love your
> hard questions in the last 10 minutes."

**Goal of opening**: Make it clear this is a portfolio-specific demo, not
a generic Redis sales pitch.

---

## Slides 1–3 — The problem (4 min)

**Slide 2 — "The Portfolio screen"**
Talk about the four reads every customer does on login. Anchor it to
their reality: NSE/BSE open at 9:15, retail traffic spikes, Oracle CPU
goes up. Ask: "Does that match what you see during the open?"

**Slide 3 — "Why cache-aside fails"**
This is the slide that wins the engineering team. Walk down the table.
Pause on "TTL strategy" — every cache-aside team has a story here.

**Likely objections at this point**
- *"We already cache on the app side."* → "Right. That's cache-aside, and
  it works — until it doesn't. Stay with me for two more slides on what
  flips with CDC."
- *"We have GoldenGate."* → "Great, that's exactly the kind of CDC source
  RDI integrates with. GoldenGate replicates Oracle-to-Oracle. RDI is
  what runs *after* that, so the Redis cache becomes another consumer.
  No new Oracle config."

---

## Slide 4 — What is RDI (2 min)

Three words: **Capture, Transform, Deliver**. Anchor each:
- *Capture*: "It tails the redo log. Your application code does not
  change one line."
- *Transform*: "YAML jobs — they read like a recipe. JSON, Hash,
  Stream, Sorted Set — whatever your read path wants."
- *Deliver*: "At-least-once, ordered per key, back-pressure built in."

> "If you take one thing from this slide: this is *configuration*, not
> code."

---

## Slide 5 — Deployment (2 min)

Briefly walk through 3 planes. Emphasise:

> "Two VMs or two pods. State lives in Redis Enterprise, not on the RDI
> VMs themselves. That means the operational runbook for RDI is the
> same one you already run against Redis Enterprise. Nothing new for
> your backup or platform team."

---

## Slide 6 — Lifecycle (1 min)

The word to land: **prefetch**. The day-1 snapshot.

> "Cache-aside makes you pay a cold-start tax forever — every key has a
> first miss. RDI eliminates that. Your first user request after
> go-live is already hot."

---

## Slide 7 — the firm mapping (2 min)

Walk through the table. Pause on:
- `holding` as JSON — "this is the object the mobile app needs whole."
- `trade` as Stream + JSON — "fan-out from one CDC event. Show in demo."
- `price` as Hash — "small fields, very frequent updates."

> "I built this mapping for you, not from a textbook. We will see it
> populate live in the next 25 minutes."

---

## Slide 8 — 10-line YAML (1 min)

Read it out. Then:

> "That is the entire job to keep one Redis JSON document in sync with
> one Oracle table, forever. Compare it to the cache-aside helper your
> Java team is maintaining today. We will see this exact file live in
> Redis Insight in a minute."

---

## Slide 9 — Demo Center walkthrough (1 min)

This is the link to the public Redis content. Mention it briefly:

> "Redis has a 4-minute interactive walkthrough on
> redis.io/demo-center that shows exactly the editor I'm about to open.
> I'm not going to play that video because we have a more interesting
> dataset to look at — yours."

---

## Slide 10 — Six demo scenarios (30 sec)

Show the slide as an at-a-glance preview of the next ~25 minutes.

> "Six scenarios, all under five minutes each. Stop me at any point."

---

## Slides 11–13 — Business close (delivered BEFORE the demo, ~3 min)

Pre-cover the business slides so the audience holds the value
proposition in their head while watching the demo. You'll briefly
re-anchor on these at the end.

**Slide 11 — TCO** · "Three numbers will decide this: P95 latency
reduction, Oracle CPU reduction, and engineering effort reclaimed. The
PoC measures the first two on your actual data."

**Slide 12 — Security** · "TLS, mTLS, air-gapped install, RBAC on the
source DB user. Docs after this meeting."

**Slide 13 — Next steps** · "We're asking for a yes/no on a 4-week
PoC. Redis SA at no cost; you walk out with hard numbers."

---

## Slide 14 — Let's run the demo (30 sec)

Transition slide. Switch to Presenter View (slides 15–20 hold the
speaker-notes script).

---

## DEMO BLOCK (25 min total, slides 15–20)

> Switch to the dashboard tab in your browser. Have ready, in separate
> tabs/windows:
> - `http://localhost:5050` (portfolio dashboard)
> - `http://localhost:5540` (Redis Insight)
> - A terminal in the project folder
> - A psql terminal: `docker exec -it sectrade-postgres psql -U postgres sectrade`
>
> Every command below is also in the speaker notes of the matching
> scenario slide so you can drive the demo from PowerPoint Presenter
> View alone if you prefer.

### Slide 15 · Scenario 1 — Initial snapshot (3 min)

**On the dashboard**, pick `Rajesh Kumar Sharma (HS0010001)`.

> "Everything you are seeing on this screen is being read from Redis
> Enterprise. Look at the bottom-left of the header — it says 'Cache: Redis
> Enterprise'. The dashboard never queries Postgres at runtime.
>
> The data got here through the RDI pipeline. When I started the stack
> two minutes ago, RDI took a full snapshot of the 5 source tables and
> wrote 60+ keys into Redis. *That* is what I mean by prefetching — when
> the first customer logged in, the cache was already hot."

**Sanity check the audience**: ask them to switch customers in the left
panel — show that each customer's holdings are different and the KPIs
recompute in single-digit milliseconds (the "Refresh" KPI shows the
Redis-fetch time).

### Slide 16 · Scenario 2 — Live trade (4 min)

Open a terminal:

```bash
docker exec -it sectrade-postgres psql -U postgres sectrade
```

```sql
INSERT INTO portfolio.trade
  (trade_id, customer_id, security_id, side, quantity, price,
   trade_value, brokerage, order_id, exchange, executed_at)
VALUES (nextval('portfolio.trade_id_seq'), 10001, 1001, 'BUY',
        10, 2945.50, 29455.00, 14.73, 'DEMO-LIVE-001', 'NSE', now());
```

> "I just inserted a buy order for Mr Sharma — 10 shares of Reliance at
> the current market price. This is what your OMS does after every
> execution. Watch the dashboard."

Within ~1 second:
- The `trades:10001` stream panel on the right shows a new BUY entry.
- The "Holdings" table updates (Reliance quantity goes up).
- The KPIs at the top recompute.

> "One INSERT in Postgres. Three updates in Redis: the per-customer
> trade stream, the holding JSON, and downstream KPIs. *Zero* lines of
> application code did this. The YAML files I showed on slide 7 are the
> only thing that knew about this fan-out."

**If somebody asks**: how does it know to update the holding too?
Answer: in our demo simulator we update both `trade` and `holding` in
the same transaction — exactly like a real OMS would. RDI captures both
changes from the WAL and applies them to Redis independently.

### Slide 17 · Scenario 3 — Live load (peak-hour simulation) (3 min)

In a fresh terminal:

```bash
./scripts/run-simulation.sh
```

> "I am now starting the market-data simulator — it inserts ~2 trades
> per second and updates ~8 price ticks per second on Postgres. This is
> a tiny fraction of NSE peak rate, but enough for you to see the cache
> stay perfectly in sync. The RDI processor sustains ~10k records/sec
> per core in production — we are nowhere near saturation here."

Switch back to dashboard:
- Watch the `Day %` column flicker as LTPs update.
- The P&L recalculates every 2 seconds (the dashboard polls).
- The "RDI pipeline streams" card on the right shows event counts
  ticking up per table.

> "Notice the bottom-right card — those are the CDC streams inside the
> RDI state database. Every change you saw was an event there. If your
> ops team wants raw counters, that's where they live, and Prometheus
> exports them too."

### Slide 18 · Scenario 4 — Postgres vs Redis (3 min)

Stop the simulator (`Ctrl-C`) so the latency numbers stabilise.

On the dashboard, click "Run again" in the **Latency** panel several times:

> "Same query, same customer's portfolio summary. Postgres ~4-6 ms,
> Redis under 1 ms. That is just the read time, end-to-end, from this
> dashboard app. On your production Oracle the *absolute* numbers will
> be smaller, but the *ratio* holds — Redis stays 5-10x faster on the
> same query."

Pause. Then put it in business context:

> "Now multiply this by what happens at 9:15 AM IST when NSE opens.
> Five thousand customers refresh the portfolio screen in the first
> minute. That's 5,000 of these reads per second hitting Oracle today.
> With RDI, the same 5,000 reads hit Redis Enterprise, which costs you
> nothing extra in compute because it scales out by shard. *That* is
> where Oracle license dollars get saved."

If somebody from engineering pushes for raw numbers, mention the
benchmark exists (`./scripts/benchmark.sh`) but skip running it live
— it shows persistent-pool latency, which on a laptop with tiny data
makes Postgres look artificially great. It's a discussion for the
PoC where we will measure on actual hardware.

### Slide 19 · Scenario 5 — Redis Insight + RDI tab (5 min)

Open Redis Insight at `http://localhost:5540`. Both Redis connections
should already exist from setup (`Portfolio cache (target)` and
`RDI state DB`).

**Part 1 — Data view (~2 min)**

In the **Portfolio cache (target)** connection, filter:

- `holding:10001:*` → click `holding:10001:1001` — JSON tree
- `trades:10001` — Stream view
- `price:*` → click `price:1001` — Hash view

Switch to **RDI state DB**, filter `sectrade.portfolio.*`:

- 5 streams, one per source table.
- Click one, expand an entry — show the Debezium CDC envelope JSON
  (`before`, `after`, `op`, `source`, `ts_ms`).

**Part 2 — RDI tab (~3 min)**

Click the **Redis Data Integration** icon in the left rail. If not
yet added: URL `https://rdi-api`, user `default`, pass
`rdi_demo_pass`.

Walk three views:

1. **Pipeline Management** — Insight loaded `config.yaml` and the 5
   `jobs/*.yaml` files from the actual demo. Click `holding.yaml`,
   scroll the YAML editor.
   > "This is what your platform team commits to git."
2. **Test Connection** — both target and source come back green.
   > "Insight is asking the RDI server to validate the config against
   > real DBs before deploy. Zero risk of a bad config getting pushed."
3. **Analytics** — live throughput, 5 CDC streams, processed counts,
   snapshot status. Refresh interval 5s.

> "Same tool, same UI, in dev / staging / prod. Free with Redis
> Enterprise. Your engineers already know it. The pipeline is
> observable from the moment Debezium captures an event until it lands
> in the target — lag, throughput, DLQ — all here, all queryable."

**Disclosure if a sharp engineer asks**: the Deploy / Start / Stop /
Reset buttons are no-ops in *this* demo because the pipeline is
already running via docker-compose. In production they trigger the
RDI control plane API to roll the pipeline; same UX.

### Slide 20 · Scenario 6 — Schema change (4 min)

In the psql terminal:

```sql
ALTER TABLE portfolio.holding ADD COLUMN strategy_tag VARCHAR(40);
UPDATE portfolio.holding
   SET strategy_tag = 'LONG_TERM'
 WHERE customer_id = 10001;
```

> "I added a new column and tagged Mr Sharma's holdings. In a
> cache-aside world I would need a code change and a redeploy. With
> RDI, the column flowed through. Let me show you."

Switch to Redis Insight, refresh `holding:10001:1001`:

```bash
docker exec sectrade-redis-enterprise redis-cli -p 12000 \
  JSON.GET holding:10001:1001 $
```

You should see `"strategy_tag":"LONG_TERM"` in the JSON.

> "No pipeline restart, no application change. The cache picked it up
> the moment the UPDATE event flowed through. If you wanted to *exclude*
> this column from the cache or *transform it* — say, hash a PII field —
> you'd add 3 lines to `holdings.yaml`. We'll do that in the PoC."

---

## Slide 21 — Q&A / Recap close (3 min)

Switch back to the deck. Slide 21 (Q&A wrap) shows three KPIs — one
per buyer constituency in the firm. Anchor the closing exchange on
them:

| KPI | Maps to |
|---|---|
| **P95** portfolio read latency       | CTO / VP Engineering |
| **% ↓** Oracle CPU                   | CFO / IT Finance |
| **h/wk** engineering time reclaimed  | Eng Manager / Tech Lead |

> "Three numbers we'll hand you at the end of the PoC. The first two
> we will measure on your UAT load; the third you have already seen a
> sense of with the 10-line YAML."

**Ask for** before leaving the room:

1. A name + email for the PoC technical lead on their side.
2. A read-only Oracle clone (or willingness to use Postgres for v1).
3. A target 2-week window for the design session.

**Commit to** on the call:

- SOC 2 + architecture docs by EOD.
- TCO worksheet template within 48h.
- Design session scheduled within 5 working days.

Re-open slides 11–13 (TCO / Security / Next Steps) on demand during
Q&A if anyone wants to dig into a specific number.

---

## Recovery lines (use if demo hiccups)

| Hiccup | Say this |
|---|---|
| Dashboard slow to first render | "Redis Enterprise is finishing its bootstrap — give it 30 seconds. In production this is a one-time install step." |
| A scenario doesn't fire | "Let me drop into the RDI state DB to show you why. ... [show the stream length is non-zero]. The pipeline is working, my dashboard just needs to refresh. This is a polling demo UI, real apps subscribe." |
| LTP doesn't tick | "Did the simulator stop? Let me restart it." (`./scripts/run-simulation.sh`) |
| Postgres slow to insert | "That's the laptop, not the design. In production Postgres / Oracle is a fraction of this latency." |
| Audience pulls on Kafka | "Right — RDI does ship the Debezium engine inside the collector. The difference is Redis manages the whole engine, the operator, the deployment, the HA, and you only edit YAML. No Kafka cluster to operate." |

---

## Anticipated objections & responses

**"We can write CDC ourselves with Debezium + Kafka + a custom sink."**
> Yes you can. The question is whether you want your team to *operate*
> that stack for the next five years — that means Zookeeper or KRaft,
> schema registry, sink connectors, dead-letter queue plumbing,
> upgrades, security patching. RDI is that whole stack productised,
> with one vendor on the support phone.

**"We want to start with Redis Cloud, not on-prem."**
> Great — RDI public preview is already in Redis Cloud. You can run the
> same pipeline configuration there. Migration path between
> Cloud / Software is your choice later.

**"Our Oracle has 600 tables."**
> Don't try to mirror all 600. Start with the hot 8–10 that drive the
> portfolio screen. Add the next 5 in a follow-up release. That is the
> exact pattern every successful customer has followed.

**"What about deletes / GDPR-style erasure?"**
> RDI propagates DELETEs from the source. You configure the
> transformation to either delete the Redis key or rewrite it with a
> tombstone — your call. For Indian DPDP / RBI data residency we run
> entirely inside your VPC, no outbound traffic.

**"Throughput?"**
> ~10k records/sec per processor core for ~1KB records. The bottleneck
> is almost always Oracle redo extraction, not RDI. We will measure on
> your actual hardware in the PoC.

**"Can we use Redis as the source?"**
> Not today via RDI — RDI is one-way from a system of record into
> Redis. If you have a reverse-sync need we have a different pattern
> using Redis Streams + your application; we can discuss separately.
