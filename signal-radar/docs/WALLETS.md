# Wallet Intelligence

This module detects large wallet transactions and follows wallets whose results **can be demonstrated** from their own transactions.
It does not predict anything. Historical results say nothing about future results.

> **Status:** the module is complete and tested on synthetic events. **No data source for real wallet events is connected yet.** See [Connecting a data source](#databron-koppelen).
> Until one is connected, the radar stores no wallet events and sends no wallet alerts. Startup logs a warning about this.

## Two kinds of activity, kept separate

| | WHALE ACTIVITY | TRACKED WALLET ACTIVITY |
|---|---|---|
| Question | Is this **transaction** large? | Is this a wallet **on the watchlist**? |
| Criterion | Swap ≥ `WHALE_MIN_TRADE_USD`, or ≥ `WHALE_MIN_LIQUIDITY_SHARE` of the pool liquidity (and ≥ `WHALE_LIQUIDITY_SHARE_FLOOR_USD`) | Added manually (`TRACKED_WALLETS`), or meets **all** classification criteria |
| Alert | `🐋 Wallet bought $48,000 of TOKEN` | `👁️ Tracked wallet bought $12,400 of TOKEN` |
| Says **nothing** about | the trader's quality | the future |

A whale is not automatically a successful trader. A large trade by a whale that is not on the watchlist stays a WHALE alert.
When a tracked wallet trades, you get **one** alert (TRACKED_WALLET). If the trade is also large, the whale reason appears as a field in that alert.

**Cluster:** `👥 4 tracked wallets bought TOKEN within 3 minutes`.
- This fires when ≥ `CLUSTER_MIN_WALLETS` **different** tracked wallets buy the same token within `CLUSTER_WINDOW_MINUTES`.
- Each wallet counts once (its buys are added together).
- **No single wallet may dominate:** no wallet counts for more than `CLUSTER_MAX_WALLET_SHARE` of the weighted total (water-filling cap). The alert shows the raw and the weighted totals, and the share per wallet.
- Cooldown: `CLUSTER_COOLDOWN_MINUTES` per token.

A wallet alert is only created when all of these hold:
- the swap succeeded (failed transactions are stored, but never counted and never alerted);
- the swap has a known USD value;
- the token is in the radar's `tokens` table;
- the event is at most `WALLET_ALERT_MAX_EVENT_AGE_SECONDS` old, so backfill and late deliveries never give alerts;
- the address is not on `WALLET_IGNORE_LIST`. It is also not a program-owned account (pool vault, router), unless you set `WALLET_EXCLUDE_PROGRAM_OWNED=false`.

The following limits also apply:
- at most `WHALE_MAX_ALERTS_PER_WALLET_PER_HOUR` whale alerts per wallet;
- the global `ALERTS_MAX_PER_HOUR`.

Anything above a limit is stored as `SUPPRESSED`, with the reason.

## What is computed (src/wallets/)

### Positions and PnL (`ledger.ts`, FIFO)

- A **position** runs from the first incoming token to (nearly) zero balance. The remainder counts as zero when it is ≤ 0.1% of the peak.
  - Several buys (averaging in) and several partial sells belong to **one** position.
  - **One closed position = one trade** for the win rate.
- **Realised PnL** per sell = proceeds − FIFO cost of the tokens sold.
- **Unrealised PnL** = current value (fresh price × quantity) − cost of the tokens still held.

**Only where the data reliably allows it.** A position is *unreliable* when any of these holds:
- tokens arrived by transfer (cost basis unknown);
- tokens left by transfer (outcome unknown; that is **not** a loss);
- a buy or sell has no USD value;
- more was sold than the ledger saw arrive (history from before the window).

Unreliable positions are counted, but excluded from win rate and average return. PnL is then `null`, never an invented number.

Other data handling:
- **Duplicates:** an event is unique by `(chain, signature, instruction index, wallet)`. The database primary key and the ledger both ignore duplicates, so a re-delivered transaction never produces a second alert.
- **Order:** by block time, slot, signature, then instruction.

### Statistics per wallet (`stats.ts`), over `WALLET_STATS_WINDOW_DAYS`

| Field | Meaning |
|---|---|
| `trades`, `buys`, `sells` | Successful swaps |
| `closedPositions` / `reliableClosedPositions` | All / measurable positions |
| `winRate` | Wins (realised PnL > 0) ÷ reliable positions; `null` without positions |
| `winRateLowerBound` | Lower bound of the 90% Wilson interval: 3 out of 3 is **not** 100% here |
| `avgReturnPct`, `medianReturnPct` | Return per position (PnL ÷ cost) |
| `realizedPnlUsd` | Sum over sells whose cost and proceeds are known |
| `unrealizedPnlUsd`, `exposureUsd` | Open positions with a fresh price; `unpricedOpenPositions` counts the rest |
| `avgHoldingSec`, `medianHoldingSec` | Holding time of sold positions |
| `largestWinShare` | Share of the best position in the total profit |
| `tradesPerDay` | For bot detection |

### Classification (`classify.ts`)

| Class | When |
|---|---|
| `INSUFFICIENT_HISTORY` | Fewer than `WALLET_MIN_CLOSED_POSITIONS` reliable positions: too few to say anything |
| `BOT_LIKE` | More than `WALLET_MAX_TRADES_PER_DAY` |
| `NOT_QUALIFIED` | At least one criterion fails (the alert and the log name which one) |
| `QUALIFIED` | **All** criteria hold: win rate, Wilson lower bound, average and median return, realised PnL, largest-win share |

The resulting text is "voldoet aan volgcriteria wallet-criteria-v1 (34 posities in 90d, winrate 62%, …)" ("meets tracking criteria"). It is **never** "smart money".
Each result lists every criterion with its value, threshold and pass/fail, plus the criteria version (`WALLET_CRITERIA_VERSION`). Bump that version whenever you change a criterion.

**Automatic following** (`WALLET_AUTO_TRACK_QUALIFIED=true`):
- a wallet that becomes `QUALIFIED` goes onto the watchlist with source `criteria`, and comes off again when it no longer qualifies;
- manual wallets (`TRACKED_WALLETS`) are never removed automatically.

The `wallet-stats` job recomputes a wallet's statistics:
- after new events;
- for tracked wallets, at least every `WALLET_STATS_REFRESH_MINUTES` (because the window moves).

## Database (migration 0003)

| Table | Contents |
|---|---|
| `wallet_events` | Every event, failed ones included. PK `(chain, signature, ix_index, wallet)`. Kept for `WALLET_EVENT_RETENTION_DAYS` (≥ the statistics window) |
| `wallets` | Per wallet: `last_event_at`, latest `stats` (jsonb), `classification`, `criteria_version` |
| `tracked_wallets` | Watchlist: `source` manual/criteria, `reason`, `active` |
| `alerts` | New types `WHALE`, `TRACKED_WALLET`, `TRACKED_CLUSTER`, plus a `wallet` column for per-wallet limits |

Wallet events deliberately do **not** go into the `trades` table of the Momentum Engine. They cover only a few wallets, not all trades in a token. Unique buyers counted from them would be misleadingly low.

<a id="databron-koppelen"></a>
## Connecting a data source

Two interfaces in `src/providers/interfaces.ts`:
- `WalletActivitySource`: a live stream of `WalletEvent`s. It receives the watchlist through `setWatchlist`.
- `WalletProvider.getHistory`: history for backfilling a wallet that was just added.

Requirements for every event (`src/wallets/model.ts`):
- `signature` + `ixIndex`: unique per swap within a transaction (needed for dedupe);
- `status`: `failed` for failed transactions (never leave them out silently);
- `kind`: `buy` / `sell` (a swap against a quote token) or `transfer_in` / `transfer_out`;
- `amountRaw`: raw token amount (bigint);
- `valueUsd`: **only** when the source actually knows it. Otherwise `null`: the module then marks the position as unreliable, instead of guessing.

Procedure (as for the other providers, see [PROVIDERS.md](PROVIDERS.md)):
1. Verify the documentation.
2. Record real responses (`scripts/record-fixtures.ts`).
3. Write the mapper with a contract test.
4. Pass the source to `createApp` in `src/app.ts`.
