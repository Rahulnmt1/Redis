#!/usr/bin/env bash
# =====================================================================
# Side-by-side latency benchmark: same query (live portfolio summary)
# run against Postgres (system of record) and Redis Enterprise (RDI-fed
# cache). Shows why brokers move read traffic out of Oracle.
# =====================================================================
set -e
cd "$(dirname "$0")/.."

ITERATIONS=${ITERATIONS:-200}
CONCURRENCY=${CONCURRENCY:-10}

docker run --rm \
  --network rdi-demo-hdfcsec_rdi-net \
  -e PG_HOST=postgres -e PG_PORT=5432 -e PG_DB=hdfcsec -e PG_USER=postgres -e PG_PASS=postgres \
  -e TARGET_DB_HOST=redis-enterprise -e TARGET_DB_PORT=12000 \
  -e ITERATIONS="$ITERATIONS" -e CONCURRENCY="$CONCURRENCY" \
  -v "$PWD/scripts:/scripts" \
  python:3.12-slim bash -c "pip install -q psycopg2-binary redis && python /scripts/benchmark.py"
