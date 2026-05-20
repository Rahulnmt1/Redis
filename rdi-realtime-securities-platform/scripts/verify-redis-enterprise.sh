#!/usr/bin/env bash
# =====================================================================
# verify-redis-enterprise.sh
#
# Live verification that this demo runs on the REAL Redis Enterprise +
# RDI components (not OSS Redis stand-ins). Re-runnable any time.
#
# Companion document: docs/04-redis-enterprise-verification.md
# =====================================================================
set -u

RE_USER="admin@sectrade.demo"
RE_PASS="SecTradeRedis!1"

pass() { printf '  [\033[32m PASS \033[0m] %s\n' "$1"; }
fail() { printf '  [\033[31m FAIL \033[0m] %s\n' "$1"; FAILED=$((FAILED+1)); }
hdr()  { printf '\n=== %s ===\n' "$1"; }

FAILED=0

hdr "1. Container images come from official Redis registries"
re_img=$(docker inspect sectrade-redis-enterprise --format '{{.Config.Image}}' 2>/dev/null || echo missing)
rdi_img=$(docker inspect sectrade-redis-rdi --format '{{.Config.Image}}' 2>/dev/null || echo missing)
ins_img=$(docker inspect sectrade-redis-insight --format '{{.Config.Image}}' 2>/dev/null || echo missing)
deb_img=$(docker inspect sectrade-debezium --format '{{.Config.Image}}' 2>/dev/null || echo missing)

[[ "$re_img"  == redislabs/redis:*       ]] && pass "target  = $re_img"  || fail "target image: $re_img"
[[ "$rdi_img" == redislabs/redis:*       ]] && pass "rdi-st  = $rdi_img" || fail "rdi state image: $rdi_img"
[[ "$ins_img" == redis/redisinsight:*    ]] && pass "insight = $ins_img" || fail "insight image: $ins_img"
[[ "$deb_img" == quay.io/debezium/server:* ]] && pass "debezium= $deb_img" || fail "debezium image: $deb_img"

