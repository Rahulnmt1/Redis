#!/usr/bin/env python3
"""
Bulk-seed the demo at SCALE.

What this does
--------------
1. TRUNCATE the portfolio.customer + portfolio.holding tables.
2. Bulk-load N customers into Postgres via COPY FROM STDIN.
3. Bulk-load ~H*N holdings (H per customer) into Postgres.
4. Add a btree index on customer.pan (so the latency comparison is FAIR
   - Postgres gets an indexed point lookup, not a sequential scan).
5. Bulk-load the SAME customers + holdings into the target Redis Enterprise
   directly, using pipelined JSON.SET and HSET via redis-py.
6. Create a RediSearch FT index on customer:* JSON so the dashboard
   can FT.SEARCH any of N records in sub-millisecond time.

Why we bypass the RDI snapshot for the BULK load
------------------------------------------------
In production, the same data would be loaded by RDI's initial snapshot
phase (Debezium + processor with `initial_sync_processes: 4`, ~10k
records/sec/core). For 10M records, real RDI takes ~3-5 minutes; that
is a realistic and documented number. On a laptop demo we choose to
pre-seed Redis directly so the demo is interactive immediately. After
the bulk load is done, RDI is responsible for every NEW change (the
live-update scenarios continue to flow Postgres -> Debezium -> RDI
processor -> Redis unchanged).

This is documented in docs/05-rdi-spec-conformance.md and the talk-track.

Usage
-----
    # from the repo root, with the stack running:
    CUSTOMERS=1000000 ./scripts/seed-large-scale.sh
    # or for the full 1 crore stretch:
    CUSTOMERS=10000000 ./scripts/seed-large-scale.sh
"""
from __future__ import annotations

import io
import os
import random
import string
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable

import psycopg2
import redis

# ---- knobs -------------------------------------------------------------
CUSTOMERS         = int(os.getenv("CUSTOMERS",         "3000000"))   # 30 lakh — measured sweet spot @ 4 GiB BDB on 12 GB Docker
# In Redis we deliberately skip bulk holdings: 5M × 3 holdings × ~350B
# is ~12 GiB — would not fit on a single-shard trial-licensed laptop
# BDB. The scale story comes from the customer set (FT.SEARCH count,
# name prefix etc.); the portfolio multi-key fetch is demoed on the
# original 10 hand-crafted customers which carry their full holdings.
HOLDINGS_PER_CUST = int(os.getenv("HOLDINGS_PER_CUST", "3"))         # Postgres only; Redis bulk skips holdings
BULK_HOLDINGS_REDIS = os.getenv("BULK_HOLDINGS_REDIS", "0") == "1"   # opt-in only — needs ≥12 GiB BDB
START_CUSTOMER_ID = int(os.getenv("START_CUSTOMER_ID","100000"))     # leave 10001..99999 for the original 10 demo customers
BATCH_PG          = int(os.getenv("BATCH_PG",          "100000"))    # rows per COPY chunk
BATCH_REDIS       = int(os.getenv("BATCH_REDIS",       "5000"))      # cmds per redis pipeline
REDIS_WORKERS     = int(os.getenv("REDIS_WORKERS",     "8"))         # parallel redis seeders
SKIP_PG           = os.getenv("SKIP_PG",    "0") == "1"
SKIP_REDIS        = os.getenv("SKIP_REDIS", "0") == "1"

PG = dict(
    host     = os.getenv("PG_HOST",     "localhost"),
    port     = int(os.getenv("PG_PORT", "5432")),
    dbname   = os.getenv("PG_DB",       "hdfcsec"),
    user     = os.getenv("PG_USER",     "postgres"),
    password = os.getenv("PG_PASS",     "postgres"),
)

RD = dict(
    host = os.getenv("TARGET_DB_HOST", "localhost"),
    port = int(os.getenv("TARGET_DB_PORT", "12000")),
    password = os.getenv("TARGET_DB_PASSWORD", "") or None,
)

