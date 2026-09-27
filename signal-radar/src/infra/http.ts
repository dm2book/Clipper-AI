import type { ZodType } from 'zod';
import {
  NotFoundError,
  PermanentError,
  RateLimitedError,
  SchemaError,
  TransientError,
  isAbortError,
} from '../core/errors.js';
import { CircuitBreaker } from './circuitBreaker.js';
import type { Logger } from './logger.js';
import type { Metrics } from './metrics.js';
import type { TokenBucket } from './rateLimiter.js';
import { withRetry } from './retry.js';

export type FetchFn = (input: string, init: RequestInit) => Promise<Response>;

export interface SchemaErrorInfo {
  provider: string;
  endpoint: string;
  issues: string[];
  sample: unknown;
}

export interface HttpClientOptions {
  provider: string;
  logger: Logger;
  limiter: TokenBucket;
  timeoutMs?: number;
  retries?: number;
  retryBaseDelayMs?: number;
  retryMaxDelayMs?: number;
  breaker?: CircuitBreaker | null;
  headers?: Record<string, string>;
  fetchFn?: FetchFn;
  metrics?: Metrics;
  onSchemaError?: (info: SchemaErrorInfo) => void;
}

export interface JsonRequest<T> {
  method?: 'GET' | 'POST';
  url: string;
  /** Low-cardinality label for logs/metrics, e.g. "tokens". Never the URL. */
  endpoint: string;
  body?: unknown;
  headers?: Record<string, string>;
  schema: ZodType<T>;
  signal?: AbortSignal;
  timeoutMs?: number;
  /**
   * Whether repeating the request is harmless. Defaults to true for GET and
   * false for POST: a POST is only retried on 429 (the server did nothing).
   */
  idempotent?: boolean;
}

export interface UnwrappingRequest<T, U> extends JsonRequest<T> {
  /**
   * Runs inside the retried section on the validated body. Throwing a
   * TransientError here (e.g. a JSON-RPC "node is behind" inside an HTTP 200)
   * is retried exactly like a 5xx.
   */
  unwrap: (data: T) => U;
}

/** Parse `Retry-After` (seconds or HTTP date) and Discord-style fallbacks. */
export function parseRetryAfterMs(headers: Headers, body: unknown, now = Date.now()): number | null {
  const header = headers.get('retry-after');
  if (header !== null) {
    const seconds = Number(header);
    if (Number.isFinite(seconds)) return Math.max(0, seconds * 1000);
    const at = Date.parse(header);
    if (Number.isFinite(at)) return Math.max(0, at - now);
  }
  const resetAfter = Number(headers.get('x-ratelimit-reset-after'));
  if (headers.has('x-ratelimit-reset-after') && Number.isFinite(resetAfter)) return Math.max(0, resetAfter * 1000);
  if (body && typeof body === 'object' && 'retry_after' in body) {
    const s = Number((body as { retry_after: unknown }).retry_after);
    if (Number.isFinite(s)) return Math.max(0, s * 1000);
  }
  return null;
}

function truncate(value: unknown, max = 2_000): unknown {
  try {
    const text = JSON.stringify(value);
    if (text === undefined) return null;
    return text.length <= max ? value : { truncated: text.slice(0, max) };
  } catch {
    return null;
  }
}

/**
 * JSON over HTTP with the resilience every provider needs: rate limiting,
 * timeouts, retry with jittered backoff, Retry-After, a circuit breaker,
 * schema validation, and error classification into the core taxonomy.
 */
export class HttpClient {
  readonly provider: string;
  private readonly fetchFn: FetchFn;
  private readonly breaker: CircuitBreaker | null;

  constructor(private readonly opts: HttpClientOptions) {
    this.provider = opts.provider;
    this.fetchFn = opts.fetchFn ?? ((input, init) => fetch(input, init));
    this.breaker =
      opts.breaker === undefined
        ? new CircuitBreaker({
            name: opts.provider,
            failureThreshold: 5,
            cooldownMs: 30_000,
            onStateChange: (state) => {
              opts.metrics?.circuitState.set({ provider: opts.provider }, state === 'open' ? 1 : 0);
              opts.logger.warn({ provider: opts.provider, state }, 'circuit breaker state changed');
            },
          })
        : opts.breaker;
  }

  get limiter(): TokenBucket {
    return this.opts.limiter;
  }

  requestJson<T>(req: JsonRequest<T>): Promise<T> {
    return this.request({ ...req, unwrap: (data: T) => data });
  }

