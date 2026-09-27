/**
 * NotificationProvider for Discord webhooks.
 *   POST https://discord.com/api/webhooks/{id}/{token}?wait=true
 * `wait=true` makes Discord return the created message (we store its id).
 * Limits: ~30 messages/min per webhook, 5 requests/5 s per channel; 429s
 * carry Retry-After / retry_after and are handled by HttpClient.
 * Delivery is at-least-once: an ambiguous failure (timeout, 5xx) is retried
 * by the outbox, which can occasionally produce a duplicate message.
 */
import { z } from 'zod';
import type { AlertPayload } from '../../core/alerts.js';
import type { HttpClient } from '../../infra/http.js';
import type { NotificationProvider } from '../interfaces.js';
import { renderAlert } from './formatter.js';

const messageSchema = z.looseObject({ id: z.string() });

export class DiscordWebhookProvider implements NotificationProvider {
  readonly name = 'discord-webhook';

  constructor(
    private readonly http: HttpClient,
    private readonly webhookUrl: string,
    private readonly username: string,
  ) {}

  async send(alert: AlertPayload, signal?: AbortSignal): Promise<{ messageId: string | null }> {
    const url = new URL(this.webhookUrl);
    url.searchParams.set('wait', 'true');
    const message = await this.http.requestJson({
      method: 'POST',
      url: url.toString(),
      endpoint: 'execute_webhook',
      body: renderAlert(alert, this.username),
      schema: messageSchema,
      ...(signal ? { signal } : {}),
    });
    return { messageId: message.id };
  }
}
