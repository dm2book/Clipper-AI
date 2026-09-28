/**
 * Position accounting per wallet and token (FIFO lots), as a pure function.
 *
 * A POSITION opens when the wallet's balance goes from zero to positive and
 * closes when it falls back to (nearly) zero: several buys (averaging in) and
 * several partial sells belong to one position. One closed position is one
 * "trade" for win rate and average return.
 *
 * Reliability — PnL is only claimed where the data supports it. A position is
 * UNRELIABLE (and excluded from win rate / average return) when any of:
 *  - a buy or sell had no USD value
 *  - tokens arrived by transfer (cost basis unknown)
 *  - tokens left by transfer (their outcome is unknown)
 *  - more was sold than the ledger saw bought (history before the window)
 * The realised PnL of a single sell is still reported when that sell's own
 * cost and proceeds are fully known.
 */
import { eventKey, type WalletEvent } from './model.js';

export interface LedgerOptions {
  /** A position counts as closed when at most this fraction of its peak size remains. */
  dustFraction: number;
}

export const DEFAULT_LEDGER_OPTIONS: LedgerOptions = { dustFraction: 0.001 };

interface Lot {
  qty: bigint;
  /** Cost of this lot in USD; null = unknown (transfer or unpriced buy). */
  costUsd: number | null;
}

export interface SellResult {
  signature: string;
  tokenAddress: string;
  at: Date;
  soldRaw: bigint;
  /** Part of the sale matched against tokens the ledger saw arrive. */
  matchedRaw: bigint;
  costUsd: number | null;
  proceedsUsd: number | null;
  realizedPnlUsd: number | null;
  unreliableReason: string | null;
}

export interface ClosedPosition {
  tokenAddress: string;
  openedAt: Date;
  closedAt: Date;
  holdingSec: number;
  buys: number;
  sells: number;
  /** Cost of the tokens that were sold (known part). */
  costUsd: number;
  proceedsUsd: number;
  realizedPnlUsd: number;
  /** realizedPnl / cost × 100; null when unreliable. */
  returnPct: number | null;
  closeReason: 'sold' | 'transferred_out';
  reliable: boolean;
  unreliableReasons: string[];
}

export interface OpenPosition {
  tokenAddress: string;
  openedAt: Date;
  qtyRaw: bigint;
  /** Cost of the tokens still held; null when any of them has unknown cost. */
  remainingCostUsd: number | null;
  /** PnL already realised by partial sells of this position (known part). */
  realizedPnlUsd: number;
  reliable: boolean;
  unreliableReasons: string[];
}

export interface LedgerResult {
  closed: ClosedPosition[];
  open: OpenPosition[];
  sells: SellResult[];
  counts: { buys: number; sells: number; transfersIn: number; transfersOut: number };
  ignored: { failed: number; duplicates: number; invalid: number };
}

interface State {
  lots: Lot[];
  openedAt: Date | null;
  peak: bigint;
  buys: number;
  sells: number;
  costSold: number;
  proceeds: number;
  realized: number;
  reasons: Set<string>;
}

const fresh = (): State => ({
  lots: [],
  openedAt: null,
  peak: 0n,
  buys: 0,
  sells: 0,
  costSold: 0,
  proceeds: 0,
  realized: 0,
  reasons: new Set(),
});

const held = (s: State) => s.lots.reduce((a, l) => a + l.qty, 0n);
const share = (part: bigint, whole: bigint) => (whole === 0n ? 0 : Number(part) / Number(whole));

/** Removes `qty` FIFO; returns what was taken and its cost (null if any part had unknown cost). */
function consume(s: State, qty: bigint): { taken: bigint; costUsd: number | null } {
  let remaining = qty;
  let cost: number | null = 0;
  while (remaining > 0n && s.lots.length) {
    const lot = s.lots[0]!;
    const take = lot.qty <= remaining ? lot.qty : remaining;
    const part = lot.costUsd === null ? null : lot.costUsd * share(take, lot.qty);
    cost = cost === null || part === null ? null : cost + part;
    if (lot.costUsd !== null) lot.costUsd -= part!;
    lot.qty -= take;
    remaining -= take;
    if (lot.qty === 0n) s.lots.shift();
  }
  return { taken: qty - remaining, costUsd: cost };
}

/** Deterministic processing order: time, then slot, then position in the transaction. */
function byTime(a: WalletEvent, b: WalletEvent): number {
  return (
    a.blockTime.getTime() - b.blockTime.getTime() ||
    (a.slot ?? 0) - (b.slot ?? 0) ||
    a.signature.localeCompare(b.signature) ||
    a.ixIndex - b.ixIndex
  );
}

