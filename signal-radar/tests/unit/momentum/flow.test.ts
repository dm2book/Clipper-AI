import { describe, expect, it } from 'vitest';
import { flowFromH24Delta, flowFromRolling, flowFromTrades, flowSeries } from '../../../src/momentum/flow.js';
import { MIN, NOW, cumulativeSnapshots, snap, trade, tradesIn } from '../../support/momentum.js';

const opts = { singleTradeMaxShare: 0.4, singleTradeMedianMultiple: 10 };
const W5 = 5 * MIN;

describe('flowFromTrades', () => {
  it('aggregates volume, sides and unique wallets inside [start, end)', () => {
    const trades = [
      trade(1 * MIN, 'a', 'buy', 100),
      trade(2 * MIN, 'a', 'buy', 100), // same buyer twice
      trade(3 * MIN, 'b', 'buy', 50),
      trade(4 * MIN, 'c', 'sell', 70),
      trade(5 * MIN, 'd', 'buy', 999), // exactly at start: included
      trade(0, 'e', 'buy', 999), // exactly at end: excluded
      trade(6 * MIN, 'f', 'buy', 999), // before the window
    ];
    const f = flowFromTrades(trades, NOW - W5, NOW, opts);
    expect(f).toMatchObject({ source: 'trades', buys: 4, sells: 1, transactions: 5, uniqueBuyers: 3, uniqueSellers: 1 });
    expect(f.rawVolumeUsd).toBe(1_319);
  });

  it('excludes an extreme single trade from volume, but still counts it as a transaction', () => {
    const trades = [...tradesIn(0, W5, 20, 'buy', 1_000), trade(MIN, 'whale', 'buy', 40_000)];
    const f = flowFromTrades(trades, NOW - W5, NOW, opts);
    expect(f.excludedTrades).toBe(1);
    expect(f.excludedVolumeUsd).toBe(40_000);
    expect(f.volumeUsd).toBeCloseTo(1_000);
    expect(f.transactions).toBe(21);
    expect(f.largestTradeShare).toBeCloseTo(40_000 / 41_000);
  });

  it('does not treat a few equal trades as extreme', () => {
    const f = flowFromTrades([trade(MIN, 'a', 'buy', 500), trade(2 * MIN, 'b', 'buy', 500)], NOW - W5, NOW, opts);
    expect(f.excludedTrades).toBe(0);
    expect(f.volumeUsd).toBe(1_000);
    const single = flowFromTrades([trade(MIN, 'a', 'buy', 500)], NOW - W5, NOW, opts);
    expect(single.excludedTrades).toBe(0); // the median is the trade itself
  });

  it('measures wash-trading patterns', () => {
    // Two wallets buy and sell nearly the same amount back and forth.
    const washy = [
      trade(MIN, 'w1', 'buy', 1_000),
      trade(1.5 * MIN, 'w1', 'sell', 950),
      trade(2 * MIN, 'w2', 'buy', 1_000),
      trade(2.5 * MIN, 'w2', 'sell', 1_050),
      trade(3 * MIN, 'real', 'buy', 100),
    ];
    const f = flowFromTrades(washy, NOW - W5, NOW, opts);
    expect(f.wash!.roundTripShare).toBeCloseTo(4_000 / 4_100);
    expect(f.wash!.topWalletsShare).toBe(1);
    expect(f.wash!.transactionsPerWallet).toBeCloseTo(5 / 3);

    const organic = flowFromTrades(tradesIn(0, W5, 40, 'buy', 4_000), NOW - W5, NOW, opts);
    expect(organic.wash!.roundTripShare).toBe(0);
    expect(organic.wash!.topWalletsShare).toBeCloseTo(0.075);
    expect(organic.wash!.transactionsPerWallet).toBe(1);
  });

  it('returns an empty flow for an empty window', () => {
    const f = flowFromTrades([], NOW - W5, NOW, opts);
    expect(f).toMatchObject({ volumeUsd: 0, transactions: 0, uniqueBuyers: 0, wash: null, largestTradeShare: null });
  });
});

