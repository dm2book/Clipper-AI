import { describe, expect, it } from 'vitest';
import { capShares, classifyActivity, detectCluster } from '../../../src/wallets/activity.js';
import { DEFAULT_ACTIVITY, type ActivityThresholds } from '../../../src/wallets/criteria.js';
import { T0, ev } from '../../support/wallets.js';

const t: ActivityThresholds = { ...DEFAULT_ACTIVITY };
const onCurve = () => true;
const ctx = { liquidityUsd: null, tracked: null, isOnCurve: onCurve };

describe('whale threshold', () => {
  it('flags a swap at or above the absolute USD threshold', () => {
    expect(classifyActivity(ev('buy', 1, 25_000, 0), ctx, t).whale).toBe(true);
    expect(classifyActivity(ev('sell', 1, 48_000, 0), ctx, t).whale).toBe(true);
    expect(classifyActivity(ev('buy', 1, 24_999, 0), ctx, t).whale).toBe(false);
  });

  it('flags a swap that is a large share of pool liquidity, above the floor', () => {
    const c = { ...ctx, liquidityUsd: 100_000 };
    const d = classifyActivity(ev('buy', 1, 6_000, 0), c, t);
    expect(d.whale).toBe(true);
    expect(d.whaleReason).toMatch(/6\.0% of pool liquidity/);
    // 5% of a tiny pool is not a whale: below the USD floor
    expect(classifyActivity(ev('buy', 1, 500, 0), { ...ctx, liquidityUsd: 5_000 }, t).whale).toBe(false);
    // without liquidity data only the absolute rule applies
    expect(classifyActivity(ev('buy', 1, 6_000, 0), ctx, t).whale).toBe(false);
  });

  it('a whale is not automatically a tracked wallet', () => {
    const d = classifyActivity(ev('buy', 1, 100_000, 0), ctx, t);
    expect(d.whale).toBe(true);
    expect(d.tracked).toBe(false);
  });

  it('a tracked wallet is reported above its own (lower) minimum', () => {
    const c = { ...ctx, tracked: { source: 'criteria' as const, reason: 'voldoet aan volgcriteria v1' } };
    const d = classifyActivity(ev('buy', 1, 12_400, 0), c, t);
    expect(d.tracked).toBe(true);
    expect(d.whale).toBe(false);
    expect(classifyActivity(ev('buy', 1, 100, 0), c, t).tracked).toBe(false);
  });

  it('skips failed transactions, transfers, unpriced swaps, ignored and program-owned accounts', () => {
    expect(classifyActivity(ev('buy', 1, 1e6, 0, { status: 'failed' }), ctx, t).skipped).toMatch(/failed/);
    expect(classifyActivity(ev('transfer_in', 1, 1e6, 0), ctx, t).skipped).toMatch(/not a swap/);
    expect(classifyActivity(ev('buy', 1, null, 0), ctx, t).skipped).toMatch(/USD/);
    const e = ev('buy', 1, 1e6, 0);
    expect(classifyActivity(e, ctx, { ...t, ignoredWallets: [e.wallet] }).skipped).toMatch(/ignore/);
    expect(classifyActivity(e, { ...ctx, isOnCurve: () => false }, t).skipped).toMatch(/program-owned/);
    expect(classifyActivity(e, { ...ctx, isOnCurve: () => false }, { ...t, excludeProgramOwned: false }).whale).toBe(true);
  });
});

describe('capShares', () => {
  const share = (xs: number[]) => Math.max(...xs) / xs.reduce((a, b) => a + b, 0);

  it('leaves balanced values alone', () => {
    expect(capShares([10, 10, 10], 0.4)).toEqual([10, 10, 10]);
  });

  it('caps one dominant value to the maximum share', () => {
    const out = capShares([1_000_000, 1_000, 1_000, 1_000], 0.4);
    expect(share(out)).toBeCloseTo(0.4);
    expect(out.slice(1)).toEqual([1_000, 1_000, 1_000]);
  });

  it('caps several dominant values', () => {
    const out = capShares([500, 400, 10, 10, 10], 0.3);
    expect(share(out)).toBeLessThanOrEqual(0.3 + 1e-9);
    expect(out[0]).toBeCloseTo(out[1]!);
  });

  it('falls back to equal weights when the cap is impossible', () => {
    expect(capShares([100, 1], 0.4)).toEqual([1, 1]);
  });
});

describe('cluster detection', () => {
  const at = (m: number) => new Date(T0.getTime() + m * 60_000);
  const buys = (list: [string, number, number][]) => list.map(([wallet, valueUsd, m]) => ({ wallet, valueUsd, at: at(m) }));

  it('detects N distinct tracked wallets within the window', () => {
    const c = detectCluster(buys([['a', 1_000, 0], ['b', 2_000, 1], ['c', 500, 2], ['d', 800, 2.5]]), at(3), t);
    expect(c).not.toBeNull();
    expect(c!.wallets).toHaveLength(4);
    expect(c!.totalUsd).toBe(4_300);
  });

  it('counts a wallet once, however many buys it makes', () => {
    expect(detectCluster(buys([['a', 1_000, 0], ['a', 1_000, 1], ['a', 1_000, 2], ['b', 100, 2]]), at(3), t)).toBeNull();
  });

  it('ignores buys outside the window', () => {
    expect(detectCluster(buys([['a', 1_000, 0], ['b', 1_000, 1], ['c', 1_000, 10]]), at(10), t)).toBeNull();
  });

  it('prevents one wallet from dominating the cluster', () => {
    const c = detectCluster(buys([['whale', 1_000_000, 0], ['b', 1_000, 1], ['c', 1_000, 2]]), at(3), t)!;
    expect(c.wallets[0]!.wallet).toBe('whale');
    expect(c.wallets[0]!.sharePct).toBeLessThanOrEqual(40 + 1e-9);
    expect(c.cappedWallets).toBe(1);
    expect(c.totalUsd).toBe(1_002_000);
    expect(c.cappedTotalUsd).toBeLessThan(5_000);
  });
});
