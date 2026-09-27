import { describe, expect, it } from 'vitest';
import { MOMENTUM_DISCLAIMER, detectMomentum, explain, type MomentumSignal } from '../../../src/momentum/engine.js';
import type { Trade } from '../../../src/momentum/flow.js';
import type { TokenSeries } from '../../../src/momentum/metrics.js';
import { MIN, NOW, cumulativeSnapshots, holders, series, snap, thresholds, trade, tradesIn } from '../../support/momentum.js';

const W5 = 5 * MIN;

interface Windows {
  /** [buys, distinct buyers, buy volume, sells, sell volume] per 5m window: current, previous, before previous */
  flow?: [number, number, number, number, number][];
  liquidity?: [number, number];
  holderCounts?: [number, number];
  marketCap?: [number, number];
  extraTrades?: Trade[];
}

/**
 * The scenario from the specification, with SYNTHETIC trades:
 * volume +428%, transactions +267%, unique buyers +111%,
 * liquidity +24%, holders +37%.
 */
function scenario(o: Windows = {}): TokenSeries {
  const flow = o.flow ?? [
    [120, 74, 42_000, 45, 10_800],
    [35, 35, 8_000, 10, 2_000],
    [25, 25, 6_400, 5, 1_600],
  ];
  const trades: Trade[] = [];
  flow.forEach(([buys, buyers, buyVol, sells, sellVol], i) => {
    trades.push(...tradesIn(i * W5, W5, buys, 'buy', buyVol, (k) => `buyer-${i}-${k % buyers}`));
    trades.push(...tradesIn(i * W5, W5, sells, 'sell', sellVol, (k) => `seller-${i}-${k}`));
  });
  trades.push(...(o.extraTrades ?? []));
  const [liqNow, liqThen] = o.liquidity ?? [62_000, 50_000];
  const [capNow, capThen] = o.marketCap ?? [230_000, 200_000];
  const [hNow, hThen] = o.holderCounts ?? [137, 100];
  return series({
    snapshots: [snap(0, { liquidityUsd: liqNow, marketCapUsd: capNow }), snap(W5, { liquidityUsd: liqThen, marketCapUsd: capThen })],
    holders: [holders(0, hNow), holders(W5, hThen)],
    trades: { items: trades, coverageFrom: new Date(NOW - 3 * 3_600_000) },
  });
}

const run = (s: TokenSeries, t = thresholds()) => detectMomentum(s, NOW, t, ['default']);
const filter = (s: MomentumSignal, id: string) => s.filters.find((f) => f.id === id)!;

describe('detectMomentum — the specification example', () => {
  const signal = run(scenario());

  it('reports MOMENTUM with the documented output shape', () => {
    expect(signal.signalType).toBe('MOMENTUM');
    expect(Object.keys(signal)).toEqual(
      expect.arrayContaining(['token', 'timestamp', 'signalType', 'metrics', 'triggeredRules', 'score', 'confidence', 'reasons']),
    );
    expect(signal.token).toEqual({ chain: 'solana', address: 'Token11111111111111111111111111111111111111', tokenType: 'raydium' });
    expect(signal.timestamp).toBe('2026-01-01T12:00:00.000Z');
    expect(signal.metrics['5m'].flowSource).toBe('trades');
  });

  it('explains itself in plain reasons', () => {
    const text = signal.reasons.join('\n');
    expect(text).toContain('volume +428% (5m: $52.8k vs $10.0k)');
    expect(text).toContain('transactions +267% (5m: 165 vs 45)');
    expect(text).toContain('unique buyers +111% (5m: 74 vs 35)');
    expect(text).toContain('liquidity +24% (5m: $50.0k → $62.0k)');
    expect(text).toContain('holder growth +37% (5m: 100 → 137)');
    // market cap +15% stays below the 20% threshold and is therefore not a reason
    expect(text).not.toContain('market cap');
  });

  it('has a score that is exactly the sum of its parts', () => {
    const sum = signal.rules.reduce((a, r) => a + r.points, 0) - signal.penalties.reduce((a, p) => a + p.points, 0);
    expect(signal.score).toBeCloseTo(Math.min(100, Math.max(0, sum)), 5);
    expect(signal.score).toBeGreaterThanOrEqual(60);
    for (const r of signal.triggeredRules) {
      expect(r.points).toBeCloseTo(r.weight * (0.5 + 0.5 * r.strength), 0);
      expect(r.points).toBeGreaterThanOrEqual(r.weight / 2);
    }
    // sorted by contribution
    const points = signal.triggeredRules.map((r) => r.points);
    expect(points).toEqual([...points].sort((a, b) => b - a));
  });

  it('has full confidence when every rule and filter had data', () => {
    expect(signal.confidence).toBe(1);
    expect(signal.filters.every((f) => f.passed === true)).toBe(true);
    expect(signal.warnings).toEqual([]);
  });

  it('measures accelerations and buy pressure', () => {
    const ids = signal.triggeredRules.map((r) => r.id);
    expect(ids).toEqual(expect.arrayContaining(['volume_acceleration', 'tx_acceleration', 'buy_pressure']));
    expect(signal.metrics['5m'].buyShare).toBeCloseTo(120 / 165);
    expect(signal.rules.find((r) => r.id === 'buy_pressure')!.value).toBe(0.727);
  });

  it('never calls itself a prediction', () => {
    expect(signal.disclaimer).toBe(MOMENTUM_DISCLAIMER);
    expect(signal.disclaimer).toMatch(/Geen voorspelling/);
    const text = explain(signal);
    expect(text).toMatch(/^Momentum Score: \d+ \(MOMENTUM, confidence 1\)/);
    expect(text).toContain('* volume +428%');
    expect(text).toContain(MOMENTUM_DISCLAIMER);
    expect(`${text} ${JSON.stringify(signal)}`).not.toMatch(/guarantee|koop nu|moon|pump/i);
  });

  it('reports every window', () => {
    expect(Object.keys(signal.metrics)).toEqual(['1m', '5m', '15m', '30m', '1h']);
    expect(signal.metrics['1m'].flowSource).toBe('trades');
  });
});

