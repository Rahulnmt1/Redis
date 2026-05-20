-- =====================================================================
-- Securities & Trading Firm - Portfolio System of Record (PostgreSQL)
-- This schema models a brokerage portfolio system similar to what a
-- broker like Securities & Trading Firm runs on Oracle in production.
-- For the demo, PostgreSQL stands in for the Oracle system of record.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS portfolio;
SET search_path TO portfolio, public;

-- ---------------------------------------------------------------------
-- 1. CUSTOMER (Demat / Trading account holder)
-- ---------------------------------------------------------------------
CREATE TABLE customer (
    customer_id      BIGINT       PRIMARY KEY,
    client_code      VARCHAR(20)  NOT NULL UNIQUE,        -- SECTRADE client code, e.g. HS0001234
    pan              VARCHAR(10)  NOT NULL,
    full_name        VARCHAR(120) NOT NULL,
    email            VARCHAR(120),
    phone            VARCHAR(20),
    demat_account    VARCHAR(20)  NOT NULL,               -- DP + Client ID
    risk_profile     VARCHAR(20)  NOT NULL DEFAULT 'MODERATE', -- CONSERVATIVE / MODERATE / AGGRESSIVE
    segment          VARCHAR(20)  NOT NULL DEFAULT 'RETAIL',   -- RETAIL / HNI / UHNI / NRI / INSTITUTIONAL
    kyc_status       VARCHAR(20)  NOT NULL DEFAULT 'VERIFIED',
    onboarded_on     DATE         NOT NULL DEFAULT CURRENT_DATE,
    updated_at       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------
-- 2. SECURITY MASTER (Tradable instrument reference data)
-- ---------------------------------------------------------------------
CREATE TABLE security_master (
    security_id      BIGINT       PRIMARY KEY,
    isin             VARCHAR(12)  NOT NULL UNIQUE,
    symbol           VARCHAR(20)  NOT NULL,               -- e.g. RELIANCE, TCS, INFY
    company_name     VARCHAR(120) NOT NULL,
    exchange         VARCHAR(10)  NOT NULL,               -- NSE / BSE
    segment          VARCHAR(20)  NOT NULL DEFAULT 'EQ',  -- EQ / FNO / DEBT / MF
    sector           VARCHAR(40),
    lot_size         INT          NOT NULL DEFAULT 1,
    face_value       NUMERIC(10,2),
    is_active        BOOLEAN      NOT NULL DEFAULT TRUE,
    updated_at       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------
-- 3. HOLDING (Current position per customer per security)
-- ---------------------------------------------------------------------
CREATE TABLE holding (
    holding_id        BIGINT       PRIMARY KEY,
    customer_id       BIGINT       NOT NULL REFERENCES customer(customer_id),
    security_id       BIGINT       NOT NULL REFERENCES security_master(security_id),
    quantity          NUMERIC(18,4) NOT NULL,
    avg_buy_price     NUMERIC(18,4) NOT NULL,
    invested_value    NUMERIC(20,4) NOT NULL,             -- quantity * avg_buy_price
    last_trade_date   DATE,
    updated_at        TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (customer_id, security_id)
);

CREATE INDEX idx_holding_customer ON holding(customer_id);
CREATE INDEX idx_holding_security ON holding(security_id);

-- ---------------------------------------------------------------------
-- 4. TRADE (Every executed order - append-only)
-- ---------------------------------------------------------------------
CREATE TABLE trade (
    trade_id          BIGINT       PRIMARY KEY,
    customer_id       BIGINT       NOT NULL REFERENCES customer(customer_id),
    security_id       BIGINT       NOT NULL REFERENCES security_master(security_id),
    side              VARCHAR(4)   NOT NULL CHECK (side IN ('BUY','SELL')),
    quantity          NUMERIC(18,4) NOT NULL,
    price             NUMERIC(18,4) NOT NULL,
    trade_value       NUMERIC(20,4) NOT NULL,
    brokerage         NUMERIC(18,4) NOT NULL DEFAULT 0,
    order_id          VARCHAR(40)  NOT NULL,
    exchange          VARCHAR(10)  NOT NULL,
    executed_at       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_trade_customer ON trade(customer_id);
CREATE INDEX idx_trade_executed_at ON trade(executed_at);

-- ---------------------------------------------------------------------
-- 5. MARKET PRICE (LTP - Last Traded Price feed table)
-- ---------------------------------------------------------------------
CREATE TABLE market_price (
    security_id      BIGINT        PRIMARY KEY REFERENCES security_master(security_id),
    ltp              NUMERIC(18,4) NOT NULL,               -- last traded price
    prev_close       NUMERIC(18,4) NOT NULL,
    day_open         NUMERIC(18,4) NOT NULL,
    day_high         NUMERIC(18,4) NOT NULL,
    day_low          NUMERIC(18,4) NOT NULL,
    volume           BIGINT        NOT NULL DEFAULT 0,
    updated_at       TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------
-- Make tables fully replicable for CDC (Debezium / RDI)
-- REPLICA IDENTITY FULL ensures UPDATEs include the full "before" row
-- so transformations can compute deltas reliably.
-- ---------------------------------------------------------------------
ALTER TABLE customer        REPLICA IDENTITY FULL;
ALTER TABLE security_master REPLICA IDENTITY FULL;
ALTER TABLE holding         REPLICA IDENTITY FULL;
ALTER TABLE trade           REPLICA IDENTITY FULL;
ALTER TABLE market_price    REPLICA IDENTITY FULL;
