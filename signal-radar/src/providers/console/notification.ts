import type { AlertPayload } from '../../core/alerts.js';
import type { Logger } from '../../infra/logger.js';
import { renderAlert } from '../discord/formatter.js';
import type { NotificationProvider } from '../interfaces.js';

/**
 * Dry-run notifier used when DISCORD_WEBHOOK_URL is not set: logs exactly the
 * message that would have been posted, so the pipeline can be watched safely.
 */
export class ConsoleNotificationProvider implements NotificationProvider {
  readonly name = 'console';

  constructor(private readonly logger: Logger) {}

  async send(alert: AlertPayload): Promise<{ messageId: string | null }> {
    this.logger.info({ dryRun: true, alert: renderAlert(alert, 'Signal Radar (dry run)') }, 'alert (dry run, not sent)');
    return { messageId: null };
  }
}
