#!/usr/bin/env bash
# =====================================================================
# Quick verification: show key counts and sample data in the target
# Redis Enterprise DB.  Useful as a sanity-check before the demo.
# =====================================================================
set -e

echo "=== Target Redis Enterprise (portfolio cache) ==="
docker exec hdfcsec-redis-enterprise redis-cli -p 12000 INFO keyspace
echo
echo "Sample keys:"
docker exec hdfcsec-redis-enterprise redis-cli -p 12000 SCAN 0 COUNT 30
echo
echo "Customer Rajesh Sharma (HS0010001):"
docker exec hdfcsec-redis-enterprise redis-cli -p 12000 JSON.GET customer:HS0010001 \$
echo
echo "Live price for RELIANCE (security_id=1001):"
docker exec hdfcsec-redis-enterprise redis-cli -p 12000 HGETALL price:1001
echo
echo "=== RDI Redis (Debezium streams) ==="
docker exec hdfcsec-redis-rdi redis-cli -p 12001 INFO keyspace
echo
echo "CDC stream lengths:"
docker exec hdfcsec-redis-rdi redis-cli -p 12001 KEYS 'hdfcsec.portfolio.*' | while read s; do
  if [ -n "$s" ]; then
    len=$(docker exec hdfcsec-redis-rdi redis-cli -p 12001 XLEN "$s")
    echo "  $s  ->  $len events"
  fi
done
