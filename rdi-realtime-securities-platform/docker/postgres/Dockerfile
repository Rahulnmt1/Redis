# Custom Postgres image pre-loaded with the Securities & Trading Firm portfolio
# schema, seed data, and Debezium replication user.
FROM postgres:16

# Init scripts run in alphabetical order via /docker-entrypoint-initdb.d
COPY 00-init.sh           /docker-entrypoint-initdb.d/00-init.sh
COPY 01-schema.sql        /docker-entrypoint-initdb.d/01-schema.sql
COPY 02-seed-data.sql     /docker-entrypoint-initdb.d/02-seed-data.sql
COPY 03-debezium-user.sql /docker-entrypoint-initdb.d/03-debezium-user.sql

RUN chmod +x /docker-entrypoint-initdb.d/00-init.sh

ENV POSTGRES_DB=sectrade
ENV POSTGRES_USER=postgres
ENV LANG=en_US.UTF8