describe('flowFromH24Delta', () => {
  const snaps = cumulativeSnapshots([
    [0, 10_000, 100, 40],
    [5 * MIN, 6_000, 70, 30],
  ]);

  it('derives window flow from rolling 24h totals for young tokens', () => {
    const f = flowFromH24Delta(snaps, '5m', NOW - W5, NOW, 2 * 3_600_000);
    expect(f).toMatchObject({ source: 'provider_h24_delta', volumeUsd: 4_000, buys: 30, sells: 10, transactions: 40 });
    expect(f!.uniqueBuyers).toBeNull();
    expect(f!.normalizedFromSec).toBe(300);
  });

  it('normalises an uneven span to the window length', () => {
    const uneven = cumulativeSnapshots([
      [0, 10_000, 100, 40],
      [4 * MIN, 8_000, 80, 40], // 4 min span inside tolerance of a 5m window
    ]);
    const f = flowFromH24Delta(uneven, '5m', NOW - W5, NOW, 3_600_000);
    expect(f!.volumeUsd).toBeCloseTo(2_500); // 2000 per 4 min → per 5 min
    expect(f!.normalizedFromSec).toBe(240);
  });

  it('refuses when the result could be wrong', () => {
    expect(flowFromH24Delta(snaps, '5m', NOW - W5, NOW, 25 * 3_600_000)).toBeNull(); // older than 24h
    expect(flowFromH24Delta(snaps, '5m', NOW - W5, NOW, null)).toBeNull(); // unknown age
    const shrinking = cumulativeSnapshots([
      [0, 5_000, 100, 40],
      [5 * MIN, 6_000, 70, 30],
    ]);
    expect(flowFromH24Delta(shrinking, '5m', NOW - W5, NOW, 3_600_000)).toBeNull(); // inconsistent provider data
    expect(flowFromH24Delta([snaps[0]!], '5m', NOW - W5, NOW, 3_600_000)).toBeNull(); // no start point
  });
});

describe('flowFromRolling', () => {
  const s = [snap(0, { volumeUsd: { m5: 1_234, h1: 9_999 }, txns: { m5: { buys: 7, sells: 3 }, h1: { buys: 70, sells: 30 } } })];

  it('uses the provider window for 5m and 1h only', () => {
    expect(flowFromRolling(s, '5m', NOW)).toMatchObject({ source: 'provider_rolling', volumeUsd: 1_234, transactions: 10 });
    expect(flowFromRolling(s, '1h', NOW)).toMatchObject({ volumeUsd: 9_999, buys: 70 });
    expect(flowFromRolling(s, '1m', NOW)).toBeNull();
    expect(flowFromRolling(s, '15m', NOW)).toBeNull();
  });
});

describe('flowSeries', () => {
  const snaps = cumulativeSnapshots([
    [0, 30_000, 300, 100],
    [5 * MIN, 20_000, 200, 80],
    [10 * MIN, 15_000, 150, 70],
    [15 * MIN, 12_000, 120, 60],
  ]);

  it('prefers trades when the feed covers every interval', () => {
    const r = flowSeries(
      { snapshots: snaps, tokenAgeMs: 3_600_000, trades: { items: tradesIn(0, W5, 5, 'buy', 500), coverageFrom: new Date(NOW - 20 * MIN) } },
      '5m',
      NOW,
      opts,
    );
    expect(r.source).toBe('trades');
    expect(r.intervals[0]!.transactions).toBe(5);
    expect(r.intervals[1]!.transactions).toBe(0);
  });

  it('falls back consistently when trades do not cover the previous window', () => {
    const r = flowSeries(
      { snapshots: snaps, tokenAgeMs: 3_600_000, trades: { items: [], coverageFrom: new Date(NOW - 6 * MIN) } },
      '5m',
      NOW,
      opts,
    );
    expect(r.source).toBe('provider_h24_delta');
    expect(r.intervals.map((f) => f?.volumeUsd)).toEqual([10_000, 5_000, 3_000]);
  });

  it('returns no source at all rather than mixing sources', () => {
    const r = flowSeries({ snapshots: [snaps[0]!], tokenAgeMs: null, trades: null }, '15m', NOW, opts);
    expect(r).toEqual({ source: null, intervals: [null, null, null] });
  });
});
