-- Signal Radar — initial schema (docs/ARCHITECTURE.md §D).
--
-- Conventions
--   * every entity carries `chain`, so a second chain is data, not a migration
--   * all times are timestamptz (UTC); scheduling uses the database clock
--   * USD values are numeric; raw token amounts are numeric(40,0) (u64+ safe)
--   * provider payloads that fed a decision are kept (jsonb) for audit/replay

CREATE TABLE tokens (
    chain             text        NOT NULL,
    address           text        NOT NULL,
    symbol            text,
    name              text,
    decimals          integer     CHECK (decimals BETWEEN 0 AND 255),
    supply            numeric(40,0) CHECK (supply >= 0),
    token_program     text,
    created_at_chain  timestamptz,              -- blockTime of the pool-creation tx: source of token age
    detected_at       timestamptz NOT NULL DEFAULT now(),
    detection_source  text        NOT NULL,
    detection_ref     text,                     -- e.g. the transaction signature
    tier              text        NOT NULL DEFAULT 'HOT'
                      CHECK (tier IN ('HOT', 'WARM', 'COOL', 'ARCHIVED')),
    next_snapshot_at  timestamptz NOT NULL DEFAULT now(),
    next_enrich_at    timestamptz NOT NULL DEFAULT now(),
    snapshot_misses   integer     NOT NULL DEFAULT 0 CHECK (snapshot_misses >= 0),
    last_snapshot_at  timestamptz,
    archived_at       timestamptz,
    archived_reason   text,
    PRIMARY KEY (chain, address),
    CHECK ((tier = 'ARCHIVED') = (archived_at IS NOT NULL))
);
CREATE INDEX tokens_snapshot_queue ON tokens (chain, next_snapshot_at) WHERE tier <> 'ARCHIVED';
CREATE INDEX tokens_enrich_queue   ON tokens (next_enrich_at)          WHERE tier <> 'ARCHIVED';

CREATE TABLE pools (
    chain          text        NOT NULL,
    address        text        NOT NULL,
    token_address  text        NOT NULL,
    quote_address  text,
    dex            text,
    first_seen_at  timestamptz NOT NULL DEFAULT now(),
    last_seen_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chain, address),
    FOREIGN KEY (chain, token_address) REFERENCES tokens (chain, address)
);
CREATE INDEX pools_token ON pools (chain, token_address);

CREATE TABLE market_snapshots (
    id                bigserial   PRIMARY KEY,
    chain             text        NOT NULL,
    token_address     text        NOT NULL,
    pair_address      text,
    dex_id            text,
    quote_address     text,
    source            text        NOT NULL,
    observed_at       timestamptz NOT NULL,
    price_usd         numeric,
    liquidity_usd     numeric     CHECK (liquidity_usd >= 0),
    fdv_usd           numeric,
    market_cap_usd    numeric,
    volume_m5         numeric     CHECK (volume_m5 >= 0),
    volume_h1         numeric     CHECK (volume_h1 >= 0),
    volume_h6         numeric     CHECK (volume_h6 >= 0),
    volume_h24        numeric     CHECK (volume_h24 >= 0),
    buys_m5           integer,
    sells_m5          integer,
    buys_h1           integer,
    sells_h1          integer,
    buys_h24          integer,
    sells_h24         integer,
    price_change_m5   numeric,
    price_change_h1   numeric,
    price_change_h24  numeric,
    pair_created_at   timestamptz,
    FOREIGN KEY (chain, token_address) REFERENCES tokens (chain, address)
);
CREATE INDEX market_snapshots_token_time ON market_snapshots (chain, token_address, observed_at DESC);
CREATE INDEX market_snapshots_observed   ON market_snapshots (observed_at);

CREATE TABLE safety_reports (
    id              bigserial   PRIMARY KEY,
    chain           text        NOT NULL,
    token_address   text        NOT NULL,
    provider        text        NOT NULL,
    kind            text        NOT NULL CHECK (kind IN ('onchain', 'external')),
    checked_at      timestamptz NOT NULL,
    verdict         text        NOT NULL CHECK (verdict IN ('PASS', 'WARN', 'FAIL', 'UNKNOWN')),
    flags           jsonb       NOT NULL DEFAULT '{}',
    reasons         jsonb       NOT NULL DEFAULT '[]',
    provider_score  numeric,
    raw             jsonb,
    FOREIGN KEY (chain, token_address) REFERENCES tokens (chain, address)
);
CREATE INDEX safety_reports_latest ON safety_reports (chain, token_address, provider, checked_at DESC);