# ---- helpers -----------------------------------------------------------
def banner(s: str) -> None:
    print(f"\n\033[1;36m=== {s} ===\033[0m", flush=True)

def step(s: str) -> None:
    print(f"  \033[2m›\033[0m {s}", flush=True)

def progress(done: int, total: int, t0: float) -> None:
    pct = done * 100 / total if total else 0
    rate = done / max(time.time()-t0, 0.001)
    bar = "█" * int(pct/3) + "·" * (33 - int(pct/3))
    sys.stdout.write(f"\r    {bar} {pct:5.1f}%  {done:>9,}/{total:,}  ({rate:>9,.0f} rec/s)")
    sys.stdout.flush()

# ---- synthetic data --------------------------------------------------
FIRST_NAMES = ["Aarav","Vihaan","Aditya","Krishna","Ishaan","Ananya","Aanya","Saanvi",
               "Diya","Aadhya","Rohan","Kabir","Reyansh","Arjun","Sai","Riya","Mira",
               "Kavya","Ira","Anika","Rahul","Vikram","Priya","Anita","Sunil",
               "Neha","Pooja","Ravi","Amit","Suresh","Manish","Ajay","Sneha",
               # Include Raj-prefix names so a recruiter-style "search 'Raj'" demo
               # produces realistic results, and a few other common prefixes:
               "Rajesh","Rajat","Rajeev","Rajiv","Raja","Rajan","Raju",
               "Anand","Anil","Ashwin","Sanjay","Vinod","Deepak","Naveen","Sandeep"]
LAST_NAMES = ["Sharma","Verma","Patel","Iyer","Reddy","Nair","Mehta","Shah","Kapoor",
              "Singh","Khan","Gupta","Das","Chatterjee","Kumar","Banerjee","Rao","Joshi"]
SEGMENTS = ["RETAIL"]*70 + ["HNI"]*20 + ["UHNI"]*5 + ["NRI"]*5
RISK     = ["CONSERVATIVE","MODERATE","MODERATE","MODERATE","AGGRESSIVE"]
KYC      = ["VERIFIED"]*97 + ["PENDING"]*2 + ["REJECTED"]*1

