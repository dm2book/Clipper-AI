import { describe, expect, it } from 'vitest';
import { notIndexedBackoffSec, snapshotIntervalSec, tierForAge, tokenAge } from '../../src/core/token.js';

const NOW = new Date('2026-01-01T12:00:00Z');
const cfg = {
  hotMinutes: 15,
  warmMinutes: 60,
  snapshotIntervalSec: { HOT: 15, WARM: 60, COOL: 300 },
  lowLiquidityUsd: 1_000,
};

describe('tokenAge', () => {
  it('prefers on-chain creation time', () => {
    const age = tokenAge({ createdAtChain: new Date('2026-01-01T11:55:00Z') }, new Date('2026-01-01T11:00:00Z'), NOW);
    expect(age).toEqual({ ms: 5 * 60_000, source: 'onchain' });
  });

  it('falls back to the provider pair time, labelled as such', () => {
    const age = tokenAge({ createdAtChain: null }, new Date('2026-01-01T11:58:00Z'), NOW);
    expect(age).toEqual({ ms: 2 * 60_000, source: 'provider' });
  });

  it('is unknown without either', () => {
    expect(tokenAge({ createdAtChain: null }, null, NOW)).toBeNull();
  });
});

describe('tiers and intervals', () => {
  it('tiers by age', () => {
    expect(tierForAge(0, cfg)).toBe('HOT');
    expect(tierForAge(14.9 * 60_000, cfg)).toBe('HOT');
    expect(tierForAge(15 * 60_000, cfg)).toBe('WARM');
    expect(tierForAge(60 * 60_000, cfg)).toBe('COOL');
  });

  it('polls illiquid tokens at the COOL rate regardless of age', () => {
    expect(snapshotIntervalSec('HOT', 50_000, cfg)).toBe(15);
    expect(snapshotIntervalSec('HOT', 500, cfg)).toBe(300);
    expect(snapshotIntervalSec('HOT', null, cfg)).toBe(15); // unknown is not "low"
    expect(snapshotIntervalSec('ARCHIVED', 50_000, cfg)).toBe(300);
  });

  it('backs off while a token is not indexed yet', () => {
    expect([1, 2, 3, 4, 10].map(notIndexedBackoffSec)).toEqual([20, 40, 80, 120, 120]);
  });
});
