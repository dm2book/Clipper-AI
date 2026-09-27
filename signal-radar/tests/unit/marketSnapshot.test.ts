import { describe, expect, it } from 'vitest';
import { capitalisation, snapshotNear, txCount } from '../../src/core/marketSnapshot.js';
import { makeSnapshot } from '../support/factories.js';

const at = (iso: string) => ({ observedAt: new Date(iso) });

describe('market snapshot helpers', () => {
  it('counts transactions only when both sides are known', () => {
    expect(txCount({ buys: 3, sells: 4 })).toBe(7);
    expect(txCount({ buys: 3, sells: null })).toBeNull();
  });

  it('finds the snapshot closest to a target inside the window only', () => {
    const history = [at('2026-01-01T12:00:00Z'), at('2026-01-01T11:55:20Z'), at('2026-01-01T11:54:30Z'), at('2026-01-01T11:50:00Z')];
    const target = new Date('2026-01-01T11:55:00Z');
    expect(snapshotNear(history, target, 120_000, 30_000)).toBe(history[1]);
    // nothing inside a narrow window
    expect(snapshotNear(history, target, 10_000, 10_000)).toBeNull();
  });

  it('uses market cap, falls back to FDV and says which', () => {
    expect(capitalisation(makeSnapshot({ marketCapUsd: 5, fdvUsd: 9 }))).toEqual({ usd: 5, basis: 'market_cap' });
    expect(capitalisation(makeSnapshot({ marketCapUsd: null, fdvUsd: 9 }))).toEqual({ usd: 9, basis: 'fdv' });
    expect(capitalisation(makeSnapshot({ marketCapUsd: null, fdvUsd: null }))).toBeNull();
  });
});
