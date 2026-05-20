#!/usr/bin/env bash
# =====================================================================
# Tear down the demo, remove containers + volumes.
# =====================================================================
set -e
cd "$(dirname "$0")/.."
docker compose down -v --remove-orphans
echo "All demo resources removed."
