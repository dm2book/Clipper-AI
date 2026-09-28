/**
 * Detection of wallet activity worth reporting, as pure functions.
 *
 * WHALE ACTIVITY is about SIZE only: a large swap. It says nothing about the
 * wallet's skill. TRACKED WALLET ACTIVITY is about WHO: a wallet on the
 * watchlist (manually added, or meeting the configured criteria) trading.
 * The two are reported separately and never merged into one "smart" label.
 */
import type { ActivityThresholds } from './criteria.js';
import type { WalletEvent } from './model.js';

export interface TrackedInfo {
  source: 'manual' | 'criteria';
  reason: string;
}

export interface ActivityDecision {
  whale: boolean;
  whaleReason: string | null;
  tracked: boolean;
  trackedReason: string | null;
  /** Why the event was not considered at all, if so. */
  skipped: string | null;
}

export interface ActivityContext {
  liquidityUsd: number | null;
  tracked: TrackedInfo | null;
  isOnCurve: (address: string) => boolean;
}

const usd = (v: number) => `$${Math.round(v).toLocaleString('en-US')}`;

export function classifyActivity(e: WalletEvent, ctx: ActivityContext, t: ActivityThresholds): ActivityDecision {
  const none = (skipped: string): ActivityDecision => ({ whale: false, whaleReason: null, tracked: false, trackedReason: null, skipped });
  if (e.status !== 'success') return none('failed transaction');
  if (e.kind !== 'buy' && e.kind !== 'sell') return none('not a swap');
  if (e.valueUsd === null) return none('swap without USD value');
  if (t.ignoredWallets.includes(e.wallet)) return none('wallet on ignore list');
  if (t.excludeProgramOwned && !ctx.isOnCurve(e.wallet)) return none('program-owned account, not a wallet');

  const v = e.valueUsd;
  let whaleReason: string | null = null;
  if (v >= t.whaleMinTradeUsd) {
    whaleReason = `${usd(v)} ≥ ${usd(t.whaleMinTradeUsd)}`;
  } else if (
    ctx.liquidityUsd !== null &&
    ctx.liquidityUsd > 0 &&
    v >= t.whaleLiquidityShareFloorUsd &&
    v / ctx.liquidityUsd >= t.whaleMinLiquidityShare
  ) {
    whaleReason = `${((v / ctx.liquidityUsd) * 100).toFixed(1)}% of pool liquidity (${usd(ctx.liquidityUsd)})`;
  }
  const tracked = ctx.tracked !== null && v >= t.trackedMinTradeUsd;
  return {
    whale: whaleReason !== null,
    whaleReason,
    tracked,
    trackedReason: tracked ? ctx.tracked!.reason : null,
    skipped: null,
  };
}

export interface ClusterMember {
  wallet: string;
  buyUsd: number;
  /** USD counted towards the cluster after the per-wallet share cap. */
  contributionUsd: number;
  sharePct: number;
  firstBuyAt: Date;
}

export interface Cluster {
  wallets: ClusterMember[];
  totalUsd: number;
  cappedTotalUsd: number;
  windowMinutes: number;
  firstAt: Date;
  lastAt: Date;
  cappedWallets: number;
}

/**
 * Caps each value so no single entry exceeds `maxShare` of the capped total
 * (water-filling). If the cap is impossible (maxShare < 1/n) all entries end
 * up equal. Returns contributions in the input order.
 */
export function capShares(values: readonly number[], maxShare: number): number[] {
  const n = values.length;
  if (n === 0) return [];
  if (maxShare >= 1) return [...values];
  if (maxShare * n <= 1) {
    const equal = Math.min(...values);
    return values.map(() => equal);
  }
  const order = values.map((v, i) => [v, i] as const).sort((a, b) => b[0] - a[0]);
  // Find k (number of capped entries) and cap C with C = s·(rest + k·C).
  for (let k = 1; k <= n; k++) {
    const rest = order.slice(k).reduce((a, [v]) => a + v, 0);
    if (maxShare * k >= 1) break;
    const cap = (maxShare * rest) / (1 - maxShare * k);
    const nextValue = order[k]?.[0] ?? -Infinity;
    if (order[k - 1]![0] > cap && nextValue <= cap) {
      const out = [...values];
      for (let j = 0; j < k; j++) out[order[j]![1]] = cap;
      return out;
    }
    if (order[0]![0] <= cap) break;
  }
  return [...values];
}

/**
 * Tracked wallets buying the same token within the window. Each wallet counts
 * once (its buys are summed), and no wallet may make up more than
 * `clusterMaxWalletShare` of the capped cluster USD.
 */
export function detectCluster(
  buys: readonly { wallet: string; valueUsd: number; at: Date }[],
  now: Date,
  t: ActivityThresholds,
): Cluster | null {
  const from = now.getTime() - t.clusterWindowMinutes * 60_000;
  const recent = buys.filter((b) => b.at.getTime() >= from && b.at.getTime() <= now.getTime() && b.valueUsd > 0);
  const byWallet = new Map<string, { usd: number; first: Date }>();
  for (const b of recent) {
    const w = byWallet.get(b.wallet) ?? { usd: 0, first: b.at };
    w.usd += b.valueUsd;
    if (b.at < w.first) w.first = b.at;
    byWallet.set(b.wallet, w);
  }
  if (byWallet.size < t.clusterMinWallets) return null;
  const entries = [...byWallet.entries()];
  const contributions = capShares(
    entries.map(([, w]) => w.usd),
    t.clusterMaxWalletShare,
  );
  const cappedTotal = contributions.reduce((a, b) => a + b, 0);
  const members = entries
    .map(([wallet, w], i) => ({
      wallet,
      buyUsd: w.usd,
      contributionUsd: contributions[i]!,
      sharePct: cappedTotal > 0 ? (contributions[i]! / cappedTotal) * 100 : 0,
      firstBuyAt: w.first,
    }))
    .sort((a, b) => b.contributionUsd - a.contributionUsd);
  const times = recent.map((b) => b.at.getTime());
  return {
    wallets: members,
    totalUsd: entries.reduce((a, [, w]) => a + w.usd, 0),
    cappedTotalUsd: cappedTotal,
    windowMinutes: t.clusterWindowMinutes,
    firstAt: new Date(Math.min(...times)),
    lastAt: new Date(Math.max(...times)),
    cappedWallets: members.filter((m) => m.contributionUsd < m.buyUsd - 1e-9).length,
  };
}
