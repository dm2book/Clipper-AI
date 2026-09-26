-- Initial schema. See docs/ARCHITECTURE.md §4.
--
-- Amounts: SOL in lamports (bigint), token amounts in raw base units
-- (numeric(40,0), u64 does not fit in bigint), USD as numeric.

CREATE TABLE tokens (
    mint              text PRIMARY KEY,
    status            text NOT NULL DEFAULT 'WATCHING'
                      CHECK (status IN ('WATCHING','REJECTED','EXPIRED','PASSED','BOUGHT','SKIPPED')),
    source            text NOT NULL,
    detect_signature  text,
    pool_address      text,
    symbol            text,
    name              text,
    decimals          integer,
    detected_at       timestamptz NOT NULL DEFAULT now(),
    next_check_at     timestamptz NOT NULL DEFAULT now(),
    last_checked_at   timestamptz,
    check_count       integer NOT NULL DEFAULT 0,
    last_reason       text
);
CREATE INDEX tokens_queue_idx ON tokens (next_check_at) WHERE status = 'WATCHING';

CREATE TABLE token_analyses (
    id             bigserial PRIMARY KEY,
    mint           text NOT NULL REFERENCES tokens(mint),
    analyzed_at    timestamptz NOT NULL DEFAULT now(),
    verdict        text NOT NULL CHECK (verdict IN ('PASS','WAIT','REJECT')),
    liquidity_usd  numeric,
    holders        integer,
    top10_pct      numeric,
    checks         jsonb NOT NULL,
    report         jsonb NOT NULL
);
CREATE INDEX token_analyses_mint_idx ON token_analyses (mint, analyzed_at DESC);

CREATE TABLE positions (
    id                 bigserial PRIMARY KEY,
    mint               text NOT NULL REFERENCES tokens(mint),
    status             text NOT NULL
                       CHECK (status IN ('OPENING','OPEN','CLOSING','CLOSED','FAILED')),
    decimals           integer NOT NULL,
    entry_lamports     bigint,              -- SOL into the swap (fees tracked on trades)
    entry_usd          numeric,
    initial_amount     numeric(40,0),       -- raw tokens received
    remaining_amount   numeric(40,0),
    entry_price        numeric,             -- lamports per raw token
    peak_price         numeric,
    last_price         numeric,
    tp_hit             boolean NOT NULL DEFAULT false,
    realized_lamports  bigint NOT NULL DEFAULT 0,   -- SOL received from sells
    sell_failures      integer NOT NULL DEFAULT 0,
    opened_at          timestamptz NOT NULL DEFAULT now(),
    closed_at          timestamptz,
    close_reason       text
);
-- At most one live position per token, enforced by the database itself.
CREATE UNIQUE INDEX positions_one_active_per_mint
    ON positions (mint) WHERE status IN ('OPENING','OPEN','CLOSING');

CREATE TABLE trades (
    id                       bigserial PRIMARY KEY,
    position_id              bigint NOT NULL REFERENCES positions(id),
    side                     text NOT NULL CHECK (side IN ('BUY','SELL')),
    reason                   text NOT NULL,
    signature                text NOT NULL UNIQUE,
    status                   text NOT NULL CHECK (status IN ('PENDING','CONFIRMED','FAILED')),
    paper                    boolean NOT NULL,
    quoted_in_amount         numeric(40,0) NOT NULL,
    quoted_out_amount        numeric(40,0) NOT NULL,
    lamports                 bigint,          -- swap leg in SOL (spent on BUY, received on SELL)
    fee_lamports             bigint,          -- network + priority fees, token-account rent
    token_amount             numeric(40,0),   -- tokens moved
    last_valid_block_height  bigint,
    error                    text,
    created_at               timestamptz NOT NULL DEFAULT now(),
    settled_at               timestamptz
);
CREATE INDEX trades_pending_idx ON trades (created_at) WHERE status = 'PENDING';

CREATE TABLE notifications (
    id          bigserial PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    priority    smallint NOT NULL DEFAULT 1,   -- 0 = critical, 1 = trade, 2 = info
    body        text NOT NULL,
    status      text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','SENT','FAILED')),
    attempts    integer NOT NULL DEFAULT 0,
    last_error  text,
    sent_at     timestamptz,
    dedupe_key  text UNIQUE
);
CREATE INDEX notifications_pending_idx ON notifications (priority, id) WHERE status = 'PENDING';

CREATE TABLE events (
    id           bigserial PRIMARY KEY,
    at           timestamptz NOT NULL DEFAULT now(),
    kind         text NOT NULL,
    mint         text,
    position_id  bigint,
    data         jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX events_at_idx ON events (at);

CREATE TABLE bot_state (
    key         text PRIMARY KEY,
    value       text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
