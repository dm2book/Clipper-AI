import { describe, expect, it } from 'vitest';
import { TokenBucket } from '../../src/infra/rateLimiter.js';

/** A fake clock where sleeping advances time instantly. */
function fakeClock() {
  let t = 0;
  return {
    now: () => t,
    sleepFn: async (ms: number) => {
      t += ms;
    },
    get time() {
      return t;
    },
  };
}

describe('TokenBucket', () => {
  it('allows the burst immediately, then paces at the sustained rate', async () => {
    const clock = fakeClock();
    const bucket = new TokenBucket({ perSecond: 2, burst: 2, now: clock.now, sleepFn: clock.sleepFn });
    await bucket.acquire();
    await bucket.acquire();
    expect(clock.time).toBe(0);
    await bucket.acquire();
    expect(clock.time).toBe(500);
    await bucket.acquire();
    expect(clock.time).toBe(1_000);
  });

  it('serves waiters in arrival order', async () => {
    const clock = fakeClock();
    const bucket = new TokenBucket({ perSecond: 1, burst: 1, now: clock.now, sleepFn: clock.sleepFn });
    const order: number[] = [];
    await Promise.all([1, 2, 3].map((n) => bucket.acquire().then(() => order.push(n))));
    expect(order).toEqual([1, 2, 3]);
    expect(clock.time).toBe(2_000);
  });

  it('pauses entirely after a 429', async () => {
    const clock = fakeClock();
    const bucket = new TokenBucket({ perSecond: 10, burst: 10, now: clock.now, sleepFn: clock.sleepFn });
    bucket.pauseFor(3_000);
    await bucket.acquire();
    expect(clock.time).toBe(3_000);
  });

  it('an aborted waiter does not block the queue', async () => {
    const bucket = new TokenBucket({ perSecond: 1000, burst: 1 });
    await bucket.acquire();
    const ctrl = new AbortController();
    ctrl.abort();
    await expect(bucket.acquire(ctrl.signal)).rejects.toBeDefined();
    await expect(bucket.acquire()).resolves.toBeUndefined();
  });

  it('builds per-minute buckets', () => {
    const b = TokenBucket.perMinute(150);
    expect(b.perSecond).toBe(2.5);
    expect(b.capacity).toBe(2.5);
  });
});
