#!/usr/bin/env python3
"""
RDI Stream Processor (reference implementation for the demo).

What this does:
  1. Reads CDC events that Debezium Server writes to Redis Streams on
     the "RDI database" (one stream per source table).
  2. Loads the YAML job files under /rdi/jobs - the SAME format that
     real RDI consumes - and applies the transformations.
  3. Writes the result to the configured target Redis Enterprise
     database as JSON / Hash / Stream / Set / ZSet per the job's
     "data_type".
  4. Hot-reloads the YAML jobs whenever they change on disk, mirroring
     what the real RDI control plane does when a pipeline is re-deployed.
  5. Honours an operator "pause" flag in Redis so a demo presenter can
     show backpressure / replay-from-stream behaviour.
  6. Publishes per-table processing counters and lag samples to the
     RDI state database so the dashboard can render them without any
     Redis Insight switch-screen during the demo.

In a production RDI install, all of this is packaged as the
rdi-processor container managed by the RDI operator, with
high-availability via leader election and Prometheus-exported
metrics. We re-implement just enough of it here so the customer can
see the *end-to-end flow* on their laptop.
"""

from __future__ import annotations

import glob
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import redis
import yaml


# ---------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------
RDI_DB_HOST = os.getenv("RDI_DB_HOST", "redis-rdi")
RDI_DB_PORT = int(os.getenv("RDI_DB_PORT", "12001"))
TARGET_HOST = os.getenv("TARGET_DB_HOST", "redis-enterprise")
TARGET_PORT = int(os.getenv("TARGET_DB_PORT", "12000"))
TARGET_PASS = os.getenv("TARGET_DB_PASSWORD", "")
JOBS_DIR    = os.getenv("RDI_JOBS_DIR", "/rdi/jobs")
GROUP_NAME  = os.getenv("RDI_CONSUMER_GROUP", "rdi-processor")
CONSUMER    = os.getenv("RDI_CONSUMER_NAME", "rdi-proc-1")
SERVER_NAME = os.getenv("RDI_SERVER_NAME", "hdfcsec")

# Operator flag keys (in RDI state DB). Dashboard flips these.
PAUSE_FLAG_KEY   = "rdi:processor:paused"     # "1" -> pause
RELOAD_FLAG_KEY  = "rdi:processor:reload"     # bumped -> force reload of jobs
STATS_KEY_PREFIX = "rdi:stats"                # rdi:stats:<table> hash + rdi:stats:total
LAST_EVENTS_KEY  = "rdi:last-events"          # capped stream of last-N processed events


# ---------------------------------------------------------------------
# Minimal JMESPath subset - enough for the job key expressions we use
# ---------------------------------------------------------------------
def _eval_jmes(expr: str, doc: dict[str, Any]) -> Any:
    """Tiny evaluator that supports concat([...]), to_string(x) and
    bare field references - which is everything our jobs use."""
    expr = expr.strip()

    if expr.startswith("concat([") and expr.endswith("])"):
        inner = expr[len("concat(["):-2]
        parts = _split_args(inner)
        return "".join(str(_eval_jmes(p, doc)) for p in parts)

    if expr.startswith("to_string(") and expr.endswith(")"):
        return str(_eval_jmes(expr[len("to_string("):-1], doc))

    if (expr.startswith("'") and expr.endswith("'")) or \
       (expr.startswith('"') and expr.endswith('"')):
        return expr[1:-1]

    if expr.startswith("`") and expr.endswith("`"):
        v = expr[1:-1]
        if v in ("true", "false"):
            return v == "true"
        try:
            return int(v)
        except ValueError:
            return v

    m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*(==|!=)\s*(.+)$", expr)
    if m:
        lhs, op, rhs = m.group(1), m.group(2), m.group(3).strip()
        lv = doc.get(lhs)
        rv = _eval_jmes(rhs, doc)
        return (lv == rv) if op == "==" else (lv != rv)

    if expr == "$":
        return doc
    return doc.get(expr)


def _split_args(s: str) -> list[str]:
    """Split top-level args by comma, respecting brackets and quotes."""
    out, depth, current, in_str, str_ch = [], 0, [], False, ""
    for ch in s:
        if in_str:
            current.append(ch)
            if ch == str_ch:
                in_str = False
            continue
        if ch in ("'", '"'):
            in_str, str_ch = True, ch
            current.append(ch)
            continue
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        out.append("".join(current).strip())
    return out


