-- Momentum Detection Engine (replaces the v1 score tables).
--
-- * momentum_signals: the full, explainable engine output (metrics per window,
--   rules, filters, penalties, reasons) for every signal that produced an alert
-- * trades: per-trade data for unique buyers/sellers, extreme-trade filtering
--   and wash-trading heuristics. Filled once a trade stream is connected
--   (roadmap phase 3); until then the engine works from market snapshots.

ALTER TABLE alerts DROP COLUMN score_id;
DROP TABLE signals;
DROP TABLE scores;

CREATE TABLE momentum_signals (
    id               bigserial   PRIMARY KEY,
    chain            text        NOT NULL,
    token_address    text        NOT NULL,
    evaluated_at     timestamptz NOT NULL,
    signal_type      text        NOT NULL CHECK (signal_type IN ('MOMENTUM', 'FILTERED', 'NO_SIGNAL')),
    engine_version   text        NOT NULL,
    primary_window   text        NOT NULL,
    profile          text[]      NOT NULL,
    score            numeric     NOT NULL CHECK (score BETWEEN 0 AND 100),
    confidence       numeric     NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    triggered_rules  text[]      NOT NULL,
    payload          jsonb       NOT NULL,
    FOREIGN KEY (chain, token_address) REFERENCES tokens (chain, address)
);
CREATE INDEX momentum_signals_token_time ON momentum_signals (chain, token_address, evaluated_at DESC);

ALTER TABLE alerts ADD COLUMN momentum_signal_id bigint REFERENCES momentum_signals (id);

CREATE TABLE trades (
    chain          text        NOT NULL,
    signature      text        NOT NULL,
    ix_index       integer     NOT NULL DEFAULT 0,
    token_address  text        NOT NULL,
    pool_address   text,
    wallet         text        NOT NULL,
    side           text        NOT NULL CHECK (side IN ('buy', 'sell')),
    value_usd      numeric     NOT NULL CHECK (value_usd >= 0),
    amount_token   numeric(40,0),
    block_time     timestamptz NOT NULL,
    slot           bigint,
    source         text        NOT NULL,
    PRIMARY KEY (chain, signature, ix_index),
    FOREIGN KEY (chain, token_address) REFERENCES tokens (chain, address)
);
CREATE INDEX trades_token_time ON trades (chain, token_address, block_time);
