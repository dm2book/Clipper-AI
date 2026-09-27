/**
 * Error taxonomy (docs/ARCHITECTURE.md §J). Every failure that crosses a
 * provider boundary is one of these, so callers decide on retry/fallback by
 * type instead of by parsing messages.
 */

export class RadarError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = new.target.name;
  }
}

/** Timeouts, dropped connections, 5xx: safe to retry an idempotent request. */
export class TransientError extends RadarError {}

/** HTTP 429 or a provider-specific "slow down". */
export class RateLimitedError extends RadarError {
  constructor(
    message: string,
    readonly retryAfterMs: number | null,
  ) {
    super(message);
  }
}

/** The resource does not exist (yet). Indexers lag behind brand-new tokens. */
export class NotFoundError extends RadarError {}

/** 4xx other than 404/429: retrying will not help (bad key, bad request). */
export class PermanentError extends RadarError {
  constructor(
    message: string,
    readonly status: number | null = null,
  ) {
    super(message);
  }
}

/** The provider answered, but not in the shape we validated against. */
export class SchemaError extends RadarError {
  constructor(
    message: string,
    readonly issues: string[],
    readonly sample?: unknown,
  ) {
    super(message);
  }
}

/** The circuit breaker for a provider is open; fail fast. */
export class CircuitOpenError extends RadarError {
  constructor(
    readonly provider: string,
    readonly retryAtMs: number,
  ) {
    super(`circuit open for ${provider}`);
  }
}

/** A provider interface exists but no implementation is connected yet. */
export class NotConfiguredError extends RadarError {}

export function isRetryable(err: unknown): boolean {
  return err instanceof TransientError || err instanceof RateLimitedError;
}

export function isAbortError(err: unknown): boolean {
  return err instanceof Error && (err.name === 'AbortError' || err.name === 'TimeoutError');
}

export function errorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}
