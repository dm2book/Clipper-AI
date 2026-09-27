import { HttpClient, type FetchFn } from '../../src/infra/http.js';
import { silentLogger } from '../../src/infra/logger.js';
import { TokenBucket } from '../../src/infra/rateLimiter.js';

export type Reply = { status: number; body?: unknown; headers?: Record<string, string> } | Error;
export interface RecordedCall {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
}

/**
 * A scripted fetch for tests. Replies are SYNTHETIC — shaped after the
 * provider documentation, not recorded from a live API.
 */
export function scriptedFetch(replies: Reply[] | ((call: RecordedCall) => Reply)) {
  const calls: RecordedCall[] = [];
  const fetchFn: FetchFn = async (url, init) => {
    const call: RecordedCall = {
      url,
      method: init.method ?? 'GET',
      headers: Object.fromEntries(new Headers(init.headers).entries()),
      body: typeof init.body === 'string' ? JSON.parse(init.body) : undefined,
    };
    calls.push(call);
    const next = typeof replies === 'function' ? replies(call) : replies.shift();
    if (!next) throw new Error('no more scripted replies');
    if (next instanceof Error) throw next;
    const text = next.body === undefined ? '' : typeof next.body === 'string' ? next.body : JSON.stringify(next.body);
    return new Response(text, { status: next.status, headers: next.headers ?? {} });
  };
  return { fetchFn, calls };
}

export function testHttp(fetchFn: FetchFn, provider = 'test'): HttpClient {
  return new HttpClient({
    provider,
    logger: silentLogger(),
    limiter: new TokenBucket({ perSecond: 10_000, burst: 10_000 }),
    retries: 2,
    retryBaseDelayMs: 1,
    retryMaxDelayMs: 2,
    breaker: null,
    fetchFn,
  });
}
