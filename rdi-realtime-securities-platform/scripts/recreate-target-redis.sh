#!/usr/bin/env bash
# =====================================================================
# Recreate just the TARGET Redis Enterprise cluster so the new BDB
# sizing in bootstrap-re.sh takes effect.
#
# Use this before running ./scripts/seed-large-scale.sh at high scale.
#
# This DOES NOT touch the RDI state cluster, Postgres, Debezium, or
# the dashboard. After re-bootstrap, RDI will re-snapshot the small
# baseline dataset; then the seeder can fill the cache to the new size.
# =====================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")"/.. && pwd)"
cd "$HERE"

# Default matches scripts/bootstrap-re.sh and README §"Scale & measured
# performance" (4 GiB BDB on a 12 GB Docker laptop = 3M customers).
TARGET_MEM_GB="${TARGET_MEM_GB:-4}"
TARGET_MEM_BYTES=$(( TARGET_MEM_GB * 1024 * 1024 * 1024 ))

# Persist the chosen sizing to .env (auto-loaded by docker compose) so
# subsequent `teardown.sh && setup.sh` invocations — including after a
# laptop reboot — bring the cluster back at the same size without the
# operator having to remember the magic env var. We only touch the
# TARGET_MEM_BYTES line; other keys (e.g. RDI_MEM_BYTES) are left
# untouched.
ENV_FILE="$HERE/.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "[recreate] creating $ENV_FILE from .env.example template"
  if [ -f "$HERE/.env.example" ]; then
    cp "$HERE/.env.example" "$ENV_FILE"
  else
    : > "$ENV_FILE"
  fi
fi
if grep -q '^TARGET_MEM_BYTES=' "$ENV_FILE"; then
  # portable in-place sed (works on both BSD/macOS and GNU)
  tmp="$ENV_FILE.tmp.$$"
  sed "s|^TARGET_MEM_BYTES=.*|TARGET_MEM_BYTES=${TARGET_MEM_BYTES}|" \
      "$ENV_FILE" > "$tmp" && mv "$tmp" "$ENV_FILE"
else
  printf '\nTARGET_MEM_BYTES=%s\n' "$TARGET_MEM_BYTES" >> "$ENV_FILE"
fi
echo "[recreate] persisted TARGET_MEM_BYTES=$TARGET_MEM_BYTES to $ENV_FILE"

echo "[recreate] tearing down dependent services so they reconnect cleanly..."
docker compose stop dashboard rdi-processor rdi-api debezium >/dev/null

echo "[recreate] removing the target redis-enterprise container..."
docker compose rm -sf redis-enterprise >/dev/null

echo "[recreate] starting redis-enterprise..."
docker compose up -d redis-enterprise

echo "[recreate] re-running bootstrap with TARGET_MEM_BYTES=$TARGET_MEM_BYTES ..."
docker compose run --rm \
  -e TARGET_MEM_BYTES="$TARGET_MEM_BYTES" \
  re-bootstrap

echo "[recreate] bringing back the rest..."
docker compose up -d debezium rdi-processor rdi-api dashboard

echo
echo "[recreate] DONE. Target BDB now sized to ${TARGET_MEM_GB} GiB."
echo "           Persisted to .env — survives teardown/setup/reboot."
echo "           Next: ./scripts/seed-large-scale.sh"
