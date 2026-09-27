import { describe, expect, it } from 'vitest';
import { KeyedMutex, mapLimit } from '../../src/infra/concurrency.js';
import { silentLogger } from '../../src/infra/logger.js';
import { Scheduler } from '../../src/workers/scheduler.js';

const tick = (ms = 5) => new Promise((r) => setTimeout(r, ms));

describe('Scheduler', () => {
  it('never overlaps a job with itself and drains while there is more work', async () => {
    const s = new Scheduler(silentLogger());
    let running = 0;
    let maxRunning = 0;
    let runs = 0;
    s.add({
      name: 'drain',
      intervalMs: 10_000,
      run: async () => {
        running++;
        maxRunning = Math.max(maxRunning, running);
        await tick(2);
        running--;
        return ++runs < 5; // "more work" four times, then idle
      },
    });
    s.start();
    await tick(100);
    expect(runs).toBe(5);
    expect(maxRunning).toBe(1);
    expect(await s.stop(1_000)).toBe(true);
  });

  it('isolates a failing job and backs it off', async () => {
    const s = new Scheduler(silentLogger());
    let healthy = 0;
    let broken = 0;
    s.add({ name: 'healthy', intervalMs: 5, run: async () => void healthy++ });
    s.add({
      name: 'broken',
      intervalMs: 5,
      run: async () => {
        broken++;
        throw new Error('boom');
      },
    });
    s.start();
    await tick(120);
    await s.stop(1_000);
    expect(healthy).toBeGreaterThan(5);
    expect(broken).toBeLessThan(healthy); // exponential backoff: 10, 20, 40, 80ms…
    expect(s.jobStatus().broken!.consecutiveFailures).toBeGreaterThan(0);
    expect(s.jobStatus().healthy!.lastSuccessAt).not.toBeNull();
  });

  it('stop() aborts waits and lets in-flight runs finish', async () => {
    const s = new Scheduler(silentLogger());
    let finished = false;
    s.add({
      name: 'slow',
      intervalMs: 60_000,
      run: async () => {
        await tick(30);
        finished = true;
      },
    });
    s.start();
    await tick(5);
    const t0 = Date.now();
    expect(await s.stop(1_000)).toBe(true);
    expect(finished).toBe(true);
    expect(Date.now() - t0).toBeLessThan(500); // did not wait for the 60s interval
  });

  it('reports a timeout when a job ignores the stop signal', async () => {
    const s = new Scheduler(silentLogger());
    s.add({ name: 'stubborn', intervalMs: 1_000, run: () => new Promise((r) => setTimeout(r, 300)) });
    s.start();
    await tick(5);
    expect(await s.stop(20)).toBe(false);
  });
});

describe('concurrency helpers', () => {
  it('mapLimit keeps order and bounds parallelism', async () => {
    let inFlight = 0;
    let peak = 0;
    const out = await mapLimit([1, 2, 3, 4, 5, 6], 2, async (n) => {
      inFlight++;
      peak = Math.max(peak, inFlight);
      await tick(3);
      inFlight--;
      return n * 10;
    });
    expect(out).toEqual([10, 20, 30, 40, 50, 60]);
    expect(peak).toBe(2);
  });

  it('KeyedMutex serialises per key but not across keys', async () => {
    const m = new KeyedMutex();
    const log: string[] = [];
    const job = (key: string, id: string) =>
      m.run(key, async () => {
        log.push(`start ${id}`);
        await tick(5);
        log.push(`end ${id}`);
      });
    await Promise.all([job('a', 'a1'), job('a', 'a2'), job('b', 'b1')]);
    expect(log.indexOf('end a1')).toBeLessThan(log.indexOf('start a2'));
    expect(log.indexOf('start b1')).toBeLessThan(log.indexOf('end a1'));
  });
});
