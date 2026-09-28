import { describe, expect, it } from 'vitest';
import { buildLedger, unrealizedPnl } from '../../../src/wallets/ledger.js';
import { ev, roundTrip } from '../../support/wallets.js';

describe('ledger: realised PnL', () => {
  it('computes profit, return and holding time of a simple round trip', () => {
    const l = buildLedger(roundTrip('A', 1_000, 1_500, 0, 90));
    expect(l.closed).toHaveLength(1);
    const p = l.closed[0]!;
    expect(p.realizedPnlUsd).toBe(500);
    expect(p.returnPct).toBe(50);
    expect(p.holdingSec).toBe(90 * 60);
    expect(p.reliable).toBe(true);
    expect(p.closeReason).toBe('sold');
    expect(l.open).toHaveLength(0);
  });

  it('computes a loss', () => {
    const p = buildLedger(roundTrip('A', 2_000, 500, 0)).closed[0]!;
    expect(p.realizedPnlUsd).toBe(-1_500);
    expect(p.returnPct).toBe(-75);
  });

  it('uses FIFO cost across several buys', () => {
    const l = buildLedger([
      ev('buy', 100, 100, 0), // $1.00 per unit
      ev('buy', 100, 300, 1), // $3.00 per unit
      ev('sell', 150, 450, 2), // sells 100 @ cost 100 + 50 @ cost 150 = 250
    ]);
    expect(l.sells[0]!.costUsd).toBe(250);
    expect(l.sells[0]!.realizedPnlUsd).toBe(200);
    expect(l.open[0]!.qtyRaw).toBe(50n);
    expect(l.open[0]!.remainingCostUsd).toBe(150);
  });
});

describe('ledger: partial sells', () => {
  it('keeps one position open across partial sells and closes it on the last one', () => {
    const l = buildLedger([
      ev('buy', 1_000, 1_000, 0),
      ev('sell', 400, 800, 10), // cost 400 -> +400
      ev('sell', 300, 300, 20), // cost 300 -> 0
      ev('sell', 300, 150, 30), // cost 300 -> -150
    ]);
    expect(l.closed).toHaveLength(1); // one position = one trade, not three
    const p = l.closed[0]!;
    expect(p.sells).toBe(3);
    expect(p.costUsd).toBe(1_000);
    expect(p.proceedsUsd).toBe(1_250);
    expect(p.realizedPnlUsd).toBe(250);
    expect(p.returnPct).toBe(25);
    expect(p.holdingSec).toBe(30 * 60);
  });

  it('reports realised PnL of a partial sell while the position stays open', () => {
    const l = buildLedger([ev('buy', 1_000, 1_000, 0), ev('sell', 500, 900, 5)]);
    expect(l.closed).toHaveLength(0);
    expect(l.open[0]!.realizedPnlUsd).toBe(400);
    expect(l.open[0]!.qtyRaw).toBe(500n);
    expect(l.open[0]!.remainingCostUsd).toBe(500);
  });

  it('treats a leftover below the dust fraction as closed', () => {
    const l = buildLedger([ev('buy', 1_000_000, 1_000, 0), ev('sell', 999_500, 2_000, 5)]);
    expect(l.closed).toHaveLength(1);
    expect(l.open).toHaveLength(0);
  });

  it('starts a new position after a full close', () => {
    const l = buildLedger([...roundTrip('A', 100, 200, 0), ...roundTrip('A', 100, 50, 120)]);
    expect(l.closed.map((p) => p.realizedPnlUsd)).toEqual([100, -50]);
  });
});

