#!/usr/bin/env bash
# =====================================================================
# Wrapper around scripts/seed_large_scale.py.
# Runs INSIDE the dashboard container so we hit the docker-network
# endpoints (postgres:5432, redis-enterprise:12000) directly. This
# avoids any clash with a native Postgres or other service on the
# host's port 5432.
#
# Usage:
#   ./scripts/seed-large-scale.sh                       # default 3,000,000 (30 lakh)
#   CUSTOMERS=1000000 ./scripts/seed-large-scale.sh     # 10 lakh — faster smoke test
#   SKIP_PG=1 ./scripts/seed-large-scale.sh             # rebuild Redis only, PG untouched
#   CUSTOMERS=5000000 TARGET_MEM_GB=6 \
#       ./scripts/recreate-target-redis.sh \
#         && CUSTOMERS=5000000 ./scripts/seed-large-scale.sh   # 50 lakh —
#                                                              # needs Docker
#                                                              # bumped to 16 GiB
#                                                              # so the cluster
#                                                              # can host a 6 GiB BDB
#
# Prereqs: the demo stack must be UP (./scripts/setup.sh) and the target
# BDB must already have RedisJSON + RediSearch loaded (it does via
# bootstrap-re.sh). PostgreSQL must already be seeded with the
# security_master rows (it is via docker/postgres/02-seed-data.sql).
# =====================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$0")"/.. && pwd)"
cd "$HERE"

CUSTOMERS="${CUSTOMERS:-3000000}"             # 30 lakh — safe ceiling at 12 GB Docker
HOLDINGS_PER_CUST="${HOLDINGS_PER_CUST:-3}"
BULK_HOLDINGS_REDIS="${BULK_HOLDINGS_REDIS:-0}"   # bulk customers carry holdings in PG only

# Scale guidance — actual measured footprint at 3M, 4 GiB BDB:
#   3,000,010 customer JSONs + FT cust-idx (TAG/TEXT/NUMERIC) + hold-idx
#     used_memory = 3.74 GiB  on a 4 GiB BDB  (~6.5% headroom)
#   => 1 customer ≈ 1.25 KiB on the wire (JSON ~280 B
#                                         + RedisJSON overhead
#                                         + FT cust-idx attributes
#                                         + FT term dictionary for @name TEXT)
#
# Docker memory ↔ cluster ↔ max BDB ↔ max customers (measured / projected):
#
#   12 GiB Docker  →  cluster_provisional 9.57 GiB →  max BDB 4 GiB → 3.0M (sweet spot)
#   16 GiB Docker  →  cluster_provisional ~13.5 GiB → max BDB 6 GiB → 5.0M
#   20 GiB Docker  →  cluster_provisional ~17.5 GiB → max BDB 8 GiB → 7.0M
#   24 GiB Docker  →  cluster_provisional ~21.5 GiB → max BDB 10 GiB → 9.0M
#
# Why the BDB caps so far below the host RAM: Redis Enterprise reserves
# ~2× the configured BDB size as cluster provisional headroom (AOF
# buffers, persistence, replication shadows). To grow beyond 4 GiB BDB
# in this demo, raise Docker Desktop's memory slider, then re-run
# scripts/recreate-target-redis.sh with TARGET_MEM_GB=<new size>.
#
# If you turn on BULK_HOLDINGS_REDIS=1 the per-customer footprint
# multiplies (3 × holding JSON + hold-idx entries) — keep it off unless
# you have a much larger BDB. The portfolio-fetch comparison still works
# without bulk holdings: it runs against the original 10 hand-crafted
# customers seeded by docker/postgres/02-seed-data.sql.
REDIS_WORKERS="${REDIS_WORKERS:-8}"

# Confirm the demo stack is running
if ! docker ps --format '{{.Names}}' | grep -q '^hdfcsec-dashboard$'; then
  echo "FATAL: hdfcsec-dashboard container is not running."
  echo "       Run ./scripts/setup.sh first."
  exit 1
fi

# Copy the seeder INTO the dashboard container so it shares the docker-network DNS
docker cp scripts/seed_large_scale.py hdfcsec-dashboard:/tmp/seed_large_scale.py

docker exec \
  -e CUSTOMERS="$CUSTOMERS" \
  -e HOLDINGS_PER_CUST="$HOLDINGS_PER_CUST" \
  -e BULK_HOLDINGS_REDIS="$BULK_HOLDINGS_REDIS" \
  -e REDIS_WORKERS="$REDIS_WORKERS" \
  -e SKIP_PG="${SKIP_PG:-0}" \
  -e SKIP_REDIS="${SKIP_REDIS:-0}" \
  -e PG_HOST=postgres \
  -e PG_PORT=5432 \
  -e PG_DB=hdfcsec \
  -e PG_USER=postgres \
  -e PG_PASS=postgres \
  -e TARGET_DB_HOST=redis-enterprise \
  -e TARGET_DB_PORT=12000 \
  hdfcsec-dashboard python /tmp/seed_large_scale.py
