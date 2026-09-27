import { describe, expect, it } from 'vitest';
import { MemoryCache, RedisCache, SingleFlight, cached } from '../../src/infra/cache.js';
import { silentLogger } from '../../src/infra/logger.js';

describe('MemoryCache', () => {
  it('expires entries and evicts least recently used', async () => {
    let t = 0;
    const cache = new MemoryCache(2, () => t);
    await cache.set('a', { n: 1 }, 1_000);
    await cache.set('b', 2, 1_000);
    await cache.get('a'); // a is now most recent
    await cache.set('c', 3, 1_000); // evicts b
    expect(await cache.get('b')).toBeUndefined();
    expect(await cache.get('a')).toEqual({ n: 1 });
    t = 1_000;
    expect(await cache.get('a')).toBeUndefined();
  });

  it('returns copies, not shared references', async () => {
    const cache = new MemoryCache();
    const value = { list: [1] };
    await cache.set('k', value, 1_000);
    value.list.push(2);
    expect(await cache.get('k')).toEqual({ list: [1] });
  });
});

describe('cached + SingleFlight', () => {
  it('loads once for concurrent callers and serves later calls from cache', async () => {
    const cache = new MemoryCache();
    const flight = new SingleFlight();
    let loads = 0;
    const load = async () => {
      loads++;
      await new Promise((r) => setTimeout(r, 10));
      return 'value';
    };
    const results = await Promise.all([1, 2, 3].map(() => cached(cache, flight, 'k', 1_000, load)));
    expect(results).toEqual(['value', 'value', 'value']);
    expect(await cached(cache, flight, 'k', 1_000, load)).toBe('value');
    expect(loads).toBe(1);
  });

  it('does not cache failures', async () => {
    const cache = new MemoryCache();
    const flight = new SingleFlight();
    await expect(cached(cache, flight, 'k', 1_000, () => Promise.reject(new Error('boom')))).rejects.toThrow('boom');
    expect(await cached(cache, flight, 'k', 1_000, async () => 'ok')).toBe('ok');
  });
});

const REDIS_URL = process.env.TEST_REDIS_URL;

describe.skipIf(!REDIS_URL)('RedisCache', () => {
  it('stores JSON values with a TTL', async () => {
    const cache = await RedisCache.connect(REDIS_URL!, silentLogger());
    try {
      expect(await cache.ping()).toBe(true);
      await cache.set('test:k', { a: [1, 'x'] }, 200);
      expect(await cache.get('test:k')).toEqual({ a: [1, 'x'] });
      await new Promise((r) => setTimeout(r, 300));
      expect(await cache.get('test:k')).toBeUndefined();
    } finally {
      await cache.close();
    }
  });

  it('degrades to misses when Redis is unreachable', async () => {
    const cache = await RedisCache.connect('redis://127.0.0.1:1', silentLogger(), 200);
    try {
      expect(await cache.get('k')).toBeUndefined();
      await expect(cache.set('k', 1, 1_000)).resolves.toBeUndefined();
    } finally {
      await cache.close();
    }
  });
});
