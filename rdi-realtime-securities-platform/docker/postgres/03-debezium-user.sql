-- =====================================================================
-- Set up PostgreSQL for CDC (Change Data Capture)
-- This is the exact preparation RDI requires for a Postgres source.
-- =====================================================================

-- 1. Replication role (matches RDI prepare-source guide)
CREATE ROLE rdi_cdc_user WITH REPLICATION LOGIN PASSWORD 'rdi_cdc_pwd';
GRANT ALL PRIVILEGES ON DATABASE sectrade TO rdi_cdc_user;

-- 2. Replication group so the Debezium user shares ownership of the tables
CREATE ROLE replication_group;
GRANT replication_group TO postgres;
GRANT replication_group TO rdi_cdc_user;

-- 3. Transfer ownership of all portfolio tables to the replication group
ALTER TABLE portfolio.customer        OWNER TO replication_group;
ALTER TABLE portfolio.security_master OWNER TO replication_group;
ALTER TABLE portfolio.holding         OWNER TO replication_group;
ALTER TABLE portfolio.trade           OWNER TO replication_group;
ALTER TABLE portfolio.market_price    OWNER TO replication_group;

-- 4. Grant rights for snapshot reads + sequences (load generator uses these)
GRANT USAGE ON SCHEMA portfolio TO rdi_cdc_user;
GRANT SELECT ON ALL TABLES IN SCHEMA portfolio TO rdi_cdc_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA portfolio TO rdi_cdc_user;

-- 4b. Tables are now owned by replication_group. The role needs schema
--     USAGE so FK checks pass when *any* member of the group writes.
GRANT USAGE ON SCHEMA portfolio TO replication_group;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA portfolio TO replication_group;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA portfolio TO replication_group;

-- 5. Create publication for pgoutput plugin
CREATE PUBLICATION rdi_publication FOR TABLE
    portfolio.customer,
    portfolio.security_master,
    portfolio.holding,
    portfolio.trade,
    portfolio.market_price;

-- 6. Hand the publication over to the Debezium user so it can manage it.
--    Required so Debezium can add/remove tables at runtime via ALTER PUBLICATION.
ALTER PUBLICATION rdi_publication OWNER TO rdi_cdc_user;
GRANT CREATE ON DATABASE sectrade TO rdi_cdc_user;
