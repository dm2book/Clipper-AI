import type { TierSettings } from '../config/env.js';
import { TIERS } from '../core/token.js';
import type { Pool } from '../infra/db.js';
import type { Logger } from '../infra/logger.js';
import type { Metrics } from '../infra/metrics.js';
import { deleteHolderSnapshotsBefore, deleteProviderErrorsBefore } from '../repositories/enrichment.js';
import { deleteSnapshotsBefore } from '../repositories/snapshots.js';
import { deleteTradesBefore } from '../repositories/trades.js';
import { deleteWalletEventsBefore } from '../repositories/wallets.js';
import { archiveTokens, countByTier, updateTiers } from '../repositories/tokens.js';

/** Tiering, archiving and data retention. */
export class MaintenanceService {
  constructor(
    private readonly deps: {
      db: Pool;
      tiers: TierSettings;
      retentionDays: number;
      /** Wallet history is kept longer: statistics need the whole window. */
      walletEventRetentionDays?: number;
      logger: Logger;
      metrics: Metrics;
    },
  ) {}

  async tiersOnce(): Promise<void> {
    const { db, tiers, logger, metrics } = this.deps;
    const retiered = await updateTiers(db, tiers);
    const archived = await archiveTokens(db, tiers);
    const counts = await countByTier(db);
    for (const tier of TIERS) metrics.tokensTracked.set({ tier }, counts[tier]);
    if (retiered || archived.age || archived.neverIndexed || archived.lowLiquidity) {
      logger.info({ retiered, archived, tracked: counts }, 'tiers updated');
    }
  }

  async retentionOnce(): Promise<void> {
    const { db, retentionDays, logger } = this.deps;
    const cutoff = new Date(Date.now() - retentionDays * 86_400_000);
    const snapshots = await deleteSnapshotsBefore(db, cutoff);
    const holders = await deleteHolderSnapshotsBefore(db, cutoff);
    const trades = await deleteTradesBefore(db, cutoff);
    const providerErrors = await deleteProviderErrorsBefore(db, new Date(Date.now() - 14 * 86_400_000));
    const walletEvents = this.deps.walletEventRetentionDays
      ? await deleteWalletEventsBefore(db, new Date(Date.now() - this.deps.walletEventRetentionDays * 86_400_000))
      : 0;
    if (snapshots || holders || trades || providerErrors || walletEvents) {
      logger.info({ snapshots, holders, trades, providerErrors, walletEvents }, 'old data removed');
    }
  }
}