CREATE TABLE holder_snapshots (
    id                   bigserial   PRIMARY KEY,
    chain                text        NOT NULL,
    token_address        text        NOT NULL,
    observed_at          timestamptz NOT NULL,
    holder_count         integer     NOT NULL CHECK (holder_count >= 0),
    holder_count_capped  boolean     NOT NULL DEFAULT false,
    top10_pct            numeric     CHECK (top10_pct BETWEEN 0 AND 100),
    method               text        NOT NULL,
    top_holders          jsonb       NOT NULL DEFAULT '[]',
    FOREIGN KEY (chain, token_address) REFERENCES tokens (chain, address)
);
CREATE INDEX holder_snapshots_token_time ON holder_snapshots (chain, token_address, observed_at DESC);

CREATE TABLE scores (
    id               bigserial   PRIMARY KEY,
    chain            text        NOT NULL,
    token_address    text        NOT NULL,
    computed_at      timestamptz NOT NULL,
    score            numeric     NOT NULL CHECK (score BETWEEN 0 AND 100),
    confidence       numeric     NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    scoring_version  text        NOT NULL,
    components       jsonb       NOT NULL,
    penalties        jsonb       NOT NULL,
    FOREIGN KEY (chain, token_address) REFERENCES tokens (chain, address)
);
CREATE INDEX scores_token_time ON scores (chain, token_address, computed_at DESC);

CREATE TABLE signals (
    id                bigserial   PRIMARY KEY,
    chain             text        NOT NULL,
    token_address     text        NOT NULL,
    score_id          bigint      REFERENCES scores (id),
    type              text        NOT NULL,
    detected_at       timestamptz NOT NULL,
    window_label      text        NOT NULL,
    value             numeric,
    baseline          numeric,
    metric            numeric,
    strength          numeric     CHECK (strength BETWEEN 0 AND 1),
    evidence          jsonb       NOT NULL DEFAULT '{}',
    detector_version  text        NOT NULL,
    FOREIGN KEY (chain, token_address) REFERENCES tokens (chain, address)
);
CREATE INDEX signals_token_time ON signals (chain, token_address, detected_at DESC);

-- Alerts double as the notification outbox: a row is written in the same
-- place the decision is made, and a dispatcher delivers PENDING rows.
CREATE TABLE alerts (
    id                   bigserial   PRIMARY KEY,
    chain                text        NOT NULL,
    token_address        text        NOT NULL,
    type                 text        NOT NULL CHECK (type IN ('NEW_TOKEN', 'MOMENTUM')),
    score_id             bigint      REFERENCES scores (id),
    score                numeric,
    created_at           timestamptz NOT NULL DEFAULT now(),
    dedupe_key           text        NOT NULL UNIQUE,
    status               text        NOT NULL CHECK (status IN ('PENDING', 'SENT', 'FAILED', 'SUPPRESSED')),
    suppressed_reason    text,
    payload              jsonb       NOT NULL,
    attempts             integer     NOT NULL DEFAULT 0,
    next_attempt_at      timestamptz NOT NULL DEFAULT now(),
    last_error           text,
    sent_at              timestamptz,
    provider_message_id  text,
    FOREIGN KEY (chain, token_address) REFERENCES tokens (chain, address),
    CHECK ((status = 'SUPPRESSED') = (suppressed_reason IS NOT NULL))
);
CREATE INDEX alerts_outbox     ON alerts (next_attempt_at) WHERE status = 'PENDING';
CREATE INDEX alerts_token_type ON alerts (chain, token_address, type, created_at DESC);
CREATE INDEX alerts_created    ON alerts (created_at);

-- Quarantine for provider answers that failed schema validation, so an API
-- change is visible instead of silently turning into missing data.
CREATE TABLE provider_errors (
    id           bigserial   PRIMARY KEY,
    provider     text        NOT NULL,
    endpoint     text        NOT NULL,
    occurred_at  timestamptz NOT NULL DEFAULT now(),
    kind         text        NOT NULL,
    message      text        NOT NULL,
    sample       jsonb
);
CREATE INDEX provider_errors_time ON provider_errors (occurred_at);

CREATE TABLE system_state (
    key         text        PRIMARY KEY,
    value       text        NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