describe('false-positive filters', () => {
  it('blocks below the liquidity floor', () => {
    const s = run(scenario({ liquidity: [15_000, 12_000] }));
    expect(s.signalType).toBe('FILTERED');
    expect(filter(s, 'min_liquidity')).toMatchObject({ passed: false, blocking: true });
    expect(s.warnings.join()).toMatch(/min_liquidity/);
  });

  it('blocks below the holder minimum', () => {
    const s = run(scenario({ holderCounts: [40, 25] }));
    expect(s.signalType).toBe('FILTERED');
    expect(filter(s, 'min_holders').blocking).toBe(true);
  });

  it('blocks when absolute volume is too small, whatever the percentage', () => {
    const s = run(
      scenario({
        flow: [
          [120, 74, 420, 45, 108],
          [35, 35, 80, 10, 20],
          [25, 25, 64, 5, 16],
        ],
      }),
    );
    expect(s.metrics['5m'].volumeUsd.current).toBeCloseTo(528);
    expect(filter(s, 'min_volume').blocking).toBe(true);
    expect(s.signalType).not.toBe('MOMENTUM');
  });

  it('blocks when there are too few transactions', () => {
    const s = run(
      scenario({
        flow: [
          [15, 15, 42_000, 5, 10_800],
          [4, 4, 8_000, 1, 2_000],
          [2, 2, 6_400, 1, 1_600],
        ],
      }),
    );
    expect(filter(s, 'min_transactions')).toMatchObject({ passed: false, blocking: true });
    expect(s.signalType).not.toBe('MOMENTUM');
  });

  it('blocks when too few distinct wallets are buying', () => {
    const s = run(scenario(), thresholds({ minUniqueBuyers: 100 }));
    expect(s.signalType).toBe('FILTERED');
    expect(filter(s, 'min_unique_buyers')).toMatchObject({ passed: false, blocking: true });
  });

  it('blocks wash-trading patterns', () => {
    // three wallets trade the same amounts back and forth all window long
    const wash: Trade[] = [];
    for (let i = 0; i < 60; i++) {
      const w = `ww${i % 3}`;
      wash.push(trade(i * 4_000 + 1_000, w, 'buy', 1_000), trade(i * 4_000 + 2_000, w, 'sell', 1_000));
    }
    const s = run(scenario({ extraTrades: wash }));
    const f = filter(s, 'wash_trading');
    expect(f).toMatchObject({ passed: false, blocking: true });
    expect(f.detail).toMatch(/heen-en-terug/);
    expect(s.signalType).toBe('FILTERED');
  });

  it('removes one extreme trade from the volume instead of letting it create a spike', () => {
    const calm: [number, number, number, number, number][] = [
      [35, 35, 8_000, 10, 2_000],
      [35, 35, 8_000, 10, 2_000],
      [35, 35, 8_000, 10, 2_000],
    ];
    const s = run(scenario({ flow: calm, extraTrades: [trade(MIN, 'whale', 'buy', 60_000)] }));
    const v = s.metrics['5m'].volumeUsd;
    expect(v.rawCurrent).toBeCloseTo(70_000);
    expect(v.current).toBeCloseTo(10_000);
    expect(v.excludedTrades).toBe(1);
    expect(s.rules.find((r) => r.id === 'volume_spike')!.triggered).toBe(false);
    expect(s.warnings.join()).toMatch(/1 extreme trade/);
  });
});