export function buildLedger(events: readonly WalletEvent[], opts: LedgerOptions = DEFAULT_LEDGER_OPTIONS): LedgerResult {
  const result: LedgerResult = {
    closed: [],
    open: [],
    sells: [],
    counts: { buys: 0, sells: 0, transfersIn: 0, transfersOut: 0 },
    ignored: { failed: 0, duplicates: 0, invalid: 0 },
  };
  const seen = new Set<string>();
  const valid: WalletEvent[] = [];
  for (const e of events) {
    if (e.status !== 'success') {
      result.ignored.failed++;
      continue;
    }
    const key = eventKey(e);
    if (seen.has(key)) {
      result.ignored.duplicates++;
      continue;
    }
    seen.add(key);
    if (e.amountRaw <= 0n || (e.valueUsd !== null && !(e.valueUsd >= 0))) {
      result.ignored.invalid++;
      continue;
    }
    valid.push(e);
  }
  valid.sort(byTime);

  const states = new Map<string, State>();
  const stateFor = (token: string) => {
    let s = states.get(token);
    if (!s) states.set(token, (s = fresh()));
    return s;
  };

  const maybeClose = (token: string, s: State, at: Date, reason: 'sold' | 'transferred_out') => {
    if (s.openedAt === null) return;
    const remaining = held(s);
    if (remaining > 0n && Number(remaining) > Number(s.peak) * opts.dustFraction) return;
    if (reason === 'transferred_out') s.reasons.add('position left the wallet by transfer (outcome unknown)');
    const reliable = s.reasons.size === 0 && s.costSold > 0;
    result.closed.push({
      tokenAddress: token,
      openedAt: s.openedAt,
      closedAt: at,
      holdingSec: Math.max(0, (at.getTime() - s.openedAt.getTime()) / 1000),
      buys: s.buys,
      sells: s.sells,
      costUsd: s.costSold,
      proceedsUsd: s.proceeds,
      realizedPnlUsd: s.realized,
      returnPct: reliable ? (s.realized / s.costSold) * 100 : null,
      closeReason: reason,
      reliable,
      unreliableReasons: [...s.reasons],
    });
    states.set(token, fresh());
  };

  for (const e of valid) {
    const s = stateFor(e.tokenAddress);
    switch (e.kind) {
      case 'buy':
      case 'transfer_in': {
        if (e.kind === 'buy') result.counts.buys++;
        else result.counts.transfersIn++;
        if (s.openedAt === null) s.openedAt = e.blockTime;
        const cost = e.kind === 'buy' ? e.valueUsd : null;
        if (e.kind === 'transfer_in') s.reasons.add('tokens received by transfer (cost basis unknown)');
        else if (cost === null) s.reasons.add('a buy without USD value');
        if (e.kind === 'buy') s.buys++;
        s.lots.push({ qty: e.amountRaw, costUsd: cost });
        const h = held(s);
        if (h > s.peak) s.peak = h;
        break;
      }
      case 'sell': {
        result.counts.sells++;
        const { taken, costUsd } = consume(s, e.amountRaw);
        const unmatched = e.amountRaw - taken;
        let reason: string | null = null;
        if (unmatched > 0n) reason = 'sold more than the ledger saw arrive (history before the window)';
        else if (costUsd === null) reason = 'sold tokens with unknown cost basis';
        else if (e.valueUsd === null) reason = 'a sell without USD value';
        // Proceeds are attributed to the matched part only.
        const proceeds = e.valueUsd === null ? null : e.valueUsd * share(taken, e.amountRaw);
        const pnl = reason === null && costUsd !== null && proceeds !== null ? proceeds - costUsd : null;
        result.sells.push({
          signature: e.signature,
          tokenAddress: e.tokenAddress,
          at: e.blockTime,
          soldRaw: e.amountRaw,
          matchedRaw: taken,
          costUsd,
          proceedsUsd: proceeds,
          realizedPnlUsd: pnl,
          unreliableReason: reason,
        });
        if (s.openedAt === null) break; // nothing held: no position to attach to
        s.sells++;
        if (reason) s.reasons.add(reason);
        if (costUsd !== null) s.costSold += costUsd;
        if (proceeds !== null) s.proceeds += proceeds;
        if (pnl !== null) s.realized += pnl;
        maybeClose(e.tokenAddress, s, e.blockTime, 'sold');
        break;
      }
      case 'transfer_out': {
        result.counts.transfersOut++;
        if (s.openedAt === null) break;
        consume(s, e.amountRaw);
        s.reasons.add('tokens left the wallet by transfer (outcome unknown)');
        maybeClose(e.tokenAddress, s, e.blockTime, 'transferred_out');
        break;
      }
    }
  }

  for (const [token, s] of states) {
    if (s.openedAt === null) continue;
    const remainingCost = s.lots.reduce<number | null>((a, l) => (a === null || l.costUsd === null ? null : a + l.costUsd), 0);
    result.open.push({
      tokenAddress: token,
      openedAt: s.openedAt,
      qtyRaw: held(s),
      remainingCostUsd: remainingCost,
      realizedPnlUsd: s.realized,
      reliable: s.reasons.size === 0,
      unreliableReasons: [...s.reasons],
    });
  }
  return result;
}

/**
 * Unrealised PnL of an open position at `priceUsd` per whole token. Null when
 * the price, the decimals or the cost of any remaining token is unknown.
 */
export function unrealizedPnl(
  position: Pick<OpenPosition, 'qtyRaw' | 'remainingCostUsd'>,
  priceUsd: number | null,
  decimals: number | null,
): { valueUsd: number; pnlUsd: number } | null {
  if (priceUsd === null || decimals === null || position.remainingCostUsd === null) return null;
  const valueUsd = (Number(position.qtyRaw) / 10 ** decimals) * priceUsd;
  return { valueUsd, pnlUsd: valueUsd - position.remainingCostUsd };
}
