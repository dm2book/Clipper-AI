import type { TierSettings } from '../config/env.js';
import {
  CircuitOpenError,
  NotFoundError,
  RateLimitedError,
  SchemaError,
  errorMessage,
  isAbortError,
} from '../core/errors.js';
import { activeTier, type Token } from '../core/token.js';
import type { Chain } from '../core/types.js';
import { mapLimit } from '../infra/concurrency.js';
import type { Pool } from '../infra/db.js';
import type { Logger } from '../infra/logger.js';
import type { Metrics } from '../infra/metrics.js';
import type { HolderProvider, SafetyProvider } from '../providers/interfaces.js';
import { insertHolderSnapshot, insertSafetyReport, latestSafetyReports } from '../repositories/enrichment.js';
import { latestSnapshot } from '../repositories/snapshots.js';
import { claimDueForEnrichment, rescheduleEnrichment, updateTokenInfo } from '../repositories/tokens.js';

export interface EnrichmentServiceDeps {
  db: Pool;
  chain: Chain;
  safetyProviders: SafetyProvider[];
  holderProvider: HolderProvider | null;
  tiers: TierSettings;
  concurrency: number;
  logger: Logger;
  metrics: Metrics;
  /** Persists schema problems (throttled by the caller). */
  onSchemaError?: (provider: string, err: SchemaError) => void;
  /** Called after a token was enriched (e.g. evaluate alerts right away). */
  onEnriched?: (token: Token) => Promise<void>;
}

/** A check that answered UNKNOWN (e.g. provider has not indexed the token) is retried sooner. */
const UNKNOWN_RECHECK_SEC = 60;
/** Wait for market data before spending safety/holder budget on a token. */
const AWAIT_MARKET_DATA_SEC = 20;

/** Safety checks and holder counts for tokens with meaningful liquidity. */
export class EnrichmentService {
  constructor(private readonly deps: EnrichmentServiceDeps) {}

  async runOnce(signal: AbortSignal): Promise<boolean> {
    const { db, chain, concurrency } = this.deps;
    const batch = concurrency * 2;
    const tokens = await claimDueForEnrichment(db, chain, batch, 120);
    if (!tokens.length) return false;
    await mapLimit(tokens, concurrency, (t) =>
      this.enrich(t, signal).catch((err: unknown) => {
        if (signal.aborted) throw err;
        // Contained per token: its lease expires and it is retried; the batch goes on.
        this.deps.logger.error({ token: t.address, err: errorMessage(err) }, 'enrichment failed');
      }),
    );
    return tokens.length === batch;
  }

  async enrich(token: Token, signal: AbortSignal): Promise<void> {
    const { db, chain, tiers, logger } = this.deps;
    const tier = activeTier(token.tier);
    const market = await latestSnapshot(db, chain, token.address);
    if (!market) {
      await rescheduleEnrichment(db, chain, token.address, AWAIT_MARKET_DATA_SEC);
      return;
    }
    if (market.liquidityUsd !== null && market.liquidityUsd < tiers.lowLiquidityUsd) {
      await rescheduleEnrichment(db, chain, token.address, tiers.enrichIntervalSec.COOL);
      return;
    }

    const now = Date.now();
    const latest = new Map((await latestSafetyReports(db, chain, token.address)).map((r) => [r.provider, r]));
    const due = this.deps.safetyProviders.filter((p) => {
      const last = latest.get(p.name);
      if (!last) return true;
      const ageSec = (now - last.checkedAt.getTime()) / 1000;
      return ageSec >= (last.verdict === 'UNKNOWN' ? Math.min(UNKNOWN_RECHECK_SEC, tiers.safetyRecheckSec[tier]) : tiers.safetyRecheckSec[tier]);
    });

    await Promise.all(
      due.map(async (provider) => {
        try {
          const report = await provider.check(chain, token.address, signal);
          await insertSafetyReport(db, report);
          if (report.tokenInfo) await updateTokenInfo(db, chain, token.address, report.tokenInfo);
          this.deps.metrics.enrichments.inc({ provider: provider.name, result: report.verdict.toLowerCase() });
        } catch (err) {
          this.handleError(provider.name, token.address, err, signal);
        }
      }),
    );

    if (this.deps.holderProvider) {
      const hp = this.deps.holderProvider;
      try {
        await insertHolderSnapshot(db, await hp.getHolders(chain, token.address, signal));
        this.deps.metrics.enrichments.inc({ provider: hp.name, result: 'ok' });
      } catch (err) {
        this.handleError(hp.name, token.address, err, signal);
      }
    }

    await rescheduleEnrichment(db, chain, token.address, tiers.enrichIntervalSec[tier]);
    if (this.deps.onEnriched) {
      try {
        await this.deps.onEnriched(token);
      } catch (err) {
        if (signal.aborted) throw err;
        logger.error({ token: token.address, err: errorMessage(err) }, 'post-enrichment evaluation failed');
      }
    }
  }

  private handleError(provider: string, token: string, err: unknown, signal: AbortSignal): void {
    if (signal.aborted || isAbortError(err)) throw err;
    const { logger, metrics } = this.deps;
    if (err instanceof NotFoundError) {
      metrics.enrichments.inc({ provider, result: 'not_found' });
      logger.debug({ provider, token }, 'provider does not know the token yet');
    } else if (err instanceof SchemaError) {
      metrics.enrichments.inc({ provider, result: 'schema_error' });
      logger.warn({ provider, token, issues: err.issues }, 'provider answered in an unexpected shape');
      this.deps.onSchemaError?.(provider, err);
    } else if (err instanceof CircuitOpenError || err instanceof RateLimitedError) {
      metrics.enrichments.inc({ provider, result: 'throttled' });
      logger.debug({ provider, token, err: err.message }, 'provider temporarily unavailable');
    } else {
      metrics.enrichments.inc({ provider, result: 'error' });
      logger.warn({ provider, token, err: errorMessage(err) }, 'enrichment check failed');
    }
  }
}