describe('without trade data', () => {
  // Same activity, but only provider snapshots with cumulative 24h totals.
  const snapshots = cumulativeSnapshots(
    [
      [0, 67_200, 180, 60],
      [W5, 14_400, 60, 15],
      [2 * W5, 4_400, 25, 5],
      [3 * W5, 0, 0, 0],
    ],
    (msAgo) => (msAgo === 0 ? { liquidityUsd: 62_000, marketCapUsd: 230_000 } : msAgo === W5 ? { liquidityUsd: 50_000, marketCapUsd: 200_000 } : {}),
  );
  const s = series({ snapshots, holders: [holders(0, 137), holders(W5, 100)], tokenAgeMs: 40 * MIN });

  it('still measures volume/transactions but not unique wallets, and says so', () => {
    const signal = run(s, thresholds({ minScore: 50 }));
    expect(signal.metrics['5m'].flowSource).toBe('provider_h24_delta');
    expect(signal.rules.find((r) => r.id === 'buyer_growth')).toMatchObject({ available: false, points: 0 });
    expect(filter(signal, 'min_unique_buyers')).toMatchObject({ passed: null, blocking: false });
    expect(filter(signal, 'wash_trading')).toMatchObject({ passed: null, blocking: false });
    expect(signal.warnings.join()).toMatch(/geen trade-data/);
    // 85 of 100 weight measurable, 4 of 6 filters: 0.85 × (0.7 + 0.3 × 4/6) = 0.765 → 0.76
    expect(signal.confidence).toBe(0.76);
    expect(signal.signalType).toBe('MOMENTUM');
  });

  it('can be configured to require trade data', () => {
    const signal = run(s, thresholds({ minScore: 50, requireTradeData: true }));
    expect(signal.signalType).toBe('FILTERED');
    expect(filter(signal, 'min_unique_buyers').blocking).toBe(true);
  });
});

describe('scoring edge cases', () => {
  it('returns NO_SIGNAL when too few rules trigger', () => {
    // balanced, steady trading; only liquidity grows
    const quiet: [number, number, number, number, number][] = [
      [20, 20, 5_000, 20, 5_000],
      [20, 20, 5_000, 20, 5_000],
      [20, 20, 5_000, 20, 5_000],
    ];
    const s = run(scenario({ flow: quiet, liquidity: [70_000, 50_000], holderCounts: [100, 100] }));
    expect(s.triggeredRules.map((r) => r.id)).toEqual(['liquidity_growth']);
    expect(s.signalType).toBe('NO_SIGNAL');
  });

  it('applies a visible penalty when liquidity drops sharply', () => {
    const base = run(scenario());
    const draining = run(scenario({ liquidity: [35_000, 50_000] }));
    const penalty = draining.penalties.find((p) => p.id === 'liquidity_decline');
    expect(penalty).toMatchObject({ points: 20 });
    expect(draining.warnings.join()).toMatch(/aftrek −20/);
    const lostLiquidityRule = base.rules.find((r) => r.id === 'liquidity_growth')!.points;
    expect(draining.score).toBeCloseTo(base.score - lostLiquidityRule - 20, 5);
  });

  it('handles a token without any data without throwing', () => {
    const s = run(series());
    expect(s).toMatchObject({ signalType: 'NO_SIGNAL', score: 0, triggeredRules: [] });
    expect(s.confidence).toBe(0); // no rule had data
    expect(s.warnings.join()).toMatch(/geen flow-data/);
  });

  it('scores on the configured primary window', () => {
    const s = run(scenario(), thresholds({ primaryWindow: '15m' }));
    expect(s.primaryWindow).toBe('15m');
    expect(s.triggeredRules.every((r) => r.window === '15m')).toBe(true);
  });
});
