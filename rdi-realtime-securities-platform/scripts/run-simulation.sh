#!/usr/bin/env bash
# =====================================================================
# Generate live trades and price ticks against the Postgres source.
# Watch them appear on the dashboard at http://localhost:5050.
# =====================================================================
set -e
cd "$(dirname "$0")/.."

TRADES_PER_SEC=${TRADES_PER_SEC:-2}
PRICES_PER_SEC=${PRICES_PER_SEC:-8}
DURATION=${DURATION:-0}

echo "[sim] generating ~${TRADES_PER_SEC} trades/s and ~${PRICES_PER_SEC} price ticks/s"
echo "[sim] press Ctrl-C to stop"

docker run --rm \
  --network rdi-demo-sectrade_rdi-net \
  -e PG_HOST=postgres -e PG_PORT=5432 \
  -e PG_DB=sectrade -e PG_USER=postgres -e PG_PASS=postgres \
  -v "$PWD/scripts:/scripts" \
  python:3.12-slim bash -c "pip install -q psycopg2-binary && python /scripts/simulate_trades.py \
    --trades-per-sec ${TRADES_PER_SEC} \
    --price-updates-per-sec ${PRICES_PER_SEC} \
    --duration ${DURATION}"
