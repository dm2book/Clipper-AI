import type { TierSettings } from '../config/env.js';
import { CircuitOpenError, RateLimitedError, errorMessage } from '../core/errors.js';
import type { MarketSnapshot } from '../core/marketSnapshot.js';
import { notIndexedBackoffSec, snapshotIntervalSec, type Token } from '../core/token.js';
import type { Chain } from '../core/types.js';
import type { Pool } from '../infra/db.js';
import type { Logger } from '../infra/logger.js';
import type { Metrics } from '../infra/metrics.js';
import type { MarketDataProvider } from '../providers/interfaces.js';
import { insertSnapshot, upsertPool } from '../repositories/snapshots.js';
import { claimDueForSnapshot, markSnapshotFound, markSnapshotMissing, rescheduleSnapshot } from '../repositories/tokens.js';

export interface MarketDataServiceDeps {
  db: Pool;
  chain: Chain;
  provider: MarketDataProvider;
  tiers: TierSettings;
  logger: Logger;
  metrics: Metrics;
  /** Called for every stored snapshot (signal evaluation). Errors are contained. */
  onSnapshot?: (token: Token, snapshot: MarketSnapshot) => Promise<void>;
  leaseSec?: number;
}

/** Polls market data for due tokens in provider-sized batches. */
export class MarketDataService {
  constructor(private readonly deps: MarketDataServiceDeps) {}

  /** One batch. Returns true when the batch was full (more work is likely waiting). */
  async runOnce(signal: AbortSignal): Promise<boolean> {
    const { db, chain, provider, tiers, logger, metrics } = this.deps;
    const tokens = await claimDueForSnapshot(db, chain, provider.maxBatchSize, this.deps.leaseSec ?? 60);
    if (!tokens.length) return false;
    const addresses = tokens.map((t) => t.address);

    let snapshots: Map<string, MarketSnapshot>;
    try {
      snapshots = await provider.getSnapshots(chain, addresses, signal);
    } catch (err) {
      if (signal.aborted) throw err;
      const retryIn =
        err instanceof RateLimitedError && err.retryAfterMs !== null
          ? Math.max(5, Math.ceil(err.retryAfterMs / 1000))
          : err instanceof CircuitOpenError
            ? 30
            : 10;
      await rescheduleSnapshot(db, chain, addresses, retryIn);
      metrics.snapshots.inc({ result: 'provider_error' }, tokens.length);
      logger.warn({ provider: provider.name, tokens: tokens.length, retryIn, err: errorMessage(err) }, 'market data batch failed');
      return false;
    }

    for (const token of tokens) {
      const snapshot = snapshots.get(token.address);
      try {
        if (!snapshot) {
          const misses = token.snapshotMisses + 1;
          await markSnapshotMissing(db, chain, token.address, misses, notIndexedBackoffSec(misses));
          metrics.snapshots.inc({ result: 'not_indexed' });
          continue;
        }
        await insertSnapshot(db, snapshot);
        await upsertPool(db, snapshot);
        await markSnapshotFound(db, chain, token.address, snapshotIntervalSec(token.tier, snapshot.liquidityUsd, tiers), {
          symbol: snapshot.symbol,
          name: snapshot.name,
        });
        metrics.snapshots.inc({ result: 'stored' });
      } catch (err) {
        logger.error({ token: token.address, err: errorMessage(err) }, 'storing snapshot failed');
        continue;
      }
      if (this.deps.onSnapshot) {
        try {
          await this.deps.onSnapshot(
            { ...token, symbol: token.symbol ?? snapshot.symbol, name: token.name ?? snapshot.name },
            snapshot,
          );
        } catch (err) {
          if (signal.aborted) throw err;
          logger.error({ token: token.address, err: errorMessage(err) }, 'signal evaluation failed');
        }
      }
    }
    return tokens.length === provider.maxBatchSize;
  }
}
