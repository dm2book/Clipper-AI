import { errorMessage } from '../core/errors.js';
import type { DiscoveredToken } from '../core/token.js';
import type { Pool } from '../infra/db.js';
import type { Logger } from '../infra/logger.js';
import type { Metrics } from '../infra/metrics.js';
import { withRetry } from '../infra/retry.js';
import type { DiscoveryHealth, TokenDiscoveryProvider } from '../providers/interfaces.js';
import { insertDiscoveredToken } from '../repositories/tokens.js';

/** Connects discovery providers to storage: every new token becomes a HOT row. */
export class DiscoveryService {
  constructor(
    private readonly deps: {
      db: Pool;
      providers: TokenDiscoveryProvider[];
      logger: Logger;
      metrics: Metrics;
    },
  ) {}

  async start(): Promise<void> {
    for (const provider of this.deps.providers) {
      await provider.start(async (token) => {
        await this.onToken(token);
      });
      this.deps.logger.info({ provider: provider.name }, 'discovery provider started');
    }
  }

  async stop(): Promise<void> {
    await Promise.allSettled(this.deps.providers.map((p) => p.stop()));
  }

  health(): { connected: boolean; providers: Record<string, DiscoveryHealth> } {
    const providers = Object.fromEntries(this.deps.providers.map((p) => [p.name, p.health()]));
    return { connected: Object.values(providers).some((h) => h.connected), providers };
  }

  /** Stores a discovered token; a brief DB hiccup must not lose a launch. */
  async onToken(token: DiscoveredToken): Promise<boolean> {
    const { db, logger, metrics } = this.deps;
    try {
      const isNew = await withRetry(() => insertDiscoveredToken(db, token), {
        retries: 3,
        baseDelayMs: 250,
        maxDelayMs: 2_000,
        shouldRetry: () => true,
      });
      if (!isNew) return false;
      metrics.tokensDiscovered.inc({ source: token.source });
      if (token.createdAtChain) {
        metrics.detectionLatency.observe(Math.max(0, (Date.now() - token.createdAtChain.getTime()) / 1000));
      }
      logger.info({ token: token.address, source: token.source, ref: token.reference }, 'new token detected');
      return true;
    } catch (err) {
      logger.error({ token: token.address, err: errorMessage(err) }, 'could not store discovered token');
      return false;
    }
  }
}
