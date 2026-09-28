-- Wallet Intelligence.
--
-- * wallet_events: every wallet-level swap/transfer a wallet activity source
--   reports, including failed transactions (kept for audit, ignored in all
--   calculations). The primary key makes re-delivery harmless (dedupe).
--   No FK to tokens: a wallet's history covers tokens the radar never saw.
-- * wallets: one row per wallet seen, with its latest statistics and
--   classification (stats_computed_at < last_event_at = needs recompute).
-- * tracked_wallets: the watchlist. source 'manual' (TRACKED_WALLETS) is never
--   removed automatically; source 'criteria' follows the classification.
-- * alerts: three wallet alert types and the wallet column for per-wallet caps.

CREATE TABLE wallet_events (
    chain          text        NOT NULL,
    signature      text        NOT NULL,
    ix_index       integer     NOT NULL,
    wallet         text        NOT NULL,
    token_address  text        NOT NULL,
    kind           text        NOT NULL CHECK (kind IN ('buy', 'sell', 'transfer_in', 'transfer_out')),
    status         text        NOT NULL CHECK (status IN ('success', 'failed')),
    amount_raw     numeric(40,0) NOT NULL CHECK (amount_raw >= 0),
    value_usd      numeric     CHECK (value_usd >= 0),
    block_time     timestamptz NOT NULL,
    slot           bigint,
    source         text        NOT NULL,
    ingested_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chain, signature, ix_index, wallet)
);
CREATE INDEX wallet_events_wallet_time ON wallet_events (chain, wallet, block_time);
CREATE INDEX wallet_events_token_time  ON wallet_events (chain, token_address, block_time);

CREATE TABLE wallets (
    chain              text        NOT NULL,
    address            text        NOT NULL,
    first_seen_at      timestamptz NOT NULL DEFAULT now(),
    last_event_at      timestamptz NOT NULL,
    stats_computed_at  timestamptz,
    classification     text        CHECK (classification IN ('INSUFFICIENT_HISTORY', 'BOT_LIKE', 'NOT_QUALIFIED', 'QUALIFIED')),
    criteria_version   text,
    stats              jsonb,
    PRIMARY KEY (chain, address),
    CHECK ((stats_computed_at IS NULL) = (stats IS NULL))
);
CREATE INDEX wallets_stats_due ON wallets (stats_computed_at NULLS FIRST);

CREATE TABLE tracked_wallets (
    chain       text        NOT NULL,
    address     text        NOT NULL,
    source      text        NOT NULL CHECK (source IN ('manual', 'criteria')),
    reason      text        NOT NULL,
    active      boolean     NOT NULL DEFAULT true,
    added_at    timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chain, address)
);

ALTER TABLE alerts DROP CONSTRAINT alerts_type_check;
ALTER TABLE alerts ADD CONSTRAINT alerts_type_check
    CHECK (type IN ('NEW_TOKEN', 'MOMENTUM', 'WHALE', 'TRACKED_WALLET', 'TRACKED_CLUSTER'));
ALTER TABLE alerts ADD COLUMN wallet text;
CREATE INDEX alerts_wallet_time ON alerts (wallet, created_at) WHERE wallet IS NOT NULL;
