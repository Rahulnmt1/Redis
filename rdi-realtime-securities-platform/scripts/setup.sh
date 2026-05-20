#!/usr/bin/env bash
# =====================================================================
# One-command setup for the HDFC Securities RDI demo.
#   Builds containers, brings up the stack, waits for everything to be
#   green, then prints the URLs the presenter should open.
# =====================================================================
set -e
cd "$(dirname "$0")/.."

echo "================================================================="
echo " HDFC Securities - Redis Data Integration (RDI) Demo"
echo "================================================================="

echo "[1/4] Building images (postgres seed, rdi-processor, dashboard)..."
docker compose build --pull

echo "[2/4] Starting the full stack..."
docker compose up -d

echo "[3/4] Waiting for Redis Enterprise bootstrap to finish..."
# bootstrap container is "one-shot" - it exits 0 when both clusters & DBs are ready
for _ in $(seq 1 120); do
  state=$(docker inspect -f '{{.State.Status}}' hdfcsec-re-bootstrap 2>/dev/null || echo missing)
  code=$(docker inspect  -f '{{.State.ExitCode}}' hdfcsec-re-bootstrap 2>/dev/null || echo 1)
  if [ "$state" = "exited" ] && [ "$code" = "0" ]; then
    echo "      bootstrap OK"; break
  fi
  sleep 3
done

echo "[4/4] Waiting for the RDI processor + Debezium to come online..."
# We do NOT wait for the target BDB to hydrate here — by design, the
# target stays empty after setup. Debezium runs with snapshot.mode=never
# (see debezium/conf/application.properties for the rationale), so the
# baseline data is loaded by ./scripts/seed-large-scale.sh in the next
# step the operator runs.
#
# Health signal: the RDI processor XGROUP-CREATEs five streams (one
# per portfolio table) on the rdi-state BDB at start-up. When all five
# exist, processor + Debezium are correctly wired to Postgres + Redis.
ok=0
for _ in $(seq 1 30); do
  c=$(docker exec hdfcsec-redis-rdi sh -c \
        "redis-cli -p 12001 --scan --pattern 'hdfcsec.portfolio.*' | wc -l" \
        2>/dev/null | tr -d '[:space:]' || echo 0)
  if [ "${c:-0}" -ge 5 ]; then
    echo "      pipeline live — 5 CDC streams registered on rdi-state"
    ok=1; break
  fi
  sleep 3
done
if [ "$ok" != "1" ]; then
  echo "      WARN: only $c/5 CDC streams visible after 90s — check 'docker logs hdfcsec-rdi-processor'"
fi

cat <<EOF

=================================================================
 SETUP DONE.

 NEXT STEP — populate the target Redis. setup.sh leaves the target
 BDB empty by design (Debezium runs with snapshot.mode=never; the
 bulk seeder writes the baseline data). Pick a scale:

   CUSTOMERS=10000   ./scripts/seed-large-scale.sh   # ~3 s   - basic-scenario demo
   CUSTOMERS=1000000 ./scripts/seed-large-scale.sh   # ~3 min - 1M-row scale demo
   CUSTOMERS=3000000 ./scripts/seed-large-scale.sh   # ~12 min - full Performance-tab scale

 Whichever scale you choose, the seeder always loads the 10 hand-crafted
 demo customers (HS0010001 - HS0010010), 20 securities, and 20 prices.
 Those are what every Scenario in README §4 uses; the synthetic
 customers above that only matter for the Performance tab and the
 FT.SEARCH-vs-Postgres race.

 RECOMMENDED — after the seeder finishes, run the assertive sanity
 check below. It exits 0 only when every layer (Postgres, Debezium,
 rdi-state streams, rdi-processor, target BDB, dashboard, end-to-end
 round-trip) is green. Catches drift before a customer sees it.

     ./scripts/health-check.sh

 Once the seeder + health-check finish, open these in your browser:

  Portfolio dashboard   :  http://localhost:5050
  Redis Insight         :  http://localhost:5540
  Target RE Cluster UI  :  https://localhost:8443   (admin@hdfcsec.demo / HDFCsecRedis!1)
  RDI    RE Cluster UI  :  https://localhost:8444   (admin@hdfcsec.demo / HDFCsecRedis!1)
  RDI control-plane     :  https://localhost:8445   (default / rdi_demo_pass, mock)

 In Insight -> Redis Data Integration tab, click "Let's connect":
     URL      = https://rdi-api
     Username = default
     Password = rdi_demo_pass

 To start live trade simulation in another terminal:
     ./scripts/run-simulation.sh

 To compare Postgres vs Redis latency:
     ./scripts/benchmark.sh

 To tear everything down:
     ./scripts/teardown.sh

=================================================================
EOF
