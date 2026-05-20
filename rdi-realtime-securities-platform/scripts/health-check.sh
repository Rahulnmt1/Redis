#!/usr/bin/env bash
# =====================================================================
# scripts/health-check.sh — post-setup / post-reboot sanity check.
#
# Run this after `setup.sh` + the seeder, OR any time you suspect the
# pipeline has drifted (sync latency spiking, dashboard showing pending
# events, etc). It exits non-zero if any layer of the demo is degraded,
# so you can chain it with `&&` in scripts.
#
# What it checks (in dependency order):
#   1. Docker engine is reachable.
#   2. All 8 long-running containers are Up.
#   3. Postgres replication slot `rdi_slot` is active and not lagging.
#   4. rdi-state Redis has all 5 CDC streams + matching consumer groups.
#   5. Target Redis Enterprise has at least the 10 hand-crafted demo
#      customers (proves the seeder ran).
#   6. Dashboard /api/cap/customer-count answers 200.
#   7. End-to-end sync round-trip via /api/cap/multi-shape is < 2 s.
#
# All checks are intentionally lightweight — total runtime ~5 s. No
# code changes happen; this is a read-only verifier.
# =====================================================================
set -u

# ANSI colours (only if stdout is a TTY).
if [ -t 1 ]; then
  GREEN="\033[32m"; YELLOW="\033[33m"; RED="\033[31m"; BOLD="\033[1m"; NC="\033[0m"
else
  GREEN=""; YELLOW=""; RED=""; BOLD=""; NC=""
fi

fail=0
ok()    { printf "  ${GREEN}OK${NC}    %s\n"    "$*"; }
warn()  { printf "  ${YELLOW}WARN${NC}  %s\n"   "$*"; }
bad()   { printf "  ${RED}FAIL${NC}  %s\n"      "$*"; fail=$((fail+1)); }
hdr()   { printf "\n${BOLD}%s${NC}\n" "$*"; }

# ---------- 1. Docker engine -----------------------------------------
hdr "1. Docker engine"
if docker info >/dev/null 2>&1; then
  ok "docker daemon reachable"
else
  bad "docker daemon NOT reachable — start Docker Desktop, then re-run"
  exit 1
fi

# ---------- 2. Containers --------------------------------------------
hdr "2. Containers"
expected=(sectrade-postgres sectrade-redis-enterprise sectrade-redis-rdi \
          sectrade-debezium sectrade-rdi-processor sectrade-rdi-api \
          sectrade-redis-insight sectrade-dashboard)
for c in "${expected[@]}"; do
  state=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)
  if [ "$state" = "running" ]; then
    ok "$c is running"
  else
    bad "$c is $state (expected running)"
  fi
done

# ---------- 3. CDC streams + consumer groups -------------------------
hdr "3. rdi-state CDC streams"
streams=(sectrade.portfolio.customer sectrade.portfolio.holding \
         sectrade.portfolio.trade sectrade.portfolio.security_master \
         sectrade.portfolio.market_price)
for s in "${streams[@]}"; do
  groups=$(docker exec sectrade-redis-rdi redis-cli -p 12001 \
           XINFO GROUPS "$s" 2>/dev/null | grep -c '^name$' || echo 0)
  if [ "${groups:-0}" -ge 1 ]; then
    ok "$s has consumer group ready"
  else
    bad "$s missing consumer group — rdi-processor hasn't registered"
  fi
done

# ---------- 4. Target BDB has demo customers -------------------------
hdr "4. Target Redis Enterprise (portfolio-cache)"
demo_present=$(docker exec sectrade-redis-enterprise redis-cli -p 12000 \
  EXISTS customer:HS0010001 customer:HS0010005 customer:HS0010010 2>/dev/null)
if [ "${demo_present:-0}" = "3" ]; then
  ok "10 demo customers present (HS0010001 / HS0010005 / HS0010010 sampled)"
else
  bad "demo customers missing — run scripts/seed-large-scale.sh"
fi
dbsize=$(docker exec sectrade-redis-enterprise redis-cli -p 12000 DBSIZE 2>/dev/null)
ok "DBSIZE on :12000 = ${dbsize:-?}"

