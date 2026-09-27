import { describe, expect, it } from 'vitest';
import { countHolders, top10Percentage } from '../../src/core/holders.js';
import { aggregateSafety, verdictFromFlags } from '../../src/core/safety.js';
import { makeSafety } from '../support/factories.js';

describe('aggregateSafety', () => {
  const onchainPass = makeSafety({ provider: 'solana-onchain', kind: 'onchain', verdict: 'PASS' });

  it('passes with the on-chain check and enough external confirmations', () => {
    const s = aggregateSafety([onchainPass, makeSafety({ provider: 'rugcheck', verdict: 'PASS' })], 1);
    expect(s).toMatchObject({ verdict: 'PASS', onchain: 'PASS', externalOk: 1 });
  });

  it('any FAIL wins, with the reasons attached', () => {
    const s = aggregateSafety(
      [onchainPass, makeSafety({ provider: 'goplus', verdict: 'FAIL', reasons: ['freezable'] })],
      0,
    );
    expect(s.verdict).toBe('FAIL');
    expect(s.reasons).toEqual(['goplus: freezable']);
  });

  it('never turns missing data into a pass', () => {
    expect(aggregateSafety([makeSafety({ verdict: 'PASS' })], 1).verdict).toBe('UNKNOWN'); // no on-chain check
    expect(aggregateSafety([onchainPass], 1).verdict).toBe('UNKNOWN'); // no external confirmation
    expect(aggregateSafety([onchainPass, makeSafety({ verdict: 'UNKNOWN' })], 1).verdict).toBe('UNKNOWN');
    expect(aggregateSafety([], 0).verdict).toBe('UNKNOWN');
  });

  it('reports warnings as WARN', () => {
    const s = aggregateSafety([onchainPass, makeSafety({ provider: 'rugcheck', verdict: 'WARN', reasons: ['Mutable metadata (warn)'] })], 1);
    expect(s).toMatchObject({ verdict: 'WARN', reasons: ['rugcheck: Mutable metadata (warn)'] });
  });

  it('derives verdicts from flags', () => {
    expect(verdictFromFlags({}, ['mint_authority_active'], [])).toBe('UNKNOWN');
    expect(verdictFromFlags({ mint_authority_active: false }, ['mint_authority_active'], [])).toBe('PASS');
    expect(verdictFromFlags({ mint_authority_active: true }, ['mint_authority_active'], [])).toBe('FAIL');
    expect(verdictFromFlags({ transfer_fee: true }, [], ['transfer_fee'])).toBe('WARN');
  });
});

describe('holder helpers', () => {
  const onCurve = (a: string) => !a.startsWith('pda');

  it('counts distinct funded owners', () => {
    expect(countHolders([{ owner: 'a', amount: 1n }, { owner: 'a', amount: 2n }, { owner: 'b', amount: 0n }])).toBe(1);
  });

  it('computes top-10 without pools and burns, merging accounts per owner', () => {
    const holdings = [
      { owner: 'pda-pool', amount: 500n },
      { owner: 'burn', amount: 100n },
      { owner: 'whale', amount: 60n },
      { owner: 'whale', amount: 40n },
      ...Array.from({ length: 12 }, (_, i) => ({ owner: `w${i}`, amount: 10n })),
    ];
    const r = top10Percentage(holdings, 1_000n, { excludedOwners: new Set(['burn']), excludeProgramOwned: true, isOnCurve: onCurve });
    expect(r.pct).toBe(19); // whale 100 + 9 × 10 = 190 of 1000
    expect(r.top).toHaveLength(10);
    expect(r.top[0]).toEqual({ owner: 'whale', amount: '100', pct: 10 });
  });

  it('handles amounts beyond 2^53 exactly', () => {
    const supply = 10n ** 30n;
    const r = top10Percentage([{ owner: 'w', amount: supply / 4n }], supply, {
      excludedOwners: new Set(),
      excludeProgramOwned: false,
      isOnCurve: onCurve,
    });
    expect(r.pct).toBe(25);
  });

  it('rejects a zero supply', () => {
    expect(() => top10Percentage([], 0n, { excludedOwners: new Set(), excludeProgramOwned: false, isOnCurve: onCurve })).toThrow(RangeError);
  });
});
