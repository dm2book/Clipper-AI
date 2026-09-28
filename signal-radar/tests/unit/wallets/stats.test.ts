import { describe, expect, it } from 'vitest';
import { classifyWallet } from '../../../src/wallets/classify.js';
import { DEFAULT_CRITERIA, type WalletCriteria } from '../../../src/wallets/criteria.js';
import { buildLedger } from '../../../src/wallets/ledger.js';
import { computeStats, wilsonLowerBound } from '../../../src/wallets/stats.js';
import { T0, WALLET, ev, roundTrip } from '../../support/wallets.js';

const TO = new Date(T0.getTime() + 30 * 86_400_000);
const stats = (events: Parameters<typeof buildLedger>[0], prices = new Map()) =>
  computeStats(buildLedger(events), { chain: 'solana', wallet: WALLET, from: T0, to: TO, prices });

/** n round trips; `wins` of them +pct%, the rest -lossPct%. Each on its own token. */
function history(n: number, wins: number, winPct = 50, lossPct = 20, cost = 1_000) {
  const events = [];
  for (let i = 0; i < n; i++) {
    const proceeds = i < wins ? cost * (1 + winPct / 100) : cost * (1 - lossPct / 100);
    events.push(...roundTrip(`T${i}`, cost, proceeds, i * 180, 60));
  }
  return events;
}

describe('win rate', () => {
  it('counts closed positions, not individual sells', () => {
    const s = stats([
      ev('buy', 1_000, 1_000, 0, { tokenAddress: 'A' }),
      ev('sell', 500, 400, 1, { tokenAddress: 'A' }),
      ev('sell', 500, 1_000, 2, { tokenAddress: 'A' }), // total +400: one win
      ...roundTrip('B', 1_000, 900, 10),
    ]);
    expect(s.trades).toBe(5);
    expect(s.closedPositions).toBe(2);
    expect(s.wins).toBe(1);
    expect(s.losses).toBe(1);
    expect(s.winRate).toBe(0.5);
  });

  it('excludes unreliable positions from win rate and average return', () => {
    const s = stats([...roundTrip('A', 100, 200, 0), ev('transfer_in', 1_000, null, 5, { tokenAddress: 'B' }), ev('sell', 1_000, 5, 6, { tokenAddress: 'B' })]);
    expect(s.closedPositions).toBe(2);
    expect(s.reliableClosedPositions).toBe(1);
    expect(s.winRate).toBe(1);
    expect(s.avgReturnPct).toBe(100);
    expect(s.unreliableSells).toBe(1);
  });

  it('is null (not 0%) without closed positions', () => {
    const s = stats([ev('buy', 100, 100, 0)]);
    expect(s.winRate).toBeNull();
    expect(s.winRateLowerBound).toBeNull();
    expect(s.avgReturnPct).toBeNull();
  });

  it('a break-even position is not a win', () => {
    expect(stats(roundTrip('A', 100, 100, 0)).wins).toBe(0);
  });

  it('Wilson lower bound punishes small samples', () => {
    expect(wilsonLowerBound(3, 3)!).toBeLessThan(0.55);
    expect(wilsonLowerBound(60, 100)!).toBeGreaterThan(0.51);
    expect(wilsonLowerBound(60, 100)!).toBeLessThan(0.6);
    expect(wilsonLowerBound(0, 0)).toBeNull();
  });
});

