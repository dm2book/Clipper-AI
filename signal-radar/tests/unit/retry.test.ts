import { describe, expect, it, vi } from 'vitest';
import { NotFoundError, RateLimitedError, TransientError } from '../../src/core/errors.js';
import { backoffDelay, sleep, withRetry } from '../../src/infra/retry.js';

const noSleep = vi.fn(async () => undefined);
const base = { baseDelayMs: 100, maxDelayMs: 1_000, sleepFn: noSleep, random: () => 1 };

describe('withRetry', () => {
  it('retries transient errors and returns the eventual result', async () => {
    let calls = 0;
    const result = await withRetry(async () => {
      calls++;
      if (calls < 3) throw new TransientError('flaky');
      return 'ok';
    }, { ...base, retries: 3 });
    expect(result).toBe('ok');
    expect(calls).toBe(3);
  });

  it('does not retry permanent errors', async () => {
    let calls = 0;
    await expect(
      withRetry(async () => {
        calls++;
        throw new NotFoundError('gone');
      }, { ...base, retries: 5 }),
    ).rejects.toBeInstanceOf(NotFoundError);
    expect(calls).toBe(1);
  });

  it('gives up after the configured retries', async () => {
    let calls = 0;
    await expect(
      withRetry(async () => {
        calls++;
        throw new TransientError('down');
      }, { ...base, retries: 2 }),
    ).rejects.toThrow('down');
    expect(calls).toBe(3);
  });

  it('waits at least as long as Retry-After, and refuses absurd waits', async () => {
    const waits: number[] = [];
    let calls = 0;
    await withRetry(
      async () => {
        if (calls++ === 0) throw new RateLimitedError('slow down', 2_500);
        return 1;
      },
      { ...base, retries: 1, sleepFn: async (ms) => void waits.push(ms) },
    );
    expect(waits[0]).toBe(2_500);
    await expect(
      withRetry(async () => {
        throw new RateLimitedError('come back tomorrow', 86_400_000);
      }, { ...base, retries: 3 }),
    ).rejects.toBeInstanceOf(RateLimitedError);
  });

  it('uses capped exponential backoff with full jitter', () => {
    expect(backoffDelay(0, 100, 1_000, () => 1)).toBe(100);
    expect(backoffDelay(3, 100, 1_000, () => 1)).toBe(800);
    expect(backoffDelay(10, 100, 1_000, () => 1)).toBe(1_000);
    expect(backoffDelay(10, 100, 1_000, () => 0)).toBe(0);
  });

  it('stops when aborted', async () => {
    const ctrl = new AbortController();
    const p = sleep(10_000, ctrl.signal);
    ctrl.abort();
    await expect(p).rejects.toBeDefined();
  });
});
