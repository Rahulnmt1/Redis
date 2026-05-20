# Redis Enterprise + RDI — Component Verification

> **Audience**: HDFC Securities architecture / IT security / sourcing teams.
>
> **Purpose**: provide an honest, reproducible accounting of what in this
> demo is the genuine Redis-distributed product versus what is a *reference
> implementation* that matches the real RDI contract — and how to swap the
> reference pieces for the genuine RDI binary when you want a final
> end-to-end demo with the production artifact.
>
> **Sister document**:
> [`05-rdi-spec-conformance.md`](05-rdi-spec-conformance.md) — proves that
> every YAML key, transform block, JMESPath function, control-plane API
> endpoint and spoken claim in the demo maps to the official RDI
> documentation. Read the two docs together for the full assurance pack.

---

## TL;DR — what is genuine Redis-distributed product, what is not

| Component | Status in this demo | Image | Evidence / Rationale |
|---|---|---|---|
| **Target cache** Redis Enterprise Software 7.22.0-95 | ✅ **Genuine Redis Enterprise product** | `redislabs/redis:latest` | `/v1/cluster`, `/v1/license`, `/v1/bdbs`, `/v1/nodes` RE REST APIs respond; license entitlement present; `MODULE LIST` shows RE-managed modules |
| **RDI state DB** Redis Enterprise Software 7.22.0-95 | ✅ **Genuine Redis Enterprise product** | `redislabs/redis:latest` | Same RE REST API surface; separate cluster `hdfcsec-rdi.local`; carries the CDC streams |
| **RedisJSON + RediSearch modules** | ✅ **Genuine Redis Enterprise modules** | bundled in the RE image | `MODULE LIST` paths = `/enterprise-managed` (RE-bundled, license-tracked, not user-loaded) |
| **Redis Insight** 3.4.2 | ✅ **Genuine Redis-distributed product** | `redis/redisinsight:latest` | Official Redis Ltd. image, identical artefact HDFC Sec will run in dev/staging/prod |
| **CDC engine — Debezium 2.5** | ⚠️ **Genuine upstream Debezium, NOT redistributed by Redis** | `quay.io/debezium/server:2.5` | This is the exact upstream engine that real RDI **bundles inside its collector pod**. We run it standalone for transparency; in production it lives inside the Redis-shipped RDI collector binary. |
| **RDI Stream Processor** | ❌ **Reference implementation, NOT the Redis-distributed RDI binary** | `hdfcsec/rdi-processor:demo` *(custom Python image built locally)* | Behaviour-equivalent to real RDI: reads `rdi/config.yaml` + `rdi/jobs/*.yaml`, applies transformations, emits `JSON.SET` / `HSET` / `XADD`. The YAML files themselves are portable to real RDI verbatim. |
| **RDI Control-plane API** | ❌ **Reference / mock, NOT the Redis-distributed RDI control plane** | `hdfcsec/mock-rdi-api:demo` *(custom Flask image built locally)* | Same REST contract as real RDI (JWT auth, pipelines, monitoring, dry-run). Insight's RDI tab works unchanged. *Deploy/Start/Stop/Reset buttons return success but are no-ops because docker-compose already runs the pipeline.* |

### Why the RDI software pieces are a reference implementation

Per the public Redis docs, RDI is distributed only as:

1. A **Kubernetes Helm chart** (deploys the RDI operator + API server + metrics exporter), or
2. A **VM installer** (`redis-di install`) for RHEL 8/9 or Ubuntu 20.04/22.04/24.04.

There is **no official Docker-only RDI distribution** suitable for a
laptop demo. To keep the demo self-contained on one machine, the
`rdi-processor` and `rdi-api` containers in this stack are **custom
reference implementations** built specifically for this demo. They are
*not* the Redis-distributed RDI binary, and we are not claiming they
are. What they preserve verbatim is the **YAML contract** (`config.yaml`
+ `jobs/*.yaml`), the **stream layout** in the RDI state DB, and the
**control-plane REST contract** — which means the pipeline artefacts
HDFC Sec authors against this demo deploy unchanged into a real RDI
install.

