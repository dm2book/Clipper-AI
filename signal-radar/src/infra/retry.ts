import { RateLimitedError, isRetryable } from '../core/errors.js';

/** Resolves after `ms`, rejects with the signal's reason when aborted. */
export function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason);
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, Math.max(0, ms));
    const onAbort = () => {
      clearTimeout(timer);
      reject(signal!.reason);
    };
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

export interface RetryOptions {
  /** Retries after the first attempt (so `retries: 3` means up to 4 attempts). */
  retries: number;
  baseDelayMs: number;
  maxDelayMs: number;
  /** Longest we are willing to wait on a provider's Retry-After before giving up. */
  maxRateLimitWaitMs?: number;
  signal?: AbortSignal;
  shouldRetry?: (err: unknown) => boolean;
  onRetry?: (err: unknown, attempt: number, delayMs: number) => void;
  random?: () => number;
  sleepFn?: (ms: number, signal?: AbortSignal) => Promise<void>;
}

/** Exponential backoff with full jitter: delay ∈ [0, min(max, base·2^attempt)]. */
export function backoffDelay(attempt: number, baseMs: number, maxMs: number, random = Math.random): number {
  return Math.floor(random() * Math.min(maxMs, baseMs * 2 ** attempt));
}

export async function withRetry<T>(fn: (attempt: number) => Promise<T>, opts: RetryOptions): Promise<T> {
  const shouldRetry = opts.shouldRetry ?? isRetryable;
  const wait = opts.sleepFn ?? sleep;
  const random = opts.random ?? Math.random;
  const maxRateLimitWait = opts.maxRateLimitWaitMs ?? 60_000;
  for (let attempt = 0; ; attempt++) {
    opts.signal?.throwIfAborted();
    try {
      return await fn(attempt);
    } catch (err) {
      if (attempt >= opts.retries || !shouldRetry(err) || opts.signal?.aborted) throw err;
      let delay = backoffDelay(attempt, opts.baseDelayMs, opts.maxDelayMs, random);
      if (err instanceof RateLimitedError && err.retryAfterMs !== null) {
        if (err.retryAfterMs > maxRateLimitWait) throw err;
        delay = Math.max(delay, err.retryAfterMs);
      }
      opts.onRetry?.(err, attempt + 1, delay);
      await wait(delay, opts.signal);
    }
  }
}