  async request<T, U>(req: UnwrappingRequest<T, U>): Promise<U> {
    const method = req.method ?? 'GET';
    const idempotent = req.idempotent ?? method === 'GET';
    const attempt = async () => req.unwrap(await this.once(req, method));
    return withRetry(
      () => (this.breaker ? this.breaker.exec(attempt) : attempt()),
      {
        retries: this.opts.retries ?? 3,
        baseDelayMs: this.opts.retryBaseDelayMs ?? 500,
        maxDelayMs: this.opts.retryMaxDelayMs ?? 8_000,
        ...(req.signal ? { signal: req.signal } : {}),
        shouldRetry: (err) =>
          err instanceof RateLimitedError || (idempotent && err instanceof TransientError),
        onRetry: (err, attempt, delayMs) =>
          this.opts.logger.debug(
            { provider: this.provider, endpoint: req.endpoint, attempt, delayMs, err: (err as Error).message },
            'retrying provider request',
          ),
      },
    );
  }

  private async once<T>(req: JsonRequest<T>, method: 'GET' | 'POST'): Promise<T> {
    const { provider } = this;
    const { endpoint } = req;
    await this.opts.limiter.acquire(req.signal);
    const timeout = AbortSignal.timeout(req.timeoutMs ?? this.opts.timeoutMs ?? 10_000);
    const signal = req.signal ? AbortSignal.any([req.signal, timeout]) : timeout;
    const started = performance.now();
    const outcome = (label: string) => {
      this.opts.metrics?.providerRequests.inc({ provider, endpoint, outcome: label });
      this.opts.metrics?.providerLatency.observe({ provider, endpoint }, (performance.now() - started) / 1000);
    };

    let res: Response;
    try {
      res = await this.fetchFn(req.url, {
        method,
        headers: {
          accept: 'application/json',
          ...(req.body !== undefined ? { 'content-type': 'application/json' } : {}),
          ...this.opts.headers,
          ...req.headers,
        },
        ...(req.body !== undefined ? { body: JSON.stringify(req.body) } : {}),
        signal,
      });
    } catch (err) {
      // Shutdown: propagate the abort untouched so callers stop quietly.
      if (req.signal?.aborted) throw req.signal.reason;
      outcome(isAbortError(err) ? 'timeout' : 'network_error');
      throw new TransientError(`${provider} ${endpoint}: ${isAbortError(err) ? 'timeout' : (err as Error).message}`, {
        cause: err,
      });
    }

    const text = await res.text().catch(() => '');
    let body: unknown = undefined;
    if (text) {
      try {
        body = JSON.parse(text);
      } catch {
        body = undefined;
      }
    }

    if (res.status === 429) {
      outcome('rate_limited');
      this.opts.metrics?.rateLimitWaits.inc({ provider });
      const retryAfter = parseRetryAfterMs(res.headers, body);
      this.opts.limiter.pauseFor(retryAfter ?? 1_000);
      throw new RateLimitedError(`${provider} ${endpoint}: rate limited`, retryAfter);
    }
    if (res.status >= 500) {
      outcome(`http_${res.status}`);
      throw new TransientError(`${provider} ${endpoint}: HTTP ${res.status}`);
    }
    if (res.status === 404) {
      outcome('not_found');
      throw new NotFoundError(`${provider} ${endpoint}: not found`);
    }
    if (res.status >= 400) {
      outcome(`http_${res.status}`);
      throw new PermanentError(`${provider} ${endpoint}: HTTP ${res.status} ${text.slice(0, 200)}`, res.status);
    }
    if (body === undefined) {
      outcome('invalid_json');
      this.reportSchemaError(endpoint, ['response is not JSON'], text.slice(0, 500));
      throw new SchemaError(`${provider} ${endpoint}: response is not JSON`, ['response is not JSON']);
    }

    const parsed = req.schema.safeParse(body);
    if (!parsed.success) {
      outcome('schema_error');
      const issues = parsed.error.issues.slice(0, 10).map((i) => `${i.path.join('.') || '(root)'}: ${i.message}`);
      this.reportSchemaError(endpoint, issues, truncate(body));
      throw new SchemaError(`${provider} ${endpoint}: unexpected response shape`, issues);
    }
    outcome('ok');
    return parsed.data;
  }

  private reportSchemaError(endpoint: string, issues: string[], sample: unknown): void {
    this.opts.metrics?.schemaErrors.inc({ provider: this.provider, endpoint });
    this.opts.logger.warn({ provider: this.provider, endpoint, issues }, 'provider response failed validation');
    this.opts.onSchemaError?.({ provider: this.provider, endpoint, issues, sample });
  }
}