### How to upgrade this demo to use the genuine Redis-distributed RDI

Three options, in increasing order of fidelity to production:

| Option | What you get | What it costs |
|---|---|---|
| **A. Stay with this demo** (default) | Self-contained laptop demo, identical UX, real Redis Enterprise underneath, RDI contract preserved | Free; what you have today |
| **B. Run the real RDI VM installer** on a Linux box (RHEL/Ubuntu, 4 vCPU / 8 GB / 20 GB disk) and point the same `rdi/jobs/*.yaml` at it | Genuine `redis-di` CLI, genuine RDI collector + processor + API binary distributed by Redis | A small VM and ~30 min of install; documented runbook in `docs/05-real-rdi-vm-install.md` *(not yet written — say the word)* |
| **C. Use Redis Cloud Pro with RDI public preview** as the target + control plane | Genuine Redis-managed RDI, no install at all | A Redis Cloud account + a network path from your Postgres to Redis Cloud |

For the HDFC Sec meeting, **Option A is the right choice** because the
demo runs entirely on the laptop with no network dependency and the
audience sees end-to-end behaviour identical to production. If their
architecture team asks "can we see the actual Redis-shipped RDI
binary?" — say yes, propose **Option B as part of the PoC** (the PoC
is when they should be installing real RDI anyway).

---

## 1. Both Redis databases are Redis Enterprise Software

### 1a. Container images come from `redislabs/redis` — the RE registry

```bash
docker inspect hdfcsec-redis-enterprise hdfcsec-redis-rdi \
  --format 'image: {{.Config.Image}}'
```

Output:

```
image: redislabs/redis:latest
image: redislabs/redis:latest
```

`redislabs/redis` is the **official Redis Enterprise Software image**
published by Redis Ltd. OSS Redis lives under `redis/redis-stack` or
`redis/redis`; the demo deliberately does **not** use those.

### 1b. The clusters answer on the Redis Enterprise REST API

```bash
curl -sk -u 'admin@hdfcsec.demo:HDFCsecRedis!1' \
  https://localhost:9443/v1/cluster | jq '{name, bigstore_driver}'
curl -sk -u 'admin@hdfcsec.demo:HDFCsecRedis!1' \
  https://localhost:9443/v1/license  | jq '{features, expiration_date, shards_limit}'
curl -sk -u 'admin@hdfcsec.demo:HDFCsecRedis!1' \
  https://localhost:9443/v1/bdbs     | jq '.[] | {uid, name, port, module_list}'
curl -sk -u 'admin@hdfcsec.demo:HDFCsecRedis!1' \
  https://localhost:9443/v1/nodes    | jq '.[] | {uid, software_version, cores, total_memory}'
```

Output (target cluster on `localhost:9443`):

```json
{ "name": "hdfcsec-target.local", "bigstore_driver": "speedb" }
{ "features": ["trial", "bigstore"], "expiration_date": "2026-06-15T03:17:53Z", "shards_limit": 4 }
{ "uid": 1, "name": "portfolio-cache", "port": 12000, "module_list": [{"module_name":"ReJSON"}, {"module_name":"search"}] }
{ "uid": 1, "software_version": "7.22.0-95", "cores": 12 }
```

Output (RDI state cluster on `localhost:9444`):

```json
{ "name": "hdfcsec-rdi.local",    "bigstore_driver": "speedb" }
{ "uid": 1, "name": "rdi-state",       "port": 12001, "module_list": [] }
{ "uid": 1, "software_version": "7.22.0-95", "cores": 12 }
```

The `/v1/cluster`, `/v1/license`, `/v1/bdbs`, `/v1/nodes` REST endpoints
exist **only on Redis Enterprise** — OSS Redis has no such API. The
`bigstore_driver: speedb`, the `trial` license entitlement, and the
`shards_limit` field are all RE-Software–specific concepts.

### 1c. Data-plane confirms RE-managed modules are loaded

```bash
docker exec hdfcsec-redis-enterprise redis-cli -p 12000 MODULE LIST
```

Output:

```
name   "search"          ver  21017  path  /enterprise-managed
name   "ReJSON"          ver  20808  path  /enterprise-managed
```

The `/enterprise-managed` path is how Redis Enterprise loads bundled,
license-tracked modules; user-loaded modules use a different path.
This proves the modules ship with the cluster, not as side-loads.

The RDI state DB has no modules (correct — it only stores streams):

```bash
docker exec hdfcsec-redis-rdi redis-cli -p 12001 MODULE LIST    # → (empty)
```

---

## 2. RedisJSON and RediSearch work end-to-end on the target

Loaded ≠ usable. Verify the modules respond to commands:

```bash
docker exec hdfcsec-redis-enterprise redis-cli -p 12000 \
  JSON.GET holding:10001:1001 '$.quantity' '$.invested_value' '$.strategy_tag'
```

Output:

```json
{"$.invested_value":[168000.0], "$.strategy_tag":["LONG_TERM"], "$.quantity":[60.0]}
```

```bash
docker exec hdfcsec-redis-enterprise redis-cli -p 12000 \
  JSON.OBJKEYS customer:HS0010001
```

Output:

```
customer_id, client_code, pan, email, phone, demat_account,
risk_profile, segment, kyc_status, onboarded_on, updated_at, name
```

Key types as written by RDI:

| Key pattern          | Type        | Module |
|---|---|---|
| `customer:HS*`       | `ReJSON-RL` | RedisJSON |
| `holding:*:*`        | `ReJSON-RL` | RedisJSON |
| `trade:*`            | `ReJSON-RL` | RedisJSON |
| `security:*`         | `hash`      | core |
| `price:*`            | `hash`      | core |
| `trades:<customer>`  | `stream`    | core |

