#!/bin/sh
# =====================================================================
# Bootstrap both Redis Enterprise clusters used by the demo:
#   - redis-enterprise  : "target" cluster, hot cache for the broker app
#                         DB created on port 12000 with RedisJSON + Search
#   - redis-rdi         : "RDI state" cluster - holds CDC streams &
#                         pipeline metadata. DB on port 12001.
# This script is idempotent - safe to re-run.
# =====================================================================
set -e

CLUSTER_USER="admin@sectrade.demo"
CLUSTER_PASS="SecTradeRedis!1"

# ------ helper: poll the REST API until it responds ------------------
# Before the cluster is bootstrapped, /v1/bootstrap returns a JSON body
# with a "state" field. After bootstrap completes, the endpoint requires
# authentication and returns HTTP 401 with an empty body. Either of
# those states means "the API is alive", so we accept both.
wait_for_api() {
  host=$1
  echo "[bootstrap] waiting for $host:9443 ..."
  i=0
  while [ $i -lt 90 ]; do
    body=$(curl -sk --max-time 3 "https://$host:9443/v1/bootstrap" 2>/dev/null || true)
    code=$(curl -sk -o /dev/null --max-time 3 -w '%{http_code}' \
                "https://$host:9443/v1/bootstrap" 2>/dev/null || echo 000)
    if [ -n "$body" ] && echo "$body" | grep -q '"state"'; then
      echo "[bootstrap] $host API is up (pre-bootstrap)"
      return 0
    fi
    case "$code" in
      401|200)
        echo "[bootstrap] $host API is up (already bootstrapped)"
        return 0 ;;
    esac
    i=$((i+1)); sleep 3
  done
  echo "[bootstrap] FATAL: $host did not come up"; exit 1
}

# ------ helper: get current bootstrap state ---------------------------
get_state() {
  host=$1
  curl -sk --max-time 3 "https://$host:9443/v1/bootstrap" 2>/dev/null |
    sed -n 's/.*"state":"\([^"]*\)".*/\1/p' | head -1
}

# ------ helper: is cluster actually bootstrapped? --------------------
# After create_cluster completes, /v1/cluster requires authentication.
# Anonymous GET returns HTTP 401 with WWW-Authenticate=Digest. Before
# create_cluster the same endpoint returns 503/200 with "no_cluster"
# error body. We use the HTTP status code, NOT the body.
cluster_exists() {
  host=$1
  code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 3 \
          "https://$host:9443/v1/cluster" 2>/dev/null || echo 000)
  case "$code" in
    401|200) return 0 ;;     # 401 => cluster up, needs auth (most common)
    *)       return 1 ;;     # 503/000/etc => no cluster yet
  esac
}

wait_until_cluster_ok() {
  host=$1
  echo "[bootstrap] waiting for $host cluster to come up..."
  i=0
  while [ $i -lt 90 ]; do
    if cluster_exists "$host"; then
      echo "[bootstrap] $host cluster is up"
      sleep 4   # give the REST API a moment to be fully usable
      return 0
    fi
    i=$((i+1)); sleep 3
  done
  echo "[bootstrap] FATAL: $host cluster never came up"
  exit 1
}

create_cluster() {
  host=$1; name=$2
  if cluster_exists "$host"; then
    echo "[bootstrap] $host cluster already exists"
    return 0
  fi
  state=$(get_state "$host")
  echo "[bootstrap] creating cluster $name on $host (state=$state)"
  curl -sk -X POST "https://$host:9443/v1/bootstrap/create_cluster" \
    -H "Content-Type: application/json" \
    -d "{
      \"action\":\"create_cluster\",
      \"cluster\":{\"name\":\"$name\"},
      \"node\":{\"paths\":{\"persistent_path\":\"/var/opt/redislabs/persist\",\"ephemeral_path\":\"/var/opt/redislabs/tmp\"}},
      \"credentials\":{\"username\":\"$CLUSTER_USER\",\"password\":\"$CLUSTER_PASS\"},
      \"license\":\"\"
    }" >/dev/null
  wait_until_cluster_ok "$host"
}

db_exists() {
  host=$1; name=$2
  curl -sk -u "$CLUSTER_USER:$CLUSTER_PASS" \
    "https://$host:9443/v1/bdbs" 2>/dev/null |
    grep -o "\"name\":\"$name\"" >/dev/null
}

