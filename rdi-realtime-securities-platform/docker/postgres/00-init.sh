#!/bin/bash
# =====================================================================
# Postgres init script - configures the DB for logical replication
# (Debezium / RDI requirement) before loading the portfolio schema.
# =====================================================================
set -e

# Enable logical replication so Debezium/RDI can capture changes
PGDATA="${PGDATA:-/var/lib/postgresql/data}"

echo "wal_level = logical"               >> "$PGDATA/postgresql.conf"
echo "max_wal_senders = 10"              >> "$PGDATA/postgresql.conf"
echo "max_replication_slots = 10"        >> "$PGDATA/postgresql.conf"

# Allow rdi_cdc_user replication connections from anywhere on the docker net
echo "host replication rdi_cdc_user 0.0.0.0/0 md5" >> "$PGDATA/pg_hba.conf"
echo "host all         rdi_cdc_user 0.0.0.0/0 md5" >> "$PGDATA/pg_hba.conf"

echo "[init] Postgres logical replication enabled."
