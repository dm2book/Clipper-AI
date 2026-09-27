import type { Chain } from './types.js';

export interface TopHolder {
  owner: string;
  /** Raw amount as a decimal string. */
  amount: string;
  pct: number;
}

export interface HolderSnapshot {
  chain: Chain;
  tokenAddress: string;
  observedAt: Date;
  holderCount: number;
  /** True when the count stopped at a scan limit (the real number is higher). */
  holderCountCapped: boolean;
  /** Share of supply held by the ten largest real holders, 0–100. Null if unknown. */
  top10Pct: number | null;
  method: string;
  topHolders: TopHolder[];
}

export interface Holding {
  owner: string;
  amount: bigint;
}

/** Distinct owners with a non-zero balance. */
export function countHolders(accounts: Iterable<Holding>): number {
  const owners = new Set<string>();
  for (const a of accounts) if (a.amount > 0n) owners.add(a.owner);
  return owners.size;
}

export interface Top10Options {
  excludedOwners: ReadonlySet<string>;
  /** Pool vaults, bonding curves and lockers are owned by off-curve program addresses. */
  excludeProgramOwned: boolean;
  isOnCurve: (address: string) => boolean;
}

/**
 * Share of `supply` held by the ten largest owners, in percent (2 decimals).
 * Several token accounts of one owner count as one holder.
 */
export function top10Percentage(
  holdings: readonly Holding[],
  supply: bigint,
  opts: Top10Options,
): { pct: number; top: TopHolder[] } {
  if (supply <= 0n) throw new RangeError('supply must be positive');
  const perOwner = new Map<string, bigint>();
  for (const h of holdings) {
    if (h.amount <= 0n || opts.excludedOwners.has(h.owner)) continue;
    if (opts.excludeProgramOwned && !opts.isOnCurve(h.owner)) continue;
    perOwner.set(h.owner, (perOwner.get(h.owner) ?? 0n) + h.amount);
  }
  const sorted = [...perOwner.entries()].sort((a, b) => (a[1] > b[1] ? -1 : a[1] < b[1] ? 1 : 0)).slice(0, 10);
  const pctOf = (amount: bigint) => Number((amount * 10_000n) / supply) / 100;
  const sum = sorted.reduce((acc, [, amount]) => acc + amount, 0n);
  return {
    pct: pctOf(sum),
    top: sorted.map(([owner, amount]) => ({ owner, amount: amount.toString(), pct: pctOf(amount) })),
  };
}
