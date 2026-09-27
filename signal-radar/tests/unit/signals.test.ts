import { describe, expect, it } from 'vitest';
import {
  liquidityChange5mPct,
  measureAll,
  measureBuyCountRatio,
  measureHolderGrowth,
  measureLiquidityGrowth,
  measureMcapChange,
  measureTxGrowth,
  measureVolumeSpike,
  type SignalContext,
} from '../../src/core/signals.js';
import { makeHolders, makeSnapshot } from '../support/factories.js';

const NOW = new Date('2026-01-01T12:30:00Z');
const minutesAgo = (m: number) => new Date(NOW.getTime() - m * 60_000);

function ctx(overrides: Partial<SignalContext> = {}): SignalContext {
  return {
    now: NOW,
    current: makeSnapshot({ observedAt: NOW }),
    history: [],
    holders: [],
    tokenAgeMs: 8 * 60_000,
    ...overrides,
  };
}

describe('volume spike', () => {
  it('compares against the previous, non-overlapping 5-minute window', () => {
    const m = measureVolumeSpike(
      ctx({
        current: makeSnapshot({ observedAt: NOW, volumeUsd: { m5: 20_000 } }),
        history: [
          makeSnapshot({ observedAt: minutesAgo(1), volumeUsd: { m5: 19_000 } }), // overlaps: ignored
          makeSnapshot({ observedAt: minutesAgo(5.2), volumeUsd: { m5: 2_500 } }),
        ],
      }),
    );
    expect(m).toMatchObject({ available: true, qualifies: true, value: 20_000, baseline: 2_500, metric: 8 });
  });

  it('floors a tiny baseline so $0 → $6k is not "infinite"', () => {
    const m = measureVolumeSpike(
      ctx({
        current: makeSnapshot({ observedAt: NOW, volumeUsd: { m5: 6_000 } }),
        history: [makeSnapshot({ observedAt: minutesAgo(5), volumeUsd: { m5: 0 } })],
      }),
    );
    expect(m.metric).toBe(12); // 6000 / 500
  });

  it('does not qualify below the absolute minimum', () => {
    const m = measureVolumeSpike(
      ctx({
        current: makeSnapshot({ observedAt: NOW, volumeUsd: { m5: 900 } }),
        history: [makeSnapshot({ observedAt: minutesAgo(5), volumeUsd: { m5: 10 } })],
      }),
    );
    expect(m).toMatchObject({ available: true, qualifies: false });
  });

  it('falls back to the 1h average only when the token is old enough', () => {
    const current = makeSnapshot({ observedAt: NOW, volumeUsd: { m5: 10_000, h1: 32_000 } });
    // 30 min old: h1 minus current spread over (30 - 5) / 5 = 5 earlier windows = 4400
    const m = measureVolumeSpike(ctx({ current, tokenAgeMs: 30 * 60_000 }));
    expect(m.baseline).toBeCloseTo(4_400);
    expect(m.detail).toContain('gemiddelde');
    expect(measureVolumeSpike(ctx({ current, tokenAgeMs: 8 * 60_000 })).available).toBe(false);
  });
});

describe('other market signals', () => {
  const history = [
    makeSnapshot({
      observedAt: minutesAgo(15),
      liquidityUsd: 20_000,
      marketCapUsd: 100_000,
      txns: { m5: { buys: 5, sells: 5 } },
    }),
    makeSnapshot({
      observedAt: minutesAgo(5),
      liquidityUsd: 25_000,
      marketCapUsd: 150_000,
      txns: { m5: { buys: 10, sells: 10 } },
    }),
  ];
  const current = makeSnapshot({
    observedAt: NOW,
    liquidityUsd: 40_000,
    marketCapUsd: 300_000,
    txns: { m5: { buys: 60, sells: 20 } },
  });

  it('measures transaction growth', () => {
    expect(measureTxGrowth(ctx({ current, history }))).toMatchObject({ metric: 4, value: 80, baseline: 20, qualifies: true });
  });

  it('measures liquidity growth over 15 minutes', () => {
    const m = measureLiquidityGrowth(ctx({ current, history }));
    expect(m).toMatchObject({ window: '15m', metric: 100, qualifies: true });
  });

  it('measures market-cap change and never mixes market cap with FDV', () => {
    expect(measureMcapChange(ctx({ current, history })).metric).toBe(200);
    const fdvOnly = makeSnapshot({ observedAt: NOW, marketCapUsd: null, fdvUsd: 400_000 });
    expect(measureMcapChange(ctx({ current: fdvOnly, history })).available).toBe(false);
  });

  it('measures the share of buys', () => {
    expect(measureBuyCountRatio(ctx({ current }))).toMatchObject({ metric: 0.75, qualifies: true });
    expect(measureBuyCountRatio(ctx({ current: makeSnapshot({ txns: { m5: { buys: 0, sells: 0 } } }) })).available).toBe(false);
  });

  it('reports the 5-minute liquidity change used for the drop penalty', () => {
    expect(liquidityChange5mPct(ctx({ current, history }))).toBe(60);
  });
});

describe('holder growth', () => {
  it('normalises to holders per 5 minutes', () => {
    const m = measureHolderGrowth(
      ctx({
        holders: [
          makeHolders({ observedAt: minutesAgo(6), holderCount: 60 }),
          makeHolders({ observedAt: NOW, holderCount: 132 }),
        ],
      }),
    );
    expect(m.metric).toBeCloseTo(60); // +72 in 6 minutes
    expect(m.qualifies).toBe(true);
  });
});

describe('measureAll', () => {
  it('marks everything unavailable without history instead of inventing values', () => {
    const all = measureAll(ctx({ current: makeSnapshot({ observedAt: NOW, txns: { m5: { buys: 1, sells: 1 } } }) }));
    const unavailable = all.filter((m) => !m.available).map((m) => m.type);
    expect(unavailable).toEqual(['volume_spike', 'tx_growth', 'liquidity_growth', 'mcap_change', 'holder_growth']);
  });
});
