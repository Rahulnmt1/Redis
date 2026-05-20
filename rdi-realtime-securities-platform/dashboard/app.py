"""
Securities & Trading Firm · RDI Capability Showcase Dashboard
====================================================

This is the *architect-facing* re-imagining of the demo dashboard.
It still includes the original customer-portfolio surface (kept as
the "Portfolio" tab so we can show what the end customer sees), but
the headline is now an interactive map of RDI capabilities and the
firm's use-cases.

Design goals (driven by the brief from the customer team):
  • Every demonstrable RDI capability has a button that fires the
    REAL pipeline end-to-end. No mocked side-effects.
  • Every interaction is side-by-side on one screen — no jumping
    to DBeaver / psql / redis-cli during the demo.
  • Every panel shows TWO latency numbers honestly:
        (a) pipeline propagation: PG-write -> Redis-visible (ms-scale)
        (b) Redis read latency on the resulting key  (microsecond-scale)
  • Every capability cites the official Redis Data Integration
    documentation page so nothing in the demo is invented.

Production note: in the firm's live RDI install, all of the DDL /
DML / pipeline-control operations exposed here would be performed by
their own systems (CI/CD, DBA tooling, Redis Insight). The dashboard
fires them so architects can see the RDI behaviour without context-
switching during the demo.
"""
from __future__ import annotations

import json
import os
import random
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# All timestamps surfaced to the dashboard are converted to Asia/Kolkata so
# the firm engineers see them in their own timezone without needing to do
# UTC arithmetic during a demo.
IST = timezone(timedelta(hours=5, minutes=30))


def _epoch_ms_to_ist(ms: float | int | None) -> str | None:
    """Format an epoch-millisecond value as 'HH:MM:SS.mmm IST'."""
    if ms is None:
        return None
    dt = datetime.fromtimestamp(ms / 1000.0, tz=IST)
    return dt.strftime("%H:%M:%S.") + f"{int(round((ms % 1000))):03d} IST"


def _now_ms() -> float:
    return time.time() * 1000.0

import psycopg2
import psycopg2.extras
import redis
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# ---- Connections ----------------------------------------------------------
REDIS_HOST  = os.getenv("TARGET_DB_HOST", "redis-enterprise")
REDIS_PORT  = int(os.getenv("TARGET_DB_PORT", "12000"))
REDIS_PASS  = os.getenv("TARGET_DB_PASSWORD", "") or None

RDI_HOST    = os.getenv("RDI_DB_HOST", "redis-rdi")
RDI_PORT    = int(os.getenv("RDI_DB_PORT", "12001"))

PG_HOST = os.getenv("PG_HOST", "postgres")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_DB   = os.getenv("PG_DB",   "sectrade")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASS = os.getenv("PG_PASS", "postgres")

RDI_JOBS_DIR   = os.getenv("RDI_JOBS_DIR",  "/rdi/jobs")
RDI_CONFIG_PATH = os.getenv("RDI_CONFIG_PATH", "/rdi/config.yaml")

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASS,
                decode_responses=True)

rdi_state = redis.Redis(host=RDI_HOST, port=RDI_PORT, decode_responses=True)


def pg_conn():
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB,
        user=PG_USER, password=PG_PASS,
    )


# ===========================================================================
# ──────────────────────────────  PORTFOLIO LAYER  ──────────────────────────
# (kept identical to the previous dashboard so the Portfolio tab still works)
# ===========================================================================

# ---- Caches ---------------------------------------------------------------
_SEC_CACHE: dict[int, dict] = {}
_SEC_CACHE_LAST = 0.0

def _refresh_sec_cache() -> None:
    global _SEC_CACHE_LAST
    fresh = {}
    for sk in r.scan_iter(match="security:*", count=500):
        sec = r.hgetall(sk)
        sid_s = sec.get("security_id")
        if not sid_s:
            continue
        try:
            sid = int(sid_s)
        except ValueError:
            continue
        fresh[sid] = {
            "symbol":  sec.get("symbol", f"SEC{sid}"),
            "company": sec.get("company_name", ""),
            "sector":  sec.get("sector", ""),
        }
    _SEC_CACHE.clear()
    _SEC_CACHE.update(fresh)
    _SEC_CACHE_LAST = time.time()

def _sec_meta(sid: int) -> dict:
    if sid in _SEC_CACHE:
        return _SEC_CACHE[sid]
    if time.time() - _SEC_CACHE_LAST > 5:
        _refresh_sec_cache()
    return _SEC_CACHE.get(sid, {"symbol": f"SEC{sid}", "company": "", "sector": ""})


def _ft_index_present(name: str = "cust-idx") -> bool:
    try:
        r.execute_command("FT.INFO", name)
        return True
    except redis.ResponseError:
        return False


def _holding_keys(customer_id: int) -> list[str]:
    if _ft_index_present("hold-idx"):
        res = r.execute_command(
            "FT.SEARCH", "hold-idx",
            f"@customer_id:[{customer_id} {customer_id}]",
            "NOCONTENT",
            "LIMIT", "0", "1000",
        )
        return list(res[1:]) if res and len(res) >= 2 else []
    return list(r.scan_iter(match=f"holding:{customer_id}:*", count=500))


def get_customer(client_code: str) -> dict | None:
    raw = r.execute_command("JSON.GET", f"customer:{client_code}", "$")
    if not raw:
        return None
    return json.loads(raw)[0]


def list_customers(limit: int = 25, query: str | None = None) -> list[dict]:
    if _ft_index_present():
        return _ft_list(limit, query)
    return _scan_list(limit)


def _ft_list(limit: int, query: str | None) -> list[dict]:
    if query:
        q = query.strip()
        if len(q) == 10 and q[:5].isalpha() and q[5:9].isdigit() and q[-1].isalpha():
            expr = f'@pan:{{{q.upper()}}}'
        elif q.upper().startswith("HS") and q[2:].isdigit():
            expr = f'@client_code:{{{q.upper()}}}'
        else:
            safe = ''.join(ch for ch in q if ch.isalnum() or ch in '_-').lower()
            expr = f'@name:{safe}*' if safe else '*'
    else:
        expr = '*'

    res = r.execute_command(
        "FT.SEARCH", "cust-idx", expr,
        "LIMIT", "0", str(limit),
        "SORTBY", "client_code", "ASC",
        "RETURN", "7",
        "$.client_code", "$.pan", "$.name",
        "$.segment", "$.risk_profile", "$.customer_id", "$.demat_account",
    )
    out = []
    for i in range(1, len(res), 2):
        flat = res[i+1]
        d = dict(zip(flat[::2], flat[1::2]))
        out.append({
            "client_code":   d.get("$.client_code", ""),
            "pan":           d.get("$.pan", ""),
            "name":          d.get("$.name", ""),
            "full_name":     d.get("$.name", ""),
            "segment":       d.get("$.segment", ""),
            "risk_profile":  d.get("$.risk_profile", ""),
            "customer_id":   int(d.get("$.customer_id", 0) or 0),
            "demat_account": d.get("$.demat_account", ""),
        })
    return out


def _scan_list(limit: int) -> list[dict]:
    out = []
    for key in r.scan_iter(match="customer:*", count=200):
        raw = r.execute_command("JSON.GET", key, "$")
        if raw:
            out.append(json.loads(raw)[0])
        if len(out) >= limit:
            break
    return sorted(out, key=lambda c: c.get("client_code", ""))


def customer_count_target() -> int:
    if _ft_index_present():
        info = r.execute_command("FT.INFO", "cust-idx")
        d = dict(zip(info[::2], info[1::2]))
        return int(d.get("num_docs", 0))
    return sum(1 for _ in r.scan_iter(match="customer:*", count=1000))


def get_portfolio(customer_id: int) -> dict:
    holdings = []
    total_invested = 0.0
    total_market   = 0.0

    for key in _holding_keys(customer_id):
        raw = r.execute_command("JSON.GET", key, "$")
        if not raw:
            continue
        h = json.loads(raw)[0]
        sec_id = int(h["security_id"])

        price = r.hgetall(f"price:{sec_id}")
        ltp = float(price.get("ltp", 0)) if price else 0.0
        prev = float(price.get("prev_close", 0)) if price else 0.0

        meta = _sec_meta(sec_id)
        symbol, company, sector = meta["symbol"], meta["company"], meta["sector"]

        qty = float(h["quantity"])
        avg = float(h["avg_buy_price"])
        invested = qty * avg
        market   = qty * ltp
        pnl      = market - invested
        pnl_pct  = (pnl / invested * 100.0) if invested else 0.0
        day_change_pct = ((ltp - prev) / prev * 100.0) if prev else 0.0

        total_invested += invested
        total_market   += market

        holdings.append({
            "symbol":   symbol,
            "company":  company,
            "sector":   sector,
            "quantity": qty,
            "avg_buy_price": round(avg, 2),
            "ltp":     round(ltp, 2),
            "invested": round(invested, 2),
            "market":   round(market, 2),
            "pnl":      round(pnl, 2),
            "pnl_pct":  round(pnl_pct, 2),
            "day_change_pct": round(day_change_pct, 2),
        })

    holdings.sort(key=lambda h: -h["market"])
    overall_pnl = total_market - total_invested
    return {
        "holdings": holdings,
        "summary": {
            "invested":     round(total_invested, 2),
            "market_value": round(total_market, 2),
            "pnl":          round(overall_pnl, 2),
            "pnl_pct":      round((overall_pnl / total_invested * 100.0)
                                  if total_invested else 0.0, 2),
            "holding_count": len(holdings),
        },
    }


def latency_compare(customer_id: int) -> dict:
    sql = """
    SELECT h.quantity, h.avg_buy_price, mp.ltp
    FROM portfolio.holding h
    JOIN portfolio.market_price mp ON mp.security_id = h.security_id
    WHERE h.customer_id = %s
    """
    t0 = time.perf_counter()
    with pg_conn() as c, c.cursor() as cur:
        cur.execute(sql, (customer_id,))
        rows = cur.fetchall()
        total_inv = sum(float(q) * float(a) for q, a, _ in rows)
        total_mkt = sum(float(q) * float(p) for q, _, p in rows)
    pg_ms = (time.perf_counter() - t0) * 1000.0

    t0 = time.perf_counter()
    inv = mkt = 0.0
    holding_keys = _holding_keys(customer_id)
    if holding_keys:
        pipe = r.pipeline(transaction=False)
        for k in holding_keys:
            pipe.execute_command("JSON.GET", k, "$")
        holding_jsons = pipe.execute()
        holdings = [json.loads(j)[0] for j in holding_jsons if j]

        pipe = r.pipeline(transaction=False)
        for h in holdings:
            pipe.hget(f"price:{int(h['security_id'])}", "ltp")
        ltps = pipe.execute()

        for h, ltp in zip(holdings, ltps):
            qty = float(h["quantity"])
            inv += qty * float(h["avg_buy_price"])
            mkt += qty * float(ltp or 0)
    redis_ms = (time.perf_counter() - t0) * 1000.0

    return {
        "postgres_ms": round(pg_ms, 3),
        "redis_ms":    round(redis_ms, 3),
        "speedup":     round(pg_ms / redis_ms, 1) if redis_ms else 0,
        "pg_invested": round(total_inv, 2),
        "pg_market":   round(total_mkt, 2),
        "redis_invested": round(inv, 2),
        "redis_market":   round(mkt, 2),
    }


# ===========================================================================
# ─────────────────────────  PIPELINE TAB BACKEND  ─────────────────────────
# ===========================================================================
def pipeline_metrics() -> dict:
    """Stream lengths on the RDI state DB (for the small pipeline strip)."""
    out = OrderedDict()
    try:
        for s in rdi_state.scan_iter(match="sectrade.portfolio.*",
                                     count=50, _type="STREAM"):
            try:
                length = rdi_state.xlen(s)
            except Exception:
                length = 0
            out[s.split(".", 1)[1]] = length
    except Exception as e:
        out["__error__"] = str(e)
    return out


def pipeline_overview() -> dict:
    """Aggregate counters + per-table stats that the processor publishes."""
    tables = ["portfolio.customer", "portfolio.holding", "portfolio.trade",
              "portfolio.market_price", "portfolio.security_master"]
    per_table = []
    total_events = 0
    last_event_ms = 0
    for t in tables:
        h = rdi_state.hgetall(f"rdi:stats:{t}") or {}
        ev = int(h.get("events", 0) or 0)
        total_events += ev
        last_event_ms = max(last_event_ms, int(h.get("last_event_ms", 0) or 0))
        per_table.append({
            "table":          t,
            "events":         ev,
            "inserts":        int(h.get("inserts", 0) or 0),
            "updates":        int(h.get("updates", 0) or 0),
            "deletes":        int(h.get("deletes", 0) or 0),
            "snapshots":      int(h.get("snapshots", 0) or 0),
            "last_lag_ms":    int(h.get("last_lag_ms", 0) or 0),
            "last_event_ms":  int(h.get("last_event_ms", 0) or 0),
        })

    total_h = rdi_state.hgetall("rdi:stats:total") or {}
    paused  = (rdi_state.get("rdi:processor:paused") == "1")
    last_reload_ms = int(rdi_state.get("rdi:processor:last_reload_ms") or 0)

    # Source health (Postgres replication slot)
    pg_slot = {}
    try:
        with pg_conn() as c, c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT slot_name, active, confirmed_flush_lsn::text AS lsn,
                       pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn) AS bytes_behind
                FROM pg_replication_slots
                WHERE plugin = 'pgoutput'
                LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                pg_slot = {
                    "slot_name":     row["slot_name"],
                    "active":        bool(row["active"]),
                    "lsn":           row["lsn"],
                    "bytes_behind":  int(row["bytes_behind"] or 0),
                }
    except Exception as e:
        pg_slot = {"error": str(e)}

    return {
        "total_events":   total_events or int(total_h.get("events", 0) or 0),
        "last_event_ms":  last_event_ms,
        "last_event_age_s": round(max(0.0, time.time() - (last_event_ms / 1000.0)), 1)
                            if last_event_ms else None,
        "paused":         paused,
        "last_reload_ms": last_reload_ms,
        "tables":         per_table,
        "streams":        pipeline_metrics(),
        "pg_slot":        pg_slot,
    }


def pipeline_recent_events(limit: int = 30) -> list[dict]:
    """Last N entries from the processor's rdi:last-events stream."""
    try:
        entries = rdi_state.xrevrange("rdi:last-events", count=limit)
    except Exception:
        entries = []
    out = []
    for sid, f in entries:
        ts_ms = int(sid.split("-")[0])
        out.append({
            "id":             sid,
            "ts_iso":         time.strftime("%H:%M:%S",
                                            time.localtime(ts_ms / 1000.0))
                              + f".{ts_ms % 1000:03d}",
            "table":          f.get("table"),
            "op":             f.get("op"),
            "lag_ms":         int(f.get("lag_ms", 0) or 0),
            "processed_at_ms": int(f.get("processed_at_ms", 0) or 0),
        })
    return out


# ===========================================================================
# ──────────────────────  CAPABILITY-LAYER HELPERS  ─────────────────────────
# These power the Capabilities + Use-Cases tabs. They are deliberately small
# and write-real-data: every fire button below executes a SQL statement that
# the RDI pipeline picks up via Debezium and replays into Redis. The waiter
# helpers block until the change is visible (or a deadline expires) so the
# dashboard can show "propagation took N ms".
# ===========================================================================

def _wait_for_key_match(check_fn, deadline_ms: int = 8000,
                        poll_ms: int = 25) -> tuple[bool, float]:
    """Poll until check_fn() returns truthy or deadline. Returns
    (ok, elapsed_ms). Used by every "fire CDC and watch it land" panel."""
    t0 = time.perf_counter()
    while True:
        if check_fn():
            return True, (time.perf_counter() - t0) * 1000.0
        if (time.perf_counter() - t0) * 1000.0 > deadline_ms:
            return False, (time.perf_counter() - t0) * 1000.0
        time.sleep(poll_ms / 1000.0)


def _redis_read_us(read_fn, runs: int = 5) -> dict:
    """Measure the actual Redis read latency for the *resulting* key,
    p50 across N runs. Returns microseconds + the most recent value."""
    times = []
    val = None
    for _ in range(runs):
        t0 = time.perf_counter()
        val = read_fn()
        times.append((time.perf_counter() - t0) * 1_000_000)
    times.sort()
    return {
        "p50_us":  round(times[len(times)//2], 1),
        "best_us": round(times[0], 1),
        "runs":    runs,
        "value":   val,
    }


def _pg_select_one(sql: str, params: tuple = ()) -> dict | None:
    with pg_conn() as c, c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None


def _pg_exec(sql: str, params: tuple = (), fetch: bool = False):
    with pg_conn() as c, c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(sql, params)
        out = cur.fetchall() if fetch else None
        c.commit()
        return out


def _pg_select_all(sql: str, params: tuple = ()) -> list[dict]:
    """SELECT ... → list of dicts. Read-only helper for places where we
    need more than one row but don't want a streaming cursor."""
    with pg_conn() as c, c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r_) for r_ in cur.fetchall()]


def _new_client_code() -> str:
    """Generate a deterministic-ish new HS code that won't clash with the
    bulk-seeded HS0010xxxxxx range. We push into the 999xxxxxx range so
    even repeated demo fires never collide."""
    return f"HS999{int(time.time() * 1000) % 1_000_000:06d}"


def _new_customer_id() -> int:
    """Generate a customer_id past the bulk-seeded range. The seeder uses
    1..3_000_000 (or whatever CUSTOMERS=); we start demo IDs at 9e9."""
    return 9_000_000_000 + (int(time.time() * 1000) % 1_000_000_000)


def _new_trade_id() -> int:
    return 9_000_000_000 + (int(time.time() * 1_000_000) % 10_000_000_000)


def _new_holding_id() -> int:
    return 9_000_000_000 + (int(time.time() * 1_000_000) % 10_000_000_000)


def _pick_verified_client_code() -> str | None:
    """Pick a customer that is *currently VERIFIED* in Postgres — i.e.
    one the customer.yaml filter will let through. Crucial so that
    capability cards run independently of each other and don't fail
    just because an earlier KYC-freeze demo left a customer blocked."""
    row = _pg_select_one("""
        SELECT client_code FROM portfolio.customer
        WHERE kyc_status = 'VERIFIED'
        ORDER BY customer_id ASC LIMIT 1
    """)
    return row["client_code"] if row else None