create_db() {
  host=$1; name=$2; port=$3; modules=$4; mem_bytes=$5
  if db_exists "$host" "$name"; then
    echo "[bootstrap] db $name on $host already exists"
    return
  fi
  echo "[bootstrap] creating db $name on $host:$port  mem=$mem_bytes bytes"
  payload="{\"name\":\"$name\",\"type\":\"redis\",\"memory_size\":$mem_bytes,\"port\":$port,\"replication\":false,\"sharding\":false,\"eviction_policy\":\"noeviction\",\"data_persistence\":\"aof\",\"aof_policy\":\"appendfsync-every-sec\",\"module_list\":$modules}"
  code=$(curl -sk -u "$CLUSTER_USER:$CLUSTER_PASS" -X POST \
    "https://$host:9443/v1/bdbs" \
    -H "Content-Type: application/json" \
    -d "$payload" -o /tmp/bdb.json -w '%{http_code}')
  if [ "$code" != "200" ]; then
    echo "[bootstrap] WARN: create db returned $code:"
    head -c 400 /tmp/bdb.json; echo
  fi
  # Wait for DB to become active
  i=0
  while [ $i -lt 30 ]; do
    if db_exists "$host" "$name"; then
      echo "[bootstrap] db $name is registered"
      sleep 3
      return
    fi
    i=$((i+1)); sleep 2
  done
}

# ---------------------------------------------------------------------
# Module pinning — `redislabs/redis:latest` ships TWO versions of each
# module in its catalog (one for Redis 7.1 BDBs, one for Redis 7.4
# BDBs). When the cluster default BDB version is 7.4 and we request a
# module by name only, the API can return:
#   "Unable to find module ReJSON compatible with BDB redis version 7.4"
# even though a compatible 7.4 build IS available. Pinning the exact
# `semantic_version` we want makes BDB creation deterministic across
# image updates. Versions chosen are the Redis 7.4-compatible builds
# currently shipped in `redislabs/redis:latest`; override via env to
# track a different image.
# Docs: https://redis.io/docs/latest/operate/rs/references/rest-api/objects/module/
# ---------------------------------------------------------------------
REJSON_VERSION="${REJSON_VERSION:-2.8.8}"
SEARCH_VERSION="${SEARCH_VERSION:-2.10.17}"
TARGET_MODULES="[{\"module_name\":\"ReJSON\",\"module_args\":\"\",\"semantic_version\":\"$REJSON_VERSION\"},{\"module_name\":\"search\",\"module_args\":\"\",\"semantic_version\":\"$SEARCH_VERSION\"}]"
RDI_MODULES='[]'

# Target BDB size — must hold the cached customer/holding/security set.
#   default 4 GiB matches README §"Scale & measured performance":
#     12 GB Docker → 9.57 GiB cluster provisional → 4 GiB BDB → 3.0M customers
#       + cust-idx (TAG/TEXT/NUMERIC) + hold-idx (NUMERIC)
#   To override, set TARGET_MEM_BYTES in .env (recommended — survives
#   `teardown.sh && setup.sh` across host reboots) or as a one-shot env
#   var when invoking docker compose. `scripts/recreate-target-redis.sh
#   TARGET_MEM_GB=<N>` writes the chosen size back to .env automatically.
#
# Sizing math at higher scale (Docker Desktop slider ↔ max single BDB):
#   12 GiB → 4 GiB BDB  → 3.0M customers   (shipped default)
#   16 GiB → 6 GiB BDB  → 5.0M customers
#   20 GiB → 8 GiB BDB  → 7.0M customers
#   24 GiB → 10 GiB BDB → 9.0M customers
TARGET_MEM="${TARGET_MEM_BYTES:-4294967296}"   # 4 GiB
RDI_MEM="${RDI_MEM_BYTES:-536870912}"          # 512 MiB

wait_for_api redis-enterprise
wait_for_api redis-rdi

create_cluster redis-enterprise sectrade-target.local
create_cluster redis-rdi        sectrade-rdi.local

create_db redis-enterprise portfolio-cache  12000 "$TARGET_MODULES" "$TARGET_MEM"
create_db redis-rdi        rdi-state        12001 "$RDI_MODULES"    "$RDI_MEM"

echo "[bootstrap] DONE"
echo "  - Target Redis Enterprise UI : https://localhost:8443  ($CLUSTER_USER / $CLUSTER_PASS)"
echo "  - RDI Redis Enterprise  UI   : https://localhost:8444  ($CLUSTER_USER / $CLUSTER_PASS)"
echo "  - Target DB port             : 12000"
echo "  - RDI    DB port             : 12001"