describe('ledger: duplicates and failed transactions', () => {
  it('ignores a duplicate event (same signature, instruction and wallet)', () => {
    const buy = ev('buy', 1_000, 1_000, 0);
    const sell = ev('sell', 1_000, 2_000, 5);
    const l = buildLedger([buy, { ...buy }, sell, { ...sell }]);
    expect(l.ignored.duplicates).toBe(2);
    expect(l.counts).toMatchObject({ buys: 1, sells: 1 });
    expect(l.closed[0]!.realizedPnlUsd).toBe(1_000);
  });

  it('counts two swaps in one transaction separately (different instruction index)', () => {
    const a = ev('buy', 500, 500, 0, { signature: 'same', ixIndex: 0 });
    const b = ev('buy', 500, 500, 0, { signature: 'same', ixIndex: 1 });
    expect(buildLedger([a, b]).counts.buys).toBe(2);
  });

  it('ignores failed transactions completely', () => {
    const l = buildLedger([
      ev('buy', 1_000, 1_000, 0),
      ev('sell', 1_000, 5_000, 1, { status: 'failed' }),
      ev('sell', 1_000, 1_100, 2),
    ]);
    expect(l.ignored.failed).toBe(1);
    expect(l.counts.sells).toBe(1);
    expect(l.closed[0]!.realizedPnlUsd).toBe(100);
  });

  it('rejects zero amounts and negative values as invalid', () => {
    const l = buildLedger([ev('buy', 0, 10, 0), ev('buy', 10, -5, 1)]);
    expect(l.ignored.invalid).toBe(2);
    expect(l.open).toHaveLength(0);
  });

  it('is independent of input order', () => {
    const events = [ev('buy', 100, 100, 0), ev('buy', 100, 300, 1), ev('sell', 200, 800, 2)];
    expect(buildLedger([...events].reverse()).closed).toEqual(buildLedger(events).closed);
  });
});

describe('ledger: transfers', () => {
  it('marks a position funded by a transfer as unreliable (unknown cost basis)', () => {
    const l = buildLedger([ev('transfer_in', 1_000, null, 0), ev('sell', 1_000, 5_000, 5)]);
    const p = l.closed[0]!;
    expect(p.reliable).toBe(false);
    expect(p.returnPct).toBeNull();
    expect(p.unreliableReasons.join()).toMatch(/transfer/);
    expect(l.sells[0]!.realizedPnlUsd).toBeNull(); // no invented PnL
  });

  it('marks a position that left by transfer as unreliable and not as a loss', () => {
    const l = buildLedger([ev('buy', 1_000, 1_000, 0), ev('transfer_out', 1_000, null, 5)]);
    const p = l.closed[0]!;
    expect(p.closeReason).toBe('transferred_out');
    expect(p.reliable).toBe(false);
    expect(p.realizedPnlUsd).toBe(0);
    expect(l.counts.transfersOut).toBe(1);
  });

  it('keeps PnL of a sell whose own lots were bought, even if a later transfer taints the position', () => {
    const l = buildLedger([
      ev('buy', 1_000, 1_000, 0),
      ev('sell', 500, 1_000, 1),
      ev('transfer_out', 500, null, 2),
    ]);
    expect(l.sells[0]!.realizedPnlUsd).toBe(500);
    expect(l.closed[0]!.reliable).toBe(false);
  });

  it('flags selling more than was seen arriving (history before the window)', () => {
    const l = buildLedger([ev('buy', 100, 100, 0), ev('sell', 300, 600, 1)]);
    expect(l.sells[0]!.matchedRaw).toBe(100n);
    expect(l.sells[0]!.realizedPnlUsd).toBeNull();
    expect(l.sells[0]!.unreliableReason).toMatch(/before the window/);
    expect(l.closed[0]!.reliable).toBe(false);
  });

  it('does not open a position for a sell without any prior balance', () => {
    const l = buildLedger([ev('sell', 100, 100, 0)]);
    expect(l.closed).toHaveLength(0);
    expect(l.open).toHaveLength(0);
    expect(l.sells[0]!.unreliableReason).toMatch(/before the window/);
  });

  it('marks a buy without USD value as unreliable', () => {
    const l = buildLedger([ev('buy', 100, null, 0), ev('sell', 100, 200, 1)]);
    expect(l.closed[0]!.reliable).toBe(false);
    expect(l.sells[0]!.realizedPnlUsd).toBeNull();
  });
});

describe('unrealised PnL', () => {
  it('values an open position at the current price', () => {
    // 2 whole tokens (6 decimals) bought for $10, now $8 each
    const l = buildLedger([ev('buy', 2_000_000, 10, 0)]);
    expect(unrealizedPnl(l.open[0]!, 8, 6)).toEqual({ valueUsd: 16, pnlUsd: 6 });
  });

  it('is null without price, decimals or known cost', () => {
    const bought = buildLedger([ev('buy', 1_000, 10, 0)]).open[0]!;
    expect(unrealizedPnl(bought, null, 6)).toBeNull();
    expect(unrealizedPnl(bought, 1, null)).toBeNull();
    const transferred = buildLedger([ev('transfer_in', 1_000, null, 0)]).open[0]!;
    expect(unrealizedPnl(transferred, 1, 6)).toBeNull();
  });
});
