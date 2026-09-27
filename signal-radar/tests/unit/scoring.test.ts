import { describe, expect, it } from 'vitest';
import { SCORING_V1, computeScore, strength } from '../../src/core/scoring.js';
import type { Measurement, SignalType } from '../../src/core/signals.js';

function m(type: SignalType, metric: number | null, extra: Partial<Measurement> = {}): Measurement {
  return {
    type,
    available: metric !== null,
    qualifies: true,
    metric,
    value: null,
    baseline: null,
    window: '5m',
    detail: `${type} detail`,
    evidence: {},
    ...extra,
  };
}

const noPenalty = { safetyVerdict: 'PASS' as const, top10Pct: 20, liquidityChange5mPct: 5 };

describe('scoring v1', () => {
  it('has weights that sum to 100', () => {
    expect(SCORING_V1.components.reduce((a, c) => a + c.weight, 0)).toBe(100);
  });

  it('normalises linearly between floor and full', () => {
    expect(strength(2, 2, 8)).toBe(0);
    expect(strength(5, 2, 8)).toBe(0.5);
    expect(strength(20, 2, 8)).toBe(1);
  });

  it('adds up component points transparently', () => {
    const r = computeScore(
      [
        m('volume_spike', 8), // 25
        m('tx_growth', 3.25), // 15 * 0.5 = 7.5
        m('liquidity_growth', 100), // 20
        m('mcap_change', 20), // 0
        m('buy_count_ratio', 0.75), // 10
        m('holder_growth', 55), // 15 * 0.5 = 7.5
      ],
      noPenalty,
    );
    expect(r.score).toBe(70);
    expect(r.confidence).toBe(1);
    expect(r.activeComponents).toBe(5);
    expect(r.components.find((c) => c.type === 'tx_growth')).toMatchObject({ points: 7.5, strength: 0.5 });
  });

  it('gives no points to measurements under their absolute minimum', () => {
    const r = computeScore([m('volume_spike', 50, { qualifies: false })], noPenalty);
    expect(r.components[0]).toMatchObject({ points: 0, available: true });
    expect(r.components[0]!.detail).toContain('onder absolute minimum');
  });

  it('lowers confidence for missing data rather than guessing', () => {
    const r = computeScore([m('volume_spike', 8), m('tx_growth', null)], noPenalty);
    expect(r.confidence).toBe(0.25);
    expect(r.components.find((c) => c.type === 'tx_growth')!.available).toBe(false);
  });

  it('applies explicit, listed penalties and clamps at zero', () => {
    const r = computeScore([m('volume_spike', 8)], { safetyVerdict: 'WARN', top10Pct: 55, liquidityChange5mPct: -35 });
    expect(r.penalties.map((p) => p.points)).toEqual([10, 15, 20]);
    expect(r.score).toBe(0);
  });
});