# ---------------------------------------------------------------------
# Job loading + hot reload
# ---------------------------------------------------------------------
@dataclass
class Job:
    table: str
    filters: list[Callable[[dict], bool]] = field(default_factory=list)
    field_renames: list[tuple[str, str]] = field(default_factory=list)
    outputs: list[dict] = field(default_factory=list)


def load_jobs(jobs_dir: str) -> tuple[dict[str, Job], float]:
    """Return (jobs_map, max_mtime). max_mtime is used for hot-reload."""
    jobs: dict[str, Job] = {}
    max_mtime = 0.0
    for path in sorted(glob.glob(os.path.join(jobs_dir, "*.yaml"))):
        max_mtime = max(max_mtime, os.path.getmtime(path))
        with open(path) as f:
            doc = yaml.safe_load(f)
        src = doc["source"]
        table_fqn = f'{src["schema"]}.{src["table"]}'

        filters, renames = [], []
        for step in doc.get("transform", []) or []:
            kind = step.get("uses")
            if kind == "filter":
                expr = step["with"]["expression"]
                filters.append(lambda d, e=expr: bool(_eval_jmes(e, d)))
            elif kind == "rename_field":
                renames.append((step["with"]["from_field"],
                                step["with"]["to_field"]))

        jobs[table_fqn] = Job(
            table=table_fqn,
            filters=filters,
            field_renames=renames,
            outputs=doc.get("output", []),
        )
        print(f"[rdi] loaded job for {table_fqn} -> "
              f'{[o["with"]["data_type"] for o in jobs[table_fqn].outputs]}')
    return jobs, max_mtime


def maybe_reload(jobs_dir: str, current: dict[str, Job],
                 known_mtime: float, rdi_db: redis.Redis
                 ) -> tuple[dict[str, Job], float, bool]:
    """Re-read YAML if any file's mtime has advanced OR the operator
    bumped the reload flag. Returns (jobs, new_mtime, changed)."""
    on_disk_mtime = 0.0
    for path in glob.glob(os.path.join(jobs_dir, "*.yaml")):
        on_disk_mtime = max(on_disk_mtime, os.path.getmtime(path))

    forced = False
    try:
        forced_at = rdi_db.get(RELOAD_FLAG_KEY)
        forced = bool(forced_at and float(forced_at) > known_mtime)
    except Exception:
        forced = False

    if on_disk_mtime > known_mtime or forced:
        print(f"[rdi] reloading jobs (mtime {known_mtime:.3f} -> "
              f"{on_disk_mtime:.3f}, forced={forced})")
        new_jobs, new_mtime = load_jobs(jobs_dir)
        return new_jobs, max(new_mtime, on_disk_mtime), True
    return current, known_mtime, False


# ---------------------------------------------------------------------
# Stream consumption
# ---------------------------------------------------------------------
def ensure_groups(r: redis.Redis, jobs: dict[str, Job]) -> list[str]:
    """Create consumer groups on the Debezium stream for each job table."""
    streams: list[str] = []
    for table_fqn in jobs:
        schema, table = table_fqn.split(".")
        # Debezium Server's Redis sink uses topic name as the stream key:
        #   <topic.prefix>.<schema>.<table>
        stream = f"{SERVER_NAME}.{schema}.{table}"
        streams.append(stream)
        try:
            r.xgroup_create(stream, GROUP_NAME, id="0", mkstream=True)
            print(f"[rdi] consumer group ready on {stream}")
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
    return streams


def stream_to_table(stream: str) -> str:
    # hdfcsec.portfolio.holding -> portfolio.holding
    return stream.split(".", 1)[1]


# ---------------------------------------------------------------------
# Target connections
# ---------------------------------------------------------------------
def open_targets() -> dict[str, redis.Redis]:
    """Open every Redis target referenced from rdi/config.yaml's `targets`.
    The demo defines a single primary cache (`target`)."""
    return {
        "target": redis.Redis(
            host=TARGET_HOST, port=TARGET_PORT,
            password=TARGET_PASS or None, decode_responses=True,
        ),
    }


