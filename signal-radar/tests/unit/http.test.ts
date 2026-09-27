import { describe, expect, it } from 'vitest';
import { z } from 'zod';
import { NotFoundError, PermanentError, RateLimitedError, SchemaError, TransientError } from '../../src/core/errors.js';
import { HttpClient, parseRetryAfterMs, type FetchFn, type SchemaErrorInfo } from '../../src/infra/http.js';
import { silentLogger } from '../../src/infra/logger.js';
import { Metrics } from '../../src/infra/metrics.js';
import { TokenBucket } from '../../src/infra/rateLimiter.js';

type Reply = { status: number; body?: unknown; headers?: Record<string, string> } | Error;

/** A scripted fetch: each call consumes the next reply. */
function scripted(replies: Reply[]) {
  const calls: { url: string; init: RequestInit }[] = [];
  const fetchFn: FetchFn = async (url, init) => {
    calls.push({ url, init });
    const next = replies.shift();
    if (!next) throw new Error('no more scripted replies');
    if (next instanceof Error) throw next;
    const text = next.body === undefined ? '' : typeof next.body === 'string' ? next.body : JSON.stringify(next.body);
    return new Response(text, { status: next.status, headers: next.headers ?? {} });
  };
  return { fetchFn, calls };
}

function client(fetchFn: FetchFn, extra: Partial<ConstructorParameters<typeof HttpClient>[0]> = {}) {
  return new HttpClient({
    provider: 'test',
    logger: silentLogger(),
    limiter: new TokenBucket({ perSecond: 1000, burst: 1000 }),
    retries: 3,
    retryBaseDelayMs: 1,
    retryMaxDelayMs: 2,
    breaker: null,
    fetchFn,
    ...extra,
  });
}

const schema = z.object({ value: z.number() });

describe('HttpClient', () => {
  it('validates and returns JSON', async () => {
    const { fetchFn } = scripted([{ status: 200, body: { value: 42, extra: true } }]);
    await expect(client(fetchFn).requestJson({ url: 'http://x/a', endpoint: 'a', schema })).resolves.toEqual({ value: 42 });
  });

  it('retries 5xx and network errors for GET', async () => {
    const { fetchFn, calls } = scripted([{ status: 502 }, new TypeError('fetch failed'), { status: 200, body: { value: 1 } }]);
    await expect(client(fetchFn).requestJson({ url: 'http://x/a', endpoint: 'a', schema })).resolves.toEqual({ value: 1 });
    expect(calls).toHaveLength(3);
  });

  it('does not repeat a POST after an ambiguous failure, but does after a 429', async () => {
    const a = scripted([{ status: 503 }]);
    await expect(
      client(a.fetchFn).requestJson({ method: 'POST', url: 'http://x/p', endpoint: 'p', body: {}, schema }),
    ).rejects.toBeInstanceOf(TransientError);
    expect(a.calls).toHaveLength(1);

    const b = scripted([{ status: 429, headers: { 'retry-after': '0' } }, { status: 200, body: { value: 2 } }]);
    await expect(
      client(b.fetchFn).requestJson({ method: 'POST', url: 'http://x/p', endpoint: 'p', body: { a: 1 }, schema }),
    ).resolves.toEqual({ value: 2 });
    expect(b.calls).toHaveLength(2);
    expect(b.calls[0]!.init.body).toBe('{"a":1}');
  });

  it('classifies 404 and other 4xx without retrying', async () => {
    const nf = scripted([{ status: 404 }]);
    await expect(client(nf.fetchFn).requestJson({ url: 'http://x', endpoint: 'e', schema })).rejects.toBeInstanceOf(NotFoundError);
    expect(nf.calls).toHaveLength(1);
    const forbidden = scripted([{ status: 403, body: 'bad key' }]);
    await expect(
      client(forbidden.fetchFn).requestJson({ url: 'http://x', endpoint: 'e', schema }),
    ).rejects.toBeInstanceOf(PermanentError);
  });

  it('surfaces a persistent 429 as RateLimitedError with the wait time', async () => {
    const { fetchFn } = scripted(Array.from({ length: 4 }, () => ({ status: 429, headers: { 'retry-after': '0' } })));
    const err = await client(fetchFn)
      .requestJson({ url: 'http://x', endpoint: 'e', schema })
      .catch((e: unknown) => e);
    expect(err).toBeInstanceOf(RateLimitedError);
    expect((err as RateLimitedError).retryAfterMs).toBe(0);
  });

  it('reports schema problems instead of guessing', async () => {
    const reported: SchemaErrorInfo[] = [];
    const metrics = new Metrics();
    const { fetchFn } = scripted([{ status: 200, body: { value: 'not a number' } }, { status: 200, body: '<html>' }]);
    const c = client(fetchFn, { onSchemaError: (i) => reported.push(i), metrics });
    await expect(c.requestJson({ url: 'http://x', endpoint: 'e', schema })).rejects.toBeInstanceOf(SchemaError);
    await expect(c.requestJson({ url: 'http://x', endpoint: 'e', schema })).rejects.toBeInstanceOf(SchemaError);
    expect(reported).toHaveLength(2);
    expect(reported[0]!.issues[0]).toMatch(/^value:/);
    expect(await metrics.registry.getSingleMetricAsString('radar_schema_errors_total')).toContain('provider="test"');
  });

  it('times out slow requests as transient', async () => {
    const hanging: FetchFn = (_url, init) =>
      new Promise((_, reject) => init.signal!.addEventListener('abort', () => reject(init.signal!.reason)));
    await expect(
      client(hanging, { retries: 0, timeoutMs: 20 }).requestJson({ url: 'http://x', endpoint: 'e', schema }),
    ).rejects.toBeInstanceOf(TransientError);
  });

  it('stops immediately on shutdown instead of retrying', async () => {
    const ctrl = new AbortController();
    const hanging: FetchFn = (_url, init) =>
      new Promise((_, reject) => init.signal!.addEventListener('abort', () => reject(init.signal!.reason)));
    const p = client(hanging).requestJson({ url: 'http://x', endpoint: 'e', schema, signal: ctrl.signal });
    ctrl.abort();
    const err = await p.catch((e: unknown) => e);
    expect(err).not.toBeInstanceOf(TransientError);
  });
});

describe('parseRetryAfterMs', () => {
  it('reads seconds, HTTP dates and Discord bodies', () => {
    expect(parseRetryAfterMs(new Headers({ 'retry-after': '3' }), null)).toBe(3_000);
    expect(parseRetryAfterMs(new Headers({ 'retry-after': new Date(10_000).toUTCString() }), null, 5_000)).toBe(5_000);
    expect(parseRetryAfterMs(new Headers(), { retry_after: 1.5, global: false })).toBe(1_500);
    expect(parseRetryAfterMs(new Headers({ 'x-ratelimit-reset-after': '0.25' }), null)).toBe(250);
    expect(parseRetryAfterMs(new Headers(), null)).toBeNull();
  });
});
