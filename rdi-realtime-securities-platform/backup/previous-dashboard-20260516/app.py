"""
HDFC Securities - Live Portfolio Dashboard
Renders portfolio data exclusively from Redis Enterprise (the RDI target).
The dashboard never touches Postgres at runtime - that's the whole point
of RDI: hot data lives in Redis, the system of record stays cool.
"""
from __future__ import annotations

import json
import os
import time
from collections import OrderedDict

import psycopg2
import redis
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# ---- Connections ----------------------------------------------------------
REDIS_HOST  = os.getenv("TARGET_DB_HOST", "redis-enterprise")
REDIS_PORT  = int(os.getenv("TARGET_DB_PORT", "12000"))
REDIS_PASS  = os.getenv("TARGET_DB_PASSWORD", "") or None

PG_HOST = os.getenv("PG_HOST", "postgres")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_DB   = os.getenv("PG_DB",   "hdfcsec")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASS = os.getenv("PG_PASS", "postgres")

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASS,
                decode_responses=True)


def pg_conn():
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB,
        user=PG_USER, password=PG_PASS,
    )


# ---- Caches ---------------------------------------------------------------
# Security master is small (~tens of rows) and rarely changes; resolving
# security_id -> {symbol, company, sector} via SCAN per holding is wasteful
# at scale. We refresh on misses so RDI-driven additions are still picked up.
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
    # cold or new — refresh at most every 5s
    if time.time() - _SEC_CACHE_LAST > 5:
        _refresh_sec_cache()
    return _SEC_CACHE.get(sid, {"symbol": f"SEC{sid}", "company": "", "sector": ""})


# ---- Index detection ------------------------------------------------------
# When the large-scale seeder has run, `cust-idx` exists and we can use
# FT.SEARCH for sub-ms lookups on millions of customers. When it hasn't,
# we fall back to SCAN so the small-scale demo still works.
def _ft_index_present(name: str = "cust-idx") -> bool:
    try:
        r.execute_command("FT.INFO", name)
        return True
    except redis.ResponseError:
        return False

def _holding_keys(customer_id: int) -> list[str]:
    """Return holding:{cid}:{sid} keys for one customer.

    At scale we use FT.SEARCH on `hold-idx` (numeric range on customer_id
    — O(log N)). At small scale or before the bulk seeder runs, we fall
    back to SCAN with a tight match prefix (only fine while the dbsize
    is tiny).
    """
    if _ft_index_present("hold-idx"):
        res = r.execute_command(
            "FT.SEARCH", "hold-idx",
            f"@customer_id:[{customer_id} {customer_id}]",
            "NOCONTENT",
            "LIMIT", "0", "1000",
        )
        # res = [count, key1, key2, ...]   with NOCONTENT
        return list(res[1:]) if res and len(res) >= 2 else []
    return list(r.scan_iter(match=f"holding:{customer_id}:*", count=500))


# ---- Helpers --------------------------------------------------------------
def get_customer(client_code: str) -> dict | None:
    raw = r.execute_command("JSON.GET", f"customer:{client_code}", "$")
    if not raw:
        return None
    return json.loads(raw)[0]


def list_customers(limit: int = 25, query: str | None = None) -> list[dict]:
    """List customers from the target Redis cache.

    Uses FT.SEARCH against `cust-idx` when the index exists (created by
    scripts/seed-large-scale.sh) — this stays sub-ms at 10M+ records.
    Falls back to SCAN + JSON.GET for the small-scale demo where the
    index has not been built.
    """
    if _ft_index_present():
        return _ft_list(limit, query)
    return _scan_list(limit)

