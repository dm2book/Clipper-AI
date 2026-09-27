import { sleep } from './retry.js';

export interface TokenBucketOptions {
  /** Sustained rate. */
  perSecond: number;
  /** Maximum burst; defaults to max(1, perSecond). */
  burst?: number;
  now?: () => number;
  sleepFn?: (ms: number, signal?: AbortSignal) => Promise<void>;
}

/**
 * Token bucket with FIFO fairness: callers are served strictly in arrival
 * order, so a burst of low-priority work cannot starve a request that queued
 * earlier. A 429 from the provider pauses the whole bucket (`pauseFor`).
 */
export class TokenBucket {
  readonly perSecond: number;
  readonly capacity: number;
  private tokens: number;
  private updatedAt: number;
  private blockedUntil = 0;
  private tail: Promise<void> = Promise.resolve();
  private readonly now: () => number;
  private readonly sleepFn: (ms: number, signal?: AbortSignal) => Promise<void>;

  constructor(opts: TokenBucketOptions) {
    if (!(opts.perSecond > 0)) throw new RangeError('perSecond must be positive');
    this.perSecond = opts.perSecond;
    this.capacity = opts.burst ?? Math.max(1, opts.perSecond);
    if (this.capacity < 1) throw new RangeError('burst must be at least 1');
    this.now = opts.now ?? Date.now;
    this.sleepFn = opts.sleepFn ?? sleep;
    this.tokens = this.capacity;
    this.updatedAt = this.now();
  }

  static perMinute(perMinute: number, burst?: number): TokenBucket {
    return new TokenBucket({ perSecond: perMinute / 60, burst: burst ?? Math.max(1, Math.min(5, perMinute / 60)) });
  }

  acquire(signal?: AbortSignal): Promise<void> {
    const turn = this.tail.then(() => this.take(signal));
    // A rejected (aborted) waiter must not block the ones behind it.
    this.tail = turn.catch(() => undefined);
    return turn;
  }

  /** Stop handing out tokens for `ms` (e.g. a provider's Retry-After). */
  pauseFor(ms: number): void {
    this.blockedUntil = Math.max(this.blockedUntil, this.now() + ms);
  }

  private refill(now: number): void {
    this.tokens = Math.min(this.capacity, this.tokens + ((now - this.updatedAt) / 1000) * this.perSecond);
    this.updatedAt = now;
  }

  private async take(signal?: AbortSignal): Promise<void> {
    for (;;) {
      signal?.throwIfAborted();
      const now = this.now();
      if (now < this.blockedUntil) {
        await this.sleepFn(this.blockedUntil - now, signal);
        continue;
      }
      this.refill(now);
      if (this.tokens >= 1) {
        this.tokens -= 1;
        return;
      }
      await this.sleepFn(Math.ceil(((1 - this.tokens) / this.perSecond) * 1000), signal);
    }
  }
}