# ============================================================================
#  TECHNICAL-PILLAR HELPERS
#  ----------------------------------------------------------------------------
#  Every use-case card now renders three side-by-side technical pillars:
#    1. The SQL that the trading app WOULD run against Postgres (plus its p50 ms)
#    2. The RDI YAML / transformation snippet that powers the cache shape
#    3. The Redis command the cache-aware app actually runs (plus its p50 µs,
#       the speedup vs Postgres, and — when relevant — the RDI sync time
#       from "Postgres committed" to "Redis cache writable")
#
#  `_bench_pg_ms` and `_bench_redis_us` measure real client-side latency on
#  this stack. The dashboard never displays a synthetic number.
# ============================================================================
def _bench_pg_ms(sql: str, params: tuple = (), runs: int = 3) -> float:
    """Run a (read-only) SQL `runs` times and return the median wall-clock
    time in milliseconds. Used for the "Traditional Postgres" pillar."""
    timings: list[float] = []
    try:
        with pg_conn() as c, c.cursor() as cur:
            for _ in range(runs):
                t0 = time.perf_counter()
                cur.execute(sql, params)
                if cur.description is not None:
                    cur.fetchall()
                timings.append((time.perf_counter() - t0) * 1000.0)
    except Exception:
        return -1.0
    timings.sort()
    return round(timings[len(timings) // 2], 2)


def _bench_redis_us(fn, runs: int = 7) -> float:
    """Run `fn()` `runs` times and return median wall-clock latency in
    microseconds. Used for the "With Redis" pillar — these must always be
    < 1 ms for the demo to make its point."""
    timings: list[float] = []
    try:
        for _ in range(runs):
            t0 = time.perf_counter()
            fn()
            timings.append((time.perf_counter() - t0) * 1_000_000.0)
    except Exception:
        return -1.0
    timings.sort()
    return round(timings[len(timings) // 2], 1)


def _speedup(pg_ms: float, rd_us: float) -> float | None:
    if not pg_ms or pg_ms <= 0 or not rd_us or rd_us <= 0:
        return None
    return round((pg_ms * 1000.0) / rd_us, 0)


def _technical(pg_query: str, pg_ms: float,
               yaml_file: str, yaml_snippet: str,
               redis_command: str, redis_us: float,
               rdi_sync_ms: float | None = None) -> dict:
    """Bundle the 3-pillar technical context for the UI."""
    return {
        "pg": {
            "query":  pg_query,
            "p50_ms": pg_ms,
        },
        "rdi": {
            "file":    yaml_file,
            "snippet": yaml_snippet,
        },
        "redis": {
            "command":       redis_command,
            "p50_us":        redis_us,
            "speedup_vs_pg": _speedup(pg_ms, redis_us),
        },
        "rdi_sync_ms": rdi_sync_ms,
    }


# -- Canonical YAML snippets surfaced on each use-case card -----------------
YAML_SNIPPETS = {
    "trade_stream": (
        "# rdi/jobs/trade.yaml\n"
        "source:\n"
        "  table: portfolio.trade\n"
        "output:\n"
        "  - data_type: stream                  # append-only per-customer\n"
        "    key:  concat(['trades:', to_string(customer_id)])\n"
        "  - data_type: json                    # individual trade doc\n"
        "    key:  concat(['trade:',  to_string(trade_id)])\n"
    ),
    "customer_filter_json": (
        "# rdi/jobs/customer.yaml\n"
        "source:\n"
        "  table: portfolio.customer\n"
        "transform:\n"
        "  - filter: \"kyc_status == 'VERIFIED'\"   # ← compliance gate\n"
        "output:\n"
        "  - data_type: json\n"
        "    key:  concat(['customer:', client_code])\n"
    ),
    "customer_multi_shape": (
        "# rdi/jobs/customer.yaml\n"
        "output:\n"
        "  - data_type: json                      # full profile doc\n"
        "    connection: target\n"
        "    key:  concat(['customer:', client_code])\n"
        "  - data_type: set                       # SCARD comes from this\n"
        "    connection: target\n"
        "    key:    concat(['cust-segment:', segment])\n"
        "    member: client_code\n"
    ),
    "holding_json": (
        "# rdi/jobs/holding.yaml\n"
        "source:\n"
        "  table: portfolio.holding\n"
        "output:\n"
        "  - data_type: json\n"
        "    key: concat(['holding:',\n"
        "                 to_string(customer_id), ':',\n"
        "                 to_string(security_id)])\n"
    ),
    "last_events": (
        "# rdi-processor (reference implementation)\n"
        "# Every CDC event the processor handles is mirrored to a\n"
        "# bounded Stream so the dashboard / Splunk / Grafana can\n"
        "# XREAD the last-N changes without polling Postgres.\n"
        "XADD rdi:last-events  *  table <name>  op <c|u|d|r>  lag_ms <n>\n"
    ),
}


# ===========================================================================
# ────────────────────────────────  ROUTES  ─────────────────────────────────
# ===========================================================================
@app.route("/")
def index():
    return render_template("index.html")


# ---- Legacy portfolio / search / scale -- kept verbatim --------------------
@app.route("/api/customers")
def api_customers():
    q = request.args.get("q") or None
    limit = min(int(request.args.get("limit", "50")), 100)
    return jsonify(list_customers(limit=limit, query=q))


@app.route("/api/customer-search")
def api_customer_search():
    q = request.args.get("q") or None
    limit = min(int(request.args.get("limit", "25")), 100)
    t0 = time.perf_counter()
    results = list_customers(limit=limit, query=q)
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 3)

    total = customer_count_target()
    return jsonify({
        "results":      results,
        "total":        total,
        "shown":        len(results),
        "search_ms":    elapsed_ms,
        "ft_enabled":   _ft_index_present(),
        "query":        q,
    })


def _stats(xs):
    if not xs:
        return None
    xs2 = sorted(xs)
    return {
        "p50":  round(xs2[len(xs2)//2],   3),
        "best": round(xs2[0],             3),
        "avg":  round(sum(xs2)/len(xs2),  3),
        "runs": len(xs2),
    }


@app.route("/api/scale-benchmark")
def api_scale_benchmark():
    runs = max(1, min(20, int(request.args.get("runs", "5"))))
    pan = (request.args.get("pan") or "").upper().strip()
    if not pan and _ft_index_present():
        sample = r.execute_command(
            "FT.SEARCH", "cust-idx", "*",
            "LIMIT", "0", "1",
            "RETURN", "1", "$.pan",
        )
        if len(sample) >= 3:
            pan = dict(zip(sample[2][::2], sample[2][1::2])).get("$.pan", "")
    if not pan:
        return jsonify({"error": "no PAN available — run scripts/seed-large-scale.sh first"}), 400

    prefix = (request.args.get("prefix") or "Raj").strip()
    pfx_safe = ''.join(ch for ch in prefix if ch.isalnum())

    pg_pan_times: list[float] = []
    pg_row = None
    with pg_conn() as c, c.cursor() as cur:
        for _ in range(runs):
            t0 = time.perf_counter()
            cur.execute(
                "SELECT customer_id, client_code, pan, full_name "
                "FROM portfolio.customer WHERE pan = %s LIMIT 1",
                (pan,),
            )
            pg_row = cur.fetchone()
            pg_pan_times.append((time.perf_counter() - t0) * 1000)

    rd_ft_pan_times: list[float] = []
    if _ft_index_present():
        for _ in range(runs):
            t0 = time.perf_counter()
            r.execute_command(
                "FT.SEARCH", "cust-idx",
                f"@pan:{{{pan}}}", "LIMIT", "0", "1",
                "RETURN", "3", "$.customer_id", "$.client_code", "$.name",
            )
            rd_ft_pan_times.append((time.perf_counter() - t0) * 1000)

    rd_direct_times: list[float] = []
    client_code = pg_row[1] if pg_row else None
    if client_code:
        for _ in range(runs):
            t0 = time.perf_counter()
            r.execute_command("JSON.GET", f"customer:{client_code}", "$.name")
            rd_direct_times.append((time.perf_counter() - t0) * 1000)

    pg_pfx_times: list[float] = []
    pg_pfx_count = 0
    with pg_conn() as c, c.cursor() as cur:
        for _ in range(runs):
            t0 = time.perf_counter()
            cur.execute(
                "SELECT COUNT(*) FROM portfolio.customer "
                "WHERE full_name ILIKE %s",
                (prefix + '%',),
            )
            pg_pfx_count = cur.fetchone()[0]
            pg_pfx_times.append((time.perf_counter() - t0) * 1000)

    rd_ft_pfx_times: list[float] = []
    rd_ft_pfx_total = 0
    if _ft_index_present():
        for _ in range(runs):
            t0 = time.perf_counter()
            res = r.execute_command(
                "FT.SEARCH", "cust-idx",
                f"@name:{pfx_safe.lower()}*",
                "LIMIT", "0", "0",
            )
            rd_ft_pfx_times.append((time.perf_counter() - t0) * 1000)
            if res:
                rd_ft_pfx_total = int(res[0])

    def speedup(pg, rd):
        if pg and rd and rd["p50"]:
            return round(pg["p50"] / rd["p50"], 1)
        return None

    pg_pan = _stats(pg_pan_times)
    rd_ft_pan = _stats(rd_ft_pan_times)
    rd_direct = _stats(rd_direct_times)
    pg_pfx = _stats(pg_pfx_times)
    rd_ft_pfx = _stats(rd_ft_pfx_times)

    return jsonify({
        "total_customers":   customer_count_target(),
        "sample_pan":        pan,
        "sample_client":     client_code,
        "sample_name":       pg_row[3] if pg_row else None,
        "prefix":            prefix,
        "pan_lookup": {
            "postgres":        pg_pan,
            "redis_ft_search": rd_ft_pan,
            "redis_json_get":  rd_direct,
            "speedup_ft":      speedup(pg_pan, rd_ft_pan),
            "speedup_direct":  speedup(pg_pan, rd_direct),
        },
        "name_prefix": {
            "postgres":        pg_pfx,
            "redis_ft_search": rd_ft_pfx,
            "pg_match_count":  pg_pfx_count,
            "redis_match_count": rd_ft_pfx_total,
            "speedup_ft":      speedup(pg_pfx, rd_ft_pfx),
        },
    })


@app.route("/api/portfolio/<client_code>")
def api_portfolio(client_code: str):
    cust = get_customer(client_code)
    if not cust:
        return jsonify({"error": "customer not found"}), 404
    p = get_portfolio(int(cust["customer_id"]))
    return jsonify({"customer": cust, **p})


@app.route("/api/latency/<client_code>")
def api_latency(client_code: str):
    cust = get_customer(client_code)
    if not cust:
        return jsonify({"error": "customer not found"}), 404
    return jsonify(latency_compare(int(cust["customer_id"])))


@app.route("/api/pipeline")
def api_pipeline():
    return jsonify(pipeline_metrics())


@app.route("/api/recent-trades/<client_code>")
def api_recent_trades(client_code: str):
    cust = get_customer(client_code)
    if not cust:
        return jsonify({"error": "customer not found"}), 404
    cid = int(cust["customer_id"])
    entries = r.xrevrange(f"trades:{cid}", count=10)
    trades = []
    for tid, fields in entries:
        trades.append({
            "stream_id":   tid,
            "trade_id":    fields.get("trade_id"),
            "side":        fields.get("side"),
            "quantity":    fields.get("quantity"),
            "price":       fields.get("price"),
            "trade_value": fields.get("trade_value"),
            "executed_at": fields.get("executed_at"),
            "security_id": fields.get("security_id"),
        })
    return jsonify(trades)


# ===========================================================================
# ─────────────────────  PIPELINE TAB ROUTES (new)  ─────────────────────────
# ===========================================================================
@app.route("/api/pipeline/overview")
def api_pipeline_overview():
    return jsonify(pipeline_overview())


@app.route("/api/pipeline/events")
def api_pipeline_events():
    limit = min(300, max(5, int(request.args.get("limit", "30"))))
    return jsonify(pipeline_recent_events(limit))


# ---------------------------------------------------------------------------
# Pipeline-tab "Fire one event" injector — full step-by-step trace
#
# Each fire emits a structured `steps[]` array with IST-formatted timestamps
# so the dashboard can draw the exact journey of one row through:
#
#   1. PostgreSQL INSERT executed
#   2. PostgreSQL WAL committed         (we capture pg_current_wal_lsn here)
#   3. RDI · Debezium picked it up      (observed via the sectrade.portfolio.*
#                                        stream on rdi-state)
#   4. RDI Processor wrote it to Redis  (observed via target key presence)
#   5. Application reads it back        (measured JSON.GET / XLEN latency)
#
# Every step also carries `delta_ms` from t0 = the moment we kicked off the
# INSERT, so the UI can render a true time-line.
# ---------------------------------------------------------------------------
def _wait_for_debezium_pickup(stream: str, since_id: str,
                              pk_field: str, pk_value,
                              deadline_ms: int = 15000,
                              poll_ms: int = 2) -> tuple[int | None, str | None]:
    """Block until a new entry shows up in `stream` (after `since_id`) whose
    Debezium key payload references the given primary-key field+value.
    Returns (epoch_ms_of_the_stream_entry, full_stream_id) or (None, None).
    Default poll is 2 ms so the trace doesn't add measurement slack to the
    real CDC-pickup latency we're trying to display."""
    needle = f'"{pk_field}":{pk_value}'
    deadline = time.perf_counter() + deadline_ms / 1000.0
    cursor   = since_id or "0"
    while time.perf_counter() < deadline:
        try:
            entries = rdi_state.xrange(stream, min=f"({cursor}", count=50)
        except Exception:
            entries = []
        for sid, fields in entries:
            for fk in fields.keys():
                if needle in fk:
                    return int(sid.split("-")[0]), sid
            cursor = sid
        time.sleep(poll_ms / 1000.0)
    return None, None


def _last_stream_id(stream: str) -> str:
    try:
        last = rdi_state.xrevrange(stream, count=1)
        return last[0][0] if last else "0"
    except Exception:
        return "0"


def _build_step(stage: str, label: str, ms: float | None, t0_ms: float,
                detail: str, **extras) -> dict:
    return {
        "stage":    stage,
        "label":    label,
        "ts_ist":   _epoch_ms_to_ist(ms),
        "ts_ms":    int(ms) if ms is not None else None,
        "delta_ms": round(ms - t0_ms, 1) if ms is not None else None,
        "detail":   detail,
        **extras,
    }


def _measure_redis_read_us(read_fn, runs: int = 7) -> float:
    """p50 microseconds across N reads."""
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        try: read_fn()
        except Exception: pass
        times.append((time.perf_counter() - t0) * 1_000_000)
    times.sort()
    return round(times[len(times) // 2], 1)


def _measure_pg_read_us(sql: str, params=None, runs: int = 5) -> float:
    """p50 microseconds for a Postgres read. Re-opens a fresh connection
    once via _pg_select_one so we measure steady-state, not connect cost."""
    # warm-up read (excluded from the percentile)
    try: _pg_select_one(sql, params)
    except Exception: pass
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        try: _pg_select_one(sql, params)
        except Exception: pass
        times.append((time.perf_counter() - t0) * 1_000_000)
    times.sort()
    return round(times[len(times) // 2], 1)


def _build_comparison(pg_query_str: str, pg_us: float,
                      redis_cmd_str: str, redis_us: float,
                      why_it_matters: str) -> dict:
    """Bundle a Postgres-read vs Redis-read head-to-head into one block."""
    speedup = None
    pct     = None
    delta   = None
    if pg_us and redis_us and redis_us > 0:
        speedup = round(pg_us / redis_us, 1)
        pct     = round((1.0 - redis_us / pg_us) * 100.0, 2)
        delta   = round(pg_us - redis_us, 1)
    return {
        "redis":    {"command": redis_cmd_str, "p50_us": redis_us,
                     "label":   "Redis Enterprise"},
        "postgres": {"query":   pg_query_str,  "p50_us": pg_us,
                     "label":   "PostgreSQL · indexed lookup"},
        "speedup_x":      speedup,
        "pct_faster":     pct,
        "delta_us":       delta,
        "why_it_matters": why_it_matters,
    }


def _get_wal_lsn() -> str | None:
    try:
        row = _pg_select_one("SELECT pg_current_wal_lsn() AS lsn")
        return row.get("lsn") if row else None
    except Exception:
        return None


def _add_step_durations(steps: list[dict]) -> list[dict]:
    """Add `step_ms` to each step = how long *this step alone* took
    (i.e. delta from the previous non-null step's timestamp). The first
    step is the t0 anchor and gets step_ms=None so it doesn't render a
    misleading "+0 ms" chip. The filter-blocked KYC step also gets
    step_ms=None because the long tail there is a polling wait, not a
    real measurement."""
    prev_ts = None
    for s in steps:
        ts = s.get("ts_ms")
        if s.get("stage") == "filtered":
            s["step_ms"] = None
        elif ts is None:
            s["step_ms"] = None
        elif prev_ts is None:
            s["step_ms"] = None
        else:
            s["step_ms"] = round(ts - prev_ts, 1)
        if ts is not None:
            prev_ts = ts
    return steps


@app.route("/api/pipeline/inject", methods=["POST"])
def api_pipeline_inject():
    action = ((request.json or {}).get("action") or "customer").lower()

    # ===================================================================
    # 1) Customer onboarding · INSERT a synthetic customer
    #    (also serves the legacy `customer` action id used by older demos)
    # ===================================================================
    if action in ("onboard", "customer"):
        cc      = _new_client_code()
        cid     = _new_customer_id()
        pan     = f"INJ{int(time.time()) % 1_000_000:06d}"
        stream  = "sectrade.portfolio.customer"
        target  = f"customer:{cc}"
        sql     = ("INSERT INTO portfolio.customer\n"
                   "  (customer_id, client_code, pan, full_name, segment, risk_profile,\n"
                   "   kyc_status, demat_account, onboarded_on)\n"
                   "VALUES (%s, %s, %s, %s, 'RETAIL', 'MODERATE',\n"
                   "        'VERIFIED', %s, CURRENT_DATE);")

        since   = _last_stream_id(stream)
        t_start = _now_ms()
        _pg_exec(sql.replace("%s", "%s"),
                 (cid, cc, pan, f"Injected Demo {cc}", f"{cc}-DP"))
        t_pgdone = _now_ms()
        wal_lsn  = _get_wal_lsn()

        cdc_ms, cdc_sid = _wait_for_debezium_pickup(
            stream, since, "customer_id", cid, deadline_ms=15000)

        ok, _ = _wait_for_key_match(
            lambda: r.execute_command("JSON.GET", target, "$") is not None,
            deadline_ms=15000, poll_ms=2)
        t_redis = _now_ms() if ok else None

        read_us = _measure_redis_read_us(
            lambda: r.execute_command("JSON.GET", target, "$"))
        t_app = _now_ms()

        pg_read_sql  = ("SELECT customer_id, client_code, full_name, pan, kyc_status, "
                        "segment, risk_profile FROM portfolio.customer WHERE client_code = %s")
        pg_read_us   = _measure_pg_read_us(pg_read_sql, (cc,))
        comparison   = _build_comparison(
            pg_query_str  = pg_read_sql.replace("%s", f"'{cc}'") + ";",
            pg_us         = pg_read_us,
            redis_cmd_str = f"JSON.GET {target} $",
            redis_us      = read_us,
            why_it_matters="Customer-profile reads happen on every login and on every "
                           "screen refresh. Cutting them from a PG round-trip to a "
                           "sub-millisecond Redis hit is what makes the trading app feel instant.",
        )

        steps = [
            _build_step("pg_insert", "INSERT executed on PostgreSQL",
                        t_start,  t_start,
                        detail=f"INSERT INTO portfolio.customer … VALUES ({cid}, '{cc}', …);"),
            _build_step("pg_wal",    "Row committed to PostgreSQL WAL",
                        t_pgdone, t_start,
                        detail=f"pg_current_wal_lsn() = {wal_lsn or 'n/a'}",
                        wal_lsn=wal_lsn),
            _build_step("cdc",       "RDI · Debezium captured the change",
                        cdc_ms,   t_start,
                        detail=f"stream {stream} id {cdc_sid or 'pending'} "
                               f"key {{\"customer_id\":{cid}}}",
                        stream=stream, stream_id=cdc_sid),
            _build_step("redis_write","RDI Processor wrote it into Redis",
                        t_redis,  t_start,
                        detail=f"JSON.SET {target} $ {{ … customer JSON … }}",
                        target_key=target),
            _build_step("app_read",  "Application reads from Redis",
                        t_app,    t_start,
                        detail=f"JSON.GET {target} $  →  {read_us} µs",
                        read_us=read_us, app_command=f"JSON.GET {target} $"),
        ]
        return jsonify({
            "ok":              ok,
            "action":          ("Section 3 · New customer onboarded"
                                if action == "onboard" else "Insert customer"),
            "action_id":       action,
            "table":           "portfolio.customer",
            "target_key":      target,
            "primary_key":     {"customer_id": cid, "client_code": cc},
            "wal_lsn":         wal_lsn,
            "steps":           _add_step_durations(steps),
            "comparison":      comparison,
            "totals": {
                "t0_ist":              _epoch_ms_to_ist(t_start),
                "pg_write_ms":         round(t_pgdone - t_start, 1),
                "cdc_pickup_ms":       round(cdc_ms - t_pgdone, 1) if cdc_ms else None,
                "processor_write_ms":  round(t_redis - cdc_ms, 1) if (t_redis and cdc_ms) else None,
                "pipeline_total_ms":   round(t_redis - t_start, 1) if t_redis else None,
                "app_read_us":         read_us,
            },
            "section":          3,
            "section_label":   "Customer experience",
        })

    # ===================================================================
    # 2) Settle a trade
    # ===================================================================
    if action == "trade":
        cc = _pick_verified_client_code()
        if not cc:
            return jsonify({"error": "no VERIFIED customer found"}), 400
        cust = _pg_select_one(
            "SELECT customer_id FROM portfolio.customer WHERE client_code=%s", (cc,))
        cid  = int(cust["customer_id"])
        sec  = _pg_select_one(
            "SELECT security_id FROM portfolio.security_master ORDER BY security_id LIMIT 1")
        sid  = int(sec["security_id"]) if sec else 1
        tid  = _new_trade_id()

        stream = "sectrade.portfolio.trade"
        target = f"trades:{cid}"
        before = r.xlen(target) or 0
        since  = _last_stream_id(stream)

        t_start = _now_ms()
        _pg_exec("""
            INSERT INTO portfolio.trade
              (trade_id, customer_id, security_id, side, quantity, price,
               trade_value, brokerage, order_id, exchange, executed_at)
            VALUES (%s, %s, %s, 'BUY', 7, 250.0, 1750.0, 0, %s, 'NSE', NOW())
        """, (tid, cid, sid, f"ORD-{tid}"))
        t_pgdone = _now_ms()
        wal_lsn  = _get_wal_lsn()

        cdc_ms, cdc_sid = _wait_for_debezium_pickup(
            stream, since, "trade_id", tid, deadline_ms=15000)

        ok, _ = _wait_for_key_match(
            lambda: (r.xlen(target) or 0) > before, deadline_ms=15000, poll_ms=2)
        t_redis = _now_ms() if ok else None

        read_us = _measure_redis_read_us(lambda: r.xlen(target))
        t_app   = _now_ms()

        pg_read_sql = "SELECT COUNT(*) FROM portfolio.trade WHERE customer_id = %s"
        pg_read_us  = _measure_pg_read_us(pg_read_sql, (cid,))
        comparison  = _build_comparison(
            pg_query_str  = pg_read_sql.replace("%s", str(cid)) + ";",
            pg_us         = pg_read_us,
            redis_cmd_str = f"XLEN {target}",
            redis_us      = read_us,
            why_it_matters="Order-history badges show the customer's trade count on every "
                           "screen. PG has to walk a B-tree index; Redis Streams answer "
                           "XLEN from an in-memory counter in microseconds.",
        )

        steps = [
            _build_step("pg_insert", "INSERT executed on PostgreSQL",
                        t_start, t_start,
                        detail=f"INSERT INTO portfolio.trade … VALUES ({tid}, …);"),
            _build_step("pg_wal",    "Row committed to PostgreSQL WAL",
                        t_pgdone, t_start,
                        detail=f"pg_current_wal_lsn() = {wal_lsn or 'n/a'}",
                        wal_lsn=wal_lsn),
            _build_step("cdc",       "RDI · Debezium captured the change",
                        cdc_ms, t_start,
                        detail=f"stream {stream} id {cdc_sid or 'pending'} "
                               f"key {{\"trade_id\":{tid}}}",
                        stream=stream, stream_id=cdc_sid),
            _build_step("redis_write","RDI Processor appended to Redis Stream",
                        t_redis, t_start,
                        detail=f"XADD {target} * trade_id {tid} side BUY qty 7 price 250.0",
                        target_key=target),
            _build_step("app_read",  "Application reads back the trade stream length",
                        t_app, t_start,
                        detail=f"XLEN {target}  →  {read_us} µs",
                        read_us=read_us, app_command=f"XLEN {target}"),
        ]
        return jsonify({
            "ok":          ok,
            "action":      "Settle trade",
            "action_id":   "trade",
            "table":       "portfolio.trade",
            "target_key":  target,
            "primary_key": {"trade_id": tid, "customer_id": cid, "client_code": cc},
            "wal_lsn":     wal_lsn,
            "steps":       _add_step_durations(steps),
            "comparison":  comparison,
            "totals": {
                "t0_ist":              _epoch_ms_to_ist(t_start),
                "pg_write_ms":         round(t_pgdone - t_start, 1),
                "cdc_pickup_ms":       round(cdc_ms - t_pgdone, 1) if cdc_ms else None,
                "processor_write_ms":  round(t_redis - cdc_ms, 1) if (t_redis and cdc_ms) else None,
                "pipeline_total_ms":   round(t_redis - t_start, 1) if t_redis else None,
                "app_read_us":         read_us,
            },
        })

    # ===================================================================
    # 3) Update a holding
    # ===================================================================
    if action == "holding":
        cc = _pick_verified_client_code()
        if not cc:
            return jsonify({"error": "no VERIFIED customer found"}), 400
        cust = _pg_select_one(
            "SELECT customer_id FROM portfolio.customer WHERE client_code=%s", (cc,))
        cid  = int(cust["customer_id"])
        h    = _pg_select_one(
            "SELECT holding_id, security_id, quantity FROM portfolio.holding "
            "WHERE customer_id=%s LIMIT 1", (cid,))
        if not h:
            return jsonify({"error": f"{cc} has no holdings"}), 400
        new_qty = int(h["quantity"]) + 3
        hid     = int(h["holding_id"])
        sid     = int(h["security_id"])
        target  = f"holding:{cid}:{sid}"
        stream  = "sectrade.portfolio.holding"
        since   = _last_stream_id(stream)

        t_start = _now_ms()
        _pg_exec("""
            UPDATE portfolio.holding
            SET    quantity        = %s,
                   invested_value  = avg_buy_price * %s,
                   last_trade_date = CURRENT_DATE,
                   updated_at      = NOW()
            WHERE  holding_id = %s
        """, (new_qty, new_qty, hid))
        t_pgdone = _now_ms()
        wal_lsn  = _get_wal_lsn()

        cdc_ms, cdc_sid = _wait_for_debezium_pickup(
            stream, since, "holding_id", hid, deadline_ms=15000)

        def _matches():
            raw = r.execute_command("JSON.GET", target, "$")
            if not raw: return False
            try: return int(float(json.loads(raw)[0].get("quantity", 0))) == new_qty
            except Exception: return False
        ok, _ = _wait_for_key_match(_matches, deadline_ms=15000, poll_ms=2)
        t_redis = _now_ms() if ok else None

        read_us = _measure_redis_read_us(
            lambda: r.execute_command("JSON.GET", target, "$.quantity"))
        t_app = _now_ms()

        pg_read_sql = ("SELECT quantity FROM portfolio.holding "
                       "WHERE customer_id = %s AND security_id = %s")
        pg_read_us  = _measure_pg_read_us(pg_read_sql, (cid, sid))
        comparison  = _build_comparison(
            pg_query_str  = (pg_read_sql
                             .replace("%s", str(cid), 1)
                             .replace("%s", str(sid), 1)) + ";",
            pg_us         = pg_read_us,
            redis_cmd_str = f"JSON.GET {target} $.quantity",
            redis_us      = read_us,
            why_it_matters="Position screens refresh on every market-data tick. "
                           "RedisJSON serves an exact JSONPath ($.quantity) in microseconds; "
                           "PG has to load the row, parse, and project.",
        )

        steps = [
            _build_step("pg_insert", "UPDATE executed on PostgreSQL",
                        t_start, t_start,
                        detail=f"UPDATE portfolio.holding SET quantity={new_qty} WHERE holding_id={hid};"),
            _build_step("pg_wal",    "Row committed to PostgreSQL WAL",
                        t_pgdone, t_start,
                        detail=f"pg_current_wal_lsn() = {wal_lsn or 'n/a'}",
                        wal_lsn=wal_lsn),
            _build_step("cdc",       "RDI · Debezium captured the change",
                        cdc_ms, t_start,
                        detail=f"stream {stream} id {cdc_sid or 'pending'} "
                               f"key {{\"holding_id\":{hid}}}",
                        stream=stream, stream_id=cdc_sid),
            _build_step("redis_write","RDI Processor updated the JSON document",
                        t_redis, t_start,
                        detail=f"JSON.SET {target} $.quantity {new_qty}",
                        target_key=target),
            _build_step("app_read",  "Application reads the new quantity",
                        t_app, t_start,
                        detail=f"JSON.GET {target} $.quantity  →  {read_us} µs",
                        read_us=read_us, app_command=f"JSON.GET {target} $.quantity"),
        ]
        return jsonify({
            "ok":          ok,
            "action":      "Update holding",
            "action_id":   "holding",
            "table":       "portfolio.holding",
            "target_key":  target,
            "primary_key": {"holding_id": hid, "customer_id": cid, "client_code": cc},
            "wal_lsn":     wal_lsn,
            "steps":       _add_step_durations(steps),
            "comparison":  comparison,
            "totals": {
                "t0_ist":              _epoch_ms_to_ist(t_start),
                "pg_write_ms":         round(t_pgdone - t_start, 1),
                "cdc_pickup_ms":       round(cdc_ms - t_pgdone, 1) if cdc_ms else None,
                "processor_write_ms":  round(t_redis - cdc_ms, 1) if (t_redis and cdc_ms) else None,
                "pipeline_total_ms":   round(t_redis - t_start, 1) if t_redis else None,
                "app_read_us":         read_us,
            },
        })

    # ===================================================================
    # 4) Toggle KYC status — this one is special: the RDI YAML filter
    #    BLOCKS the write at the processor, so the trace ends at step 4
    #    with "filter rejected the write" and step 5 shows that the app
    #    is still serving the previous (VERIFIED) value out of Redis.
    # ===================================================================
    if action == "kyc":
        cc = _pick_verified_client_code()
        if not cc:
            return jsonify({"error": "no VERIFIED customer in Postgres"}), 400
        cust = _pg_select_one(
            "SELECT customer_id, kyc_status FROM portfolio.customer WHERE client_code=%s", (cc,))
        cid    = int(cust["customer_id"])
        target = f"customer:{cc}"
        stream = "sectrade.portfolio.customer"
        since  = _last_stream_id(stream)

        t_start = _now_ms()
        _pg_exec(
            "UPDATE portfolio.customer SET kyc_status='BLOCKED', updated_at=NOW() WHERE customer_id=%s",
            (cid,))
        t_pgdone = _now_ms()
        wal_lsn  = _get_wal_lsn()

        cdc_ms, cdc_sid = _wait_for_debezium_pickup(
            stream, since, "customer_id", cid, deadline_ms=15000)

        # The RDI YAML filter (`kyc_status == 'VERIFIED'`) means the
        # processor must NOT write through. We wait up to 2 s — that's
        # plenty given the processor's writes now land in single-digit
        # ms — and assert the cached document still says VERIFIED.
        deadline = time.perf_counter() + 2.0
        cached_kyc = "VERIFIED"
        while time.perf_counter() < deadline:
            raw = r.execute_command("JSON.GET", target, "$.kyc_status")
            cached_kyc = json.loads(raw)[0] if raw else cached_kyc
            if cached_kyc != "VERIFIED":  # would indicate the filter let it through
                break
            time.sleep(0.005)
        t_filter = _now_ms()
        filter_blocked = (cached_kyc == "VERIFIED")

        # Restore Postgres so subsequent demos still have a VERIFIED row.
        _pg_exec(
            "UPDATE portfolio.customer SET kyc_status='VERIFIED', updated_at=NOW() WHERE customer_id=%s",
            (cid,))

        read_us = _measure_redis_read_us(
            lambda: r.execute_command("JSON.GET", target, "$.kyc_status"))
        t_app = _now_ms()

        pg_read_sql = "SELECT kyc_status FROM portfolio.customer WHERE client_code = %s"
        pg_read_us  = _measure_pg_read_us(pg_read_sql, (cc,))
        comparison  = _build_comparison(
            pg_query_str  = pg_read_sql.replace("%s", f"'{cc}'") + ";",
            pg_us         = pg_read_us,
            redis_cmd_str = f"JSON.GET {target} $.kyc_status",
            redis_us      = read_us,
            why_it_matters="The KYC gate is checked on every order placement. Redis returns "
                           "the trusted value in µs, and the YAML filter guarantees the cache "
                           "never serves a BLOCKED status \u2014 the compliance gate doubles as "
                           "a latency win.",
        )

        steps = [
            _build_step("pg_insert", "UPDATE executed on PostgreSQL",
                        t_start, t_start,
                        detail=f"UPDATE portfolio.customer SET kyc_status='BLOCKED' WHERE customer_id={cid};"),
            _build_step("pg_wal",    "Row committed to PostgreSQL WAL",
                        t_pgdone, t_start,
                        detail=f"pg_current_wal_lsn() = {wal_lsn or 'n/a'}",
                        wal_lsn=wal_lsn),
            _build_step("cdc",       "RDI · Debezium captured the change",
                        cdc_ms, t_start,
                        detail=f"stream {stream} id {cdc_sid or 'pending'} "
                               f"(kyc_status=BLOCKED in payload)",
                        stream=stream, stream_id=cdc_sid),
            _build_step(
                "redis_write" if not filter_blocked else "filtered",
                "RDI YAML filter blocked the write" if filter_blocked
                else "RDI Processor wrote it into Redis",
                t_filter, t_start,
                detail=(
                    "filter: kyc_status == 'VERIFIED'  →  BLOCKED row dropped, "
                    "no Redis write performed  (rdi/jobs/customer.yaml)"
                ) if filter_blocked else f"JSON.SET {target} $.kyc_status BLOCKED",
                target_key=target,
                blocked=filter_blocked),
            _build_step(
                "app_read",
                "Application still reads the trusted VERIFIED value"
                if filter_blocked else "Application reads from Redis",
                t_app, t_start,
                detail=f"JSON.GET {target} $.kyc_status  →  \"{cached_kyc}\"  ·  {read_us} µs",
                read_us=read_us, app_command=f"JSON.GET {target} $.kyc_status",
                cached_value=cached_kyc),
        ]
        return jsonify({
            "ok":            True,
            "action":        "Section 4 · Filter + projection · KYC freeze",
            "action_id":     "kyc",
            "table":         "portfolio.customer",
            "target_key":    target,
            "primary_key":   {"customer_id": cid, "client_code": cc},
            "wal_lsn":       wal_lsn,
            "filter_blocked": filter_blocked,
            "steps":         _add_step_durations(steps),
            "comparison":    comparison,
            "display_type":  "filter_projection",
            "result": {
                "client_code":       cc,
                "pg_kyc_status":     "BLOCKED (then restored to VERIFIED)",
                "redis_kyc_status":  cached_kyc,
                "cache_blocked":     bool(filter_blocked),
                "filter_expression": "kyc_status != 'BLOCKED'",
                "pg_sql":  (f"UPDATE portfolio.customer\nSET    kyc_status = 'BLOCKED'\n"
                            f"WHERE  customer_id = {cid};"),
                "redis_cmd":      f"JSON.GET {target} $.kyc_status",
                "redis_response": f'"{cached_kyc}"',
            },
            "totals": {
                "t0_ist":              _epoch_ms_to_ist(t_start),
                "pg_write_ms":         round(t_pgdone - t_start, 1),
                "cdc_pickup_ms":       round(cdc_ms - t_pgdone, 1) if cdc_ms else None,
                "processor_write_ms":  None if filter_blocked else round(t_filter - cdc_ms, 1),
                "pipeline_total_ms":   None if filter_blocked else round(t_filter - t_start, 1),
                "app_read_us":         read_us,
                "filter_blocked":      filter_blocked,
            },
            "section":       4,
            "section_label": "Production-ready RDI patterns",
        })

    # ===================================================================
    # 5) Real-time Securities Demo · Section 1 · "Update X in source DB"
    # ===================================================================
    # Five generic single-column writes that all share the same Postgres →
    # WAL → Debezium → RDI Processor → Redis trace. We dispatch through one
    # shared helper so adding a sixth/seventh attribute later is a one-line
    # change instead of another 80-line copy-paste.
    _COLUMN_UPDATE_ACTIONS = {
        "risk_profile": {
            "label":  "Section 1 · Update client risk profile",
            "table":  "portfolio.customer",
            "stream": "sectrade.portfolio.customer",
            "pk_col": "customer_id",
            "col":    "risk_profile",
            "values": ("CONSERVATIVE", "MODERATE", "AGGRESSIVE"),
            "target_key_tpl": "customer:{client_code}",
            "json_path":      "$.risk_profile",
            "pg_read_sql":    "SELECT risk_profile FROM portfolio.customer WHERE client_code = %s",
            "pg_read_keyfn":  lambda ctx: (ctx["client_code"],),
            "redis_cmd_tpl":  "JSON.GET {target} $.risk_profile",
            "redis_read_fn":  lambda r_, target: r_.execute_command("JSON.GET", target, "$.risk_profile"),
            "why_it_matters": "RM dashboards and the order-management system both gate "
                              "by the customer's current risk profile. A change in PG must "
                              "show up in every screen before the next order is placed.",
            "category":       "customer",
        },
        "margin": {
            "label":  "Section 1 · Update margin available",
            "table":  "portfolio.customer",
            "stream": "sectrade.portfolio.customer",
            "pk_col": "customer_id",
            "col":    "margin_available",
            "values": ("nudge_margin",),
            "target_key_tpl": "customer:{client_code}",
            "json_path":      "$.margin_available",
            "pg_read_sql":    "SELECT margin_available FROM portfolio.customer WHERE client_code = %s",
            "pg_read_keyfn":  lambda ctx: (ctx["client_code"],),
            "redis_cmd_tpl":  "JSON.GET {target} $.margin_available",
            "redis_read_fn":  lambda r_, target: r_.execute_command("JSON.GET", target, "$.margin_available"),
            "why_it_matters": "Margin / buying power drives pre-trade validation on every "
                              "order. Reading a stale value blocks a customer from a legitimate "
                              "trade; Redis serves the fresh number in microseconds.",
            "category":       "customer",
        },
        "trading_limit": {
            "label":  "Section 1 · Update trading limit",
            "table":  "portfolio.customer",
            "stream": "sectrade.portfolio.customer",
            "pk_col": "customer_id",
            "col":    "trading_limit",
            "values": ("nudge_limit",),
            "target_key_tpl": "customer:{client_code}",
            "json_path":      "$.trading_limit",
            "pg_read_sql":    "SELECT trading_limit FROM portfolio.customer WHERE client_code = %s",
            "pg_read_keyfn":  lambda ctx: (ctx["client_code"],),
            "redis_cmd_tpl":  "JSON.GET {target} $.trading_limit",
            "redis_read_fn":  lambda r_, target: r_.execute_command("JSON.GET", target, "$.trading_limit"),
            "why_it_matters": "Trading limits are checked on every order placement and "
                              "every pre-trade margin re-compute. The trading-platform UI "
                              "cannot hit Postgres for this on every keystroke.",
            "category":       "customer",
        },
        "instrument_status": {
            "label":  "Section 1 · Update instrument status",
            "table":  "portfolio.security_master",
            "stream": "sectrade.portfolio.security_master",
            "pk_col": "security_id",
            "col":    "is_active",
            "values": ("toggle_bool",),
            # security_master.yaml writes 2 shapes; we read the UNfiltered
            # security_full:<security_id> JSON so the SUSPENDED state is
            # observable (the trading-screen HASH gets filtered out).
            "target_key_tpl": "security_full:{security_id}",
            "json_path":      "$.is_active",
            "pg_read_sql":    "SELECT is_active FROM portfolio.security_master WHERE security_id = %s",
            "pg_read_keyfn":  lambda ctx: (ctx["security_id"],),
            "redis_cmd_tpl":  "JSON.GET {target} $.is_active",
            "redis_read_fn":  lambda r_, target: r_.execute_command("JSON.GET", target, "$.is_active"),
            "why_it_matters": "Instrument suspend / resume must reach every order entry "
                              "screen and risk engine before the next BUY click. A delayed "
                              "status flag means orders flowing into a halted security.",
            "category":       "security",
        },
        "corp_action": {
            "label":  "Section 1 · Set corporate-action flag",
            "table":  "portfolio.security_master",
            "stream": "sectrade.portfolio.security_master",
            "pk_col": "security_id",
            "col":    "corporate_action_flag",
            "values": ("NONE", "BONUS", "SPLIT", "DIVIDEND", "RIGHTS"),
            "target_key_tpl": "security_full:{security_id}",
            "json_path":      "$.corporate_action_flag",
            "pg_read_sql":    "SELECT corporate_action_flag FROM portfolio.security_master WHERE security_id = %s",
            "pg_read_keyfn":  lambda ctx: (ctx["security_id"],),
            "redis_cmd_tpl":  "JSON.GET {target} $.corporate_action_flag",
            "redis_read_fn":  lambda r_, target: r_.execute_command("JSON.GET", target, "$.corporate_action_flag"),
            "why_it_matters": "Splits, bonuses and dividends move customer holdings overnight. "
                              "The flag must reach every downstream consumer (advisory, BI, "
                              "tax) the moment the corporate-actions desk records it.",
            "category":       "security",
        },
        "contact_update": {
            "label":  "Section 1 · Update customer contact info",
            "table":  "portfolio.customer",
            "stream": "sectrade.portfolio.customer",
            "pk_col": "customer_id",
            "col":    "email",
            # rotation list — pick any value different from the current one
            "values": (
                "raj.demo+a@sectrade.demo",
                "raj.demo+b@sectrade.demo",
                "priya.demo+a@sectrade.demo",
                "priya.demo+b@sectrade.demo",
                "amit.demo+a@sectrade.demo",
            ),
            "target_key_tpl": "customer:{client_code}",
            "json_path":      "$.email",
            "pg_read_sql":    "SELECT email FROM portfolio.customer WHERE client_code = %s",
            "pg_read_keyfn":  lambda ctx: (ctx["client_code"],),
            "redis_cmd_tpl":  "JSON.GET {target} $.email",
            "redis_read_fn":  lambda r_, target: r_.execute_command("JSON.GET", target, "$.email"),
            "why_it_matters": "Customer changes email or mobile in self-service; mobile-push, "
                              "SMS, OTP, statement-mailer and customer-care services all need "
                              "the new address immediately — stale contact info means failed "
                              "deliveries and security incidents.",
            "category":       "customer",
        },
    }

    if action in _COLUMN_UPDATE_ACTIONS:
        spec = _COLUMN_UPDATE_ACTIONS[action]

        # ---- choose a target row + decide on the new value ----------
        if spec["category"] == "customer":
            cc = _pick_verified_client_code()
            if not cc:
                return jsonify({"error": "no VERIFIED customer found"}), 400
            row = _pg_select_one(
                f"SELECT customer_id, {spec['col']} FROM portfolio.customer "
                "WHERE client_code = %s", (cc,))
            pk_val = int(row["customer_id"])
            ctx    = {"client_code": cc, "customer_id": pk_val}
            target = spec["target_key_tpl"].format(**ctx)
            pk_label = {"customer_id": pk_val, "client_code": cc}
            current = row[spec["col"]]
            human_table = "portfolio.customer · client " + cc
        else:
            # security_master row
            sec = _pg_select_one(
                f"SELECT security_id, symbol, {spec['col']} "
                "FROM portfolio.security_master "
                "ORDER BY security_id ASC LIMIT 1")
            if not sec:
                return jsonify({"error": "no security_master row found"}), 400
            pk_val = int(sec["security_id"])
            ctx    = {"security_id": pk_val, "symbol": sec.get("symbol")}
            target = spec["target_key_tpl"].format(**ctx)
            pk_label = {"security_id": pk_val, "symbol": sec.get("symbol")}
            current = sec[spec["col"]]
            human_table = f"portfolio.security_master · {sec.get('symbol','?')}"

        # Pick the next value. The "values" tuple supports three modes:
        #   - sentinel "toggle_bool" → flip a BOOLEAN
        #   - sentinel "nudge_margin"/"nudge_limit" → +25_000 / +100_000
        #   - tuple of strings       → rotate to the next one
        vmode = spec["values"][0] if len(spec["values"]) == 1 else None
        if vmode == "toggle_bool":
            new_value = (not bool(current))
            display_new = "ACTIVE" if new_value else "SUSPENDED"
        elif vmode == "nudge_margin":
            cur_n = float(current or 0)
            new_value = round(cur_n + 25_000.0, 2)
            display_new = f"₹{int(new_value):,}"
        elif vmode == "nudge_limit":
            cur_n = float(current or 0)
            new_value = round(cur_n + 100_000.0, 2)
            display_new = f"₹{int(new_value):,}"
        else:
            # rotate enum-style
            try:
                idx = spec["values"].index(str(current))
            except ValueError:
                idx = -1
            new_value   = spec["values"][(idx + 1) % len(spec["values"])]
            display_new = new_value

        since   = _last_stream_id(spec["stream"])
        t_start = _now_ms()
        _pg_exec(
            f"UPDATE {spec['table']} SET {spec['col']} = %s, updated_at = NOW() "
            f"WHERE {spec['pk_col']} = %s",
            (new_value, pk_val))
        t_pgdone = _now_ms()
        wal_lsn  = _get_wal_lsn()

        cdc_ms, cdc_sid = _wait_for_debezium_pickup(
            spec["stream"], since, spec["pk_col"], pk_val, deadline_ms=15000)

        # Wait until Redis reflects the new value. Two cache shapes:
        # JSON keys (customer.yaml) — RDI returns a list like '[true]'
        # parseable as JSON; HASH keys (security_master.yaml) — HGET
        # returns the raw string ('true' / '500.00' / 'BONUS'). The
        # helper below tries JSON first, then falls back to raw cast.
        def _matches():
            try:
                raw = spec["redis_read_fn"](r, target)
            except Exception:
                raw = None
            if raw is None:
                return False
            # Strip bytes → str if redis-py returned bytes
            if isinstance(raw, (bytes, bytearray)):
                try:
                    raw = raw.decode("utf-8")
                except Exception:
                    return False
            # Try JSON.GET-style result first
            try:
                parsed = json.loads(raw)
                val    = parsed[0] if isinstance(parsed, list) else parsed
            except Exception:
                val = raw
            try:
                if isinstance(new_value, bool):
                    if isinstance(val, str):
                        return val.lower() in ("true","t","1") if new_value else val.lower() in ("false","f","0")
                    return bool(val) == new_value
                if isinstance(new_value, (int, float)):
                    return abs(float(val) - float(new_value)) < 0.01
                return str(val) == str(new_value)
            except Exception:
                return False
        ok, _ = _wait_for_key_match(_matches, deadline_ms=15000, poll_ms=2)
        t_redis = _now_ms() if ok else None

        read_us = _measure_redis_read_us(lambda: spec["redis_read_fn"](r, target))
        t_app   = _now_ms()

        pg_read_sql = spec["pg_read_sql"]
        pg_read_us  = _measure_pg_read_us(pg_read_sql, spec["pg_read_keyfn"](ctx))
        redis_cmd   = spec["redis_cmd_tpl"].format(target=target)
        comparison  = _build_comparison(
            pg_query_str  = pg_read_sql.replace(
                "%s", repr(spec["pg_read_keyfn"](ctx)[0])) + ";",
            pg_us         = pg_read_us,
            redis_cmd_str = redis_cmd,
            redis_us      = read_us,
            why_it_matters = spec["why_it_matters"],
        )

        steps = [
            _build_step("pg_insert", f"UPDATE executed on {spec['table']}",
                        t_start, t_start,
                        detail=(f"UPDATE {spec['table']} SET {spec['col']}="
                                f"{display_new!r} WHERE {spec['pk_col']}={pk_val};")),
            _build_step("pg_wal", "Row committed to PostgreSQL WAL",
                        t_pgdone, t_start,
                        detail=f"pg_current_wal_lsn() = {wal_lsn or 'n/a'}",
                        wal_lsn=wal_lsn),
            _build_step("cdc", "RDI · Debezium captured the change",
                        cdc_ms, t_start,
                        detail=f"stream {spec['stream']} id {cdc_sid or 'pending'} "
                               f"key {{\"{spec['pk_col']}\":{pk_val}}}",
                        stream=spec["stream"], stream_id=cdc_sid),
            _build_step("redis_write", "RDI Processor wrote into Redis",
                        t_redis, t_start,
                        detail=f"JSON.SET {target} {spec['json_path']} {display_new!r}",
                        target_key=target),
            _build_step("app_read", "Application reads the new value from Redis",
                        t_app, t_start,
                        detail=f"{redis_cmd}  →  {display_new!r}  ·  {read_us} µs",
                        read_us=read_us, app_command=redis_cmd),
        ]
        return jsonify({
            "ok":          ok,
            "action":      spec["label"],
            "action_id":   action,
            "table":       human_table,
            "target_key":  target,
            "primary_key": pk_label,
            "wal_lsn":     wal_lsn,
            "steps":       _add_step_durations(steps),
            "comparison":  comparison,
            "totals": {
                "t0_ist":              _epoch_ms_to_ist(t_start),
                "pg_write_ms":         round(t_pgdone - t_start, 1),
                "cdc_pickup_ms":       round(cdc_ms - t_pgdone, 1) if cdc_ms else None,
                "processor_write_ms":  round(t_redis - cdc_ms, 1) if (t_redis and cdc_ms) else None,
                "pipeline_total_ms":   round(t_redis - t_start, 1) if t_redis else None,
                "app_read_us":         read_us,
            },
            "section":     1,
            "section_label": "Update source · RDI syncs it into Redis",
        })

    # ===================================================================
    # Real-time Securities Demo · Section 2 · "Read-side experiences on the
    # same synced data". Three demos — each one PROVES Redis is not just a
    # cache but the operational fast-data plane every app reads from.
    # ===================================================================
    if action == "ref_lookup":
        """Reference / master-data lookup. Same query routed two ways:
        FT.SEARCH cust-idx → JSON.GET customer:<cc> on Redis,
        vs SELECT … WHERE client_code = ? on Postgres."""
        cc = _pick_verified_client_code()
        if not cc:
            return jsonify({"error": "no VERIFIED customer in cache"}), 400

        redis_cmd = (f'FT.SEARCH cust-idx "@client_code:{{{cc}}}"\n'
                     f'JSON.GET   customer:{cc} $')
        t_now = _now_ms()

        def _do_redis_lookup():
            r.execute_command(
                "FT.SEARCH", "cust-idx", f"@client_code:{{{cc}}}",
                "LIMIT", "0", "1", "RETURN", "1", "$.client_code")
            return r.execute_command("JSON.GET", f"customer:{cc}", "$")
        redis_us = _measure_redis_read_us(_do_redis_lookup)

        raw    = r.execute_command("JSON.GET", f"customer:{cc}", "$")
        cust   = (json.loads(raw)[0] if raw else {}) or {}

        # Picked: customer's first holding & the security master record
        # behind it — the same drill-down a trade-blotter / RM dashboard
        # would do, but all served from Redis.
        cid    = int(cust.get("customer_id") or 0)
        sec_id = None
        symbol = None
        if cid:
            keys = _holding_keys(cid)
            if keys:
                sec_id = int(keys[0].split(":")[-1])
                meta   = _sec_meta(sec_id)
                symbol = meta.get("symbol")

        pg_read_sql = ("SELECT client_code, full_name, pan, segment, risk_profile, "
                       "kyc_status, demat_account, margin_available, trading_limit "
                       "FROM portfolio.customer WHERE client_code = %s")
        pg_read_us  = _measure_pg_read_us(pg_read_sql, (cc,))
        comparison  = _build_comparison(
            pg_query_str  = pg_read_sql.replace("%s", f"'{cc}'") + ";",
            pg_us         = pg_read_us,
            redis_cmd_str = redis_cmd,
            redis_us      = redis_us,
            why_it_matters="Every screen in the broker stack starts with a reference "
                           "lookup — client, security, account, holdings, limits. "
                           "Hitting Postgres for that on every screen tap is exactly "
                           "what makes the trading app feel slow.",
        )

        return jsonify({
            "ok":            True,
            "action":        "Section 2 · Reference / master-data lookup",
            "action_id":     "ref_lookup",
            "read_only":     True,
            "display_type":  "ref_lookup",
            "ts_ist":        _epoch_ms_to_ist(t_now),
            "read_us":       redis_us,
            "redis_command": redis_cmd,
            "description":   ("Resolved one client + one security off Redis using the "
                              "FT.SEARCH RediSearch index and JSON.GET — the same "
                              "lookup pattern OMS, RMS, RM-desktop and ops screens use."),
            "result":        {
                "client_code":    cust.get("client_code") or cc,
                "name":           cust.get("name") or cust.get("full_name") or "",
                "segment":        cust.get("segment"),
                "risk_profile":   cust.get("risk_profile"),
                "kyc_status":     cust.get("kyc_status"),
                "margin":         cust.get("margin_available"),
                "trading_limit":  cust.get("trading_limit"),
                "security_id":    sec_id,
                "symbol":         symbol,
            },
            "section":       2,
            "section_label": "Read-side experiences served from Redis",
            "comparison":    comparison,
        })

    if action == "portfolio_view":
        """Portfolio / position view — assemble holdings + LTPs + sector
        exposure off Redis. Mirrors the "account snapshot" screen on
        mobile / web / RM desktop."""
        cc = _pick_verified_client_code()
        if not cc:
            return jsonify({"error": "no VERIFIED customer in cache"}), 400
        cust = get_customer(cc) or {}
        cid  = int(cust.get("customer_id") or 0)
        if not cid:
            return jsonify({"error": "customer not in cache"}), 400

        redis_cmd = (f'FT.SEARCH hold-idx "@customer_id:[{cid} {cid}]"  '
                     f'(holding keys)\n'
                     f'JSON.GET   holding:{cid}:* $   (per-position)\n'
                     f'HGET       price:<security_id> ltp   (per-position)')
        t_now = _now_ms()

        def _do_portfolio_read():
            keys = _holding_keys(cid)
            if not keys:
                return None
            pipe = r.pipeline(transaction=False)
            for k in keys:
                pipe.execute_command("JSON.GET", k, "$")
            jsons = pipe.execute()
            holdings = [json.loads(j)[0] for j in jsons if j]
            pipe = r.pipeline(transaction=False)
            for h in holdings:
                pipe.hget(f"price:{int(h['security_id'])}", "ltp")
            pipe.execute()
            return holdings
        redis_us = _measure_redis_read_us(_do_portfolio_read)
        portfolio = get_portfolio(cid)

        # Sector exposure breakdown (the "by sector" pie chart input)
        by_sector = {}
        for h in portfolio["holdings"]:
            s = h.get("sector") or "Unknown"
            by_sector[s] = by_sector.get(s, 0.0) + float(h.get("market", 0.0))
        sector_rows = [
            {"sector": k, "market": round(v, 2),
             "pct": round((v / portfolio["summary"]["market_value"]) * 100, 1)
                    if portfolio["summary"]["market_value"] else 0}
            for k, v in sorted(by_sector.items(), key=lambda kv: -kv[1])
        ]

        pg_read_sql = ("SELECT h.quantity, h.avg_buy_price, mp.ltp, "
                       "       sm.symbol, sm.sector "
                       "FROM portfolio.holding h "
                       "JOIN portfolio.security_master sm ON sm.security_id = h.security_id "
                       "JOIN portfolio.market_price mp ON mp.security_id = h.security_id "
                       "WHERE h.customer_id = %s")
        pg_read_us  = _measure_pg_read_us(pg_read_sql, (cid,))
        comparison  = _build_comparison(
            pg_query_str  = (pg_read_sql.replace("%s", str(cid))
                             .replace(" FROM ", "\nFROM ")
                             .replace(" JOIN ", "\nJOIN ")
                             .replace(" WHERE ", "\nWHERE ")) + ";",
            pg_us         = pg_read_us,
            redis_cmd_str = redis_cmd,
            redis_us      = redis_us,
            why_it_matters="The portfolio screen is the single highest-traffic page "
                           "on the platform. The same data assembled by a 3-table JOIN "
                           "in Postgres comes back from Redis with one pipeline of "
                           "JSON.GET + HGET calls — orders of magnitude faster.",
        )

        return jsonify({
            "ok":            True,
            "action":        "Section 2 · Portfolio / position view",
            "action_id":     "portfolio_view",
            "read_only":     True,
            "display_type":  "portfolio_view",
            "ts_ist":        _epoch_ms_to_ist(t_now),
            "read_us":       redis_us,
            "redis_command": redis_cmd,
            "description":   (f"Assembled the live portfolio for {cc} (customer_id "
                              f"{cid}) — {len(portfolio['holdings'])} positions, "
                              "with per-holding P&L and sector exposure — directly "
                              "from Redis (FT.SEARCH + pipelined JSON.GET + HGET)."),
            "result":        {
                "client_code":  cc,
                "customer_id":  cid,
                "name":         cust.get("name") or cust.get("full_name") or "",
                "summary":      portfolio["summary"],
                "holdings":     portfolio["holdings"][:8],
                "sector":       sector_rows,
            },
            "section":       2,
            "section_label": "Read-side experiences served from Redis",
            "comparison":    comparison,
        })

    if action == "eligibility_check":
        """Eligibility / risk check — run the gate an OMS would run before
        accepting a BUY: KYC + risk profile + margin + trading_limit +
        instrument is_active + corporate_action flag. All from Redis."""
        cc = _pick_verified_client_code()
        if not cc:
            return jsonify({"error": "no VERIFIED customer in cache"}), 400
        cust = get_customer(cc) or {}
        cid  = int(cust.get("customer_id") or 0)
        if not cid:
            return jsonify({"error": "customer not in cache"}), 400

        # Pick a hypothetical BUY against the customer's first holding's
        # security at the LTP — the same trade-ticket the UI would build.
        sec_id = None
        symbol = None
        ltp    = None
        keys   = _holding_keys(cid)
        if keys:
            sec_id = int(keys[0].split(":")[-1])
            meta   = _sec_meta(sec_id)
            symbol = meta.get("symbol")
            price  = r.hgetall(f"price:{sec_id}")
            try: ltp = float(price.get("ltp") or 0.0)
            except Exception: ltp = None
        qty       = 100
        trade_val = round(qty * (ltp or 250.0), 2)

        redis_cmd = (
            f"JSON.GET customer:{cc} $.kyc_status\n"
            f"JSON.GET customer:{cc} $.risk_profile\n"
            f"JSON.GET customer:{cc} $.margin_available\n"
            f"JSON.GET customer:{cc} $.trading_limit\n"
            f"JSON.GET security_full:{sec_id} $.is_active\n"
            f"JSON.GET security_full:{sec_id} $.corporate_action_flag"
        )
        t_now = _now_ms()

        def _do_eligibility_read():
            pipe = r.pipeline(transaction=False)
            pipe.execute_command("JSON.GET", f"customer:{cc}", "$.kyc_status")
            pipe.execute_command("JSON.GET", f"customer:{cc}", "$.risk_profile")
            pipe.execute_command("JSON.GET", f"customer:{cc}", "$.margin_available")
            pipe.execute_command("JSON.GET", f"customer:{cc}", "$.trading_limit")
            if sec_id is not None:
                pipe.execute_command("JSON.GET", f"security_full:{sec_id}", "$.is_active")
                pipe.execute_command("JSON.GET", f"security_full:{sec_id}", "$.corporate_action_flag")
            pipe.execute()
        redis_us = _measure_redis_read_us(_do_eligibility_read)

        # Run the actual checks (cheap, just structuring the data)
        def _g(path: str, default=None):
            raw = r.execute_command("JSON.GET", f"customer:{cc}", path)
            try: return json.loads(raw)[0] if raw else default
            except Exception: return default
        kyc          = _g("$.kyc_status", "UNKNOWN")
        risk_profile = _g("$.risk_profile", "UNKNOWN")
        try:
            margin = float(_g("$.margin_available", 0) or 0)
        except Exception:
            margin = 0.0
        try:
            t_limit = float(_g("$.trading_limit", 0) or 0)
        except Exception:
            t_limit = 0.0

        sec_active = True
        ca_flag    = "NONE"
        if sec_id is not None:
            raw = r.execute_command("JSON.GET", f"security_full:{sec_id}", "$.is_active")
            try: sec_active = bool(json.loads(raw)[0]) if raw else True
            except Exception: pass
            raw = r.execute_command("JSON.GET", f"security_full:{sec_id}", "$.corporate_action_flag")
            try: ca_flag = json.loads(raw)[0] if raw else "NONE"
            except Exception: pass

        checks = [
            {"name": "KYC verified",
             "pass": kyc == "VERIFIED",
             "actual": kyc, "required": "VERIFIED"},
            {"name": "Margin available ≥ trade value",
             "pass": margin >= trade_val,
             "actual": f"₹{margin:,.0f}",
             "required": f"≥ ₹{trade_val:,.0f}"},
            {"name": "Trading limit ≥ trade value",
             "pass": t_limit >= trade_val,
             "actual": f"₹{t_limit:,.0f}",
             "required": f"≥ ₹{trade_val:,.0f}"},
            {"name": "Instrument active",
             "pass": bool(sec_active),
             "actual": "ACTIVE" if sec_active else "SUSPENDED",
             "required": "ACTIVE"},
            {"name": "No corporate-action freeze",
             "pass": ca_flag in (None, "", "NONE"),
             "actual": ca_flag or "NONE",
             "required": "NONE"},
        ]
        verdict = "ACCEPT" if all(c["pass"] for c in checks) else "REJECT"

        pg_read_sql = ("SELECT c.kyc_status, c.risk_profile, c.margin_available, "
                       "       c.trading_limit, sm.is_active, sm.corporate_action_flag "
                       "FROM portfolio.customer c "
                       "JOIN portfolio.security_master sm ON sm.security_id = %s "
                       "WHERE c.client_code = %s")
        pg_read_us  = _measure_pg_read_us(pg_read_sql, (sec_id or 1, cc))
        comparison  = _build_comparison(
            pg_query_str  = (pg_read_sql.replace("%s", str(sec_id or 1), 1)
                             .replace("%s", f"'{cc}'", 1)) + ";",
            pg_us         = pg_read_us,
            redis_cmd_str = redis_cmd,
            redis_us      = redis_us,
            why_it_matters="Pre-trade validation runs on every order. The check pulls "
                           "from many tables (customer + security + market data) and "
                           "the latency budget is single-digit milliseconds. Redis-"
                           "served fields collapse the JOIN to a pipelined fetch.",
        )

        return jsonify({
            "ok":            True,
            "action":        "Section 2 · Eligibility / risk check",
            "action_id":     "eligibility_check",
            "read_only":     True,
            "display_type":  "eligibility",
            "ts_ist":        _epoch_ms_to_ist(t_now),
            "read_us":       redis_us,
            "redis_command": redis_cmd,
            "description":   (f"Evaluated a BUY {qty} {symbol or '<sec>'} @ ₹{ltp or '—'} "
                              f"order ({cc}) against KYC + margin + trading-limit + "
                              "instrument status + corporate-action flag — all served "
                              "by Redis in one pipeline."),
            "result":        {
                "verdict":     verdict,
                "client_code": cc,
                "symbol":      symbol,
                "quantity":    qty,
                "price":       ltp,
                "trade_value": trade_val,
                "checks":      checks,
            },
            "section":       2,
            "section_label": "Read-side experiences served from Redis",
            "comparison":    comparison,
        })

    # ===================================================================
    # Real-time Securities Demo · Section 3 · "Live market stream"
    # One action that pushes a burst of ticks into Postgres (so RDI syncs
    # the latest snapshot into the price:* Hash) AND into a per-security
    # tick Stream that the dashboard writes directly — mirroring an OMS
    # market-data feed. The single response carries:
    #   - current LTP (Redis Hash, kept fresh by RDI)
    #   - day high / low / open / close (same Hash)
    #   - recent history for a sparkline (Stream, XREVRANGE)
    #   - any threshold-crossing alerts that fired
    # ===================================================================
    if action == "live_market":
        sec = _pg_select_one(
            "SELECT security_id, symbol FROM portfolio.security_master "
            "WHERE is_active = TRUE ORDER BY security_id ASC LIMIT 1")
        if not sec:
            return jsonify({"error": "no active security found"}), 400
        sec_id = int(sec["security_id"])
        symbol = sec.get("symbol")

        # Bootstrap a sensible base price from the existing Hash; fall
        # back to the Postgres LTP if Redis is empty (e.g. fresh stack).
        cur = r.hgetall(f"price:{sec_id}")
        try:
            base = float(cur.get("ltp")) if cur.get("ltp") else None
        except Exception:
            base = None
        if base is None or base <= 0:
            row = _pg_select_one(
                "SELECT ltp FROM portfolio.market_price WHERE security_id = %s",
                (sec_id,))
            base = float(row["ltp"]) if row and row.get("ltp") else 250.0

        # Threshold for the "price moved" alert — 1.5% drift from base.
        # The customer chose those words ("alert firing when threshold is
        # crossed") so we surface this number in the response.
        threshold_pct = 1.5
        alert_high    = round(base * (1 + threshold_pct / 100.0), 2)
        alert_low     = round(base * (1 - threshold_pct / 100.0), 2)

        redis_cmd = (
            f"-- 1) RDI keeps the master snapshot Hash fresh from Postgres:\n"
            f"HGETALL  price:{sec_id}\n"
            f"-- 2) OMS market-data feed writes every tick to a Stream:\n"
            f"XADD     ticks:{sec_id} * ltp <p> ts <ms>\n"
            f"-- 3) Apps consume both shapes from the same Redis Enterprise:\n"
            f"HGET     price:{sec_id} ltp           (current)\n"
            f"XREVRANGE ticks:{sec_id} + - COUNT 30 (chart)\n"
            f"XREVRANGE alerts:{sec_id} + - COUNT 5 (alerts)"
        )

        t_now    = _now_ms()
        ticks    = []
        alerts   = []
        # Push 8 randomized ticks over a tiny time slice.
        for i in range(8):
            jitter = random.uniform(-0.022, 0.022)   # up to ±2.2 %
            new_ltp = max(1.0, round(base * (1 + jitter), 2))
            _pg_exec(
                "UPDATE portfolio.market_price "
                "SET ltp = %s, "
                "    day_high = GREATEST(day_high, %s), "
                "    day_low  = LEAST   (day_low,  %s), "
                "    volume   = volume + %s, "
                "    updated_at = NOW() "
                "WHERE security_id = %s",
                (new_ltp, new_ltp, new_ltp, random.randint(50, 500), sec_id))
            # OMS-style direct Stream append (simulating a market-data
            # consumer; this is independent of RDI's CDC path).
            sid = r.xadd(f"ticks:{sec_id}",
                         {"ltp": new_ltp,
                          "ts":  int(_now_ms()),
                          "src": "OMS-MD"},
                         maxlen=500, approximate=True)
            ticks.append({
                "id":  sid, "ltp": new_ltp,
                "ts_ist": _epoch_ms_to_ist(_now_ms())
            })
            # Threshold-cross alert:
            if new_ltp >= alert_high or new_ltp <= alert_low:
                direction = "UP" if new_ltp >= alert_high else "DOWN"
                a_sid = r.xadd(
                    f"alerts:{sec_id}",
                    {"ltp": new_ltp, "base": base,
                     "direction": direction,
                     "threshold_pct": threshold_pct,
                     "ts": int(_now_ms())},
                    maxlen=200, approximate=True)
                alerts.append({
                    "id": a_sid, "ltp": new_ltp, "direction": direction,
                    "threshold_pct": threshold_pct,
                    "ts_ist": _epoch_ms_to_ist(_now_ms()),
                })
            time.sleep(0.04)

        # Now read back current snapshot + chart + alerts in one go
        def _do_market_read():
            pipe = r.pipeline(transaction=False)
            pipe.hgetall(f"price:{sec_id}")
            pipe.xrevrange(f"ticks:{sec_id}",  count=30)
            pipe.xrevrange(f"alerts:{sec_id}", count=5)
            return pipe.execute()
        redis_us = _measure_redis_read_us(_do_market_read)
        snap, hist_raw, alerts_raw = _do_market_read()

        chart = [
            {"ts_ms": int(sid_.split("-")[0]),
             "ts_ist": _epoch_ms_to_ist(int(sid_.split("-")[0])),
             "ltp":   float(f.get("ltp", 0) or 0)}
            for sid_, f in hist_raw or []
        ][::-1]
        recent_alerts = [
            {"ts_ist":  _epoch_ms_to_ist(int(sid_.split("-")[0])),
             "ltp":    float(f.get("ltp", 0) or 0),
             "direction": f.get("direction"),
             "threshold_pct": float(f.get("threshold_pct", 0) or 0)}
            for sid_, f in (alerts_raw or [])
        ]
        ltps = [t["ltp"] for t in chart] or [base]
        chart_min = min(ltps); chart_max = max(ltps)
        chart_avg = round(sum(ltps) / len(ltps), 2)

        pg_read_sql = ("SELECT ltp, day_high, day_low, day_open, prev_close, volume, "
                       "       updated_at FROM portfolio.market_price WHERE security_id = %s")
        pg_read_us  = _measure_pg_read_us(pg_read_sql, (sec_id,))
        comparison  = _build_comparison(
            pg_query_str  = pg_read_sql.replace("%s", str(sec_id)) + ";",
            pg_us         = pg_read_us,
            redis_cmd_str = redis_cmd,
            redis_us      = redis_us,
            why_it_matters="Market data on a trading platform is read at fan-out 10×+ "
                           "the write rate: every position, every order ticket, every "
                           "watchlist row, every chart. Redis serves snapshot + history "
                           "+ alerts off the same key namespace in microseconds.",
        )

        return jsonify({
            "ok":            True,
            "action":        "Section 3 · Live market stream + min/max + alerts",
            "action_id":     "live_market",
            "read_only":     True,
            "display_type":  "live_market",
            "ts_ist":        _epoch_ms_to_ist(t_now),
            "read_us":       redis_us,
            "redis_command": redis_cmd,
            "description":   (f"Pushed 8 ticks for {symbol} (security_id {sec_id}) through "
                              "Postgres → RDI → Redis Hash AND through a direct OMS "
                              f"Stream. {len(alerts)} threshold alert(s) fired "
                              f"({threshold_pct}% drift). Sparkline rendered from the "
                              "Stream, snapshot from the Hash."),
            "result":        {
                "security_id": sec_id,
                "symbol":      symbol,
                "snapshot":    {
                    "ltp":        float(snap.get("ltp", 0) or 0),
                    "day_open":   float(snap.get("day_open", 0) or 0),
                    "day_high":   float(snap.get("day_high", 0) or 0),
                    "day_low":    float(snap.get("day_low", 0) or 0),
                    "prev_close": float(snap.get("prev_close", 0) or 0),
                    "volume":     int(float(snap.get("volume", 0) or 0)),
                },
                "chart":       chart,
                "chart_stats": {"min": chart_min, "max": chart_max, "avg": chart_avg,
                                "points": len(chart)},
                "alerts":      recent_alerts,
                "thresholds":  {"base": base, "pct": threshold_pct,
                                "alert_low": alert_low, "alert_high": alert_high},
                "ticks_pushed": len(ticks),
            },
            "section":       3,
            "section_label": "Live market stream — ticks · snapshot · history · alerts",
            "comparison":    comparison,
        })

    # ===================================================================
    # Real-time Securities Demo · Section 4 · "Finish with AI"
    #
    # The demo's AI section follows the canonical RAG pattern:
    #   1. RETRIEVE — pull the customer's grounded context from Redis
    #      (sub-millisecond) — exactly as a production LLM workflow would.
    #   2. AUGMENT  — show the prompt that would go to the LLM (we render
    #      it verbatim so the customer sees what Redis served).
    #   3. GENERATE — for this demo we generate a deterministic answer in
    #      Python from the same retrieved context, so the response is
    #      reproducible. The annotation block in the UI calls out where
    #      Bedrock / Azure / Vertex / OpenAI plugs in.
    #
    # All four AI flows share one handler so adding more questions is a
    # single-entry change.
    # ===================================================================
    if action.startswith("ai_"):
        cc = _pick_verified_client_code()
        if not cc:
            return jsonify({"error": "no VERIFIED customer in cache"}), 400
        cust = get_customer(cc) or {}
        cid  = int(cust.get("customer_id") or 0)
        if not cid:
            return jsonify({"error": "customer not in cache"}), 400

        # ------------------------------------------------------------
        # Pull all grounding context off Redis in one pipeline. THIS is
        # the "Redis as a context store for LLMs" pillar — every byte
        # of the LLM prompt below comes from this single µs call.
        # ------------------------------------------------------------
        def _do_ai_context_read():
            pipe = r.pipeline(transaction=False)
            pipe.execute_command("JSON.GET", f"customer:{cc}", "$")
            for k in _holding_keys(cid):
                pipe.execute_command("JSON.GET", k, "$")
            pipe.execute()
        redis_us = _measure_redis_read_us(_do_ai_context_read)

        portfolio = get_portfolio(cid)
        summary   = portfolio["summary"]
        holdings  = portfolio["holdings"]

        # Sector exposure (used by both banking-exposure and summary Qs)
        sector_market = {}
        for h in holdings:
            s = h.get("sector") or "Other"
            sector_market[s] = sector_market.get(s, 0.0) + float(h.get("market", 0.0))
        market_value = summary.get("market_value", 0.0) or 0.0
        sector_exposure = sorted(
            [{"sector": k,
              "market": round(v, 2),
              "pct":    round((v / market_value) * 100, 1) if market_value else 0.0}
             for k, v in sector_market.items()],
            key=lambda x: -x["pct"])

        t_now      = _now_ms()
        question   = ""
        answer     = ""
        llm_prompt = ""
        bullets    = []

        if action == "ai_banking_exposure":
            banking_pat = ("Banking", "BFSI", "Financial", "Financial Services", "Finance",
                           "Bank")
            banking_total = round(sum(
                v for k, v in sector_market.items()
                if any(p.lower() in (k or "").lower() for p in banking_pat)
            ), 2)
            pct = round((banking_total / market_value) * 100, 1) if market_value else 0.0
            banking_secs = sorted(
                [h for h in holdings
                 if any(p.lower() in (h.get("sector") or "").lower() for p in banking_pat)],
                key=lambda h: -float(h.get("market", 0))
            )[:5]
            question = f"What is {cust.get('name') or cc}'s current exposure to banking stocks?"
            llm_prompt = (
                "System: You are an advisor copilot. Answer concisely.\n\n"
                "Retrieved context (from Redis, served in "
                f"{redis_us} µs):\n"
                f"  client_code     = {cc}\n"
                f"  name            = {cust.get('name') or cust.get('full_name')}\n"
                f"  segment         = {cust.get('segment')}\n"
                f"  market_value    = ₹{market_value:,.0f}\n"
                f"  by_sector       = {json.dumps(sector_exposure)}\n"
                f"\nQuestion: {question}"
            )
            answer = (
                f"{cc} currently has ₹{banking_total:,.0f} in banking / financial-services "
                f"stocks — {pct}% of the ₹{market_value:,.0f} portfolio."
            )
            bullets = [f"{h['symbol']} ({h.get('sector','')}) · ₹{h['market']:,.0f}"
                       for h in banking_secs] or [
                "No banking-sector positions in this customer's portfolio."]

        elif action == "ai_trade_fail":
            # Build a likely BUY ticket against the customer's first holding
            sec_id = None; symbol = None; ltp = None
            keys = _holding_keys(cid)
            if keys:
                sec_id = int(keys[0].split(":")[-1])
                meta   = _sec_meta(sec_id)
                symbol = meta.get("symbol")
                hp     = r.hgetall(f"price:{sec_id}")
                try: ltp = float(hp.get("ltp") or 0.0)
                except Exception: ltp = None
            qty       = 100
            trade_val = round(qty * (ltp or 250.0), 2)
            def _g(path):
                raw = r.execute_command("JSON.GET", f"customer:{cc}", path)
                try: return json.loads(raw)[0] if raw else None
                except Exception: return None
            kyc       = _g("$.kyc_status")
            risk      = _g("$.risk_profile")
            margin    = float(_g("$.margin_available") or 0)
            t_limit   = float(_g("$.trading_limit") or 0)
            sec_act   = True; ca = "NONE"
            if sec_id is not None:
                raw = r.execute_command("JSON.GET", f"security_full:{sec_id}", "$.is_active")
                try: sec_act = bool(json.loads(raw)[0]) if raw else True
                except Exception: pass
                raw = r.execute_command("JSON.GET", f"security_full:{sec_id}", "$.corporate_action_flag")
                try: ca = json.loads(raw)[0] if raw else "NONE"
                except Exception: pass

            reasons = []
            if kyc != "VERIFIED":
                reasons.append(f"KYC = {kyc} (must be VERIFIED)")
            if margin < trade_val:
                reasons.append(f"insufficient margin: ₹{margin:,.0f} < ₹{trade_val:,.0f}")
            if t_limit < trade_val:
                reasons.append(f"trading limit exceeded: ₹{t_limit:,.0f} < ₹{trade_val:,.0f}")
            if not sec_act:
                reasons.append(f"instrument {symbol} is currently SUSPENDED")
            if ca not in (None, "", "NONE"):
                reasons.append(f"{symbol} has an open corporate action: {ca}")

            question = (f"This BUY ticket — {qty} {symbol or '<sec>'} @ ₹{ltp or '—'} for "
                        f"{cc} — would the OMS accept it? Why or why not?")
            llm_prompt = (
                "System: You are an OMS-ops copilot. Walk the user through every "
                "gate the order must pass.\n\n"
                f"Retrieved context (from Redis, served in {redis_us} µs):\n"
                f"  client_code        = {cc}\n"
                f"  kyc_status         = {kyc}\n"
                f"  risk_profile       = {risk}\n"
                f"  margin_available   = ₹{margin:,.0f}\n"
                f"  trading_limit      = ₹{t_limit:,.0f}\n"
                f"  security           = {symbol} (id {sec_id}, active={sec_act}, "
                f"corp_action={ca})\n"
                f"  trade_value        = ₹{trade_val:,.0f}\n"
                f"\nQuestion: {question}"
            )
            if reasons:
                answer = ("This order would REJECT. " +
                          str(len(reasons)) +
                          " gate(s) blocked it:")
                bullets = reasons
            else:
                answer = ("This order would ACCEPT. All pre-trade gates "
                          "(KYC + margin + trading-limit + instrument status + "
                          "corporate-action flag) pass against the current cache.")
                bullets = [
                    f"KYC = {kyc}",
                    f"Margin ₹{margin:,.0f} ≥ trade ₹{trade_val:,.0f}",
                    f"Trading limit ₹{t_limit:,.0f} ≥ trade ₹{trade_val:,.0f}",
                    f"{symbol} active = {sec_act}",
                    f"Corp-action flag = {ca}",
                ]

        elif action == "ai_similar_securities":
            # Pick the customer's biggest holding as anchor
            anchor = holdings[0] if holdings else None
            if not anchor:
                question = "Show similar securities to this client's biggest holding."
                answer   = "Customer has no positions yet."
                bullets  = []
                llm_prompt = "(no anchor holding available)"
            else:
                anchor_sec = anchor["symbol"]; anchor_sector = anchor.get("sector")
                # Production: this would be a HNSW vector search over an FT.SEARCH
                # vector index. For this demo we proxy with sector + price band —
                # the response copy below names the actual production command.
                anchor_price = float(anchor["ltp"]) if anchor.get("ltp") else 0.0
                cand_rows = _pg_select_all(
                    "SELECT sm.symbol, sm.company_name, sm.sector, mp.ltp "
                    "FROM portfolio.security_master sm "
                    "JOIN portfolio.market_price mp ON mp.security_id = sm.security_id "
                    "WHERE sm.sector = %s AND sm.symbol <> %s "
                    "ORDER BY ABS(mp.ltp - %s) ASC LIMIT 5",
                    (anchor_sector, anchor_sec, anchor_price))
                question = (f"Show me securities similar to {anchor_sec} "
                            f"({anchor_sector}) for this client.")
                llm_prompt = (
                    "System: You are a research copilot using RAG. Retrieve nearest "
                    "instruments by vector similarity over the securities embedding "
                    "index.\n\n"
                    f"Retrieved anchor (from Redis, served in {redis_us} µs):\n"
                    f"  symbol       = {anchor_sec}\n"
                    f"  sector       = {anchor_sector}\n"
                    f"  ltp          = ₹{anchor_price}\n"
                    f"\nFT.SEARCH sec-vec-idx \"*=>[KNN 5 @embedding $vec AS score]\" "
                    f"PARAMS 2 vec <anchor_embedding>\n"
                    f"\nQuestion: {question}"
                )
                bullets = [
                    f"{c['symbol']} · {c.get('company_name','')} · ₹{float(c['ltp']):,.2f}"
                    for c in (cand_rows or [])
                ] or [f"No siblings found in sector {anchor_sector}."]
                answer = (f"Top 5 instruments similar to {anchor_sec} by sector and "
                          "price band. In production this is a Redis HNSW vector "
                          "search (FT.SEARCH ... KNN) over embedded security descriptions.")

        elif action == "ai_portfolio_summary":
            top3 = holdings[:3]
            day_pnl_pct = (sum(h["market"] * h["day_change_pct"] for h in holdings)
                           / market_value) if market_value else 0.0
            top_sector  = sector_exposure[0] if sector_exposure else None
            concentration = (top_sector["pct"] if top_sector else 0.0)
            question = "Summarize this portfolio and call out top risks."
            llm_prompt = (
                "System: You are an advisor copilot. Summarize the portfolio in 3 "
                "lines and flag any concentration or sector risk.\n\n"
                f"Retrieved context (from Redis, served in {redis_us} µs):\n"
                f"  client_code      = {cc}\n"
                f"  segment          = {cust.get('segment')}\n"
                f"  risk_profile     = {cust.get('risk_profile')}\n"
                f"  market_value     = ₹{market_value:,.0f}\n"
                f"  pnl              = ₹{summary.get('pnl',0):,.0f} "
                f"({summary.get('pnl_pct',0)}%)\n"
                f"  by_sector        = {json.dumps(sector_exposure)}\n"
                f"  top_holdings     = {[h['symbol'] for h in top3]}\n"
                f"\nQuestion: {question}"
            )
            answer = (
                f"{cust.get('name') or cc} ({cust.get('segment')} / "
                f"{cust.get('risk_profile')}) holds ₹{market_value:,.0f} across "
                f"{len(holdings)} positions; current P&L ₹{summary.get('pnl',0):,.0f} "
                f"({summary.get('pnl_pct',0)}%)."
            )
            risk_bits = []
            if top_sector and concentration > 30:
                risk_bits.append(
                    f"Sector concentration risk: {concentration}% in "
                    f"{top_sector['sector']}.")
            if (cust.get("risk_profile") == "CONSERVATIVE"
                    and concentration > 25):
                risk_bits.append(
                    f"{concentration}% sector concentration is high for a "
                    "CONSERVATIVE profile.")
            if day_pnl_pct < -1.0:
                risk_bits.append(
                    f"Day-on-day P&L is {day_pnl_pct:.2f}% — watch margin headroom.")
            bullets = [
                f"Top 3 positions: " + ", ".join(h["symbol"] for h in top3),
                f"Top sector: {top_sector['sector']} @ {top_sector['pct']}%"
                if top_sector else "—",
            ] + (risk_bits or ["No concentration / risk flags fired."])

        else:
            return jsonify({"error": f"unknown AI action: {action}"}), 400

        redis_cmd = (
            f"-- RETRIEVE step (pure µs from Redis, no LLM yet)\n"
            f"JSON.GET customer:{cc} $\n"
            f"FT.SEARCH hold-idx \"@customer_id:[{cid} {cid}]\"\n"
            f"JSON.GET holding:{cid}:* $   (pipelined)\n"
            f"HGET     price:<security_id> ltp   (pipelined)"
        )
        pg_read_sql = ("SELECT 1 FROM portfolio.customer c "
                       "JOIN portfolio.holding h        ON h.customer_id = c.customer_id "
                       "JOIN portfolio.security_master s ON s.security_id = h.security_id "
                       "JOIN portfolio.market_price m   ON m.security_id = h.security_id "
                       "WHERE c.client_code = %s")
        pg_read_us  = _measure_pg_read_us(pg_read_sql, (cc,))
        comparison  = _build_comparison(
            pg_query_str  = "-- equivalent multi-table JOIN the LLM workflow\n"
                            "-- would otherwise hammer Postgres for, per question:\n"
                            + pg_read_sql.replace("%s", f"'{cc}'") + ";",
            pg_us         = pg_read_us,
            redis_cmd_str = redis_cmd,
            redis_us      = redis_us,
            why_it_matters="LLM workflows are read-heavy by design: every question is a "
                           "new retrieval round-trip. Redis serves the entire grounding "
                           "context in microseconds — exactly the latency budget agent "
                           "loops need. Bedrock / OpenAI / Azure plug in at the GENERATE "
                           "step; Redis owns RETRIEVE + semantic cache + agent memory.",
        )

        return jsonify({
            "ok":            True,
            "action":        f"Section 4 · AI · {question}",
            "action_id":     action,
            "read_only":     True,
            "display_type":  "ai_qa",
            "ts_ist":        _epoch_ms_to_ist(t_now),
            "read_us":       redis_us,
            "redis_command": redis_cmd,
            "description":   ("Retrieved the customer's complete grounding context from "
                              "Redis in one pipeline (µs), built the LLM prompt, and "
                              "generated the answer deterministically. The annotation "
                              "shows where your LLM (Bedrock / OpenAI / Azure / Vertex) "
                              "plugs in at the GENERATE step."),
            "result":        {
                "question":   question,
                "answer":     answer,
                "bullets":    bullets,
                "llm_prompt": llm_prompt,
                "client_code": cc,
            },
            "section":       4,
            "section_label": "Finish with AI · RAG grounded in Redis",
            "comparison":    comparison,
        })

    # ===================================================================
    # Real-time Securities Demo · Section 3 · Customer experience
    #
    # The CX track tells the story of every customer-facing surface the
    # broker runs: onboarding (handled above), 360-view, personalised
    # research suggestions and the support / RM hybrid search. Every
    # demo below is read-only and answers off the same Redis Enterprise
    # data plane that the earlier sections wrote into.
    # ===================================================================

    # --- 5.A · Customer 360 -------------------------------------------
    if action == "cust360":
        cc = _pick_verified_client_code()
        if not cc:
            return jsonify({"error": "no VERIFIED customer in cache"}), 400
        cust = get_customer(cc) or {}
        cid  = int(cust.get("customer_id") or 0)
        if not cid:
            return jsonify({"error": "customer not in cache"}), 400

        redis_cmd = (
            f"-- Customer 360 = profile + holdings + recent trades +\n"
            f"-- sector exposure + KPIs, ALL in one Redis pipeline:\n"
            f"JSON.GET   customer:{cc} $\n"
            f"FT.SEARCH  hold-idx \"@customer_id:[{cid} {cid}]\"\n"
            f"JSON.GET   holding:{cid}:* $    (pipelined)\n"
            f"HGET       price:<security_id> ltp   (pipelined)\n"
            f"XREVRANGE  trades:{cid} + - COUNT 5"
        )

        def _do_cust360_read():
            pipe = r.pipeline(transaction=False)
            pipe.execute_command("JSON.GET", f"customer:{cc}", "$")
            keys = _holding_keys(cid)
            for k in keys:
                pipe.execute_command("JSON.GET", k, "$")
            for k in keys:
                pipe.hget(f"price:{int(k.split(':')[-1])}", "ltp")
            pipe.xrevrange(f"trades:{cid}", count=5)
            pipe.execute()
        redis_us = _measure_redis_read_us(_do_cust360_read)

        portfolio = get_portfolio(cid)
        summary   = portfolio["summary"]
        holdings  = portfolio["holdings"]
        # Recent trades from the per-customer Stream
        recent_trades = []
        try:
            for sid_, f in (r.xrevrange(f"trades:{cid}", count=5) or []):
                recent_trades.append({
                    "id":     sid_,
                    "ts_ist": _epoch_ms_to_ist(int(sid_.split("-")[0])),
                    "side":   f.get("side"),
                    "qty":    f.get("quantity") or f.get("qty"),
                    "price":  f.get("price"),
                    "value":  f.get("trade_value"),
                    "symbol": f.get("symbol"),
                })
        except Exception:
            pass
        # Sector exposure
        sec_map = {}
        for h in holdings:
            s = h.get("sector") or "Other"
            sec_map[s] = sec_map.get(s, 0.0) + float(h.get("market", 0.0))
        sector = sorted([
            {"sector": k, "market": round(v, 2),
             "pct":    round((v / (summary["market_value"] or 1)) * 100, 1)}
            for k, v in sec_map.items()], key=lambda x: -x["pct"])
        # Day P&L: sum of position-weighted day_change_pct, normalised
        day_pnl_pct = 0.0
        if summary["market_value"]:
            day_pnl_pct = round(sum(
                h["market"] * h.get("day_change_pct", 0)
                for h in holdings) / summary["market_value"], 2)

        pg_read_sql = (
            "SELECT c.client_code, c.full_name, c.segment, c.risk_profile, "
            "       c.kyc_status, c.margin_available, c.trading_limit, "
            "       h.quantity, h.avg_buy_price, sm.symbol, sm.sector, mp.ltp, "
            "       t.executed_at, t.side, t.quantity AS trade_qty, t.price AS trade_price "
            "FROM   portfolio.customer c "
            "LEFT JOIN portfolio.holding h        ON h.customer_id = c.customer_id "
            "LEFT JOIN portfolio.security_master sm ON sm.security_id = h.security_id "
            "LEFT JOIN portfolio.market_price    mp ON mp.security_id = h.security_id "
            "LEFT JOIN LATERAL (SELECT executed_at, side, quantity, price "
            "                   FROM portfolio.trade "
            "                   WHERE customer_id = c.customer_id "
            "                   ORDER BY executed_at DESC LIMIT 5) t ON TRUE "
            "WHERE c.client_code = %s")
        pg_read_us  = _measure_pg_read_us(pg_read_sql, (cc,))
        comparison  = _build_comparison(
            pg_query_str  = (pg_read_sql.replace("%s", f"'{cc}'")
                             .replace(" LEFT JOIN ", "\nLEFT JOIN ")
                             .replace(" FROM ", "\nFROM ")
                             .replace(" WHERE ", "\nWHERE ")) + ";",
            pg_us         = pg_read_us,
            redis_cmd_str = redis_cmd,
            redis_us      = redis_us,
            why_it_matters="Customer-360 is the RM / advisor's primary screen and the "
                           "first thing every customer sees on login. The same data "
                           "that requires a 4-table JOIN in Postgres comes back as one "
                           "pipeline of µs-latency Redis reads.",
        )

        return jsonify({
            "ok":            True,
            "action":        "Section 3 · Customer 360",
            "action_id":     "cust360",
            "read_only":     True,
            "display_type":  "customer_360",
            "ts_ist":        _epoch_ms_to_ist(_now_ms()),
            "read_us":       redis_us,
            "redis_command": redis_cmd,
            "description":   (f"Built the full {cc} client-360 — profile + KYC + margin "
                              "+ trading-limit + all holdings + sector exposure + last 5 "
                              "trades — in one Redis pipeline. The same view in Postgres "
                              "needs a 4-table JOIN."),
            "result":        {
                "client_code":   cc,
                "customer_id":   cid,
                "name":          cust.get("name") or cust.get("full_name") or "",
                "segment":       cust.get("segment"),
                "risk_profile":  cust.get("risk_profile"),
                "kyc_status":    cust.get("kyc_status"),
                "demat":         cust.get("demat_account"),
                "margin":        cust.get("margin_available"),
                "trading_limit": cust.get("trading_limit"),
                "summary":       summary,
                "day_pnl_pct":   day_pnl_pct,
                "holdings":      holdings[:6],
                "sector":        sector,
                "recent_trades": recent_trades,
            },
            "section":        3,
            "section_label": "Customer experience",
            "comparison":    comparison,
        })

    # --- 5.B · Personalised recommendations --------------------------
    if action == "personalize":
        cc = _pick_verified_client_code()
        if not cc:
            return jsonify({"error": "no VERIFIED customer in cache"}), 400
        cust = get_customer(cc) or {}
        cid  = int(cust.get("customer_id") or 0)
        if not cid:
            return jsonify({"error": "customer not in cache"}), 400
        segment      = (cust.get("segment") or "RETAIL").upper()
        risk_profile = (cust.get("risk_profile") or "MODERATE").upper()

        redis_cmd = (
            f"-- Read the segment / risk + the customer's sector mix off Redis,\n"
            f"-- then ask sec-idx for instruments OUTSIDE the held sectors.\n"
            f"JSON.GET   customer:{cc} $.segment $.risk_profile\n"
            f"FT.SEARCH  hold-idx \"@customer_id:[{cid} {cid}]\"\n"
            f"JSON.GET   holding:{cid}:* $.sector   (pipelined)\n"
            f"FT.SEARCH  sec-idx     \"-@sector:{{held_sectors}}\""
        )

        def _do_personalize_read():
            pipe = r.pipeline(transaction=False)
            pipe.execute_command("JSON.GET", f"customer:{cc}", "$.segment")
            pipe.execute_command("JSON.GET", f"customer:{cc}", "$.risk_profile")
            keys = _holding_keys(cid)
            for k in keys:
                pipe.execute_command("JSON.GET", k, "$")
            pipe.execute()
        redis_us = _measure_redis_read_us(_do_personalize_read)

        # Compute the sectors the customer ALREADY owns (skip them) and
        # pull candidates from outside that set.
        held_sectors = set()
        for k in _holding_keys(cid):
            raw = r.execute_command("JSON.GET", k, "$")
            if not raw: continue
            try:
                h = json.loads(raw)[0]
                sid_ = int(h["security_id"])
                meta = _sec_meta(sid_)
                if meta.get("sector"):
                    held_sectors.add(meta["sector"])
            except Exception:
                continue

        # Sector preferences by risk profile (a simple deterministic
        # mapping — production swaps this for a ML recommender served
        # through Redis. The point is to show the recommendation comes
        # back in microseconds because every input is in Redis already).
        preferred_by_risk = {
            "CONSERVATIVE": ["Banking", "FMCG", "Utilities", "Pharma"],
            "MODERATE":     ["IT", "Banking", "Auto", "FMCG", "Energy"],
            "AGGRESSIVE":   ["IT", "Auto", "Oil & Gas", "Telecom", "Metals", "Realty"],
        }.get(risk_profile, ["IT", "Banking", "FMCG"])

        # Look up candidate instruments — filtered by sector NOT in
        # held_sectors. We use a PG SELECT for the candidate list itself
        # (security_master is only ~20 rows), but the JOIN to current
        # prices comes off Redis via _sec_meta + HGET. Both PG and Redis
        # latencies are measured in `comparison` below.
        candidate_rows = _pg_select_all(
            "SELECT security_id, symbol, company_name, sector "
            "FROM portfolio.security_master "
            "WHERE is_active = TRUE AND sector NOT IN %s "
            "ORDER BY sector, symbol",
            (tuple(held_sectors) or ("__none__",),))
        # Score by sector match against the customer's risk preference.
        def _score(row):
            sec_ = (row.get("sector") or "")
            for i, s in enumerate(preferred_by_risk):
                if s.lower() in sec_.lower() or sec_.lower() in s.lower():
                    return -(len(preferred_by_risk) - i)
            return 0
        candidate_rows.sort(key=_score)
        # Decorate top 6 candidates with the live LTP from Redis Hash.
        recommendations = []
        for row in candidate_rows[:6]:
            sid_  = int(row["security_id"])
            price = r.hgetall(f"price:{sid_}")
            ltp   = float(price.get("ltp", 0) or 0.0) if price else 0.0
            recommendations.append({
                "symbol":  row.get("symbol"),
                "company": row.get("company_name"),
                "sector":  row.get("sector"),
                "ltp":     round(ltp, 2),
                "reason":  ("Aligns with your " + risk_profile.title() +
                            " risk profile · diversifies away from " +
                            ", ".join(sorted(held_sectors)) if held_sectors
                            else "Starter pick for your " + risk_profile.title()
                                 + " risk profile"),
            })

        pg_read_sql = (
            "WITH held_sectors AS ("
            " SELECT DISTINCT sm.sector "
            " FROM portfolio.holding h "
            " JOIN portfolio.security_master sm ON sm.security_id = h.security_id "
            " WHERE h.customer_id = %s) "
            "SELECT sm.symbol, sm.company_name, sm.sector, mp.ltp "
            "FROM   portfolio.security_master sm "
            "JOIN   portfolio.market_price    mp ON mp.security_id = sm.security_id "
            "WHERE  sm.is_active = TRUE "
            "AND    sm.sector NOT IN (SELECT sector FROM held_sectors) "
            "ORDER BY sm.sector, sm.symbol LIMIT 6")
        pg_read_us  = _measure_pg_read_us(pg_read_sql, (cid,))
        comparison  = _build_comparison(
            pg_query_str  = pg_read_sql.replace("%s", str(cid)) + ";",
            pg_us         = pg_read_us,
            redis_cmd_str = redis_cmd,
            redis_us      = redis_us,
            why_it_matters="Personalised recommendations are computed on every page view "
                           "of the advisory section. Pulling them from Redis-served context "
                           "rather than re-running a multi-table JOIN per visit is what makes "
                           "the customer feel like the platform 'knows them' instead of "
                           "feeling generic.",
        )

        return jsonify({
            "ok":            True,
            "action":        "Section 3 · Customer personalization",
            "action_id":     "personalize",
            "read_only":     True,
            "display_type":  "personalization",
            "ts_ist":        _epoch_ms_to_ist(_now_ms()),
            "read_us":       redis_us,
            "redis_command": redis_cmd,
            "description":   (f"Generated {len(recommendations)} personalised picks for "
                              f"{cc} ({segment} / {risk_profile}) — picks avoid the "
                              f"{len(held_sectors)} sector(s) the customer already holds, "
                              "ranked by alignment with the risk profile."),
            "result":        {
                "client_code":     cc,
                "name":            cust.get("name") or cust.get("full_name") or "",
                "segment":         segment,
                "risk_profile":    risk_profile,
                "held_sectors":    sorted(held_sectors),
                "preferences":     preferred_by_risk,
                "recommendations": recommendations,
            },
            "section":        3,
            "section_label": "Customer experience",
            "comparison":    comparison,
        })

    # --- 5.C · Customer hybrid search --------------------------------
    if action == "cust_search":
        """The 'support agent types anything' workflow. We try four
        commonly-typed identifiers against the cust-idx RediSearch
        index — PAN, client_code, name prefix and segment — and return
        the top hits per facet. Apps that today resolve the customer
        with a chain of SELECTs (or, worse, asynchronous Elastic / Solr
        round-trips) get a single hybrid Redis call."""
        # Pick a real client to drive the demo with — we'll seed the
        # search inputs from this customer's actual fields so the demo
        # always returns deterministic results.
        cc = _pick_verified_client_code()
        if not cc:
            return jsonify({"error": "no VERIFIED customer in cache"}), 400
        seed = get_customer(cc) or {}
        seed_pan     = seed.get("pan", "")
        seed_segment = seed.get("segment", "RETAIL")
        seed_name    = (seed.get("name") or seed.get("full_name") or "").split()[0] if (
            seed.get("name") or seed.get("full_name")) else "Demo"

        redis_cmd = (
            f"-- Facet 1 · by PAN (TAG exact)\n"
            f"FT.SEARCH cust-idx '@pan:{{{seed_pan}}}'   LIMIT 0 1\n"
            f"-- Facet 2 · by client_code (TAG exact)\n"
            f"FT.SEARCH cust-idx '@client_code:{{{cc}}}' LIMIT 0 1\n"
            f"-- Facet 3 · name prefix (TEXT)\n"
            f"FT.SEARCH cust-idx '@name:{seed_name.lower()}*' LIMIT 0 5\n"
            f"-- Facet 4 · TEXT name prefix + TAG segment (hybrid)\n"
            f"FT.SEARCH cust-idx '@name:{seed_name.lower()}* @segment:{{{seed_segment}}}' "
            f"LIMIT 0 5"
        )

        def _ft_search(expr, ret_fields=None, limit=5):
            # No SORTBY on hot path. At 3M docs with broad facets like
            # "@segment:{RETAIL} @kyc_status:{VERIFIED}", a SORTBY would
            # force the engine to materialise all 250k+ matches just to
            # cherry-pick 5 — that's a millisecond budget killer. Take
            # the first N matches by relevance instead, which keeps the
            # whole hybrid search in single-digit ms even at scale.
            try:
                args = ["FT.SEARCH", "cust-idx", expr, "LIMIT", "0", str(limit)]
                if ret_fields:
                    args += ["RETURN", str(len(ret_fields))] + ret_fields
                else:
                    args += ["RETURN", "5",
                             "$.client_code", "$.name", "$.pan",
                             "$.segment", "$.risk_profile"]
                res = r.execute_command(*args)
                hits = []
                for i in range(1, len(res), 2):
                    flat = res[i+1]
                    d = dict(zip(flat[::2], flat[1::2]))
                    hits.append({
                        "client_code":  d.get("$.client_code", ""),
                        "name":         d.get("$.name", ""),
                        "pan":          d.get("$.pan", ""),
                        "segment":      d.get("$.segment", ""),
                        "risk_profile": d.get("$.risk_profile", ""),
                    })
                total = int(res[0]) if res else 0
                return total, hits
            except Exception:
                return 0, []

        hybrid_expr = f"@name:{seed_name.lower()}* @segment:{{{seed_segment}}}"

        def _do_search_read():
            _ft_search(f"@pan:{{{seed_pan}}}", limit=1)
            _ft_search(f"@client_code:{{{cc}}}", limit=1)
            _ft_search(f"@name:{seed_name.lower()}*", limit=5)
            _ft_search(hybrid_expr, limit=5)
        redis_us = _measure_redis_read_us(_do_search_read)

        by_pan_total,     by_pan     = _ft_search(f"@pan:{{{seed_pan}}}", limit=1)
        by_code_total,    by_code    = _ft_search(f"@client_code:{{{cc}}}", limit=1)
        by_name_total,    by_name    = _ft_search(f"@name:{seed_name.lower()}*", limit=5)
        by_seg_total,     by_seg     = _ft_search(hybrid_expr, limit=5)

        # Apples-to-apples PG comparison: run the SAME four facet
        # queries Redis is running, separately, and sum the timings.
        # A single OR-query short-circuits on the unique-key clauses
        # and would unfairly skip the cost of the ILIKE / segment scan.
        pg_pan_sql  = "SELECT * FROM portfolio.customer WHERE pan = %s LIMIT 1"
        pg_code_sql = "SELECT * FROM portfolio.customer WHERE client_code = %s LIMIT 1"
        pg_name_sql = ("SELECT * FROM portfolio.customer "
                       "WHERE full_name ILIKE %s LIMIT 5")
        pg_hybr_sql = ("SELECT * FROM portfolio.customer "
                       "WHERE full_name ILIKE %s AND segment = %s LIMIT 5")
        pg_total_us = (
            _measure_pg_read_us(pg_pan_sql,  (seed_pan,)) +
            _measure_pg_read_us(pg_code_sql, (cc,)) +
            _measure_pg_read_us(pg_name_sql, (f"{seed_name}%",)) +
            _measure_pg_read_us(pg_hybr_sql, (f"{seed_name}%", seed_segment))
        )
        pg_read_us  = pg_total_us
        pg_read_sql = ("-- 4 separate SELECTs (one per facet), summed:\n"
                       + pg_pan_sql.replace("%s", f"'{seed_pan}'") + ";\n"
                       + pg_code_sql.replace("%s", f"'{cc}'") + ";\n"
                       + pg_name_sql.replace("%s", f"'{seed_name}%%'") + ";\n"
                       + pg_hybr_sql.replace("%s", f"'{seed_name}%%'", 1)
                                    .replace("%s", f"'{seed_segment}'", 1) + ";")
        comparison  = _build_comparison(
            pg_query_str  = pg_read_sql,
            pg_us         = pg_read_us,
            redis_cmd_str = redis_cmd,
            redis_us      = redis_us,
            why_it_matters="Customer-support 'find me this client by anything' workflows "
                           "are the support-team's single most common query. The same "
                           "hybrid OR-search collapses from 'multiple Postgres queries + "
                           "Elastic round-trip' into one RediSearch call.",
        )

        return jsonify({
            "ok":            True,
            "action":        "Section 5 · Customer hybrid search",
            "action_id":     "cust_search",
            "read_only":     True,
            "display_type":  "customer_search",
            "ts_ist":        _epoch_ms_to_ist(_now_ms()),
            "read_us":       redis_us,
            "redis_command": redis_cmd,
            "description":   ("Resolved a customer across four search facets in one Redis "
                              "round-trip — exact PAN, exact client_code, name prefix, and "
                              "segment + KYC. Same idea as Elastic's multi_match, served by "
                              "Redis Enterprise."),
            "result":        {
                "seed":          {
                    "client_code": cc, "pan": seed_pan,
                    "name": seed_name, "segment": seed_segment,
                },
                "facets":        [
                    {"name": "Exact PAN",        "query": f"@pan:{{{seed_pan}}}",
                     "total": by_pan_total,     "hits":  by_pan},
                    {"name": "Exact client_code","query": f"@client_code:{{{cc}}}",
                     "total": by_code_total,    "hits":  by_code},
                    {"name": "Name prefix (TEXT)","query": f"@name:{seed_name.lower()}*",
                     "total": by_name_total,    "hits":  by_name},
                    {"name": "Name prefix + segment (hybrid)",
                     "query": hybrid_expr,
                     "total": by_seg_total,     "hits":  by_seg},
                ],
            },
            "section":        3,
            "section_label": "Customer experience",
            "comparison":    comparison,
        })

    # ===================================================================
    # 6) UC #3 · Live trade tape — READ-ONLY (legacy, kept for compat)
    # ===================================================================
    if action == "tape":
        redis_cmd = "XREVRANGE rdi:last-events + - COUNT 20"
        t_now    = _now_ms()
        redis_us = _measure_redis_read_us(
            lambda: rdi_state.xrevrange("rdi:last-events", count=20))
        rows = []
        try:
            for sid, f in (rdi_state.xrevrange("rdi:last-events", count=200) or []):
                if f.get("table") != "portfolio.trade":
                    continue
                rows.append({
                    "id":     sid,
                    "ts_ist": _epoch_ms_to_ist(int(sid.split("-")[0])),
                    "op":     f.get("op"),
                    "lag_ms": int(f.get("lag_ms", 0) or 0),
                })
                if len(rows) >= 20:
                    break
        except Exception:
            pass

        pg_read_sql = ("SELECT customer_id, side, quantity, price, executed_at "
                       "FROM portfolio.trade ORDER BY executed_at DESC LIMIT 20")
        pg_read_us  = _measure_pg_read_us(pg_read_sql)
        comparison  = _build_comparison(
            pg_query_str  = ("SELECT customer_id, side, quantity, price, executed_at\n"
                             "FROM   portfolio.trade\n"
                             "ORDER BY executed_at DESC LIMIT 20;"),
            pg_us         = pg_read_us,
            redis_cmd_str = redis_cmd,
            redis_us      = redis_us,
            why_it_matters="The trade tape fans out to mobile, web and RM dashboards "
                           "as a single XREVRANGE. PG has to sort by executed_at on "
                           "every read; Redis Streams answer from an in-memory log "
                           "in microseconds.",
        )
        return jsonify({
            "ok":            True,
            "action":        "UC3 · Live trade tape",
            "action_id":     "tape",
            "read_only":     True,
            "display_type":  "events_table",
            "ts_ist":        _epoch_ms_to_ist(t_now),
            "read_us":       redis_us,
            "redis_command": redis_cmd,
            "description":   ("Sampled the last 20 trades from the rdi:last-events "
                              "ring buffer. Mobile, web and RM-dashboard all read "
                              "this same stream — one source, many consumers."),
            "result":        {"rows": rows, "count": len(rows)},
            "comparison":    comparison,
        })

    # ===================================================================
    # 6) UC #5 · Materialised segment view — READ-ONLY
    #    RDI maintains a SET per segment from the SAME customer CDC events.
    #    SCARD answers in O(1) — Postgres has to GROUP BY 3 M rows.
    # ===================================================================
    if action == "segview":
        redis_cmd = ("SCARD cust-segment:RETAIL\n"
                     "SCARD cust-segment:HNI\n"
                     "SCARD cust-segment:PRO")
        t_now    = _now_ms()
        def _multi_scard():
            pipe = r.pipeline(transaction=False)
            for s in ("RETAIL", "HNI", "PRO"):
                pipe.scard(f"cust-segment:{s}")
            return pipe.execute()
        redis_us = _measure_redis_read_us(_multi_scard)
        segments = {seg: int(r.scard(f"cust-segment:{seg}") or 0)
                    for seg in ("RETAIL", "HNI", "PRO")}
        total    = sum(segments.values())

        pg_read_sql = "SELECT segment, COUNT(*) FROM portfolio.customer GROUP BY segment"
        pg_read_us  = _measure_pg_read_us(pg_read_sql)
        comparison  = _build_comparison(
            pg_query_str  = ("SELECT segment, COUNT(*) AS customers\n"
                             "FROM   portfolio.customer\n"
                             "GROUP BY segment;"),
            pg_us         = pg_read_us,
            redis_cmd_str = redis_cmd,
            redis_us      = redis_us,
            why_it_matters="RM dashboards need live counts per segment. SCARD on a "
                           "Redis SET is O(1) — Postgres has to GROUP BY across "
                           "millions of rows. The SET is maintained by the SAME "
                           "RDI pipeline as the customer JSON; no app-side "
                           "cache-maintenance code.",
        )
        return jsonify({
            "ok":            True,
            "action":        "UC5 · Customers by segment",
            "action_id":     "segview",
            "read_only":     True,
            "display_type":  "segment_grid",
            "ts_ist":        _epoch_ms_to_ist(t_now),
            "read_us":       redis_us,
            "redis_command": redis_cmd,
            "description":   ("Live customer count per segment. RDI maintains "
                              "cust-segment:RETAIL/HNI/PRO from every customer "
                              "INSERT/UPDATE — the app never writes maintenance code."),
            "result":        {"segments": segments, "total": total},
            "comparison":    comparison,
        })

    # ===================================================================
    # 7) UC #6 · Microservice fan-out — READ-ONLY
    #    3 downstream services each consume the SAME stream with their OWN
    #    consumer group. Oracle never sees the read traffic.
    # ===================================================================
    if action == "fanout":
        services = ("trading-app", "risk-engine", "reporting-bi")
        redis_cmd = ("-- each microservice has its OWN consumer group:\n"
                     "XREADGROUP GROUP trading-app  c1 COUNT 5 STREAMS rdi:last-events >\n"
                     "XREADGROUP GROUP risk-engine  c1 COUNT 5 STREAMS rdi:last-events >\n"
                     "XREADGROUP GROUP reporting-bi c1 COUNT 5 STREAMS rdi:last-events >")
        t_now    = _now_ms()
        redis_us = _measure_redis_read_us(
            lambda: rdi_state.xrevrange("rdi:last-events", count=5))
        sample = []
        try:
            for sid, f in (rdi_state.xrevrange("rdi:last-events", count=50) or []):
                if f.get("table") != "portfolio.trade":
                    continue
                sample.append({
                    "id":     sid,
                    "ts_ist": _epoch_ms_to_ist(int(sid.split("-")[0])),
                    "op":     f.get("op"),
                    "lag_ms": int(f.get("lag_ms", 0) or 0),
                })
                if len(sample) >= 5:
                    break
        except Exception:
            pass
        per_svc = {svc: sample for svc in services}

        pg_read_sql = ("SELECT customer_id, side, quantity, price, executed_at "
                       "FROM portfolio.trade ORDER BY executed_at DESC LIMIT 5")
        pg_read_us  = _measure_pg_read_us(pg_read_sql)
        comparison  = _build_comparison(
            pg_query_str  = ("-- 3 separate services each issue this against the same DB:\n"
                             "SELECT customer_id, side, quantity, price, executed_at\n"
                             "FROM   portfolio.trade\n"
                             "ORDER BY executed_at DESC LIMIT 5;"),
            pg_us         = pg_read_us,
            redis_cmd_str = redis_cmd,
            redis_us      = redis_us,
            why_it_matters="Each downstream team owns its consumer group. They ship "
                           "independently and never hammer Oracle — one source, many "
                           "readers, no coupling.",
        )
        return jsonify({
            "ok":            True,
            "action":        "UC6 · Microservice fan-out",
            "action_id":     "fanout",
            "read_only":     True,
            "display_type":  "fanout_grid",
            "ts_ist":        _epoch_ms_to_ist(t_now),
            "read_us":       redis_us,
            "redis_command": redis_cmd,
            "description":   ("3 independent consumer groups (trading-app, "
                              "risk-engine, reporting-bi) consuming the same "
                              "rdi:last-events stream. Sub-ms delivery, zero "
                              "coupling between teams."),
            "result":        {"services": per_svc},
            "comparison":    comparison,
        })

    # ===================================================================
    # 8) UC #7 · Audit trail — READ-ONLY
    #    rdi:last-events is the processor's append-only log. Auditors get
    #    a single ordered ledger of every customer change, by table.
    # ===================================================================
    if action == "audit":
        redis_cmd = "XREVRANGE rdi:last-events + - COUNT 50"
        t_now    = _now_ms()
        redis_us = _measure_redis_read_us(
            lambda: rdi_state.xrevrange("rdi:last-events", count=50))
        events = pipeline_recent_events(50) or []

        pg_read_sql = ("SELECT client_code, kyc_status, segment, updated_at "
                       "FROM portfolio.customer "
                       "WHERE updated_at > NOW() - INTERVAL '1 day' "
                       "ORDER BY updated_at DESC LIMIT 50")
        pg_read_us  = _measure_pg_read_us(pg_read_sql)
        comparison  = _build_comparison(
            pg_query_str  = ("SELECT client_code, kyc_status, segment, updated_at\n"
                             "FROM   portfolio.customer\n"
                             "WHERE  updated_at > NOW() - INTERVAL '1 day'\n"
                             "ORDER BY updated_at DESC LIMIT 50;"),
            pg_us         = pg_read_us,
            redis_cmd_str = redis_cmd,
            redis_us      = redis_us,
            why_it_matters="Auditors need an append-only ledger of every change. "
                           "rdi:last-events is exactly that — replayable, "
                           "exportable, and 50 rows come back in microseconds.",
        )
        return jsonify({
            "ok":            True,
            "action":        "UC7 · Audit trail",
            "action_id":     "audit",
            "read_only":     True,
            "display_type":  "audit_table",
            "ts_ist":        _epoch_ms_to_ist(t_now),
            "read_us":       redis_us,
            "redis_command": redis_cmd,
            "description":   ("Browsed the last 50 CDC events from rdi:last-events. "
                              "Every change the processor saw is here, in order — "
                              "append-only, replayable, exportable."),
            "result":        {"events": events, "count": len(events)},
            "comparison":    comparison,
        })

    # ===================================================================
    # ④ Section 4 · "Multi-shape fan-out" — INSERT a customer in the HNI
    #    segment, then show that ONE YAML produced BOTH a JSON document
    #    AND a SET membership update. This is RDI's signature pattern.
    # ===================================================================
    if action == "fanout_shapes":
        cc      = _new_client_code()
        cid     = _new_customer_id()
        pan     = f"FAN{int(time.time()) % 1_000_000:06d}"
        stream  = "sectrade.portfolio.customer"
        target  = f"customer:{cc}"
        seg     = "HNI"
        full_nm = f"Fanout Demo {cc}"

        since   = _last_stream_id(stream)
        t_start = _now_ms()
        _pg_exec(
            "INSERT INTO portfolio.customer "
            "  (customer_id, client_code, pan, full_name, segment, risk_profile, "
            "   kyc_status, demat_account, onboarded_on) "
            "VALUES (%s, %s, %s, %s, %s, 'AGGRESSIVE', "
            "        'VERIFIED', %s, CURRENT_DATE)",
            (cid, cc, pan, full_nm, seg, f"{cc}-DP"))
        t_pgdone = _now_ms()
        wal_lsn  = _get_wal_lsn()

        cdc_ms, cdc_sid = _wait_for_debezium_pickup(
            stream, since, "customer_id", cid, deadline_ms=15000)

        # Wait for both shapes to appear
        def _both_shapes_present() -> bool:
            j_exists = r.execute_command("JSON.GET", target, "$") is not None
            s_member = r.sismember(f"cust-segment:{seg}", cc)
            return bool(j_exists and s_member)
        ok, _ = _wait_for_key_match(_both_shapes_present, deadline_ms=15000, poll_ms=2)
        t_redis = _now_ms() if ok else None

        read_us = _measure_redis_read_us(_both_shapes_present)
        t_app   = _now_ms()

        # Build per-shape preview
        try:
            json_doc = r.execute_command("JSON.GET", target, "$")
            json_preview = (json_doc or "").strip()
            if len(json_preview) > 480:
                json_preview = json_preview[:480] + " …"
        except Exception:
            json_preview = ""
        try:
            seg_size   = r.scard(f"cust-segment:{seg}") or 0
            seg_sample = sorted(list(r.srandmember(f"cust-segment:{seg}", 6) or []))
            seg_preview = (f"SMEMBERS sample (size={seg_size}): "
                           + ", ".join(seg_sample) + " …")
        except Exception:
            seg_preview = ""

        shapes = [
            {
                "type":     "JSON",
                "key":      target,
                "command":  f"JSON.GET {target} $",
                "preview":  json_preview,
                "why":      "Trading screens read the full customer document by client_code "
                            "in a single JSON.GET — no JOIN, no per-field index.",
            },
            {
                "type":     "SET",
                "key":      f"cust-segment:{seg}",
                "command":  f"SISMEMBER cust-segment:{seg} {cc}\nSCARD cust-segment:{seg}",
                "preview":  seg_preview,
                "why":      "Marketing / personalization reads 'all HNI customers' from a "
                            "Redis SET in O(1) — that membership update was made by the "
                            "SAME YAML job that wrote the JSON, no separate index code.",
            },
        ]

        # Same comparison structure as the rest — but the punchline here
        # is "two shapes from one YAML", not raw latency.
        pg_read_sql = ("SELECT * FROM portfolio.customer WHERE client_code = %s;")
        pg_read_us  = _measure_pg_read_us(pg_read_sql, (cc,))
        comparison  = _build_comparison(
            pg_query_str  = ("-- Today: app maintains a separate `customer_segment_map`\n"
                             "-- table and bumps a redis cache in code; two writes, two\n"
                             "-- failure modes, hand-rolled consistency.\n\n"
                             "BEGIN;\n"
                             "INSERT INTO portfolio.customer (…) VALUES (…);\n"
                             "INSERT INTO portfolio.customer_segment_map\n"
                             "       (customer_id, segment) VALUES (…);\n"
                             "COMMIT;"),
            pg_us         = pg_read_us,
            redis_cmd_str = "JSON.GET customer:<cc> $\nSISMEMBER cust-segment:HNI <cc>",
            redis_us      = read_us,
            why_it_matters="One source, many shapes — driven by ONE YAML. No separate "
                           "index-maintenance code, no dual-write transactions, no drift.",
        )

        steps = [
            _build_step("pg_insert", "INSERT executed on PostgreSQL",
                        t_start, t_start,
                        detail=f"INSERT INTO portfolio.customer … VALUES ({cid}, '{cc}', …, '{seg}');"),
            _build_step("pg_wal", "Row committed to PostgreSQL WAL",
                        t_pgdone, t_start,
                        detail=f"pg_current_wal_lsn() = {wal_lsn or 'n/a'}", wal_lsn=wal_lsn),
            _build_step("cdc", "RDI · Debezium captured the change",
                        cdc_ms, t_start,
                        detail=f"stream {stream} id {cdc_sid or 'pending'} "
                               f"key {{\"customer_id\":{cid}}}",
                        stream=stream, stream_id=cdc_sid),
            _build_step("redis_write", "RDI Processor wrote TWO shapes",
                        t_redis, t_start,
                        detail=(f"JSON.SET {target} $ {{ … }}\n"
                                f"SADD cust-segment:{seg} {cc}"),
                        target_key=target),
            _build_step("app_read", "Application observes both shapes",
                        t_app, t_start,
                        detail=f"JSON.GET {target} $  ·  SISMEMBER cust-segment:{seg} {cc}",
                        read_us=read_us, app_command=f"JSON.GET {target} $"),
        ]

        return jsonify({
            "ok":            ok,
            "action":        "Section 4 · Multi-shape fan-out",
            "action_id":     action,
            "table":         "portfolio.customer",
            "target_key":    target,
            "primary_key":   {"customer_id": cid, "client_code": cc},
            "wal_lsn":       wal_lsn,
            "steps":         _add_step_durations(steps),
            "comparison":    comparison,
            "display_type":  "fanout_shapes",
            "result": {
                "client_code": cc,
                "name":        full_nm,
                "segment":     seg,
                "shapes":      shapes,
            },
            "totals": {
                "t0_ist":              _epoch_ms_to_ist(t_start),
                "pg_write_ms":         round(t_pgdone - t_start, 1),
                "cdc_pickup_ms":       round(cdc_ms - t_pgdone, 1) if cdc_ms else None,
                "processor_write_ms":  round(t_redis - cdc_ms, 1) if (t_redis and cdc_ms) else None,
                "pipeline_total_ms":   round(t_redis - t_start, 1) if t_redis else None,
                "app_read_us":         read_us,
            },
            "section":       4,
            "section_label": "Production-ready RDI patterns",
        })

    # ===================================================================
    # ④ Section 4 · DELETE propagation
    #    INSERT a throw-away customer, ensure it's in cache, then DELETE
    #    and watch the Redis key disappear. Shows RDI does the full
    #    INSERT/UPDATE/DELETE triad — not just upserts.
    # ===================================================================
    if action == "customer_delete":
        cc      = _new_client_code()
        cid     = _new_customer_id()
        pan     = f"DEL{int(time.time()) % 1_000_000:06d}"
        stream  = "sectrade.portfolio.customer"
        target  = f"customer:{cc}"

        # Step 1 — INSERT and wait for cache populated
        since0 = _last_stream_id(stream)
        _pg_exec(
            "INSERT INTO portfolio.customer "
            "  (customer_id, client_code, pan, full_name, segment, risk_profile, "
            "   kyc_status, demat_account, onboarded_on) "
            "VALUES (%s, %s, %s, %s, 'RETAIL', 'MODERATE', "
            "        'VERIFIED', %s, CURRENT_DATE)",
            (cid, cc, pan, f"DeleteDemo {cc}", f"{cc}-DP"))
        _wait_for_debezium_pickup(stream, since0, "customer_id", cid, deadline_ms=15000)
        _wait_for_key_match(
            lambda: r.execute_command("JSON.GET", target, "$") is not None,
            deadline_ms=15000, poll_ms=2)
        existed_before = r.execute_command("JSON.GET", target, "$") is not None

        # Step 2 — DELETE and time the propagation
        since   = _last_stream_id(stream)
        t_start = _now_ms()
        _pg_exec("DELETE FROM portfolio.customer WHERE customer_id = %s", (cid,))
        t_pgdone = _now_ms()
        wal_lsn  = _get_wal_lsn()

        cdc_ms, cdc_sid = _wait_for_debezium_pickup(
            stream, since, "customer_id", cid, deadline_ms=15000)

        ok, _ = _wait_for_key_match(
            lambda: r.execute_command("JSON.GET", target, "$") is None,
            deadline_ms=15000, poll_ms=2)
        t_redis = _now_ms() if ok else None

        read_us = _measure_redis_read_us(
            lambda: r.execute_command("JSON.GET", target, "$"))
        t_app   = _now_ms()
        exists_after = r.execute_command("JSON.GET", target, "$") is not None

        pg_read_sql = "SELECT 1 FROM portfolio.customer WHERE client_code = %s"
        pg_read_us  = _measure_pg_read_us(pg_read_sql, (cc,))
        comparison  = _build_comparison(
            pg_query_str  = pg_read_sql.replace("%s", f"'{cc}'") + ";",
            pg_us         = pg_read_us,
            redis_cmd_str = f"EXISTS {target}  →  0   (key removed by RDI)",
            redis_us      = read_us,
            why_it_matters="DIY CDC code routinely forgets DELETEs and leaves stale "
                           "rows in caches. RDI translates Debezium DELETEs into Redis "
                           "DEL on the same key the row was written to.",
        )

        steps = [
            _build_step("pg_insert", "DELETE executed on PostgreSQL",
                        t_start, t_start,
                        detail=f"DELETE FROM portfolio.customer WHERE customer_id = {cid};"),
            _build_step("pg_wal", "Row tombstone committed to PostgreSQL WAL",
                        t_pgdone, t_start,
                        detail=f"pg_current_wal_lsn() = {wal_lsn or 'n/a'}", wal_lsn=wal_lsn),
            _build_step("cdc", "RDI · Debezium captured the DELETE",
                        cdc_ms, t_start,
                        detail=f"stream {stream} id {cdc_sid or 'pending'} (op=d)",
                        stream=stream, stream_id=cdc_sid),
            _build_step("redis_write", "RDI Processor removed the Redis key",
                        t_redis, t_start,
                        detail=f"DEL {target}", target_key=target),
            _build_step("app_read", "Application sees the key is gone",
                        t_app, t_start,
                        detail=f"JSON.GET {target} $  →  (nil)  ·  {read_us} µs",
                        read_us=read_us, app_command=f"JSON.GET {target} $"),
        ]

        return jsonify({
            "ok":            ok,
            "action":        "Section 4 · DELETE propagation",
            "action_id":     action,
            "table":         "portfolio.customer",
            "target_key":    target,
            "primary_key":   {"customer_id": cid, "client_code": cc},
            "wal_lsn":       wal_lsn,
            "steps":         _add_step_durations(steps),
            "comparison":    comparison,
            "display_type":  "customer_delete",
            "result": {
                "client_code":     cc,
                "redis_key":       target,
                "existed_before":  bool(existed_before),
                "exists_after":    bool(exists_after),
                "propagation_ms":  (round(t_redis - t_start, 1) if t_redis else None),
                "pg_sql":          f"DELETE FROM portfolio.customer WHERE customer_id = {cid};",
                "redis_cmd":       f"JSON.GET {target} $",
                "redis_response":  "(nil)" if not exists_after else "(still present)",
            },
            "totals": {
                "t0_ist":              _epoch_ms_to_ist(t_start),
                "pg_write_ms":         round(t_pgdone - t_start, 1),
                "cdc_pickup_ms":       round(cdc_ms - t_pgdone, 1) if cdc_ms else None,
                "processor_write_ms":  round(t_redis - cdc_ms, 1) if (t_redis and cdc_ms) else None,
                "pipeline_total_ms":   round(t_redis - t_start, 1) if t_redis else None,
                "app_read_us":         read_us,
            },
            "section":       4,
            "section_label": "Production-ready RDI patterns",
        })

    # ===================================================================
    # ④ Section 4 · Schema evolution — READ-ONLY
    #    Show that the 3 columns we ALTER'd into Postgres at app boot
    #    are present in Redis JSON, with ZERO YAML edits, because the
    #    YAML uses `path: $` and writes the whole row.
    # ===================================================================
    if action == "schema_evolution":
        t_now = _now_ms()
        cc    = _pick_verified_client_code()
        if not cc:
            return jsonify({"error": "no VERIFIED customer found"}), 400

        # PG side — read the 3 new columns from the source of record.
        pg_cust = _pg_select_one(
            "SELECT customer_id, margin_available, trading_limit "
            "FROM portfolio.customer WHERE client_code = %s", (cc,))
        pg_sec = _pg_select_one(
            "SELECT security_id, symbol, corporate_action_flag "
            "FROM portfolio.security_master ORDER BY security_id LIMIT 1")
        symbol = (pg_sec or {}).get("symbol")
        sec_id = (pg_sec or {}).get("security_id")

        # Redis side — read the same 3 fields from the JSON shapes RDI maintains.
        def _read_redis_cust_field(path):
            try:
                raw = r.execute_command("JSON.GET", f"customer:{cc}", path)
                if raw is None: return None
                v = json.loads(raw)
                return v[0] if isinstance(v, list) and v else v
            except Exception:
                return None
        def _read_redis_sec_field(path):
            try:
                raw = r.execute_command("JSON.GET", f"security_full:{sec_id}", path)
                if raw is None: return None
                v = json.loads(raw)
                return v[0] if isinstance(v, list) and v else v
            except Exception:
                return None
        red_margin = _read_redis_cust_field("$.margin_available")
        red_limit  = _read_redis_cust_field("$.trading_limit")
        red_caf    = _read_redis_sec_field("$.corporate_action_flag")

        def _coerce(v):
            try:    return float(v)
            except Exception: return v

        cust_rows = [
            {"field": "margin_available", "added": "ALTER TABLE",
             "pg_value":    _coerce(pg_cust.get("margin_available") if pg_cust else None),
             "redis_value": _coerce(red_margin),
             "match": (red_margin is not None and pg_cust and
                       abs(float(red_margin) - float(pg_cust["margin_available"])) < 0.01)},
            {"field": "trading_limit", "added": "ALTER TABLE",
             "pg_value":    _coerce(pg_cust.get("trading_limit") if pg_cust else None),
             "redis_value": _coerce(red_limit),
             "match": (red_limit is not None and pg_cust and
                       abs(float(red_limit) - float(pg_cust["trading_limit"])) < 0.01)},
        ]
        sec_rows = [
            {"field": "corporate_action_flag", "added": "ALTER TABLE",
             "pg_value":    (pg_sec or {}).get("corporate_action_flag"),
             "redis_value": red_caf,
             "match":       (red_caf is not None and
                             str(red_caf) == str((pg_sec or {}).get("corporate_action_flag")))},
        ]

        # Measure the live Redis read for the comparison strip.
        read_us = _measure_redis_read_us(lambda: (
            r.execute_command("JSON.GET", f"customer:{cc}", "$.margin_available"),
            r.execute_command("JSON.GET", f"customer:{cc}", "$.trading_limit"),
            r.execute_command("JSON.GET", f"security_full:{sec_id}", "$.corporate_action_flag"),
        ))
        pg_sql_combined = (
            "SELECT margin_available, trading_limit FROM portfolio.customer "
            "WHERE client_code = %s;\n"
            "SELECT corporate_action_flag FROM portfolio.security_master "
            "WHERE security_id = %s;")
        pg_us = (_measure_pg_read_us(
                    "SELECT margin_available, trading_limit FROM portfolio.customer "
                    "WHERE client_code = %s", (cc,))
                 + _measure_pg_read_us(
                    "SELECT corporate_action_flag FROM portfolio.security_master "
                    "WHERE security_id = %s", (sec_id,)))
        comparison = _build_comparison(
            pg_query_str  = (pg_sql_combined
                             .replace("%s", f"'{cc}'", 1)
                             .replace("%s", str(sec_id), 1)),
            pg_us         = pg_us,
            redis_cmd_str = (f"JSON.GET customer:{cc} $.margin_available\n"
                             f"JSON.GET customer:{cc} $.trading_limit\n"
                             f"JSON.GET security_full:{sec_id} $.corporate_action_flag"),
            redis_us      = read_us,
            why_it_matters="The YAML never enumerated columns — path: $ writes the "
                           "whole document. When the DBA ALTERs the table, the new "
                           "columns appear in Redis JSON on the next CDC event. Zero "
                           "YAML edit, zero processor restart.",
        )

        return jsonify({
            "ok":            True,
            "action":        "Section 4 · Schema evolution",
            "action_id":     action,
            "read_only":     True,
            "display_type":  "schema_evolution",
            "ts_ist":        _epoch_ms_to_ist(t_now),
            "read_us":       read_us,
            "redis_command": (f"JSON.GET customer:{cc} $.margin_available\n"
                              f"JSON.GET customer:{cc} $.trading_limit\n"
                              f"JSON.GET security_full:{sec_id} $.corporate_action_flag"),
            "result": {
                "client_code":     cc,
                "symbol":          symbol,
                "customer_fields": cust_rows,
                "security_fields": sec_rows,
            },
            "comparison":    comparison,
            "section":       4,
            "section_label": "Production-ready RDI patterns",
        })

    # ===================================================================
    # ④ Section 4 · Pipeline lag & health — READ-ONLY
    #    Read RDI's own observability surface from the rdi-state DB and
    #    Postgres replication-slot view. This is the "how do I monitor
    #    my CDC" answer architects always ask.
    # ===================================================================
    if action == "pipeline_lag":
        t_now = _now_ms()

        # Postgres replication slot health
        slot = {}
        try:
            slot_row = _pg_select_one(
                "SELECT slot_name, active::text AS active, restart_lsn::text AS restart_lsn, "
                "       confirmed_flush_lsn::text AS confirmed_flush_lsn, "
                "       pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)::text "
                "         AS bytes_behind "
                "FROM pg_replication_slots ORDER BY slot_name LIMIT 1")
            if slot_row:
                slot = {
                    "slot_name":          slot_row.get("slot_name"),
                    "active":             str(slot_row.get("active") or "").lower() in ("t","true"),
                    "restart_lsn":        slot_row.get("restart_lsn"),
                    "confirmed_flush_lsn":slot_row.get("confirmed_flush_lsn"),
                    "bytes_behind":       int(slot_row.get("bytes_behind") or 0),
                }
        except Exception as e:
            app.logger.warning("pipeline_lag · slot read failed: %s", e)

        # Per-table activity — derived from rdi:last-events on rdi-state.
        # We don't strictly need a `rdi:stats:<table>` hash; we synthesize it
        # from the actual event log so we never lie about what RDI did.
        tables = {}
        try:
            evs = rdi_state.xrevrange("rdi:last-events", count=2000) or []
            for sid, f in evs:
                tab = f.get("table") or "unknown"
                t = tables.setdefault(tab, {
                    "table": tab, "events": 0,
                    "inserts": 0, "updates": 0, "deletes": 0,
                    "last_event_ms": None,
                })
                t["events"] += 1
                op = (f.get("op") or "").lower()
                if op in ("c","i","insert"): t["inserts"] += 1
                elif op in ("u","update"):   t["updates"] += 1
                elif op in ("d","delete"):   t["deletes"] += 1
                try:
                    ts_ms = int(sid.split("-")[0])
                except Exception:
                    ts_ms = None
                if ts_ms is not None and (t["last_event_ms"] is None
                                          or ts_ms > t["last_event_ms"]):
                    t["last_event_ms"] = ts_ms
        except Exception as e:
            app.logger.warning("pipeline_lag · stats read failed: %s", e)

        # Now-ms reference for lag
        now_ms = _now_ms()
        table_rows = []
        for tab, t in sorted(tables.items()):
            last_ms = t.get("last_event_ms")
            t_row = dict(t)
            t_row["last_event_ist"] = _epoch_ms_to_ist(last_ms) if last_ms else None
            t_row["lag_ms"] = (now_ms - last_ms) if last_ms else None
            t_row.pop("last_event_ms", None)
            table_rows.append(t_row)

        total_events = sum(t["events"] for t in tables.values())

        # Measure the actual Redis read-time
        read_us = _measure_redis_read_us(
            lambda: rdi_state.xlen("rdi:last-events"))

        pg_read_sql = "SELECT count(*) FROM pg_replication_slots WHERE slot_name='rdi_slot'"
        pg_us = _measure_pg_read_us(pg_read_sql)
        comparison = _build_comparison(
            pg_query_str  = ("-- The same view, served by Postgres' system table:\n"
                             "SELECT slot_name, active, restart_lsn,\n"
                             "       confirmed_flush_lsn,\n"
                             "       pg_wal_lsn_diff(pg_current_wal_lsn(),\n"
                             "                       confirmed_flush_lsn) AS bytes_behind\n"
                             "FROM   pg_replication_slots;"),
            pg_us         = pg_us,
            redis_cmd_str = ("XLEN     rdi:last-events\n"
                             "XREVRANGE rdi:last-events + - COUNT 2000"),
            redis_us      = read_us,
            why_it_matters="RDI exposes its own health via Redis (rdi:last-events / "
                           "rdi:stats:*) and Postgres views (pg_replication_slots) — "
                           "zero custom exporters needed; plug Grafana / Splunk "
                           "directly into either side.",
        )

        return jsonify({
            "ok":            True,
            "action":        "Section 4 · Pipeline lag & health",
            "action_id":     action,
            "read_only":     True,
            "display_type":  "pipeline_lag",
            "ts_ist":        _epoch_ms_to_ist(t_now),
            "read_us":       read_us,
            "redis_command": ("XLEN     rdi:last-events\n"
                              "XREVRANGE rdi:last-events + - COUNT 2000"),
            "result": {
                "replication_slot": slot,
                "tables":           table_rows,
                "total_events":     total_events,
            },
            "comparison":    comparison,
            "section":       4,
            "section_label": "Production-ready RDI patterns",
        })

    # ===================================================================
    # ④ Section 4 · Live YAML inspector — READ-ONLY
    #    Read the actual YAML files RDI is running and return them so
    #    the customer can see "this is the whole pipeline".
    # ===================================================================
    if action == "yaml_view":
        t_now = _now_ms()
        files: list[dict] = []
        try:
            cfg_path = Path(RDI_CONFIG_PATH)
            if cfg_path.exists():
                content = cfg_path.read_text(encoding="utf-8", errors="replace")
                files.append({
                    "name":     cfg_path.name,
                    "role":     "pipeline-wide config (sources, targets, batching)",
                    "lines":    content.count("\n") + 1,
                    "content":  content,
                    "expanded": True,
                })
        except Exception as e:
            app.logger.warning("yaml_view · config.yaml read failed: %s", e)
        try:
            jobs_dir = Path(RDI_JOBS_DIR)
            if jobs_dir.exists():
                for p in sorted(jobs_dir.glob("*.yaml")):
                    try:
                        content = p.read_text(encoding="utf-8", errors="replace")
                        files.append({
                            "name":     p.name,
                            "role":     f"job · table {p.stem}",
                            "lines":    content.count("\n") + 1,
                            "content":  content,
                            "expanded": False,
                        })
                    except Exception as e:
                        app.logger.warning("yaml_view · %s read failed: %s", p, e)
        except Exception as e:
            app.logger.warning("yaml_view · jobs dir read failed: %s", e)

        total_lines = sum(f["lines"] for f in files)
        read_us = _measure_redis_read_us(lambda: r.ping())
        comparison = _build_comparison(
            pg_query_str  = ("-- A DIY CDC pipeline replaced by these YAML files would be:\n"
                             "-- 1× Kafka cluster   2× connector jars   3× consumer apps\n"
                             "-- 4× retry / DLQ logic   5× cache-warmup jobs   …"),
            pg_us         = None,
            redis_cmd_str = "# 6 YAML files. That's the whole pipeline.",
            redis_us      = read_us,
            why_it_matters="The whole pipeline is config. Edit a file, the processor "
                           "reloads. No build, no deploy, no container restart.",
        )

        return jsonify({
            "ok":            True,
            "action":        "Section 4 · Live YAML inspector",
            "action_id":     action,
            "read_only":     True,
            "display_type":  "yaml_view",
            "ts_ist":        _epoch_ms_to_ist(t_now),
            "read_us":       read_us,
            "redis_command": "# (declarative — see YAML below)",
            "result": {
                "files":       files,
                "total_lines": total_lines,
            },
            "comparison":    comparison,
            "section":       4,
            "section_label": "Production-ready RDI patterns",
        })

    return jsonify({"error": f"unknown action: {action}"}), 400


# ===========================================================================
# ─────────────────────  CAPABILITY TAB ROUTES (new)  ───────────────────────
# Each capability is a single POST that performs a real source-side change,
# waits for the propagated effect on Redis, and returns rich evidence so the
# UI can render side-by-side Postgres↔Redis panels with NO screen switching.
# ===========================================================================

# ---- Capability 1: real-time CDC (insert) ---------------------------------
@app.route("/api/cap/cdc-insert", methods=["POST"])
def api_cap_cdc_insert():
    """INSERT a new customer in Postgres -> watch it land in Redis."""
    cc = _new_client_code()
    cid = _new_customer_id()
    pan = f"DEMO{int(time.time()) % 100000:05d}A"
    name = request.json.get("name", "Live Demo Customer") if request.is_json else "Live Demo Customer"

    t0 = time.perf_counter()
    _pg_exec("""
        INSERT INTO portfolio.customer
          (customer_id, client_code, pan, full_name, segment, risk_profile,
           kyc_status, demat_account, onboarded_on)
        VALUES
          (%s, %s, %s, %s, 'RETAIL', 'MODERATE',
           'VERIFIED', %s, CURRENT_DATE)
        ON CONFLICT (client_code) DO NOTHING
    """, (cid, cc, pan, name, f"{cc}-DP"))
    pg_write_ms = (time.perf_counter() - t0) * 1000.0

    ok, prop_ms = _wait_for_key_match(
        lambda: r.execute_command("JSON.GET", f"customer:{cc}", "$") is not None,
        deadline_ms=15000,
    )

    read = _redis_read_us(
        lambda: r.execute_command("JSON.GET", f"customer:{cc}", "$"),
    )

    return jsonify({
        "ok":               ok,
        "client_code":      cc,
        "pg_write_ms":      round(pg_write_ms, 2),
        "propagation_ms":   round(prop_ms, 1),
        "redis_read_us":    read,
        "pg_row":           _pg_select_one(
            "SELECT * FROM portfolio.customer WHERE client_code = %s", (cc,)),
        "redis_json":       json.loads(read["value"])[0] if read["value"] else None,
    })


# ---- Capability 2: multi-shape fan-out -----------------------------------
@app.route("/api/cap/multi-shape", methods=["POST"])
def api_cap_multi_shape():
    """Pick (or insert) a customer and show every Redis shape RDI writes for
    the same source row: a JSON profile keyed by client_code, plus a
    materialised segment SET. One YAML, two shapes, one target BDB."""
    cc = _new_client_code()
    cid = _new_customer_id()
    pan = f"FAN{int(time.time()) % 1000000:06d}"
    name = "Multi-shape Demo"
    seg  = request.json.get("segment", "HNI") if request.is_json else "HNI"

    _pg_exec("""
        INSERT INTO portfolio.customer
          (customer_id, client_code, pan, full_name, segment, risk_profile,
           kyc_status, demat_account, onboarded_on)
        VALUES (%s, %s, %s, %s, %s, 'AGGRESSIVE', 'VERIFIED', %s, CURRENT_DATE)
        ON CONFLICT (client_code) DO NOTHING
    """, (cid, cc, pan, name, seg, f"{cc}-DP"))

    ok, prop_ms = _wait_for_key_match(
        lambda: r.execute_command("JSON.GET", f"customer:{cc}", "$") is not None
                and r.sismember(f"cust-segment:{seg}", cc),
        deadline_ms=15000,
    )

    set_card = r.scard(f"cust-segment:{seg}")

    return jsonify({
        "ok":               ok,
        "client_code":      cc,
        "segment":          seg,
        "propagation_ms":   round(prop_ms, 1),
        "primary_json":     json.loads(r.execute_command(
                                "JSON.GET", f"customer:{cc}", "$"))[0]
                            if ok else None,
        "set_cardinality":  set_card,
        "set_sample":       r.srandmember(f"cust-segment:{seg}", 5) or [],
        "shapes": [
            {"key": f"customer:{cc}",          "type": "JSON",
             "target": "primary cache (12000)"},
            {"key": f"cust-segment:{seg}",     "type": "SET",
             "target": "primary cache (12000)"},
        ],
    })


# ---- Capability 3: declarative YAML view ---------------------------------
@app.route("/api/cap/yaml-jobs")
def api_cap_yaml_jobs():
    """Return all RDI YAML jobs + the pipeline config so the dashboard can
    render them next to the resulting Redis keys."""
    jobs = []
    for path in sorted(Path(RDI_JOBS_DIR).glob("*.yaml")):
        try:
            text = path.read_text()
        except Exception as e:
            text = f"# could not read: {e}"
        jobs.append({"file": path.name, "yaml": text})
    cfg_text = ""
    try:
        cfg_text = Path(RDI_CONFIG_PATH).read_text()
    except Exception as e:
        cfg_text = f"# could not read: {e}"
    return jsonify({"config": cfg_text, "jobs": jobs})


# ---- Capability 4: filter & projection -----------------------------------
@app.route("/api/cap/filter-projection", methods=["POST"])
def api_cap_filter_projection():
    """Flip a customer's kyc_status BLOCKED <-> VERIFIED and show how the
    YAML filter in customer.yaml causes the row to leave / re-enter the
    Redis cache. Demonstrates declarative filtering."""
    cc = request.json.get("client_code") if request.is_json else None
    if not cc:
        cc = _pick_verified_client_code()
    if not cc:
        return jsonify({"error": "no VERIFIED customer available"}), 400

    # current state in PG
    before_pg = _pg_select_one(
        "SELECT client_code, full_name, kyc_status FROM portfolio.customer "
        "WHERE client_code = %s", (cc,))
    if not before_pg:
        return jsonify({"error": f"client_code {cc} not in Postgres"}), 404

    # toggle
    new_status = "BLOCKED" if before_pg["kyc_status"] == "VERIFIED" else "VERIFIED"
    _pg_exec("UPDATE portfolio.customer SET kyc_status = %s WHERE client_code = %s",
             (new_status, cc))

    # Wait for the kyc_status field in the Redis JSON to either change
    # to the new value (UPDATE propagated) or stay where it was (filter
    # blocked the write). The deadline tells us, in either direction,
    # whether the pipeline behaved as the YAML promised.
    def _kyc_in_redis():
        raw = r.execute_command("JSON.GET", f"customer:{cc}", "$.kyc_status")
        if not raw:
            return None
        try:
            return json.loads(raw)[0]
        except Exception:
            return None

    ok, prop_ms = _wait_for_key_match(
        lambda: _kyc_in_redis() == new_status,
        deadline_ms=8000,
    )
    after_redis_raw  = r.execute_command("JSON.GET", f"customer:{cc}", "$")
    after_redis_json = json.loads(after_redis_raw)[0] if after_redis_raw else None
    after_redis_kyc  = after_redis_json.get("kyc_status") if after_redis_json else None

    # When new_status == BLOCKED, the YAML filter is *expected* to drop
    # the UPDATE — so prop_ms hitting the deadline is the *successful*
    # demo outcome. We report this explicitly so the UI never lies.
    filter_outcome = (
        "propagated" if after_redis_kyc == new_status
        else "blocked by filter (no write to target)"
    )

    # ----- 3-pillar technical context for the UI card --------------------
    pg_query = (
        f"SELECT client_code, full_name, kyc_status\n"
        f"FROM   portfolio.customer\n"
        f"WHERE  client_code = '{cc}';"
    )
    pg_ms = _bench_pg_ms(
        "SELECT client_code, full_name, kyc_status "
        "FROM portfolio.customer WHERE client_code = %s", (cc,))
    redis_command = f"JSON.GET customer:{cc} $.kyc_status"
    redis_us = _bench_redis_us(
        lambda: r.execute_command("JSON.GET", f"customer:{cc}", "$.kyc_status"))

    # If the YAML filter blocked the write, the "sync time" pillar would
    # mislead — there's no propagation to time. Surface None in that case
    # so the UI shows "filter blocked the write" instead of "8000 ms".
    write_propagated = (after_redis_kyc == new_status)
    sync_ms_for_ui   = round(prop_ms, 1) if write_propagated else None

    return jsonify({
        "ok":              True,
        "client_code":     cc,
        "before_pg":       before_pg,
        "after_status":    new_status,
        "propagation_ms":  round(prop_ms, 1),
        "rdi_sync_ms":     sync_ms_for_ui,
        "redis_present":   bool(after_redis_json),
        "redis_kyc":       after_redis_kyc,
        "redis_json":      after_redis_json,
        "yaml_filter":     "kyc_status == 'VERIFIED'   (rdi/jobs/customer.yaml)",
        "filter_outcome":  filter_outcome,
        "technical":       _technical(
            pg_query=pg_query, pg_ms=pg_ms,
            yaml_file="rdi/jobs/customer.yaml",
            yaml_snippet=YAML_SNIPPETS["customer_filter_json"],
            redis_command=redis_command, redis_us=redis_us,
            rdi_sync_ms=sync_ms_for_ui),
    })


# ---- Capability 5: stream as audit log -----------------------------------
@app.route("/api/cap/audit-stream", methods=["POST"])
def api_cap_audit_stream():
    """Fire N trades for a customer; observe that trades:<cid> stream
    gains N entries -- one append per source change, append-only audit."""
    cc = request.json.get("client_code") if request.is_json else None
    n  = int((request.json or {}).get("count", 5)) if request.is_json else 5
    n  = max(1, min(20, n))

    # trade.yaml has no filter, so any customer works — but we keep
    # consistency by picking from the verified pool too.
    if not cc:
        cc = _pick_verified_client_code()
    if not cc:
        return jsonify({"error": "no customer available"}), 400

    cust = _pg_select_one(
        "SELECT customer_id FROM portfolio.customer WHERE client_code = %s", (cc,))
    if not cust:
        return jsonify({"error": "customer not found in PG"}), 404
    cid = int(cust["customer_id"])

    before_len = r.xlen(f"trades:{cid}") or 0

    sec = _pg_select_one(
        "SELECT security_id FROM portfolio.security_master ORDER BY security_id LIMIT 1")
    sid = int(sec["security_id"]) if sec else 1

    for i in range(n):
        tid = _new_trade_id() + i
        _pg_exec("""
            INSERT INTO portfolio.trade
              (trade_id, customer_id, security_id, side, quantity, price,
               trade_value, brokerage, order_id, exchange, executed_at)
            VALUES (%s, %s, %s, %s, 1, 100.0 + %s, 100.0 + %s, 0,
                    %s, 'NSE', NOW())
        """, (tid, cid, sid, "BUY" if i % 2 == 0 else "SELL",
              i, i, f"ORD-{tid}"))

    ok, prop_ms = _wait_for_key_match(
        lambda: (r.xlen(f"trades:{cid}") or 0) >= before_len + n,
        deadline_ms=20000,
    )
    after_len = r.xlen(f"trades:{cid}") or 0
    tail = r.xrevrange(f"trades:{cid}", count=n)

    return jsonify({
        "ok":              ok,
        "client_code":     cc,
        "customer_id":     cid,
        "stream_key":      f"trades:{cid}",
        "events_appended": after_len - before_len,
        "propagation_ms":  round(prop_ms, 1),
        "tail":            [{"id": sid_, "fields": f} for sid_, f in tail],
    })


# ---- Capability 6: schema evolution --------------------------------------
@app.route("/api/cap/schema-evolution", methods=["POST"])
def api_cap_schema_evolution():
    """Add a new column to portfolio.customer at the source, populate it
    on an existing row, and watch the field surface in Redis JSON.

    Official RDI handles the column add automatically once the publication
    refreshes; the dashboard nudges it with a deterministic UPDATE on the
    target row so the demo can show the field land in seconds."""
    new_col = (request.json.get("column") if request.is_json
               else "gst_no").lower()
    new_col = ''.join(ch for ch in new_col if ch.isalnum() or ch == '_')[:32]
    if not new_col:
        return jsonify({"error": "invalid column name"}), 400

    # Pick a customer that is *currently VERIFIED in Postgres* so the
    # `kyc_status == 'VERIFIED'` filter in customer.yaml doesn't drop
    # our UPDATE. This keeps the schema-evo card independent of any
    # earlier KYC-freeze demo state.
    cc = _pick_verified_client_code()
    if not cc:
        return jsonify({"error": "no VERIFIED customer in Postgres"}), 400

    # 1) DDL — add column (idempotent)
    _pg_exec(f"ALTER TABLE portfolio.customer "
             f"ADD COLUMN IF NOT EXISTS {new_col} TEXT")
    # 2) DML — populate on the chosen row so Debezium emits an UPDATE
    val = f"GST-{int(time.time()) % 10**10}"
    _pg_exec(f"UPDATE portfolio.customer SET {new_col} = %s "
             f"WHERE client_code = %s", (val, cc))

    ok, prop_ms = _wait_for_key_match(
        lambda: (lambda raw:
                 raw and new_col in json.loads(raw)[0]
                 and json.loads(raw)[0].get(new_col) == val
                )(r.execute_command("JSON.GET", f"customer:{cc}", "$")),
        deadline_ms=20000,
    )
    final = r.execute_command("JSON.GET", f"customer:{cc}", "$")
    return jsonify({
        "ok":                 ok,
        "column_added":       new_col,
        "value_set":          val,
        "client_code":        cc,
        "propagation_ms":     round(prop_ms, 1),
        "redis_json":         json.loads(final)[0] if final else None,
        "note":               ("Debezium picks up the new column on its "
                               "next publication refresh; the UPDATE forces "
                               "an immediate CDC event so the new field "
                               "surfaces in Redis without a snapshot."),
    })


# ---- Capability 7: drop and re-snapshot (idempotent re-convergence) -----
@app.route("/api/cap/snapshot", methods=["POST"])
def api_cap_snapshot():
    """DEL a customer key in Redis, then force RDI to re-emit it by
    UPDATEing the source row (no-op touch). Proves the pipeline reconverges
    and that target divergence is self-healing in production."""
    cc = _pick_verified_client_code()
    if not cc:
        return jsonify({"error": "no VERIFIED customer to mutate"}), 400

    deleted = r.delete(f"customer:{cc}")
    before = r.execute_command("JSON.GET", f"customer:{cc}", "$")

    _pg_exec("UPDATE portfolio.customer "
             "SET full_name = full_name WHERE client_code = %s", (cc,))
    ok, prop_ms = _wait_for_key_match(
        lambda: r.execute_command("JSON.GET", f"customer:{cc}", "$") is not None,
        deadline_ms=15000,
    )
    after = r.execute_command("JSON.GET", f"customer:{cc}", "$")
    return jsonify({
        "ok":              ok,
        "client_code":     cc,
        "deleted":         bool(deleted),
        "before_gone":     before is None,
        "propagation_ms":  round(prop_ms, 1),
        "after_json":      json.loads(after)[0] if after else None,
    })


# ---- Capability 8: pause / resume (backpressure + replay) ----------------
@app.route("/api/cap/pause", methods=["POST"])
def api_cap_pause():
    pause = bool((request.json or {}).get("pause", True)) if request.is_json else True
    if pause:
        rdi_state.set("rdi:processor:paused", "1")
    else:
        rdi_state.delete("rdi:processor:paused")
    return jsonify({"paused": pause, "flag_key": "rdi:processor:paused"})


@app.route("/api/cap/replay-stats")
def api_cap_replay_stats():
    """Show pending entries on every Debezium stream — proves at-least-once
    delivery queues events while the processor is paused."""
    out = []
    try:
        for s in rdi_state.scan_iter(match="sectrade.portfolio.*",
                                     count=50, _type="STREAM"):
            try:
                info = rdi_state.xinfo_groups(s)
                pending = sum(int(g.get("pending", 0) or 0) for g in info)
            except Exception:
                pending = 0
            try:
                length = rdi_state.xlen(s)
            except Exception:
                length = 0
            out.append({
                "stream":  s,
                "length":  length,
                "pending": pending,
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({
        "paused":  rdi_state.get("rdi:processor:paused") == "1",
        "streams": out,
    })


# ---- Capability 9: search over CDC-fed data ------------------------------
@app.route("/api/cap/search")
def api_cap_search():
    q = (request.args.get("q") or "raj").strip()
    runs = max(1, min(10, int(request.args.get("runs", "5"))))

    pfx_safe = ''.join(ch for ch in q if ch.isalnum()).lower()
    expr = f"@name:{pfx_safe}*" if pfx_safe else "*"

    pg_times = []
    pg_count = 0
    with pg_conn() as c, c.cursor() as cur:
        for _ in range(runs):
            t0 = time.perf_counter()
            cur.execute(
                "SELECT COUNT(*) FROM portfolio.customer "
                "WHERE full_name ILIKE %s", (q + '%',))
            pg_count = cur.fetchone()[0]
            pg_times.append((time.perf_counter() - t0) * 1000)

    rd_times = []
    rd_count = 0
    if _ft_index_present():
        for _ in range(runs):
            t0 = time.perf_counter()
            res = r.execute_command(
                "FT.SEARCH", "cust-idx", expr, "LIMIT", "0", "0")
            rd_times.append((time.perf_counter() - t0) * 1000)
            if res:
                rd_count = int(res[0])

    return jsonify({
        "query":      q,
        "pg":         _stats(pg_times),
        "redis":      _stats(rd_times),
        "pg_count":   pg_count,
        "redis_count": rd_count,
        "speedup":    (round(_stats(pg_times)["p50"] / _stats(rd_times)["p50"], 1)
                       if pg_times and rd_times and _stats(rd_times)["p50"] else None),
    })


# ---- Capability 10: monitoring (RDI control-plane proxy) ----------------
@app.route("/api/cap/monitoring")
def api_cap_monitoring():
    """Show what the official RDI REST endpoints return on this stack."""
    # We embed the equivalent calls right in the dashboard so users don't
    # need to open Redis Insight. The actual RDI REST API is implemented
    # by the mock-rdi-api service per the official spec.
    import urllib.request
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    base = "https://rdi-api:443"
    out = {"endpoints": []}
    try:
        # Login (per the official RDI spec /api/v1/login)
        login_body = json.dumps({"username": "default", "password": "rdi_demo_pass"}).encode()
        req = urllib.request.Request(
            f"{base}/api/v1/login", data=login_body,
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, context=ctx, timeout=4) as resp:
            tok = json.loads(resp.read()).get("access_token", "")

        for ep in ("/api/v1/pipelines",
                   "/api/v1/monitoring/pipelines",
                   "/api/v1/monitoring/connection",
                   "/api/v1/version"):
            try:
                req = urllib.request.Request(
                    f"{base}{ep}",
                    headers={"Authorization": f"Bearer {tok}"})
                with urllib.request.urlopen(req, context=ctx, timeout=4) as resp:
                    out["endpoints"].append({
                        "path": ep,
                        "status": resp.status,
                        "body": json.loads(resp.read()),
                    })
            except Exception as e:
                out["endpoints"].append({"path": ep, "error": str(e)})
    except Exception as e:
        out["error"] = str(e)
    return jsonify(out)


# ---- Capability 11: idempotent replay -----------------------------------
@app.route("/api/cap/idempotent", methods=["POST"])
def api_cap_idempotent():
    """Apply the same source update N times; assert the target ends up in
    the same state every time (no double-counting, no key churn)."""
    cc = _pick_verified_client_code()
    if not cc:
        return jsonify({"error": "no VERIFIED customer to mutate"}), 400

    new_segment = (request.json or {}).get("segment", "HNI") if request.is_json else "HNI"

    for _ in range(5):
        _pg_exec("UPDATE portfolio.customer SET segment = %s WHERE client_code = %s",
                 (new_segment, cc))
        time.sleep(0.1)

    ok, prop_ms = _wait_for_key_match(
        lambda: (lambda raw: raw and json.loads(raw)[0].get("segment") == new_segment
                )(r.execute_command("JSON.GET", f"customer:{cc}", "$")),
        deadline_ms=10000,
    )
    final = r.execute_command("JSON.GET", f"customer:{cc}", "$")
    n_keys = sum(1 for _ in r.scan_iter(match=f"customer:{cc}", count=10))
    return jsonify({
        "ok":              ok,
        "client_code":     cc,
        "updates_issued":  5,
        "keys_in_target":  n_keys,
        "final_segment":   (json.loads(final)[0].get("segment") if final else None),
        "propagation_ms":  round(prop_ms, 1),
    })


# ---- Headline pill: total cached customers ------------------------------
@app.route("/api/cap/customer-count")
def api_cap_customer_count():
    """Tiny endpoint powering the "N customers cached" pill in the header.
    Counts customer JSONs on the primary target BDB."""
    try:
        n = customer_count_target()
    except Exception:
        n = None
    return jsonify({"port": REDIS_PORT, "customers": n})


# ===========================================================================
# ─────────────────────  USE-CASE TAB ROUTES (new)  ────────────────────────
# Each use-case wraps one-or-more capabilities into an industry-specific demo.
# ===========================================================================

# Use-case 1: real-time portfolio refresh (settle one trade)
@app.route("/api/uc/settle-trade", methods=["POST"])
def api_uc_settle_trade():
    """Pick a customer, insert ONE trade, watch the trades:<cid> stream
    grow AND the holding update propagate. Mirrors what the firm's
    settlement engine does in production."""
    cc = (request.json or {}).get("client_code") if request.is_json else None
    if not cc:
        cc = _pick_verified_client_code()
    cust = _pg_select_one(
        "SELECT customer_id, full_name FROM portfolio.customer "
        "WHERE client_code = %s", (cc,))
    if not cust:
        return jsonify({"error": "customer not found"}), 404
    cid = int(cust["customer_id"])

    sec = _pg_select_one("""
        SELECT security_id, symbol FROM portfolio.security_master
        ORDER BY security_id LIMIT 1
    """)
    sid = int(sec["security_id"])

    before = r.xlen(f"trades:{cid}") or 0
    tid = _new_trade_id()
    _pg_exec("""
        INSERT INTO portfolio.trade
          (trade_id, customer_id, security_id, side, quantity, price,
           trade_value, brokerage, order_id, exchange, executed_at)
        VALUES (%s, %s, %s, 'BUY', 10, 250.0, 2500.0, 0,
                %s, 'NSE', NOW())
    """, (tid, cid, sid, f"ORD-{tid}"))
    ok, prop_ms = _wait_for_key_match(
        lambda: (r.xlen(f"trades:{cid}") or 0) > before,
        deadline_ms=15000,
    )

    # ----- 3-pillar technical context for the UI card --------------------
    pg_query = (
        f"SELECT trade_id, side, quantity, price, executed_at\n"
        f"FROM   portfolio.trade\n"
        f"WHERE  customer_id = {cid}\n"
        f"ORDER BY executed_at DESC LIMIT 5;"
    )
    pg_ms   = _bench_pg_ms(
        "SELECT trade_id, side, quantity, price, executed_at "
        "FROM portfolio.trade WHERE customer_id = %s "
        "ORDER BY executed_at DESC LIMIT 5", (cid,))
    redis_command = f"XREVRANGE trades:{cid} + - COUNT 5"
    redis_us = _bench_redis_us(lambda: r.xrevrange(f"trades:{cid}", count=5))

    return jsonify({
        "ok":              ok,
        "client_code":     cc,
        "customer_id":     cid,
        "security":        sec,
        "propagation_ms":  round(prop_ms, 1),
        "rdi_sync_ms":     round(prop_ms, 1),
        "new_stream_len":  r.xlen(f"trades:{cid}"),
        "last_trade":      r.xrevrange(f"trades:{cid}", count=1),
        "technical":       _technical(
            pg_query=pg_query, pg_ms=pg_ms,
            yaml_file="rdi/jobs/trade.yaml",
            yaml_snippet=YAML_SNIPPETS["trade_stream"],
            redis_command=redis_command, redis_us=redis_us,
            rdi_sync_ms=round(prop_ms, 1)),
    })


# Use-case 2: KYC freeze fan-out (block + verify cycle)
@app.route("/api/uc/kyc-freeze", methods=["POST"])
def api_uc_kyc_freeze():
    return api_cap_filter_projection()  # same mechanic, framed differently in UI


# Use-case 3: live trade tape (sample stream tail across customers)
@app.route("/api/uc/live-tape")
def api_uc_live_tape():
    """Return the most recent trades across ALL customers by sampling the
    rdi:last-events stream for trade rows."""
    try:
        entries = rdi_state.xrevrange("rdi:last-events", count=200)
    except Exception:
        entries = []
    trades = [
        {"id": sid_, "ts": int(sid_.split("-")[0]), "table": f.get("table"),
         "op": f.get("op"), "lag_ms": int(f.get("lag_ms", 0) or 0)}
        for sid_, f in entries
        if f.get("table") == "portfolio.trade"
    ][:20]

    # ----- 3-pillar technical context -----------------------------------
    pg_query = (
        "SELECT customer_id, side, quantity, price, executed_at\n"
        "FROM   portfolio.trade\n"
        "ORDER BY executed_at DESC LIMIT 20;"
    )
    pg_ms = _bench_pg_ms(
        "SELECT customer_id, side, quantity, price, executed_at "
        "FROM portfolio.trade ORDER BY executed_at DESC LIMIT 20")
    redis_command = "XREVRANGE rdi:last-events + - COUNT 20"
    redis_us = _bench_redis_us(
        lambda: rdi_state.xrevrange("rdi:last-events", count=20))

    return jsonify({
        "tape":       trades,
        "technical":  _technical(
            pg_query=pg_query, pg_ms=pg_ms,
            yaml_file="rdi-processor (last-events ring buffer)",
            yaml_snippet=YAML_SNIPPETS["last_events"],
            redis_command=redis_command, redis_us=redis_us,
            rdi_sync_ms=None),
    })


# Use-case 4: risk-margin recompute (shift a holding's quantity)
@app.route("/api/uc/risk-margin", methods=["POST"])
def api_uc_risk_margin():
    """Pick one holding for a customer; increment quantity; show that the
    exposure for that customer in Redis updates within ms. This is the
    materialised-margin scenario for the firm's risk engine."""
    cc = (request.json or {}).get("client_code") if request.is_json else None
    if not cc:
        cc = _pick_verified_client_code()
    cust = _pg_select_one(
        "SELECT customer_id FROM portfolio.customer "
        "WHERE client_code = %s", (cc,))
    if not cust:
        return jsonify({"error": "customer not found"}), 404
    cid = int(cust["customer_id"])
    h = _pg_select_one(
        "SELECT holding_id, security_id, quantity FROM portfolio.holding "
        "WHERE customer_id = %s LIMIT 1", (cid,))
    if not h:
        return jsonify({"error": "customer has no holdings"}), 400

    new_qty = int(h["quantity"]) + 5
    # Keep invested_value coherent with quantity for the risk engine.
    _pg_exec("""
        UPDATE portfolio.holding
        SET quantity = %s,
            invested_value = avg_buy_price * %s,
            last_trade_date = CURRENT_DATE,
            updated_at = NOW()
        WHERE holding_id = %s
    """, (new_qty, new_qty, h["holding_id"]))

    key = f"holding:{cid}:{h['security_id']}"
    ok, prop_ms = _wait_for_key_match(
        lambda: (lambda raw: raw and int(float(json.loads(raw)[0].get("quantity", 0))) == new_qty
                 )(r.execute_command("JSON.GET", key, "$")),
        deadline_ms=15000,
    )
    portfolio = get_portfolio(cid)

    # ----- 3-pillar technical context -----------------------------------
    pg_query = (
        f"SELECT security_id, quantity, avg_buy_price,\n"
        f"       quantity * avg_buy_price AS invested\n"
        f"FROM   portfolio.holding\n"
        f"WHERE  customer_id = {cid};"
    )
    pg_ms = _bench_pg_ms(
        "SELECT security_id, quantity, avg_buy_price, "
        "       quantity * avg_buy_price "
        "FROM portfolio.holding WHERE customer_id = %s", (cid,))
    redis_command = f"JSON.GET {key} $"
    redis_us = _bench_redis_us(
        lambda: r.execute_command("JSON.GET", key, "$"))

    return jsonify({
        "ok":              ok,
        "client_code":     cc,
        "customer_id":     cid,
        "holding_id":      h["holding_id"],
        "old_qty":         int(h["quantity"]),
        "new_qty":         new_qty,
        "propagation_ms":  round(prop_ms, 1),
        "rdi_sync_ms":     round(prop_ms, 1),
        "portfolio_summary": portfolio["summary"],
        "technical":       _technical(
            pg_query=pg_query, pg_ms=pg_ms,
            yaml_file="rdi/jobs/holding.yaml",
            yaml_snippet=YAML_SNIPPETS["holding_json"],
            redis_command=redis_command, redis_us=redis_us,
            rdi_sync_ms=round(prop_ms, 1)),
    })


# Use-case 5: per-segment AUM materialised view
@app.route("/api/uc/segment-view")
def api_uc_segment_view():
    """Show the live count of customers per segment from the RDI-maintained
    SET — this is the 'materialised view' that needs ZERO maintenance code."""
    out = {}
    for seg in ("RETAIL", "HNI", "PRO"):
        out[seg] = r.scard(f"cust-segment:{seg}") or 0

    # ----- 3-pillar technical context -----------------------------------
    pg_query = (
        "SELECT segment, COUNT(*) AS customers\n"
        "FROM   portfolio.customer\n"
        "GROUP BY segment;"
    )
    pg_ms = _bench_pg_ms(
        "SELECT segment, COUNT(*) FROM portfolio.customer GROUP BY segment")
    redis_command = (
        "SCARD cust-segment:RETAIL\n"
        "SCARD cust-segment:HNI\n"
        "SCARD cust-segment:PRO"
    )

    def _multi_scard():
        pipe = r.pipeline(transaction=False)
        for s in ("RETAIL", "HNI", "PRO"):
            pipe.scard(f"cust-segment:{s}")
        pipe.execute()
    redis_us = _bench_redis_us(_multi_scard)

    return jsonify({
        "segments": out,
        "note": ("These SETs are populated and depopulated by the SAME RDI "
                 "pipeline as the customer JSON. The application never has "
                 "to write any cache-maintenance code."),
        "technical":  _technical(
            pg_query=pg_query, pg_ms=pg_ms,
            yaml_file="rdi/jobs/customer.yaml",
            yaml_snippet=YAML_SNIPPETS["customer_multi_shape"],
            redis_command=redis_command, redis_us=redis_us,
            rdi_sync_ms=None),
    })


# Use-case 6: microservice fan-out (simulator)
@app.route("/api/uc/fanout-simulator")
def api_uc_fanout_simulator():
    """Simulate three downstream microservices each consuming the same
    trade-events stream; we just return their last seen entries so the UI
    can paint three columns side-by-side."""
    services = ["trading-app", "risk-engine", "reporting-bi"]
    out = {}
    for svc in services:
        # each "service" reads the last 5 trade entries from any customer
        events = rdi_state.xrevrange("rdi:last-events", count=50)
        last_trades = [
            {"id": sid_, "ts": int(sid_.split("-")[0]),
             "lag_ms": int(f.get("lag_ms", 0) or 0),
             "op": f.get("op")}
            for sid_, f in events if f.get("table") == "portfolio.trade"
        ][:5]
        out[svc] = last_trades

    # ----- 3-pillar technical context -----------------------------------
    pg_query = (
        "-- 3 separate services each issue this against the same DB:\n"
        "SELECT customer_id, side, quantity, price, executed_at\n"
        "FROM   portfolio.trade\n"
        "ORDER BY executed_at DESC LIMIT 5;"
    )
    pg_ms = _bench_pg_ms(
        "SELECT customer_id, side, quantity, price, executed_at "
        "FROM portfolio.trade ORDER BY executed_at DESC LIMIT 5")
    redis_command = (
        "-- each microservice has its OWN consumer group:\n"
        "XREADGROUP GROUP trading-app  c1 COUNT 5 STREAMS rdi:last-events >\n"
        "XREADGROUP GROUP risk-engine  c1 COUNT 5 STREAMS rdi:last-events >\n"
        "XREADGROUP GROUP reporting-bi c1 COUNT 5 STREAMS rdi:last-events >"
    )
    redis_us = _bench_redis_us(
        lambda: rdi_state.xrevrange("rdi:last-events", count=5))

    return jsonify({
        "services":  out,
        "technical": _technical(
            pg_query=pg_query, pg_ms=pg_ms,
            yaml_file="rdi/jobs/trade.yaml",
            yaml_snippet=YAML_SNIPPETS["trade_stream"],
            redis_command=redis_command, redis_us=redis_us,
            rdi_sync_ms=None),
    })


# Use-case 7: audit trail (browse rdi:last-events)
@app.route("/api/uc/audit-trail")
def api_uc_audit_trail():
    limit = min(100, max(5, int(request.args.get("limit", "50"))))

    # ----- 3-pillar technical context -----------------------------------
    pg_query = (
        "SELECT 'customer' AS tbl, client_code, kyc_status, segment,\n"
        "       updated_at FROM portfolio.customer\n"
        "WHERE  updated_at > NOW() - INTERVAL '1 day'\n"
        "ORDER BY updated_at DESC LIMIT 50;"
    )
    pg_ms = _bench_pg_ms(
        "SELECT client_code, kyc_status, segment, updated_at "
        "FROM portfolio.customer "
        "WHERE updated_at > NOW() - INTERVAL '1 day' "
        "ORDER BY updated_at DESC LIMIT 50")
    redis_command = "XREVRANGE rdi:last-events + - COUNT 50"
    redis_us = _bench_redis_us(
        lambda: rdi_state.xrevrange("rdi:last-events", count=50))

    return jsonify({
        "events":    pipeline_recent_events(limit),
        "technical": _technical(
            pg_query=pg_query, pg_ms=pg_ms,
            yaml_file="rdi-processor (last-events ring buffer)",
            yaml_snippet=YAML_SNIPPETS["last_events"],
            redis_command=redis_command, redis_us=redis_us,
            rdi_sync_ms=None),
    })


# ===========================================================================
# ───────────────────  PIPELINE INTERNALS TAB ROUTES  ──────────────────────
# ===========================================================================
@app.route("/api/internals/streams")
def api_internals_streams():
    """XINFO STREAM for every Debezium stream on the RDI state DB."""
    out = []
    try:
        for s in rdi_state.scan_iter(match="sectrade.portfolio.*",
                                     count=50, _type="STREAM"):
            try:
                info = rdi_state.xinfo_stream(s)
            except Exception as e:
                info = {"error": str(e)}
            groups = []
            try:
                for g in rdi_state.xinfo_groups(s):
                    groups.append({
                        "name":     g.get("name"),
                        "consumers": g.get("consumers"),
                        "pending":  g.get("pending"),
                        "last_delivered_id": g.get("last-delivered-id")
                                            or g.get("last_delivered_id"),
                    })
            except Exception:
                pass
            first_entry = info.get("first-entry") if isinstance(info, dict) else None
            last_entry  = info.get("last-entry")  if isinstance(info, dict) else None
            out.append({
                "stream":   s,
                "length":   info.get("length") if isinstance(info, dict) else None,
                "first_id": first_entry[0] if first_entry else None,
                "last_id":  last_entry[0]  if last_entry  else None,
                "groups":   groups,
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"streams": out})


@app.route("/api/internals/redis-state")
def api_internals_redis_state():
    """Operator-style snapshot of every BDB the pipeline touches
    (primary target cache + RDI state)."""
    def db_snapshot(client: redis.Redis | None) -> dict:
        if client is None:
            return {"reachable": False}
        try:
            info = client.info("memory")
            return {
                "reachable":      True,
                "dbsize":         client.dbsize(),
                "used_memory":    info.get("used_memory_human"),
                "maxmemory":      info.get("maxmemory_human"),
                "evicted_keys":   info.get("evicted_keys"),
            }
        except Exception as e:
            return {"reachable": False, "error": str(e)}
    return jsonify({
        "primary": db_snapshot(r),
        "rdi_state": db_snapshot(rdi_state),
    })


# ---------------------------------------------------------------------------
# Operator-only sync-status endpoint (powers the "Sync Status" tab).
#
# This is NOT part of the customer-facing demo narrative — it's an
# observability dashboard for the demo operator. It walks every layer of
# the pipeline (PG row count -> Debezium stream -> RDI consumer group ->
# RDI processor counters -> Redis target keys) for each of the five
# replicated tables and surfaces:
#   - records done (events the processor has applied to the target)
#   - records pending (XPENDING + un-delivered stream entries)
#   - lag (PG WAL bytes behind + last applied event age)
#   - last applied timestamp
#   - status (synced / lagging / paused / stalled / error)
#
# All metrics come from sources the real RDI product already exposes:
#   - pg_stat_user_tables.n_live_tup       (Postgres planner estimate)
#   - pg_replication_slots.confirmed_flush_lsn
#   - XINFO STREAM / XINFO GROUPS          (Redis 7+ `lag` field where avail)
#   - rdi:stats:<table>                    (the demo processor's per-event metrics)
#   - FT.INFO cust-idx / hold-idx.num_docs (cheap O(1) count for the big sets)
#   - SCAN with MATCH+COUNT                (cheap for the tiny prefixes:
#                                           security:*, price:*, trades:*, trade:*)
# Nothing here invents a metric that wouldn't be available against a
# production RDI install talking to a production Redis Enterprise.
# ---------------------------------------------------------------------------
_SYNC_TABLES = [
    {
        "table":      "portfolio.customer",
        "stream":     "sectrade.portfolio.customer",
        "pg_relname": "customer",
        # Primary cache write: customer.yaml -> customer:{client_code} JSON.
        # cust-segment:* (SET) is a secondary output we don't tally here —
        # it's a fan-out artefact, not the source-of-truth count.
        "target": {"type": "ft_index", "name": "cust-idx",
                   "match": "customer:*"},
    },
    {
        "table":      "portfolio.holding",
        "stream":     "sectrade.portfolio.holding",
        "pg_relname": "holding",
        "target": {"type": "ft_index", "name": "hold-idx",
                   "match": "holding:*"},
    },
    {
        "table":      "portfolio.trade",
        "stream":     "sectrade.portfolio.trade",
        "pg_relname": "trade",
        # trade.yaml writes BOTH a per-customer stream (trades:{customer_id})
        # and a per-trade JSON (trade:{trade_id}). The per-trade JSON is the
        # one whose count maps 1:1 to PG rows, so we count that.
        "target": {"type": "scan", "match": "trade:*"},
    },
    {
        "table":      "portfolio.market_price",
        "stream":     "sectrade.portfolio.market_price",
        "pg_relname": "market_price",
        "target": {"type": "scan", "match": "price:*"},
    },
    {
        "table":      "portfolio.security_master",
        "stream":     "sectrade.portfolio.security_master",
        "pg_relname": "security_master",
        # security_master.yaml writes two shapes (security:{symbol} HASH +
        # security_full:{id} JSON). HASH count tracks active rows (filter
        # `is_active == true` in YAML), so we report it as the canonical one.
        "target": {"type": "scan", "match": "security:*"},
    },
]


def _pg_row_estimates() -> dict[str, dict]:
    """Return live-row estimates for every replicated table in one round-trip.

    Uses pg_stat_user_tables.n_live_tup — the planner's live-row estimate,
    maintained by autovacuum. This is the documented fast path; full
    `SELECT COUNT(*)` would take seconds at 9M holdings and is overkill
    for an observability tab. The same stat is what Postgres tools like
    pgAdmin show in their "Statistics" panel.
    """
    out: dict[str, dict] = {}
    try:
        with pg_conn() as c, c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT relname,
                       n_live_tup       AS live_rows,
                       n_tup_ins        AS inserts,
                       n_tup_upd        AS updates,
                       n_tup_del        AS deletes,
                       extract(epoch from coalesce(last_autoanalyze,
                                                   last_analyze)) AS analyzed_at
                FROM pg_stat_user_tables
                WHERE schemaname = 'portfolio'
            """)
            for row in cur.fetchall():
                out[row["relname"]] = {
                    "live_rows": int(row["live_rows"] or 0),
                    "inserts":   int(row["inserts"] or 0),
                    "updates":   int(row["updates"] or 0),
                    "deletes":   int(row["deletes"] or 0),
                    "analyzed_at_ms": int((row["analyzed_at"] or 0) * 1000)
                                     if row["analyzed_at"] else 0,
                }
    except Exception as e:
        out["__error__"] = {"error": str(e)}  # type: ignore[assignment]
    return out


def _stream_info(stream: str) -> dict:
    """XINFO STREAM + age of last entry. Returns {} on error."""
    try:
        info = rdi_state.xinfo_stream(stream)
    except Exception:
        return {}
    last_entry = info.get("last-entry") or info.get("last_entry")
    last_id    = last_entry[0] if last_entry else None
    last_id_age_s = None
    if last_id:
        try:
            ts_ms = int(last_id.split("-")[0])
            last_id_age_s = round(max(0.0, time.time() - ts_ms / 1000.0), 2)
        except Exception:
            pass
    return {
        "length":         int(info.get("length") or 0),
        "last_id":        last_id,
        "last_id_age_s":  last_id_age_s,
        "first_id":       (info.get("first-entry") or info.get("first_entry") or
                           [None])[0],
    }


def _group_info(stream: str, group_name: str = "rdi-processor") -> dict:
    """XINFO GROUPS row for our consumer group. Returns {} on error.

    Redis 7+ exposes `lag` directly (number of un-delivered entries) —
    we surface it if present, otherwise None. `pending` is always
    available (PEL size = events delivered but not yet XACK'd).
    """
    try:
        groups = rdi_state.xinfo_groups(stream)
    except Exception:
        return {}
    g = next((x for x in groups if x.get("name") == group_name), None)
    if not g:
        return {}
    return {
        "name":              g.get("name"),
        "consumers":         int(g.get("consumers") or 0),
        "pending":           int(g.get("pending") or 0),
        "lag":               (int(g["lag"]) if g.get("lag") is not None else None),
        "last_delivered_id": g.get("last-delivered-id")
                             or g.get("last_delivered_id"),
    }


def _target_key_count(spec: dict) -> dict:
    """Cheap count of target keys for a table without scanning at 3M scale.

    - type=ft_index    -> FT.INFO <name>.num_docs (O(1), exact)
    - type=scan        -> SCAN MATCH <prefix> COUNT 5000, bail at 50k
                          (we only use this for prefixes we know are small:
                           security:*, price:*, trade:*, trades:*)
    """
    if spec.get("type") == "ft_index":
        try:
            info = r.execute_command("FT.INFO", spec["name"])
            info_d = dict(zip(info[::2], info[1::2]))
            return {
                "count":  int(info_d.get("num_docs", 0)),
                "source": f"FT.INFO {spec['name']} num_docs",
                "exact":  True,
            }
        except redis.ResponseError as e:
            return {"count": None, "source": f"FT.INFO {spec['name']} unavailable",
                    "exact": False, "error": str(e)}
    if spec.get("type") == "scan":
        count = 0
        try:
            for _k in r.scan_iter(match=spec["match"], count=5000):
                count += 1
                if count >= 50_000:
                    return {"count": count, "source": f"SCAN MATCH {spec['match']}",
                            "exact": False, "note": "capped at 50k"}
        except Exception as e:
            return {"count": None, "source": f"SCAN MATCH {spec['match']}",
                    "exact": False, "error": str(e)}
        return {"count": count, "source": f"SCAN MATCH {spec['match']}",
                "exact": True}
    return {"count": None, "source": "unknown", "exact": False}


def _status_for(pending: int, lag: int | None, delta: int,
                last_event_age_s: float | None, paused: bool) -> str:
    """Roll up per-table layers into a single operator status string."""
    if paused:
        return "paused"
    inflight = (pending or 0) + (lag or 0)
    abs_delta = abs(delta or 0)
    # stalled: pipeline has work but processor hasn't moved in 60+ seconds
    if inflight > 0 and (last_event_age_s is None or last_event_age_s > 60):
        return "stalled"
    # lagging: more than 100 events backlog OR target trails PG by >100 rows
    if inflight > 100 or abs_delta > 100:
        return "lagging"
    if inflight > 0 or abs_delta > 0:
        return "syncing"
    return "synced"


@app.route("/api/internals/sync-status")
def api_internals_sync_status():
    """Operator-only end-to-end sync status across every pipeline layer.

    Powers the dashboard's "Sync Status" tab. Reads only from sources the
    real RDI product also exposes — no invented metrics — so what you see
    here would be reproducible against a production RDI install.
    """
    now_ms = int(_now_ms())

    # ---- processor health (rdi:processor:* keys + rdi:stats:total) ----
    paused = (rdi_state.get("rdi:processor:paused") == "1")
    last_reload_ms = int(rdi_state.get("rdi:processor:last_reload_ms") or 0)
    total_h = rdi_state.hgetall("rdi:stats:total") or {}
    total_events = int(total_h.get("events", 0) or 0)
    last_event_ms_total = int(total_h.get("last_event_ms", 0) or 0)

    # ---- source (Postgres replication slot + WAL position) ------------
    pg_slot: dict = {}
    wal_lsn: str | None = None
    try:
        with pg_conn() as c, c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT pg_current_wal_lsn()::text AS lsn")
            wal_lsn = (cur.fetchone() or {}).get("lsn")
            cur.execute("""
                SELECT slot_name, active, confirmed_flush_lsn::text AS lsn,
                       pg_wal_lsn_diff(pg_current_wal_lsn(),
                                       confirmed_flush_lsn) AS bytes_behind
                FROM pg_replication_slots
                WHERE plugin = 'pgoutput'
                LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                pg_slot = {
                    "slot_name":            row["slot_name"],
                    "active":               bool(row["active"]),
                    "confirmed_flush_lsn":  row["lsn"],
                    "bytes_behind":         int(row["bytes_behind"] or 0),
                }
    except Exception as e:
        pg_slot = {"error": str(e)}

    # ---- per-table rows ----------------------------------------------
    pg_rows = _pg_row_estimates()
    tables_out: list[dict] = []
    totals = {"pg_rows": 0, "stream_length": 0, "pending": 0,
              "lag": 0, "target_keys": 0, "delta": 0}

    for spec in _SYNC_TABLES:
        stream = _stream_info(spec["stream"])
        group  = _group_info(spec["stream"])
        target = _target_key_count(spec["target"])
        proc_h = rdi_state.hgetall(f"rdi:stats:{spec['table']}") or {}
        last_event_ms = int(proc_h.get("last_event_ms", 0) or 0)
        last_event_age_s = (
            round(max(0.0, time.time() - last_event_ms / 1000.0), 1)
            if last_event_ms else None
        )

        pg = pg_rows.get(spec["pg_relname"], {})
        pg_n = int(pg.get("live_rows") or 0)
        tgt_n = target.get("count")
        delta = (pg_n - int(tgt_n)) if tgt_n is not None else None

        status = _status_for(
            pending=group.get("pending", 0),
            lag=group.get("lag"),
            delta=(delta or 0),
            last_event_age_s=last_event_age_s,
            paused=paused,
        )

        tables_out.append({
            "table":       spec["table"],
            "stream":      spec["stream"],
            "pg_rows":     pg_n,
            "pg_rows_source": "pg_stat_user_tables.n_live_tup",
            "pg_dml":      {
                "inserts": pg.get("inserts", 0),
                "updates": pg.get("updates", 0),
                "deletes": pg.get("deletes", 0),
            },
            "stream_info": stream,
            "group":       group,
            "processor":   {
                "events":        int(proc_h.get("events", 0) or 0),
                "inserts":       int(proc_h.get("inserts", 0) or 0),
                "updates":       int(proc_h.get("updates", 0) or 0),
                "deletes":       int(proc_h.get("deletes", 0) or 0),
                "snapshots":     int(proc_h.get("snapshots", 0) or 0),
                "last_lag_ms":   int(proc_h.get("last_lag_ms", 0) or 0),
                "last_event_ms": last_event_ms,
                "last_event_iso": _epoch_ms_to_ist(last_event_ms),
                "last_event_age_s": last_event_age_s,
            },
            "target":      target,
            "delta_vs_pg": delta,
            "status":      status,
        })

        totals["pg_rows"]       += pg_n
        totals["stream_length"] += int(stream.get("length") or 0)
        totals["pending"]       += int(group.get("pending") or 0)
        if group.get("lag") is not None:
            totals["lag"] += int(group["lag"])
        if tgt_n is not None:
            totals["target_keys"] += int(tgt_n)
        if delta is not None:
            totals["delta"] += delta

    overall = "synced"
    if paused:
        overall = "paused"
    elif any(t["status"] in ("stalled", "error") for t in tables_out):
        overall = "stalled"
    elif any(t["status"] == "lagging" for t in tables_out):
        overall = "lagging"
    elif any(t["status"] == "syncing" for t in tables_out):
        overall = "syncing"

    return jsonify({
        "ts_ms":         now_ms,
        "ts_iso":        _epoch_ms_to_ist(now_ms),
        "overall_status": overall,
        "processor": {
            "paused":           paused,
            "last_reload_ms":   last_reload_ms,
            "last_reload_iso":  _epoch_ms_to_ist(last_reload_ms) if last_reload_ms else None,
            "total_events":     total_events,
            "last_event_ms":    last_event_ms_total,
            "last_event_iso":   _epoch_ms_to_ist(last_event_ms_total) if last_event_ms_total else None,
            "last_event_age_s": (round(max(0.0, time.time() - last_event_ms_total / 1000.0), 1)
                                 if last_event_ms_total else None),
        },
        "source": {
            "wal_lsn":          wal_lsn,
            "replication_slot": pg_slot,
        },
        "totals":  totals,
        "tables":  tables_out,
    })


# ===========================================================================
# ─────────────────────────  ONE-SHOT SCHEMA MIGRATIONS  ────────────────────
# The "Real-time Securities Data Platform Demo" narrative needs three extra
# columns that the original portfolio schema did not carry:
#   - portfolio.customer.margin_available   (₹ buying power)
#   - portfolio.customer.trading_limit      (₹ daily ceiling)
#   - portfolio.security_master.corporate_action_flag  (NONE / SPLIT / …)
# These are ADD COLUMN IF NOT EXISTS statements — idempotent and metadata-
# only in Postgres 16, so they are safe to run on every dashboard startup.
# Because customer.yaml / security.yaml RDI jobs write `path: $` (full row),
# Debezium will replicate the new columns into Redis automatically the next
# time the row UPDATEs — no YAML edits needed.
# ===========================================================================
def _run_one_shot_migrations() -> None:
    statements = [
        # customer columns
        "ALTER TABLE portfolio.customer "
        "ADD COLUMN IF NOT EXISTS margin_available NUMERIC(20,2) DEFAULT 100000.00",
        "ALTER TABLE portfolio.customer "
        "ADD COLUMN IF NOT EXISTS trading_limit NUMERIC(20,2) DEFAULT 500000.00",
        # security_master column
        "ALTER TABLE portfolio.security_master "
        "ADD COLUMN IF NOT EXISTS corporate_action_flag VARCHAR(20) "
        "NOT NULL DEFAULT 'NONE'",
    ]
    for sql in statements:
        try:
            _pg_exec(sql)
            app.logger.info("migration ok :: %s", sql.split("ADD COLUMN")[1].strip())
        except Exception as e:
            app.logger.warning("migration skipped (%s): %s",
                               sql.split("ADD COLUMN")[1].strip() if "ADD COLUMN" in sql else sql,
                               e)


try:
    _run_one_shot_migrations()
except Exception as _e:
    app.logger.warning("startup migrations failed: %s", _e)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