def _ft_list(limit: int, query: str | None) -> list[dict]:
    """FT.SEARCH-backed customer list.

    Query rules:
      - empty   -> top N by client_code (alphabetical, deterministic)
      - 10-char -> exact PAN tag lookup
      - else    -> prefix match on name OR client_code
    """
    if query:
        q = query.strip()
        # PAN looks like 5 letters + 4 digits + 1 letter
        if len(q) == 10 and q[:5].isalpha() and q[5:9].isdigit() and q[-1].isalpha():
            expr = f'@pan:{{{q.upper()}}}'
        elif q.upper().startswith("HS") and q[2:].isdigit():
            expr = f'@client_code:{{{q.upper()}}}'
        else:
            # FT.SEARCH escapes: spaces become wildcards; just use prefix
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
    # response: [count, doc1_key, [field, val, ...], doc2_key, [...], ...]
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
    """Total customer:* docs currently in the target cache."""
    if _ft_index_present():
        info = r.execute_command("FT.INFO", "cust-idx")
        d = dict(zip(info[::2], info[1::2]))
        return int(d.get("num_docs", 0))
    return sum(1 for _ in r.scan_iter(match="customer:*", count=1000))


def get_portfolio(customer_id: int) -> dict:
    """Build a live portfolio for a customer entirely from Redis."""
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

        # O(1) security lookup via cached map (refreshed every 5s for RDI updates)
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
    """Run the same 'portfolio summary' query against Postgres and against
    Redis and return wall-clock latency for both."""
    # ---- Postgres (system of record) ----
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

    # ---- Redis (RDI-fed cache) - production-style pipelined reads ----
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


def pipeline_metrics() -> dict:
    """Pull simple stream stats from the RDI database for the demo UI."""
    rdi_db = redis.Redis(host=os.getenv("RDI_DB_HOST", "redis-rdi"),
                         port=int(os.getenv("RDI_DB_PORT", "12001")),
                         decode_responses=True)
    out = OrderedDict()
    try:
        for s in rdi_db.scan_iter(match="hdfcsec.portfolio.*",
                                  count=50, _type="STREAM"):
            try:
                length = rdi_db.xlen(s)
            except Exception:
                length = 0
            # strip the "hdfcsec." prefix for nicer display
            out[s.split(".", 1)[1]] = length
    except Exception as e:
        out["__error__"] = str(e)
    return out


# ---- Routes ---------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/customers")
def api_customers():
    q = request.args.get("q") or None
    limit = min(int(request.args.get("limit", "50")), 100)
    return jsonify(list_customers(limit=limit, query=q))


@app.route("/api/customer-search")
def api_customer_search():
    """FT.SEARCH-backed search used by the dashboard sidebar.

    Same shape as /api/customers but always returns the live FT.INFO
    count so the UI can show "Showing N of 10,000,000".
    """
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
    """Two workloads, head-to-head against Postgres and Redis at scale.

    Workload A — exact lookup by PAN (a fair comparison: both sides indexed):
      - Postgres: btree `idx_customer_pan` (created by the bulk seeder)
      - Redis:    RediSearch tag `@pan:{...}` via `cust-idx`
                  + RedisJSON direct `JSON.GET customer:<code>`

    Workload B — prefix search on customer name (where Redis dominates):
      - Postgres: `WHERE full_name ILIKE 'Raj%'` — there is no trigram/GIN
                  index on full_name (typical for OLTP schemas), so this
                  is a sequential scan at every scale
      - Redis:    RediSearch TEXT `@name:raj*` — O(log N) prefix lookup

    All timings are p50 of N runs to smooth out single-call jitter.
    """
    runs = max(1, min(20, int(request.args.get("runs", "5"))))

    # ---- pick a sample customer for the PAN workload -----------------
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

    # ---- Workload A: PAN exact lookup --------------------------------
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

    # ---- Workload B: filtered COUNT(*) -------------------------------
    # Honest worst-case for Postgres: there is no GIN/trigram index on
    # full_name (typical OLTP schema), so a COUNT(*) WHERE full_name
    # ILIKE 'Raj%' must seq-scan every row.
    # Honest best-case for Redis: RediSearch's inverted index knows
    # the cardinality without scanning — `FT.SEARCH ... LIMIT 0 0`
    # returns the total match count instantly.
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
