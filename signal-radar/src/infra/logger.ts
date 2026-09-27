import { pino, type Logger } from 'pino';

export type { Logger };

/**
 * Structured JSON logs to stdout. Known secret-bearing fields are censored as
 * a second line of defence; the first is never logging them (see redact.ts).
 */
export function createLogger(level: string, base: Record<string, unknown> = {}): Logger {
  return pino({
    level,
    base: { service: 'signal-radar', ...base },
    timestamp: pino.stdTimeFunctions.isoTime,
    formatters: { level: (label) => ({ level: label }) },
    redact: {
      paths: [
        'apiKey',
        '*.apiKey',
        'accessToken',
        '*.accessToken',
        'webhookUrl',
        '*.webhookUrl',
        'headers.authorization',
        'headers["x-api-key"]',
        '*.headers.authorization',
        '*.headers["x-api-key"]',
      ],
      censor: '***',
    },
  });
}

/** A logger that discards everything; used in tests. */
export function silentLogger(): Logger {
  return pino({ level: 'silent' });
}