def make_pan(i: int) -> str:
    # PAN format: 5 letters + 4 digits + 1 letter; make it deterministic from i
    letters = string.ascii_uppercase
    a = letters[(i // 100000) % 26]
    b = letters[(i // 10000)  % 26]
    c = letters[(i // 1000)   % 26]
    d = letters[(i // 100)    % 26]
    e = letters[(i // 10)     % 26]
    n = (i % 10000)
    z = letters[(i * 7) % 26]
    return f"{a}{b}{c}{d}{e}{n:04d}{z}"

def make_client_code(i: int) -> str:
    return f"HS{i:09d}"

def make_demat(i: int) -> str:
    return f"IN3{(i*13)%10000000000:010d}"

def make_email(first: str, last: str, i: int) -> str:
    return f"{first.lower()}.{last.lower()}{i%1000}@example.in"

def make_phone(i: int) -> str:
    return f"+91-9{(i*7)%1000000000:09d}"

def _holdings_for(customer_id: int, security_ids: list[int]):
    """Yield H deterministic (security_id, qty, avg_price, invested) tuples
    for a given customer. Same seed on Postgres + Redis sides so both
    stores hold identical numbers — that lets the latency widget show a
    matching invested_value and a meaningful market-value comparison.
    """
    rng = random.Random(customer_id)
    picks = rng.sample(security_ids, k=min(HOLDINGS_PER_CUST, len(security_ids)))
    for sec_id in picks:
        qty = rng.randint(5, 500)
        avg = round(rng.uniform(50, 5000), 2)
        inv = round(qty * avg, 2)
        yield sec_id, qty, avg, inv


def gen_customer(i: int) -> dict:
    rng = random.Random(i)
    first = rng.choice(FIRST_NAMES)
    last  = rng.choice(LAST_NAMES)
    return {
        "customer_id":   i,
        "client_code":   make_client_code(i),
        "pan":           make_pan(i),
        "full_name":     f"{first} {last}",
        "email":         make_email(first, last, i),
        "phone":         make_phone(i),
        "demat_account": make_demat(i),
        "risk_profile":  rng.choice(RISK),
        "segment":       rng.choice(SEGMENTS),
        "kyc_status":    rng.choice(KYC),
    }

# ---- Postgres bulk load ---------------------------------------------
def pg_copy(start_id: int, n: int, security_ids: list[int]) -> None:
    conn = psycopg2.connect(**PG)
    conn.autocommit = False
    cur = conn.cursor()

    banner(f"Postgres bulk load — {n:,} customers, {n*HOLDINGS_PER_CUST:,} holdings")

    # Customers ----------------------------------------------------------
    step("COPY customers...")
    t0 = time.time()
    done = 0
    for chunk_start in range(0, n, BATCH_PG):
        chunk = min(BATCH_PG, n - chunk_start)
        buf = io.StringIO()
        for i in range(chunk_start, chunk_start + chunk):
            cid = start_id + i
            c = gen_customer(cid)
            buf.write("\t".join([
                str(cid),
                c["client_code"],
                c["pan"],
                c["full_name"],
                c["email"],
                c["phone"],
                c["demat_account"],
                c["risk_profile"],
                c["segment"],
                c["kyc_status"],
                "2024-01-01",
                "2024-01-01 00:00:00",
            ]) + "\n")
        buf.seek(0)
        cur.copy_expert(
            "COPY portfolio.customer "
            "(customer_id, client_code, pan, full_name, email, phone, "
            " demat_account, risk_profile, segment, kyc_status, onboarded_on, updated_at) "
            "FROM STDIN WITH (FORMAT text)",
            buf
        )
        done += chunk
        progress(done, n, t0)
    conn.commit()
    print()
    step(f"customers done in {time.time()-t0:.1f}s")

    # Holdings -----------------------------------------------------------
    # Generate deterministically per-customer so the Redis-side seeder
    # (running in parallel workers) produces identical holdings.
    step(f"COPY holdings (~{HOLDINGS_PER_CUST} per customer)...")
    t0 = time.time()
    total_h = n * HOLDINGS_PER_CUST
    done = 0
    next_holding_id = 1_000_000
    for chunk_start in range(0, n, BATCH_PG):
        chunk = min(BATCH_PG, n - chunk_start)
        buf = io.StringIO()
        for i in range(chunk_start, chunk_start + chunk):
            cid = start_id + i
            for sec_id, qty, avg, inv in _holdings_for(cid, security_ids):
                buf.write("\t".join([
                    str(next_holding_id),
                    str(cid),
                    str(sec_id),
                    str(qty),
                    f"{avg:.2f}",
                    f"{inv:.2f}",
                    "2024-06-01",
                    "2024-06-01 00:00:00",
                ]) + "\n")
                next_holding_id += 1
        buf.seek(0)
        cur.copy_expert(
            "COPY portfolio.holding "
            "(holding_id, customer_id, security_id, quantity, avg_buy_price, "
            " invested_value, last_trade_date, updated_at) "
            "FROM STDIN WITH (FORMAT text)",
            buf
        )
        done += chunk * HOLDINGS_PER_CUST
        progress(done, total_h, t0)
    conn.commit()
    print()
    step(f"holdings done in {time.time()-t0:.1f}s")

    # Fair-comparison indexes -------------------------------------------
    step("ensuring fair-comparison indexes (pan, client_code)...")
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_customer_pan
            ON portfolio.customer(pan);
        CREATE INDEX IF NOT EXISTS idx_customer_client_code
            ON portfolio.customer(client_code);
        ANALYZE portfolio.customer;
        ANALYZE portfolio.holding;
    """)
    conn.commit()
    cur.close()
    conn.close()

# ---- Redis bulk load -------------------------------------------------
def redis_worker(args) -> int:
    worker_id, start_id, n_workers, total, security_ids = args
    r = redis.Redis(**RD, decode_responses=False)
    written = 0
    pipe = r.pipeline(transaction=False)
    for i in range(worker_id, total, n_workers):
        cid = start_id + i
        c = gen_customer(cid)
        key = f"customer:{c['client_code']}"
        # mimic the shape produced by customer.yaml after rename_field
        doc = {
            "customer_id":   cid,
            "client_code":   c["client_code"],
            "pan":           c["pan"],
            "name":          c["full_name"],
            "email":         c["email"],
            "phone":         c["phone"],
            "demat_account": c["demat_account"],
            "risk_profile":  c["risk_profile"],
            "segment":       c["segment"],
            "kyc_status":    c["kyc_status"],
        }
        pipe.execute_command("JSON.SET", key, "$",
                             _fast_json(doc))

        # Bulk-mode holdings are off by default to keep the BDB small at
        # 50 lakh customer scale; flip BULK_HOLDINGS_REDIS=1 if you have
        # a bigger BDB (≥12 GiB) and want every bulk customer to carry a
        # tradable position.
        if BULK_HOLDINGS_REDIS:
            for sec_id, qty, avg, inv in _holdings_for(cid, security_ids):
                hdoc = {
                    "customer_id":   cid,
                    "security_id":   sec_id,
                    "quantity":      qty,
                    "avg_buy_price": avg,
                    "invested_value": inv,
                }
                pipe.execute_command("JSON.SET", f"holding:{cid}:{sec_id}", "$",
                                     _fast_json(hdoc))

        written += 1
        if written % BATCH_REDIS == 0:
            pipe.execute()
            pipe = r.pipeline(transaction=False)
    if len(pipe) > 0:
        pipe.execute()
    return written

def _fast_json(d: dict) -> str:
    # tiny inline JSON dumper, avoids json.dumps overhead in the hot loop
    parts = []
    for k, v in d.items():
        if isinstance(v, str):
            v_s = '"' + v.replace('\\', '\\\\').replace('"', '\\"') + '"'
        elif v is None:
            v_s = "null"
        elif isinstance(v, bool):
            v_s = "true" if v else "false"
        else:
            v_s = str(v)
        parts.append(f'"{k}":{v_s}')
    return "{" + ",".join(parts) + "}"

def redis_bulk(start_id: int, n: int, security_ids: list[int]) -> None:
    banner(f"Redis Enterprise bulk load — {n:,} customers + holdings")
    step(f"using {REDIS_WORKERS} parallel pipelined writers...")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=REDIS_WORKERS) as pool:
        futures = [pool.submit(redis_worker, (w, start_id, REDIS_WORKERS, n, security_ids))
                   for w in range(REDIS_WORKERS)]
        total_done = 0
        last_print = 0
        while futures:
            done_futs = [f for f in futures if f.done()]
            running_done = sum(f.result() for f in done_futs)
            total_done = running_done + sum(0 for _ in [])
            # approximate live progress by polling redis DBSIZE
            try:
                rr = redis.Redis(**RD, decode_responses=True)
                size = rr.dbsize()
            except Exception:
                size = 0
            expected = n * (1 + (HOLDINGS_PER_CUST if BULK_HOLDINGS_REDIS else 0))
            progress(min(size, expected), expected, t0)
            if all(f.done() for f in futures):
                break
            time.sleep(0.5)
    print()
    step(f"redis bulk load done in {time.time()-t0:.1f}s")

# ---- RediSearch index ------------------------------------------------
def create_search_index() -> None:
    banner("RediSearch — customer index")
    r = redis.Redis(**RD, decode_responses=True)
    try:
        r.execute_command("FT.DROPINDEX", "cust-idx")
        step("dropped existing cust-idx")
    except redis.ResponseError as e:
        if "Unknown" not in str(e):
            step(f"no existing index to drop ({e})")
    step("FT.CREATE cust-idx ON JSON PREFIX customer: …")
    r.execute_command(
        "FT.CREATE", "cust-idx",
        "ON", "JSON",
        "PREFIX", "1", "customer:",
        "SCHEMA",
        "$.client_code",   "AS", "client_code",   "TAG",  "SORTABLE",
        "$.pan",           "AS", "pan",           "TAG",
        "$.name",          "AS", "name",          "TEXT", "SORTABLE",
        "$.segment",       "AS", "segment",       "TAG",  "SORTABLE",
        "$.risk_profile",  "AS", "risk_profile",  "TAG",
        "$.kyc_status",    "AS", "kyc_status",    "TAG",
        "$.customer_id",   "AS", "customer_id",   "NUMERIC", "SORTABLE",
    )
    # FT.CREATE is async; wait for the indexer to drain
    step("waiting for the indexer to drain (FT.INFO)...")
    t0 = time.time()
    while True:
        info = r.execute_command("FT.INFO", "cust-idx")
        info_d = dict(zip(info[::2], info[1::2]))
        indexing = int(info_d.get("indexing", 0))
        num_docs = int(info_d.get("num_docs", 0))
        sys.stdout.write(f"\r    indexing={indexing}  num_docs={num_docs:,}  elapsed={time.time()-t0:.1f}s   ")
        sys.stdout.flush()
        if indexing == 0:
            break
        time.sleep(0.5)
    print()
    step(f"customer index ready — {num_docs:,} docs indexed in {time.time()-t0:.1f}s")

    # ---- holdings index ---------------------------------------------
    # Without this, fetching a single customer's holdings would have to
    # SCAN-match all `holding:*` keys (O(dbsize), millions of ops to
    # find 3 matches at scale). With a numeric customer_id index,
    # FT.SEARCH '@customer_id:[<id> <id>]' is O(log N).
    # Always create the index — it stays cheap when there are only the
    # 30 original-customer holdings, and is essential whenever bulk
    # holdings are also loaded.
    try:
        r.execute_command("FT.DROPINDEX", "hold-idx")
        step("dropped existing hold-idx")
    except redis.ResponseError as e:
        if "Unknown" not in str(e):
            step(f"no existing hold-idx to drop ({e})")
    step("FT.CREATE hold-idx ON JSON PREFIX holding: …")
    r.execute_command(
        "FT.CREATE", "hold-idx",
        "ON", "JSON",
        "PREFIX", "1", "holding:",
        "SCHEMA",
        "$.customer_id",   "AS", "customer_id",   "NUMERIC", "SORTABLE",
        "$.security_id",   "AS", "security_id",   "NUMERIC",
    )
    t0 = time.time()
    while True:
        info = r.execute_command("FT.INFO", "hold-idx")
        info_d = dict(zip(info[::2], info[1::2]))
        indexing = int(info_d.get("indexing", 0))
        num_docs = int(info_d.get("num_docs", 0))
        sys.stdout.write(f"\r    indexing={indexing}  num_docs={num_docs:,}  elapsed={time.time()-t0:.1f}s   ")
        sys.stdout.flush()
        if indexing == 0:
            break
        time.sleep(0.5)
    print()
    step(f"holdings index ready — {num_docs:,} docs indexed in {time.time()-t0:.1f}s")

# ---- discover existing security_ids -----------------------------------
SEC_RANGE = (1001, 1020)
def discover_securities() -> list[int]:
    conn = psycopg2.connect(**PG)
    cur = conn.cursor()
    cur.execute("SELECT security_id FROM portfolio.security_master ORDER BY security_id")
    rows = [r[0] for r in cur.fetchall()]
    cur.close(); conn.close()
    if not rows:
        sys.exit("FATAL: portfolio.security_master is empty - run the original seed first.")
    return rows


def clear_redis_bulk() -> None:
    """Drop existing FT indexes + flush the target BDB before reseeding.

    Why FLUSHDB rather than delete-by-pattern: at scale (millions of keys
    + active FT indexes), DEL/UNLINK by pattern via SCAN is slow and
    can run the BDB to its memory cap during the delete. FLUSHDB is O(1)
    in wall-clock for the demo: the BDB is dedicated to RDI-fed data;
    nothing in production-relevant state lives outside the keys we own.
    """
    banner("Redis target — clearing previous bulk load")
    rr = redis.Redis(**RD, decode_responses=True)
    for ix in ("cust-idx", "hold-idx"):
        try:
            rr.execute_command("FT.DROPINDEX", ix)
            step(f"dropped {ix}")
        except redis.ResponseError:
            pass
    rr.execute_command("FLUSHDB", "SYNC")
    step(f"FLUSHDB SYNC — dbsize now {rr.dbsize()}")


def push_baseline_to_redis() -> None:
    """Push security_master and market_price rows from Postgres into Redis.

    On a fresh cluster (e.g. after recreating redis-enterprise to resize the
    BDB) the original RDI snapshot is already committed, so the Postgres
    rows that existed *before* the bulk seed will not flow into Redis
    again unless we re-snapshot. We push them ourselves so the dashboard
    can compute market value for bulk-seeded customers' holdings.
    """
    banner("Redis baseline — security_master + market_price")
    conn = psycopg2.connect(**PG)
    cur = conn.cursor()

    rr = redis.Redis(**RD, decode_responses=True)
    pipe = rr.pipeline(transaction=False)

    cur.execute("""
        SELECT security_id, isin, symbol, company_name, exchange, segment,
               sector, lot_size, face_value, is_active
        FROM portfolio.security_master WHERE is_active
        ORDER BY security_id
    """)
    secs = cur.fetchall()
    for sid, isin, symbol, company, exch, seg, sector, lot, fv, active in secs:
        pipe.hset(f"security:{symbol}", mapping={
            "security_id":  str(sid),
            "isin":         isin or "",
            "symbol":       symbol,
            "company_name": company or "",
            "exchange":     exch or "",
            "segment":      seg or "",
            "sector":       sector or "",
            "lot_size":     str(lot or 1),
            "face_value":   str(fv or 0),
            "is_active":    "1" if active else "0",
        })
    step(f"pushed {len(secs)} security:* hashes")

    cur.execute("""
        SELECT security_id, ltp, prev_close, day_open, day_high, day_low, volume
        FROM portfolio.market_price ORDER BY security_id
    """)
    prices = cur.fetchall()
    for sid, ltp, prev, dop, dhi, dlo, vol in prices:
        pipe.hset(f"price:{sid}", mapping={
            "security_id":  str(sid),
            "ltp":          str(ltp),
            "prev_close":   str(prev),
            "day_open":     str(dop),
            "day_high":     str(dhi),
            "day_low":      str(dlo),
            "volume":       str(vol or 0),
        })
    step(f"pushed {len(prices)} price:* hashes")

    # ---- Original 10 demo customers + their holdings ----------------
    # The bulk seeder skips holdings for the 50-lakh dataset, so the
    # portfolio-fetch comparison runs against the original 10 customers
    # (rich, hand-crafted in seed-data.sql). After a fresh cluster
    # rebuild, RDI's slot is already past their initial snapshot so we
    # have to push them ourselves once.
    cur.execute("""
        SELECT customer_id, client_code, pan, full_name, email, phone,
               demat_account, risk_profile, segment, kyc_status
        FROM portfolio.customer
        WHERE customer_id < %s
        ORDER BY customer_id
    """, (START_CUSTOMER_ID,))
    orig = cur.fetchall()
    for cid, code, pan, name, email, phone, dmt, rsk, seg, kyc in orig:
        doc = {
            "customer_id":   cid,  "client_code":  code,  "pan":  pan,
            "name":          name, "email":        email or "",
            "phone":         phone or "", "demat_account": dmt or "",
            "risk_profile":  rsk,  "segment":      seg,  "kyc_status": kyc,
        }
        pipe.execute_command("JSON.SET", f"customer:{code}", "$", _fast_json(doc))
    step(f"pushed {len(orig)} original demo customers")

    cur.execute("""
        SELECT customer_id, security_id, quantity, avg_buy_price, invested_value
        FROM portfolio.holding
        WHERE customer_id < %s
    """, (START_CUSTOMER_ID,))
    orig_h = cur.fetchall()
    for cid, sid, qty, avg, inv in orig_h:
        hdoc = {
            "customer_id":   cid,
            "security_id":   sid,
            "quantity":      float(qty),
            "avg_buy_price": float(avg),
            "invested_value": float(inv),
        }
        pipe.execute_command("JSON.SET", f"holding:{cid}:{sid}", "$", _fast_json(hdoc))
    step(f"pushed {len(orig_h)} original demo holdings")

    pipe.execute()
    cur.close(); conn.close()

# ---- main ------------------------------------------------------------
def main():
    print(f"\n  RDI demo — large-scale seeder")
    print(f"  customers          : {CUSTOMERS:,}")
    print(f"  holdings/customer  : {HOLDINGS_PER_CUST}")
    print(f"  expected holdings  : {CUSTOMERS*HOLDINGS_PER_CUST:,}")
    print(f"  redis workers      : {REDIS_WORKERS}")

    # We always need security ids: the Postgres copy + the Redis worker
    # + the baseline push all reference them.
    security_ids = discover_securities()
    global SEC_RANGE
    SEC_RANGE = (min(security_ids), max(security_ids))

    if not SKIP_PG:
        # confirm we don't have leftover bulk data
        conn = psycopg2.connect(**PG); cur = conn.cursor()
        cur.execute("SELECT count(*) FROM portfolio.customer WHERE customer_id >= %s", (START_CUSTOMER_ID,))
        existing = cur.fetchone()[0]
        if existing:
            banner(f"clearing {existing:,} prior bulk customers + their holdings")
            cur.execute("DELETE FROM portfolio.trade   WHERE customer_id >= %s", (START_CUSTOMER_ID,))
            cur.execute("DELETE FROM portfolio.holding WHERE customer_id >= %s", (START_CUSTOMER_ID,))
            cur.execute("DELETE FROM portfolio.customer WHERE customer_id >= %s", (START_CUSTOMER_ID,))
            conn.commit()
        cur.close(); conn.close()
        pg_copy(START_CUSTOMER_ID, CUSTOMERS, security_ids)
    else:
        step("SKIP_PG=1 — skipping Postgres load")

    if not SKIP_REDIS:
        clear_redis_bulk()
        push_baseline_to_redis()
        redis_bulk(START_CUSTOMER_ID, CUSTOMERS, security_ids)
        create_search_index()
    else:
        step("SKIP_REDIS=1 — skipping Redis load")

    banner("done")
    print(f"  Postgres bulk customers : {CUSTOMERS:,}  (PAN-indexed)")
    print(f"  Postgres bulk holdings  : {CUSTOMERS*HOLDINGS_PER_CUST:,}")
    print(f"  Redis    bulk customers : {CUSTOMERS:,}  (FT cust-idx)")
    if BULK_HOLDINGS_REDIS:
        print(f"  Redis    bulk holdings  : {CUSTOMERS*HOLDINGS_PER_CUST:,}  (FT hold-idx)")
    else:
        print(f"  Redis    bulk holdings  : skipped (BULK_HOLDINGS_REDIS=0) — keeps BDB compact")
        print(f"                            portfolio demo uses the original 10 customers")
    print()
    print(f"  Try:")
    print(f"     curl 'http://localhost:5050/api/customer-search?q=Raj' | jq")
    print(f"     curl 'http://localhost:5050/api/scale-benchmark?prefix=Raj' | jq")
    print(f"     curl 'http://localhost:5050/api/latency/HS0001234'        | jq")

if __name__ == "__main__":
    main()
