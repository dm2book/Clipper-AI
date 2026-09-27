import { PermanentError, RateLimitedError, SchemaError, errorMessage } from '../core/errors.js';
import type { Pool } from '../infra/db.js';
import type { Logger } from '../infra/logger.js';
import type { Metrics } from '../infra/metrics.js';
import type { NotificationProvider } from '../providers/interfaces.js';
import { claimPendingAlerts, markAlertFailed, markAlertRetry, markAlertSent } from '../repositories/alerts.js';

export interface NotificationServiceDeps {
  db: Pool;
  provider: NotificationProvider;
  logger: Logger;
  metrics: Metrics;
  maxAttempts?: number;
  batchSize?: number;
}

/** Delivers PENDING alerts from the outbox, with backoff and a retry ceiling. */
export class NotificationService {
  private readonly maxAttempts: number;
  private readonly batchSize: number;

  constructor(private readonly deps: NotificationServiceDeps) {
    this.maxAttempts = deps.maxAttempts ?? 8;
    this.batchSize = deps.batchSize ?? 5;
  }

  async runOnce(signal: AbortSignal): Promise<boolean> {
    const { db, provider, logger, metrics } = this.deps;
    const batch = await claimPendingAlerts(db, this.batchSize, 60);
    for (const alert of batch) {
      if (signal.aborted) break; // the rest stay leased and come back after the lease
      try {
        const { messageId } = await provider.send(alert.payload, signal);
        await markAlertSent(db, alert.id, messageId);
        metrics.notifications.inc({ outcome: 'sent' });
        logger.info({ alertId: alert.id, type: alert.type, provider: provider.name, messageId }, 'alert delivered');
      } catch (err) {
        if (signal.aborted) throw err;
        const attempts = alert.attempts + 1;
        if (err instanceof SchemaError) {
          // Only raised after a 2xx: the message was posted, we just could not read the reply.
          await markAlertSent(db, alert.id, null);
          metrics.notifications.inc({ outcome: 'sent_unconfirmed' });
          logger.warn({ alertId: alert.id, issues: err.issues }, 'alert delivered, response unreadable');
        } else if (err instanceof PermanentError || attempts >= this.maxAttempts) {
          await markAlertFailed(db, alert.id, errorMessage(err));
          metrics.notifications.inc({ outcome: 'failed' });
          logger.error({ alertId: alert.id, attempts, err: errorMessage(err) }, 'alert delivery failed permanently');
        } else {
          const retryIn =
            err instanceof RateLimitedError && err.retryAfterMs !== null
              ? Math.max(1, Math.ceil(err.retryAfterMs / 1000))
              : Math.min(600, 5 * 2 ** attempts);
          await markAlertRetry(db, alert.id, errorMessage(err), retryIn);
          metrics.notifications.inc({ outcome: 'retry' });
          logger.warn({ alertId: alert.id, attempts, retryIn, err: errorMessage(err) }, 'alert delivery failed, will retry');
        }
      }
    }
    return batch.length === this.batchSize;
  }
}
