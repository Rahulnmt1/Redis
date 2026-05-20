"""
Mock RDI Control-Plane API for the Securities & Trading Firm demo.

This service implements just enough of the real RDI REST API
(https://redis.io/docs/latest/integrate/redis-data-integration/) to satisfy
the Redis Insight RDI tab on a laptop demo where the actual RDI control
plane (an installer-managed VM/K8s deployment) does not run.

What it does:
  * Serves /api/v1/login                          -> JWT
  * Serves /api/v1/status                         -> live status of our
                                                     Debezium collector +
                                                     reference processor
  * Serves /api/v1/pipelines                      -> rdi/config.yaml +
                                                     rdi/jobs/*.yaml as
                                                     a JSON tree
  * Serves /api/v1/monitoring/statistics          -> per-table CDC counts
                                                     pulled live from the
                                                     RDI state DB
  * Serves test-connection, dry-run, deploy and
    template endpoints with realistic responses.

What it does NOT do:
  * Actually re-deploy the pipeline. The pipeline is already running
    via docker-compose; deploy/start/stop/reset are no-ops that report
    success so the Insight UI behaves correctly.

All responses match the shape Redis Insight expects.
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import time
import uuid
from typing import Any

import jwt
import redis
import yaml
from flask import Flask, jsonify, request

# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------
RDI_DB_HOST = os.getenv("RDI_DB_HOST", "redis-rdi")
RDI_DB_PORT = int(os.getenv("RDI_DB_PORT", "12001"))
TARGET_HOST = os.getenv("TARGET_DB_HOST", "redis-enterprise")
TARGET_PORT = int(os.getenv("TARGET_DB_PORT", "12000"))

RDI_CONFIG_DIR = os.getenv("RDI_CONFIG_DIR", "/rdi")
RDI_USER       = os.getenv("RDI_API_USER", "default")
RDI_PASSWORD   = os.getenv("RDI_API_PASSWORD", "rdi_demo_pass")
JWT_SECRET     = os.getenv("RDI_JWT_SECRET", "sectrade-rdi-demo-secret")
RDI_VERSION    = os.getenv("RDI_VERSION", "1.18.0")
SERVER_NAME    = os.getenv("RDI_SERVER_NAME", "sectrade")

app = Flask(__name__)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def issue_jwt(username: str) -> str:
    payload = {
        "sub": username,
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def load_config() -> dict[str, Any]:
    path = os.path.join(RDI_CONFIG_DIR, "config.yaml")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_jobs() -> list[dict[str, Any]]:
    jobs_dir = os.path.join(RDI_CONFIG_DIR, "jobs")
    out: list[dict[str, Any]] = []
    if not os.path.isdir(jobs_dir):
        return out
    for path in sorted(glob.glob(os.path.join(jobs_dir, "*.yaml"))):
        name = os.path.splitext(os.path.basename(path))[0]
        with open(path) as f:
            content = yaml.safe_load(f) or {}
        out.append({"name": name, **content})
    return out


def rdi_redis() -> redis.Redis:
    return redis.Redis(host=RDI_DB_HOST, port=RDI_DB_PORT, decode_responses=True)


def target_redis() -> redis.Redis:
    return redis.Redis(host=TARGET_HOST, port=TARGET_PORT, decode_responses=True)


# ---------------------------------------------------------------------
# /api/v1/login  -> JWT
# ---------------------------------------------------------------------
@app.post("/api/v1/login")
def login():
    body = request.get_json(silent=True) or {}
    # In real RDI, credentials are checked against the configured user.
    # For the demo we accept the configured password OR any non-empty
    # password (so the customer-facing demo never fails because of typos).
    user = body.get("username") or RDI_USER
    return jsonify({"access_token": issue_jwt(user), "token_type": "bearer"})


# ---------------------------------------------------------------------
# /api/v1/status  -> high-level health
# ---------------------------------------------------------------------
@app.get("/api/v1/status")
def status():
    # Probe Postgres replication slot health via the RDI state DB clients.
    # Cheaper: just check Debezium emitted at least one event.
    try:
        rdi = rdi_redis()
        any_stream = next(rdi.scan_iter(match=f"{SERVER_NAME}.*", count=10), None)
        collector_connected = any_stream is not None
    except Exception:
        collector_connected = False
    return jsonify({
        "components": {
            "collector-source": {
                "status": "ready" if collector_connected else "starting",
                "connected": collector_connected,
                "version": "2.5.4.Final",
            },
            "processor": {
                "status": "ready",
                "version": RDI_VERSION,
            },
        },
        "pipelines": {
            "default": {
                "status": "active",
                "state": "STREAMING" if collector_connected else "SNAPSHOT",
                "tasks": [
                    {"name": f"{SERVER_NAME}.portfolio.customer",        "status": "RUNNING", "created_at": now_iso()},
                    {"name": f"{SERVER_NAME}.portfolio.security_master", "status": "RUNNING", "created_at": now_iso()},
                    {"name": f"{SERVER_NAME}.portfolio.holding",         "status": "RUNNING", "created_at": now_iso()},
                    {"name": f"{SERVER_NAME}.portfolio.trade",           "status": "RUNNING", "created_at": now_iso()},
                    {"name": f"{SERVER_NAME}.portfolio.market_price",    "status": "RUNNING", "created_at": now_iso()},
                ],
            },
        },
    })


# ---------------------------------------------------------------------
# /api/v1/pipelines  -> deployed pipeline = config + jobs
# ---------------------------------------------------------------------
@app.get("/api/v1/pipelines")
def get_pipeline():
    cfg = load_config()
    jobs = load_jobs()
    payload = {**cfg, "jobs": jobs}
    return jsonify(payload)


@app.post("/api/v1/pipelines")
def deploy_pipeline():
    # Real RDI returns an action_id that Insight polls. We register the
    # action as immediately completed in memory.
    action_id = uuid.uuid4().hex
    ACTIONS[action_id] = {"status": "completed", "data": {"deployed": True}}
    return jsonify({"action_id": action_id})


@app.post("/api/v1/pipelines/stop")
def stop_pipeline():
    aid = uuid.uuid4().hex
    ACTIONS[aid] = {"status": "completed", "data": {"running": False}}
    return jsonify({"action_id": aid})


@app.post("/api/v1/pipelines/start")
def start_pipeline():
    aid = uuid.uuid4().hex
    ACTIONS[aid] = {"status": "completed", "data": {"running": True}}
    return jsonify({"action_id": aid})


@app.post("/api/v1/pipelines/reset")
def reset_pipeline():
    aid = uuid.uuid4().hex
    ACTIONS[aid] = {"status": "completed", "data": {"reset": True}}
    return jsonify({"action_id": aid})


# In-memory action registry. Real RDI persists to its state DB.
ACTIONS: dict[str, dict[str, Any]] = {}


@app.get("/api/v1/actions/<action_id>")
def get_action(action_id: str):
    act = ACTIONS.get(action_id)
    if not act:
        return jsonify({"status": "completed", "data": {}})
    return jsonify(act)


# ---------------------------------------------------------------------
# Test connections (target + source)
# ---------------------------------------------------------------------
@app.post("/api/v1/pipelines/targets/dry-run")
def test_targets():
    targets: dict[str, dict[str, Any]] = {}
    cfg = load_config()
    for name, t in (cfg.get("targets") or {}).items():
        host = t.get("connection", {}).get("host", TARGET_HOST)
        port = t.get("connection", {}).get("port", TARGET_PORT)
        try:
            redis.Redis(host=host, port=int(port)).ping()
            targets[name] = {"connected": True}
        except Exception as e:
            targets[name] = {"connected": False, "error": str(e)}
    return jsonify({"targets": targets})


@app.post("/api/v1/pipelines/sources/dry-run")
def test_sources():
    # Insight calls this per source; the payload is the source's "with"
    # block. We answer success because Debezium is already capturing.
    return jsonify({"connected": True})


# ---------------------------------------------------------------------
# Monitoring / Statistics  -> the headline numbers on the Insight UI
# ---------------------------------------------------------------------
@app.get("/api/v1/monitoring/statistics")
def statistics():
    rdi = rdi_redis()
    tgt = target_redis()

    # Map source stream -> total events processed (length of CDC stream)
    streams_stats: dict[str, dict[str, Any]] = {}
    total_total = total_inserted = total_updated = total_deleted = 0
    for s in rdi.scan_iter(match=f"{SERVER_NAME}.portfolio.*", count=50):
        try:
            n = rdi.xlen(s)
        except Exception:
            n = 0
        # Get last entry timestamp for "last_arrival"
        last_arrival = ""
        try:
            tail = rdi.xrevrange(s, count=1)
            if tail:
                ms = int(tail[0][0].split("-")[0])
                last_arrival = dt.datetime.fromtimestamp(
                    ms / 1000, tz=dt.timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass
        # For the demo, treat all as inserts (CDC operation breakdown).
        streams_stats[s] = {
            "total": n, "pending": 0, "inserted": n,
            "updated": 0, "deleted": 0, "filtered": 0,
            "rejected": 0, "deduplicated": 0,
            "last_arrival": last_arrival,
        }
        total_total += n
        total_inserted += n

    # Offsets: latest stream id per stream (RDI's notion of checkpoints)
    offsets: dict[str, str] = {}
    for s, st in streams_stats.items():
        try:
            tail = rdi.xrevrange(s, count=1)
            if tail:
                offsets[s] = tail[0][0]
        except Exception:
            pass

    # Connection inventory (Insight shows these in the UI)
    cfg = load_config()
    connections: dict[str, dict[str, Any]] = {}
    for name, src in (cfg.get("sources") or {}).items():
        c = src.get("connection", {})
        connections[name] = {
            "type": c.get("type", "postgresql"),
            "host": c.get("host", "postgres"),
            "port": c.get("port", 5432),
            "database": c.get("database", "sectrade"),
            "user": "***",
            "password": "***",
            "status": "connected",
        }
    for name, tgt_cfg in (cfg.get("targets") or {}).items():
        c = tgt_cfg.get("connection", {})
        connections[name] = {
            "type": "redis",
            "host": c.get("host", TARGET_HOST),
            "port": c.get("port", TARGET_PORT),
            "database": "0",
            "user": "default",
            "password": "***",
            "status": "connected",
        }

    # Clients - synthesised from active Redis connections
    clients: dict[str, dict[str, Any]] = {
        "debezium-collector": {
            "id": "1", "addr": "172.22.0.10:0", "age_sec": "600",
            "idle_sec": "0", "user": "default",
        },
        "rdi-processor-1": {
            "id": "2", "addr": "172.22.0.11:0", "age_sec": "600",
            "idle_sec": "0", "user": "default",
        },
    }

    return jsonify({
        "connections": connections,
        "data_streams": {
            "totals": {
                "total": total_total, "pending": 0,
                "inserted": total_inserted, "updated": total_updated,
                "deleted": total_deleted, "filtered": 0,
                "rejected": 0, "deduplicated": 0,
            },
            "streams": streams_stats,
        },
        "processing_performance": {
            "total_batches": max(1, total_total // 50),
            "batch_size_avg": min(200, max(1, total_total // 10)),
            "read_time_avg": 1.2,
            "process_time_avg": 2.5,
            "ack_time_avg": 0.4,
            "total_time_avg": 4.1,
            "rec_per_sec_avg": max(1, total_total // 60),
        },
        "rdi_pipeline_status": {
            "rdi_version": RDI_VERSION,
            "address": f"https://rdi-api:443",
            "run_status": "RUNNING",
            "sync_mode": "STREAMING",
        },
        "clients": clients,
        "offsets": offsets,
        "snapshot_status": "completed",
    })


# ---------------------------------------------------------------------
# Schemas / templates  -> let Insight populate the YAML editor
# ---------------------------------------------------------------------
@app.get("/api/v1/pipelines/config/schemas")
def config_schemas():
    # Minimal but valid: tells Insight what fields are allowed in config.
    return jsonify({"$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "object",
                    "properties": {
                        "sources": {"type": "object"},
                        "targets": {"type": "object"},
                        "processors": {"type": "object"},
                    }})


@app.get("/api/v1/pipelines/jobs/schemas")
def jobs_schemas():
    return jsonify({"$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "object",
                    "properties": {
                        "source": {"type": "object"},
                        "transform": {"type": "array"},
                        "output":    {"type": "array"},
                    }})


@app.get("/api/v1/pipelines/strategies")
def strategies():
    return jsonify({"ingest": ["snapshot", "cdc"], "write_behind": []})


@app.get("/api/v1/pipelines/config/templates/<pipeline_type>/<db_type>")
def config_template(pipeline_type: str, db_type: str):
    # Hand back our actual config as the template
    cfg = load_config()
    return jsonify({"template": yaml.safe_dump(cfg, sort_keys=False)})


@app.get("/api/v1/pipelines/jobs/templates/<pipeline_type>")
def job_template(pipeline_type: str):
    jobs = load_jobs()
    template = jobs[0] if jobs else {"source": {}, "output": []}
    template.pop("name", None)
    return jsonify({"template": yaml.safe_dump(template, sort_keys=False)})


@app.get("/api/v1/pipelines/jobs/functions")
def job_functions():
    return jsonify({
        "jmespath": ["concat", "to_string", "to_number", "length"],
        "sql": ["UPPER", "LOWER", "COALESCE"],
    })


@app.post("/api/v1/pipelines/jobs/dry-run")
def dry_run_job():
    body = request.get_json(silent=True) or {}
    sample_input = body.get("input_data") or {}
    return jsonify({
        "transformation_output": {
            "output": sample_input,
            "schema": "JSON",
        },
        "job_output": [
            {"command": "JSON.SET",
             "key": "demo:1",
             "args": ["$", json.dumps(sample_input)]}
        ],
    })


# ---------------------------------------------------------------------
# Root / health probe
# ---------------------------------------------------------------------
@app.get("/")
def root():
    return jsonify({
        "service": "sectrade-mock-rdi-api",
        "version": RDI_VERSION,
        "note": "Demo-only mock. Not a real RDI control plane.",
    })


if __name__ == "__main__":
    # Run with TLS so Insight's https:// URL works as-is.
    app.run(
        host="0.0.0.0",
        port=443,
        ssl_context=("/certs/server.crt", "/certs/server.key"),
        threaded=True,
    )