describe('average return, PnL, holding time', () => {
  it('computes average and median return per position', () => {
    const s = stats([...roundTrip('A', 100, 200, 0), ...roundTrip('B', 100, 90, 10), ...roundTrip('C', 100, 130, 20)]);
    expect(s.avgReturnPct).toBeCloseTo((100 - 10 + 30) / 3);
    expect(s.medianReturnPct).toBe(30);
    expect(s.realizedPnlUsd).toBe(120);
  });

  it('averages holding time over sold positions', () => {
    const s = stats([...roundTrip('A', 100, 200, 0, 30), ...roundTrip('B', 100, 200, 0, 90)]);
    expect(s.avgHoldingSec).toBe(60 * 60);
  });

  it('reports the largest single win as a share of all profit', () => {
    const s = stats([...roundTrip('A', 100, 1_000, 0), ...roundTrip('B', 100, 200, 10)]);
    expect(s.largestWinShare).toBeCloseTo(900 / 1_000);
  });

  it('computes unrealised PnL and exposure only where a price is known', () => {
    const prices = new Map([['A', { priceUsd: 3, decimals: 0 }]]);
    const s = stats(
      [ev('buy', 100, 100, 0, { tokenAddress: 'A' }), ev('buy', 100, 100, 0, { tokenAddress: 'B' })],
      prices,
    );
    expect(s.openPositions).toBe(2);
    expect(s.unrealizedPnlUsd).toBe(200);
    expect(s.exposureUsd).toBe(300);
    expect(s.unpricedOpenPositions).toBe(1);
  });

  it('does not count failed transactions as trades', () => {
    const s = stats([...roundTrip('A', 100, 200, 0), ev('buy', 1, 1, 5, { status: 'failed' })]);
    expect(s.trades).toBe(2);
    expect(s.failedIgnored).toBe(1);
  });
});

describe('wallet classification', () => {
  const c: WalletCriteria = { ...DEFAULT_CRITERIA, minClosedPositions: 20, minRealizedPnlUsd: 1_000 };

  it('QUALIFIED only when every configured criterion passes, with the values listed', () => {
    const r = classifyWallet(stats(history(30, 21)), c); // 70% win rate
    expect(r.classification).toBe('QUALIFIED');
    expect(r.criteria.every((x) => x.passed)).toBe(true);
    expect(r.criteriaVersion).toBe(DEFAULT_CRITERIA.version);
    expect(r.summary).toMatch(/voldoet aan volgcriteria/);
    expect(r.summary.toLowerCase()).not.toMatch(/smart/);
  });

  it('INSUFFICIENT_HISTORY below the minimum sample, however good the results', () => {
    const r = classifyWallet(stats(history(5, 5, 500)), c);
    expect(r.classification).toBe('INSUFFICIENT_HISTORY');
  });

  it('NOT_QUALIFIED names the failing criteria', () => {
    const r = classifyWallet(stats(history(30, 12)), c); // 40%
    expect(r.classification).toBe('NOT_QUALIFIED');
    const failed = r.criteria.filter((x) => !x.passed).map((x) => x.id);
    expect(failed).toContain('win_rate');
    expect(r.summary).toMatch(/winrate/);
  });

  it('one lucky trade cannot qualify a wallet (largest-win share)', () => {
    // 25 small wins and 5 losses, but one position made almost all the profit
    const events = history(30, 25, 1, 20);
    events.push(...roundTrip('JACKPOT', 1_000, 200_000, 99_000));
    const r = classifyWallet(stats(events), c);
    expect(r.criteria.find((x) => x.id === 'largest_win_share')!.passed).toBe(false);
    expect(r.classification).toBe('NOT_QUALIFIED');
  });

  it('BOT_LIKE when the trade frequency exceeds the maximum', () => {
    const r = classifyWallet(stats(history(30, 21)), { ...c, maxTradesPerDay: 1 });
    expect(r.classification).toBe('BOT_LIKE');
  });

  it('every threshold is configurable', () => {
    const s = stats(history(30, 21));
    expect(classifyWallet(s, { ...c, minWinRate: 0.8 }).classification).toBe('NOT_QUALIFIED');
    expect(classifyWallet(s, { ...c, minAvgReturnPct: 50 }).classification).toBe('NOT_QUALIFIED');
    expect(classifyWallet(s, { ...c, minRealizedPnlUsd: 1e9 }).classification).toBe('NOT_QUALIFIED');
    expect(classifyWallet(s, { ...c, minClosedPositions: 31 }).classification).toBe('INSUFFICIENT_HISTORY');
  });
});