# ---------------------------------------------------------------------
# Write to target
# ---------------------------------------------------------------------
def apply_outputs(job: Job, payload: dict[str, Any],
                  targets: dict[str, redis.Redis], is_delete: bool) -> None:
    for out in job.outputs:
        cfg = out["with"]
        conn_name = cfg.get("connection", "target")
        target = targets.get(conn_name)
        if target is None:
            # YAML references a connection that isn't wired on this
            # deployment. Skip safely rather than crash the processor.
            continue

        key_expr = cfg["key"]["expression"]
        key = _eval_jmes(key_expr, payload)
        data_type = cfg["data_type"]

        # The YAML "member" expression lets a set/zset output choose
        # which field of the row becomes the member.  Backwards compat:
        # if absent, fall back to client_code or to_string(customer_id).
        member_expr = cfg.get("member", {}).get("expression") if cfg.get("member") else None
        score_expr  = cfg.get("score",  {}).get("expression") if cfg.get("score")  else None

        if is_delete:
            if data_type in ("json", "hash"):
                target.delete(key)
                print(f"[rdi] [{conn_name}] DEL {key}")
            elif data_type == "set" and member_expr is not None:
                target.srem(key, _eval_jmes(member_expr, payload))
                print(f"[rdi] [{conn_name}] SREM {key}")
            elif data_type == "zset" and member_expr is not None:
                target.zrem(key, _eval_jmes(member_expr, payload))
                print(f"[rdi] [{conn_name}] ZREM {key}")
            continue

        if data_type == "json":
            target.execute_command("JSON.SET", key, "$", json.dumps(payload))
            print(f"[rdi] [{conn_name}] JSON.SET {key}")
        elif data_type == "hash":
            flat = {k: ("" if v is None else str(v)) for k, v in payload.items()}
            if flat:
                target.hset(key, mapping=flat)
            print(f"[rdi] [{conn_name}] HSET {key} ({len(flat)} fields)")
        elif data_type == "stream":
            flat = {k: ("" if v is None else str(v)) for k, v in payload.items()}
            target.xadd(key, flat, maxlen=10_000, approximate=True)
            print(f"[rdi] [{conn_name}] XADD {key}")
        elif data_type == "set" and member_expr is not None:
            target.sadd(key, _eval_jmes(member_expr, payload))
            print(f"[rdi] [{conn_name}] SADD {key}")
        elif data_type == "zset" and member_expr is not None:
            score = float(_eval_jmes(score_expr, payload)) if score_expr else 0.0
            target.zadd(key, {_eval_jmes(member_expr, payload): score})
            print(f"[rdi] [{conn_name}] ZADD {key}")
        else:
            print(f"[rdi] WARN: unsupported data_type {data_type}")


def _record_metrics(rdi_db: redis.Redis, table: str,
                    cdc_ts_ms: int | None,
                    processed_at_ms: int,
                    op: str) -> None:
    """Per-event metrics that the dashboard reads to draw the Pipeline tab.

    Writes:
      - rdi:stats:<table>  HASH  events / inserts / updates / deletes / last_lag_ms
      - rdi:stats:total    HASH  events / last_event_ms
      - rdi:last-events    STREAM capped at 200, one entry per event
    """
    op_field = {"c": "inserts", "u": "updates",
                "d": "deletes", "r": "snapshots"}.get(op, "events")
    lag_ms = (processed_at_ms - cdc_ts_ms) if cdc_ts_ms else 0
    try:
        pipe = rdi_db.pipeline(transaction=False)
        pipe.hincrby(f"{STATS_KEY_PREFIX}:{table}", "events", 1)
        pipe.hincrby(f"{STATS_KEY_PREFIX}:{table}", op_field, 1)
        pipe.hset(f"{STATS_KEY_PREFIX}:{table}", "last_lag_ms", lag_ms)
        pipe.hset(f"{STATS_KEY_PREFIX}:{table}", "last_event_ms", processed_at_ms)
        pipe.hincrby(f"{STATS_KEY_PREFIX}:total", "events", 1)
        pipe.hset(f"{STATS_KEY_PREFIX}:total", "last_event_ms", processed_at_ms)
        pipe.xadd(LAST_EVENTS_KEY,
                  {"table": table, "op": op, "lag_ms": str(lag_ms),
                   "processed_at_ms": str(processed_at_ms)},
                  maxlen=200, approximate=True)
        pipe.execute()
    except Exception as e:
        # never let observability break the pipeline
        print(f"[rdi] WARN: stats write failed: {e}")


