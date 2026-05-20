#!/usr/bin/env bash
set -e

echo "[mock-rdi-api] starting on https://0.0.0.0:443"
echo "[mock-rdi-api] config dir: ${RDI_CONFIG_DIR:-/rdi}"
echo "[mock-rdi-api] state db:   ${RDI_DB_HOST:-redis-rdi}:${RDI_DB_PORT:-12001}"
echo "[mock-rdi-api] target db:  ${TARGET_DB_HOST:-redis-enterprise}:${TARGET_DB_PORT:-12000}"
exec python -u app.py