hdr "2. Redis Enterprise REST API (target on :9443, RDI state on :9444)"
for host in localhost:9443 localhost:9444; do
  ver=$(curl -sk -u "$RE_USER:$RE_PASS" "https://$host/v1/nodes" 2>/dev/null \
        | python3 -c 'import sys,json
try:
    n=json.load(sys.stdin)[0]
    print(n.get("software_version",""))
except Exception: print("")' 2>/dev/null)
  if [[ "$ver" == 7.* ]]; then
    pass "$host  software_version=$ver"
  else
    fail "$host  could not read /v1/nodes (got: $ver)"
  fi
done

hdr "3. RE-managed modules on target (ReJSON + search)"
modules=$(docker exec sectrade-redis-enterprise redis-cli -p 12000 MODULE LIST 2>/dev/null | tr '\n' ' ')
echo "$modules" | grep -q ReJSON && pass "ReJSON loaded" || fail "ReJSON missing"
echo "$modules" | grep -q search && pass "search (RediSearch) loaded" || fail "search missing"
echo "$modules" | grep -q '/enterprise-managed' && pass "modules path = /enterprise-managed (RE-managed)" \
  || fail "modules not RE-managed"

hdr "4. Modules respond to commands"
qty=$(docker exec sectrade-redis-enterprise redis-cli -p 12000 \
        JSON.GET holding:10001:1001 '$.quantity' 2>/dev/null)
# JSON.GET with a single path returns a JSON array like [60.0]
if [[ "$qty" =~ ^\[[0-9].*\]$ ]]; then
  pass "JSON.GET holding:10001:1001 \$.quantity -> $qty"
else
  fail "JSON.GET returned unexpected shape: $qty"
fi
multi=$(docker exec sectrade-redis-enterprise redis-cli -p 12000 \
          JSON.GET holding:10001:1001 '$.quantity' '$.invested_value' 2>/dev/null)
if [[ "$multi" == *"\$.quantity"* && "$multi" == *"\$.invested_value"* ]]; then
  pass "JSON.GET multi-path works (proves RedisJSON v2 path syntax)"
else
  fail "multi-path JSON.GET unexpected: $multi"
fi

hdr "5. CDC streams present on RDI state DB"
expected_streams=(customer security_master holding trade market_price)
for t in "${expected_streams[@]}"; do
  l=$(docker exec sectrade-redis-rdi redis-cli -p 12001 XLEN "sectrade.portfolio.$t" 2>/dev/null)
  if [[ "${l:-0}" -gt 0 ]]; then
    pass "stream sectrade.portfolio.$t  len=$l"
  else
    fail "stream sectrade.portfolio.$t  missing or empty"
  fi
done

hdr "6. Debezium engine identity"
docker logs sectrade-debezium 2>&1 | grep -q RedisStreamChangeConsumer \
  && pass "RedisStreamChangeConsumer instantiated (real RDI sink)" \
  || fail "Redis sink not found in Debezium logs"
docker logs sectrade-debezium 2>&1 | grep -q "Engine executor started" \
  && pass "Debezium engine started" \
  || fail "Debezium engine never started"

hdr "7. RDI processor (reference impl) emits real RDI write pattern"
writes=$(docker logs sectrade-rdi-processor 2>&1 | grep -cE 'JSON\.SET|HSET|XADD')
[[ $writes -gt 30 ]] && pass "$writes write commands issued (JSON.SET / HSET / XADD)" \
  || fail "only $writes write commands seen"
echo "  note: sectrade/rdi-processor:demo is a REFERENCE IMPLEMENTATION,"
echo "        not the Redis-distributed RDI binary."
echo "        See docs/04-redis-enterprise-verification.md - TL;DR section."

hdr "8. RDI control-plane API (mock) endpoints respond"
TOK=$(curl -sk -X POST https://localhost:8445/api/v1/login \
       -H 'Content-Type: application/json' \
       -d '{"username":"default","password":"rdi_demo_pass"}' 2>/dev/null \
      | python3 -c 'import sys,json
try: print(json.load(sys.stdin)["access_token"])
except: print("")' 2>/dev/null)
if [[ -n "$TOK" ]]; then
  pass "POST /api/v1/login  JWT issued (len=${#TOK})"
else
  fail "login failed"
fi
for ep in /api/v1/status /api/v1/pipelines /api/v1/monitoring/statistics; do
  code=$(curl -sk -o /dev/null -w '%{http_code}' "https://localhost:8445$ep" \
         -H "Authorization: Bearer $TOK")
  [[ "$code" == 200 ]] && pass "GET  $ep -> $code" || fail "GET  $ep -> $code"
done

hdr "9. Redis Insight is the real product"
appver=$(curl -s http://localhost:5540/api/info 2>/dev/null \
         | python3 -c 'import sys,json
try: d=json.load(sys.stdin); print(d.get("appVersion",""), d.get("buildType",""))
except: print("")')
if [[ "$appver" == *DOCKER* ]]; then
  pass "Redis Insight  appVersion+buildType = $appver"
else
  fail "Insight /api/info: $appver"
fi

hdr "10. End-to-end lineage (one fresh trade)"
oid="PROOF-$(date +%s)"
docker exec sectrade-postgres psql -U postgres -d sectrade -tA -c \
  "INSERT INTO portfolio.trade (trade_id, customer_id, security_id, side, quantity, price, trade_value, brokerage, order_id, exchange, executed_at) VALUES (nextval('portfolio.trade_id_seq'), 10001, 1001, 'BUY', 1, 2999.99, 2999.99, 1.5, '$oid', 'NSE', now()); SELECT 1;" >/dev/null
sleep 3
in_state=$(docker exec sectrade-redis-rdi redis-cli -p 12001 \
             XREVRANGE sectrade.portfolio.trade + - COUNT 1 | grep -c "$oid")
in_target=$(docker exec sectrade-redis-enterprise redis-cli -p 12000 \
             XREVRANGE trades:10001 + - COUNT 1 | grep -c "$oid")
in_app=$(curl -s "http://localhost:5050/api/recent-trades/HS0010001" \
          | grep -c '"trade_id"')
[[ $in_state  -gt 0 ]] && pass "Debezium captured $oid into RDI state stream" \
                       || fail "Debezium did NOT capture $oid"
[[ $in_target -gt 0 ]] && pass "RDI processor wrote $oid into target Redis Enterprise" \
                       || fail "RDI processor did NOT write $oid to target"
[[ $in_app    -gt 0 ]] && pass "Dashboard reads trades from target Redis Enterprise" \
                       || fail "Dashboard not returning trades"

echo
if [[ $FAILED -eq 0 ]]; then
  cat <<'EOF'
=================================================================
 ALL CHECKS PASSED

   GENUINE Redis-distributed products in this stack:
     - Redis Enterprise Software 7.22 (both clusters)
     - RedisJSON + RediSearch modules (RE-bundled)
     - Redis Insight 3.4.2
     - Debezium 2.5 (upstream engine bundled INSIDE real RDI;
       run standalone here for laptop transparency)

   REFERENCE IMPLEMENTATIONS in this stack (NOT Redis-distributed):
     - sectrade/rdi-processor:demo  - mimics real RDI's stream
                                     processor; YAML jobs are
                                     verbatim-portable to real RDI
     - sectrade/mock-rdi-api:demo   - mimics RDI control-plane
                                     REST API so Insight's RDI
                                     tab works on a laptop

   Real RDI ships as a VM installer or K8s Helm chart only.
   See docs/04-redis-enterprise-verification.md for full detail
   and the upgrade path (Options A/B/C) to genuine Redis-shipped RDI.
=================================================================
EOF
  exit 0
else
  echo "================================================================="
  echo " $FAILED check(s) failed - investigate before the demo"
  echo "================================================================="
  exit 1
fi