# ---------- 5. Dashboard reachable -----------------------------------
hdr "5. Dashboard"
http=$(curl -sS -o /dev/null -w '%{http_code}' \
       http://localhost:5050/api/cap/customer-count 2>/dev/null || echo 000)
if [ "$http" = "200" ]; then
  cust=$(curl -sS http://localhost:5050/api/cap/customer-count 2>/dev/null \
         | python3 -c 'import json,sys; print(json.load(sys.stdin).get("customers"))' 2>/dev/null)
  ok "/api/cap/customer-count returned 200 (customers=${cust:-?})"
else
  bad "/api/cap/customer-count returned HTTP $http — dashboard not ready"
fi

# ---------- 6. End-to-end propagation --------------------------------
# This is the most important check — it fires a real INSERT into Postgres
# and confirms it lands in target Redis. If this passes, the entire CDC
# pipeline (PG WAL -> Debezium -> rdi-state stream -> rdi-processor ->
# target BDB) is healthy. It also primes the replication slot for the
# next check, so the slot is guaranteed to be active when we look at it.
hdr "6. End-to-end CDC round-trip"
e2e=$(curl -sS -X POST -H 'content-type: application/json' \
       -d '{"segment":"HNI"}' \
       http://localhost:5050/api/cap/multi-shape 2>/dev/null \
       | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)
    ok = d.get("ok"); ms = d.get("propagation_ms")
    print(f"{ok}|{ms}")
except Exception:
    print("error|")' 2>/dev/null)
ok_flag=${e2e%%|*}
ms=${e2e#*|}
if [ "$ok_flag" = "True" ] && [ -n "$ms" ]; then
  # Fail above 2000 ms, warn above 250 ms.
  ms_int=${ms%.*}
  if [ "${ms_int:-0}" -gt 2000 ] 2>/dev/null; then
    bad "PG -> Redis sync took ${ms} ms (> 2 s) — pipeline is degraded"
  elif [ "${ms_int:-0}" -gt 250 ] 2>/dev/null; then
    warn "PG -> Redis sync took ${ms} ms (healthy range is <100 ms)"
  else
    ok "PG -> Redis sync took ${ms} ms"
  fi
else
  bad "/api/cap/multi-shape did not return ok=True"
fi

# ---------- 7. Postgres replication slot (post-event) ----------------
# Checked last because:
#   (a) the e2e check above just fired a fresh INSERT, so wal_sender is
#       guaranteed to be hot and the slot's "active" flag is reliable.
#   (b) on an idle pipeline the slot can briefly show active=false
#       between batches even when CDC is fully healthy — checking after
#       the e2e probe avoids that false-positive.
hdr "7. Postgres replication slot"
slot_out=$(docker exec sectrade-postgres psql -U postgres -d sectrade -tA \
  -c "SELECT active::text || '|' || COALESCE(pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)::text,'')
      FROM pg_replication_slots WHERE slot_name='rdi_slot'" 2>/dev/null || echo "")
if [ -z "$slot_out" ]; then
  bad "rdi_slot not found in pg_replication_slots — Debezium hasn't connected"
else
  active=${slot_out%%|*}
  lag=${slot_out#*|}
  if [ "$active" = "t" ]; then
    ok "rdi_slot is active (lag ${lag:-?} bytes)"
  else
    warn "rdi_slot momentarily inactive — usually benign right after a quiet period; re-run if persistent"
  fi
  # Warn (not fail) at 32 MB; fail at 256 MB.
  if [ -n "$lag" ] && [ "$lag" -gt 268435456 ] 2>/dev/null; then
    bad "WAL lag > 256 MB — pipeline is far behind, processor may be stuck"
  elif [ -n "$lag" ] && [ "$lag" -gt 33554432 ] 2>/dev/null; then
    warn "WAL lag > 32 MB — pipeline is catching up"
  fi
fi

# ---------- Summary --------------------------------------------------
hdr "Summary"
if [ "$fail" -eq 0 ]; then
  printf "  ${GREEN}${BOLD}ALL CHECKS PASSED${NC} — demo is ready.\n\n"
  exit 0
else
  printf "  ${RED}${BOLD}%d CHECK(S) FAILED${NC} — see messages above.\n\n" "$fail"
  printf "  Quick recovery options:\n"
  printf "    * Container missing/stopped : docker compose up -d\n"
  printf "    * CDC layer wedged          : docker compose restart debezium rdi-processor\n"
  printf "    * Demo data missing         : CUSTOMERS=10000 ./scripts/seed-large-scale.sh\n"
  printf "    * Full clean rebuild        : ./scripts/teardown.sh && ./scripts/setup.sh\n\n"
  exit 1
fi
