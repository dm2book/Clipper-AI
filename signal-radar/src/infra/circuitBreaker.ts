import { CircuitOpenError, TransientError } from '../core/errors.js';

export type CircuitState = 'closed' | 'open' | 'half_open';

export interface CircuitBreakerOptions {
  name: string;
  failureThreshold: number;
  cooldownMs: number;
  now?: () => number;
  /** Which errors mean "the provider is unhealthy". 404s, 429s and bad input do not. */
  isFailure?: (err: unknown) => boolean;
  onStateChange?: (state: CircuitState) => void;
}

/**
 * Stops hammering a provider that is down. After `failureThreshold`
 * consecutive failures the circuit opens; after `cooldownMs` one trial call is
 * let through (half-open). Success closes it, failure re-opens it.
 */
export class CircuitBreaker {
  private failures = 0;
  private openedAt = 0;
  private current: CircuitState = 'closed';
  private trialInFlight = false;
  private readonly now: () => number;
  private readonly isFailure: (err: unknown) => boolean;

  constructor(private readonly opts: CircuitBreakerOptions) {
    this.now = opts.now ?? Date.now;
    this.isFailure = opts.isFailure ?? ((err) => err instanceof TransientError);
  }

  get state(): CircuitState {
    return this.current;
  }

  async exec<T>(fn: () => Promise<T>): Promise<T> {
    if (this.current === 'open') {
      const retryAt = this.openedAt + this.opts.cooldownMs;
      if (this.now() < retryAt || this.trialInFlight) throw new CircuitOpenError(this.opts.name, retryAt);
      this.transition('half_open');
    } else if (this.current === 'half_open' && this.trialInFlight) {
      throw new CircuitOpenError(this.opts.name, this.now() + this.opts.cooldownMs);
    }
    const isTrial = this.current === 'half_open';
    if (isTrial) this.trialInFlight = true;
    try {
      const result = await fn();
      this.failures = 0;
      if (this.current !== 'closed') this.transition('closed');
      return result;
    } catch (err) {
      if (this.isFailure(err)) {
        this.failures++;
        if (isTrial || this.failures >= this.opts.failureThreshold) {
          this.openedAt = this.now();
          this.transition('open');
        }
      } else if (isTrial) {
        // The provider answered (e.g. 404): it is reachable again.
        this.failures = 0;
        this.transition('closed');
      }
      throw err;
    } finally {
      if (isTrial) this.trialInFlight = false;
    }
  }

  private transition(state: CircuitState): void {
    if (state === this.current) return;
    this.current = state;
    this.opts.onStateChange?.(state);
  }
}