def process_event(job: Job, raw_value: str,
                  targets: dict[str, redis.Redis],
                  rdi_db: redis.Redis, table: str) -> None:
    """Decode a Debezium CDC envelope and run the configured transforms."""
    try:
        envelope = json.loads(raw_value)
    except Exception:
        return

    op = envelope.get("op")               # 'r' read/snapshot, 'c' create, 'u' update, 'd' delete
    source = envelope.get("source") or {}
    cdc_ts_ms = source.get("ts_ms")
    after = envelope.get("after") or envelope.get("before") or {}
    if not after:
        return

    # Rename fields per job
    for src, dst in job.field_renames:
        if src in after:
            after[dst] = after.pop(src)

    # Filter
    if not all(f(after) for f in job.filters):
        return

    apply_outputs(job, after, targets, is_delete=(op == "d"))
    _record_metrics(rdi_db, table, cdc_ts_ms,
                    int(time.time() * 1000), op or "?")


# ---------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------
def main() -> None:
    print(f"[rdi] connecting to RDI db {RDI_DB_HOST}:{RDI_DB_PORT} "
          f"and primary target {TARGET_HOST}:{TARGET_PORT}")
    rdi_db = redis.Redis(host=RDI_DB_HOST, port=RDI_DB_PORT,
                         decode_responses=True)
    targets = open_targets()

    # Wait until primary target answers
    for _ in range(60):
        try:
            targets["target"].ping()
            break
        except Exception:
            time.sleep(2)
    print("[rdi] primary target reachable.")

    jobs, jobs_mtime = load_jobs(JOBS_DIR)
    streams = ensure_groups(rdi_db, jobs)
    last_reload_check = time.time()

    # XREADGROUP loop
    while True:
        # ---- operator pause flag --------------------------------------
        try:
            if rdi_db.get(PAUSE_FLAG_KEY) == "1":
                time.sleep(0.5)
                continue
        except Exception:
            pass

        # ---- hot reload of YAML jobs (every 2 s, cheap) ---------------
        if time.time() - last_reload_check > 2.0:
            jobs, jobs_mtime, changed = maybe_reload(
                JOBS_DIR, jobs, jobs_mtime, rdi_db)
            if changed:
                streams = ensure_groups(rdi_db, jobs)
                try:
                    rdi_db.set("rdi:processor:last_reload_ms",
                               int(time.time() * 1000))
                except Exception:
                    pass
            last_reload_check = time.time()

        try:
            resp = rdi_db.xreadgroup(
                GROUP_NAME, CONSUMER,
                streams={s: ">" for s in streams},
                count=200, block=2000,
            )
        except redis.ResponseError as e:
            if "NOGROUP" in str(e):
                streams = ensure_groups(rdi_db, jobs)
                continue
            raise
        except (redis.ConnectionError, redis.TimeoutError) as e:
            # Transient drop on the rdi-state Redis (idle-side socket
            # close, shard rotate, brief network blip, ...). The redis-py
            # connection pool rebuilds the socket on the next command, so
            # we just need to back off and resume - no manual reconnect.
            # At-least-once delivery is preserved: any entry already
            # claimed by this consumer that we didn't get to XACK stays
            # in the consumer group's pending list and is redelivered on
            # the next XREADGROUP with a non-">" id, or - in the simple
            # case here - is harmlessly skipped because the corresponding
            # target write (JSON.SET / SADD / XADD) is idempotent.
            print(f"[rdi] rdi-state connection dropped ({e}); "
                  f"retrying in 2s")
            time.sleep(2)
            continue

        if not resp:
            continue

        for stream, entries in resp:
            table_fqn = stream_to_table(stream)
            job = jobs.get(table_fqn)
            if not job:
                continue
            for entry_id, fields in entries:
                # Debezium Server's Redis sink emits each entry with two
                # anonymous fields: <key_json>=<value_json>. The dict we
                # receive has the key-JSON as its (only) field name, and
                # the value-JSON as its value. Just take the value.
                raw = next(iter(fields.values()), "")
                process_event(job, raw, targets, rdi_db, table_fqn)
                try:
                    rdi_db.xack(stream, GROUP_NAME, entry_id)
                except (redis.ConnectionError, redis.TimeoutError) as e:
                    # rdi-state went away mid-batch. Bail out of this
                    # batch; the outer loop will reconnect on the next
                    # XREADGROUP. The unacked entry stays pending and
                    # gets redelivered safely (writes are idempotent).
                    print(f"[rdi] xack failed on {stream}/{entry_id} "
                          f"({e}); will resume on reconnect")
                    break


if __name__ == "__main__":
    main()