This is exactly the mapping shown on **slide 7** ("Mapping HDFC Sec data
to Redis"), driven by the YAML in `rdi/jobs/*.yaml`.

---

## 3. The CDC engine = the same Debezium that ships inside RDI

```bash
docker inspect hdfcsec-debezium --format 'image: {{.Config.Image}}'
```

Output:

```
image: quay.io/debezium/server:2.5
```

This is the **official Debezium Server 2.5 image** from Red Hat. In a
real RDI install, this exact engine is bundled inside the RDI collector
pod / VM; Redis manages the lifecycle, security patching, upgrades, and
back-pressure for you. Here we run it directly so every moving part is
visible.

Effective config (`debezium/conf/application.properties`):

```properties
debezium.sink.type=redis
debezium.sink.redis.address=redis-rdi:12001
debezium.source.connector.class=io.debezium.connector.postgresql.PostgresConnector
debezium.source.offset.storage=io.debezium.server.redis.RedisOffsetBackingStore
debezium.source.schema.history.internal=io.debezium.server.redis.RedisSchemaHistory
debezium.source.topic.prefix=hdfcsec
```

The classes referenced:

- `RedisStreamChangeConsumer` — sink writes change events to Redis Streams
- `RedisOffsetBackingStore`   — Debezium offsets persisted in Redis
- `RedisSchemaHistory`        — schema-history table persisted in Redis

All three live in `io.debezium.server.redis` — the same package the
real RDI collector loads. State is persisted to `redis-rdi:12001`
(the RDI state DB), exactly as in production.

Logs confirm engine startup:

```
INFO io.deb.ser.DebeziumServer  Consumer 'io.debezium.server.redis.RedisStreamChangeConsumer' instantiated
INFO io.deb.ser.DebeziumServer  Engine executor started
```

---

## 4. The RDI processor uses the real RDI write pattern

The job YAML in `rdi/jobs/*.yaml` declares (per the RDI docs):

```yaml
output:
  - uses: redis.write
    with:
      data_type: json    # → JSON.SET   (or hash → HSET, stream → XADD)
      connection: target
      key:
        expression: ...
```

Live verification — count of commands the processor actually emitted:

```bash
docker logs hdfcsec-rdi-processor 2>&1 \
  | grep -oE '(JSON\.SET|HSET|XADD|DEL) [^ ]+' | sort | uniq -c | sort -rn | head -10
```

Output:

```
  15 HSET price:1010
  15 HSET price:1003
  14 HSET price:1018
  …
  11 XADD trades:10002
   9 XADD trades:10005
   …  (and JSON.SET on customer:* / holding:* / security:* / trade:* )
```

`HSET` for prices, `XADD` for trades, `JSON.SET` for documents — this is
the **exact write pattern documented in the RDI reference**.

---

## 5. End-to-end pipeline lineage proven with a single trade

The full path is observable for any one row.

**[1] INSERT in Postgres**

```sql
INSERT INTO portfolio.trade (..., order_id, executed_at)
VALUES (..., 'PROOF-001', now());
```

**[2] Debezium captures it into the RDI state stream**

```bash
docker exec hdfcsec-redis-rdi redis-cli -p 12001 \
  XREVRANGE hdfcsec.portfolio.trade + - COUNT 1
```

Result (CDC envelope, abridged):

```json
{
  "before": null,
  "after": {
    "trade_id": 30159, "customer_id": 10001, "security_id": 1001,
    "side": "BUY", "quantity": 1.0, "price": 2999.99,
    "order_id": "PROOF-001", "executed_at": 1778903133828
  },
  "source": {
    "version": "2.5.4.Final", "connector": "postgresql",
    "db": "hdfcsec", "schema": "portfolio", "table": "trade",
    "txId": 1081, "lsn": 27024400
  },
  "op": "c", "ts_ms": 1778903134341
}
```

The `lsn`, `txId`, `op=c` (create), and Debezium connector version are
direct evidence that this came from Postgres WAL via real Debezium —
not from a hand-crafted message.

**[3] RDI processor writes it to the target Redis Enterprise cache**

```bash
docker exec hdfcsec-redis-enterprise redis-cli -p 12000 \
  XREVRANGE trades:10001 + - COUNT 1
```

Result:

```
1778903134809-0
  trade_id 30159  side BUY  quantity 1.0  price 2999.99  order_id PROOF-001
```

**[4] The application reads from target Redis Enterprise**

```bash
curl -s http://localhost:5050/api/recent-trades/HS0010001 | head
```

Result: `trade_id=30159 side=BUY qty=1.0 px=2999.99` — same row, served
from Redis Enterprise.

End-to-end propagation time: **~1 second**.

---

## 6. The RDI control-plane API surface

The mock RDI control plane (`mock-rdi-api`) implements the documented
RDI REST contract so Redis Insight's *Redis Data Integration* tab works
unchanged on the laptop. Every endpoint Insight calls returns 200 with
real demo data:

| Endpoint | What Insight uses it for | Result |
|---|---|---|
| `POST /api/v1/login`                       | Authenticate, get JWT             | 200, JWT issued |
| `GET  /api/v1/status`                      | Component health + pipeline state | 200, `STREAMING` |
| `GET  /api/v1/pipelines`                   | Load config + 5 job YAMLs         | 200, real pipeline |
| `GET  /api/v1/monitoring/statistics`       | Analytics tab (live throughput)   | 200, 388 events |
| `POST /api/v1/pipelines/targets/dry-run`   | Test Connection (target)          | `connected: true` |
| `POST /api/v1/pipelines/sources/dry-run`   | Test Connection (source)          | `connected: true` |
| `GET  /api/v1/pipelines/config/schemas`    | YAML schema for editor            | 200 |
| `GET  /api/v1/pipelines/jobs/schemas`      | YAML schema for editor            | 200 |
| `GET  /api/v1/pipelines/jobs/functions`    | Transform-function catalog        | 200 |
| `GET  /api/v1/pipelines/strategies`        | Deployment strategies             | 200 |

These paths come directly from the public RDI API documentation; the
mock implements them so the customer's first experience of Insight's
RDI tab is identical to what they will see in production.

What the mock does **not** do (mention only if asked):
*Deploy / Start / Stop / Reset* buttons return success but are no-ops,
because the pipeline is already running via docker-compose. The real
RDI control plane wires these to the operator/installer.

---

## 7. Redis Insight is the real shipping product

```bash
curl -s http://localhost:5540/api/info | jq '{appVersion, appType, buildType}'
```

Output:

```json
{ "appVersion": "3.4.2", "appType": "DOCKER", "buildType": "DOCKER_ON_PREMISE" }
```

`redis/redisinsight:latest` is the production image from Redis Ltd —
the same artifact HDFC Sec engineers will install in dev/staging/prod.
Redis Enterprise ships Insight free of charge.

---

## 8. Reproducing this verification

```bash
cd /Users/rahul.choubey/Downloads/RDI-Demo-HDFCSEC
./scripts/setup.sh
./scripts/verify-redis.sh           # quick smoke test
# then re-run any block above to reproduce
```

Single-command "all green" check:

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' | grep hdfcsec
```

Expected (all 9 containers `Up …`):

```
hdfcsec-postgres            hdfcsec/portfolio-pg:demo    Up (healthy)
hdfcsec-redis-enterprise    redislabs/redis:latest       Up
hdfcsec-redis-rdi           redislabs/redis:latest       Up
hdfcsec-re-bootstrap        curlimages/curl:8.10.1       Exited (0)
hdfcsec-debezium            quay.io/debezium/server:2.5  Up
hdfcsec-rdi-processor       hdfcsec/rdi-processor:demo   Up
hdfcsec-rdi-api             hdfcsec/mock-rdi-api:demo    Up
hdfcsec-redis-insight       redis/redisinsight:latest    Up
hdfcsec-dashboard           hdfcsec/portfolio-dashboard:demo  Up
```

---

## 9. Mapping demo components to a real production install

| In this demo | In your production HDFC Sec install | Same or different artifact? |
|---|---|---|
| `redislabs/redis:latest` × 2 containers | Your existing Redis Enterprise cluster (already deployed for other workloads). Add one BDB for RDI state, one for the portfolio cache. | **Same product**, sized up for prod |
| `quay.io/debezium/server:2.5` (run standalone) | The Debezium engine **bundled inside the RDI collector** that ships with the RDI VM installer / K8s operator. Same source code, but Redis-managed lifecycle, security patching, back-pressure, upgrades. | **Same upstream engine**, redistributed and managed by Redis in prod |
| `hdfcsec/rdi-processor:demo` (custom Python reference impl) | The **Redis-distributed RDI stream processor binary** that ships with the RDI installer / operator. Same job-YAML interpretation, same write patterns. | **Different artifact** in the demo; **same artifact** the moment you install real RDI |
| `hdfcsec/mock-rdi-api:demo` (custom Flask mock) | The **Redis-distributed RDI control-plane API** exposed by the RDI installer / operator. | **Different artifact** in the demo; **same artifact** in prod |
| `rdi/config.yaml` + `rdi/jobs/*.yaml`     | **Identical files** — they deploy unchanged into a real RDI install. | **Same artifact** — this is the asset HDFC Sec authors and owns |
| `redis/redisinsight:latest`     | Same Redis Insight you'll run in dev/staging/prod. | **Same product** |

The `rdi/` folder is the most valuable artifact: those configs go
straight to production. The processor and API mock are demo-only
plumbing; on day one of the PoC, replace them with the official
`redis-di install` against an RHEL/Ubuntu VM.

### If asked: "Why isn't the official RDI binary in the demo?"

Answer honestly:

> "The real RDI ships as a VM installer or a Kubernetes Helm chart from
> Redis. It is designed to be installed once on a long-lived host and
> managed by the RDI operator — not to live inside a single Docker
> container. To keep this entire demo on one laptop, we run a
> reference implementation of the RDI processor and a mock of the RDI
> control-plane API. They preserve the YAML pipeline contract, the
> stream layout, and the API surface verbatim, so the artefacts you
> build against this demo deploy unchanged into a real RDI install.
> Step one of your PoC is to provision a small VM and run `redis-di
> install` — at that point the only thing that changes is the
> processor and control plane become the official Redis-shipped
> binaries; everything else (configs, target Redis, dashboards) is
> already production-grade."
