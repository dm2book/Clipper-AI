/**
 * Wallet performance statistics from a ledger. Win rate and average return
 * use ONLY reliable closed positions (see ledger.ts); everything that was
 * left out is counted, so a thin sample is visible as such.
 */
import { unrealizedPnl, type LedgerResult } from './ledger.js';

export interface WalletStats {
  chain: string;
  wallet: string;
  windowFrom: string;
  windowTo: string;
  /** Successful swaps in the window. */
  trades: number;
  buys: number;
  sells: number;
  transfersIn: number;
  transfersOut: number;
  failedIgnored: number;
  closedPositions: number;
  reliableClosedPositions: number;
  wins: number;
  losses: number;
  /** wins / reliable closed positions; null without any. */
  winRate: number | null;
  /** Lower bound of the 90% Wilson interval for the win rate: small samples score low. */
  winRateLowerBound: number | null;
  avgReturnPct: number | null;
  medianReturnPct: number | null;
  /** Realised PnL of sells whose cost and proceeds were fully known. */
  realizedPnlUsd: number;
  unreliableSells: number;
  /** Share of all profit that came from the single best position (0–1); null without profit. */
  largestWinShare: number | null;
  avgHoldingSec: number | null;
  medianHoldingSec: number | null;
  openPositions: number;
  /** Sum over open positions where it can be computed; null when none can. */
  unrealizedPnlUsd: number | null;
  /** Open positions whose unrealised PnL could not be computed. */
  unpricedOpenPositions: number;
  exposureUsd: number | null;
  tradesPerDay: number;
}

export interface PriceInfo {
  priceUsd: number | null;
  decimals: number | null;
}

const Z90 = 1.6448536269514722;

/** Wilson score interval lower bound. */
export function wilsonLowerBound(wins: number, n: number, z = Z90): number | null {
  if (n <= 0) return null;
  const p = wins / n;
  const z2 = z * z;
  return (p + z2 / (2 * n) - z * Math.sqrt((p * (1 - p)) / n + z2 / (4 * n * n))) / (1 + z2 / n);
}

function median(xs: number[]): number | null {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m]! : (s[m - 1]! + s[m]!) / 2;
}

const mean = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);

export function computeStats(
  ledger: LedgerResult,
  ctx: { chain: string; wallet: string; from: Date; to: Date; prices: ReadonlyMap<string, PriceInfo> },
): WalletStats {
  const reliable = ledger.closed.filter((p) => p.reliable);
  const returns = reliable.map((p) => p.returnPct!);
  const wins = reliable.filter((p) => p.realizedPnlUsd > 0).length;
  const profits = reliable.map((p) => p.realizedPnlUsd).filter((v) => v > 0);
  const totalProfit = profits.reduce((a, b) => a + b, 0);
  const holdings = ledger.closed.filter((p) => p.closeReason === 'sold').map((p) => p.holdingSec);

  let unrealized: number | null = null;
  let exposure: number | null = null;
  let unpriced = 0;
  for (const pos of ledger.open) {
    const price = ctx.prices.get(pos.tokenAddress);
    const u = unrealizedPnl(pos, price?.priceUsd ?? null, price?.decimals ?? null);
    if (!u || !pos.reliable) {
      unpriced++;
      continue;
    }
    unrealized = (unrealized ?? 0) + u.pnlUsd;
    exposure = (exposure ?? 0) + u.valueUsd;
  }

  const days = Math.max(1, (ctx.to.getTime() - ctx.from.getTime()) / 86_400_000);
  const trades = ledger.counts.buys + ledger.counts.sells;
  return {
    chain: ctx.chain,
    wallet: ctx.wallet,
    windowFrom: ctx.from.toISOString(),
    windowTo: ctx.to.toISOString(),
    trades,
    buys: ledger.counts.buys,
    sells: ledger.counts.sells,
    transfersIn: ledger.counts.transfersIn,
    transfersOut: ledger.counts.transfersOut,
    failedIgnored: ledger.ignored.failed,
    closedPositions: ledger.closed.length,
    reliableClosedPositions: reliable.length,
    wins,
    losses: reliable.length - wins,
    winRate: reliable.length ? wins / reliable.length : null,
    winRateLowerBound: wilsonLowerBound(wins, reliable.length),
    avgReturnPct: mean(returns),
    medianReturnPct: median(returns),
    realizedPnlUsd: ledger.sells.reduce((a, s) => a + (s.realizedPnlUsd ?? 0), 0),
    unreliableSells: ledger.sells.filter((s) => s.realizedPnlUsd === null).length,
    largestWinShare: totalProfit > 0 ? Math.max(...profits) / totalProfit : null,
    avgHoldingSec: mean(holdings),
    medianHoldingSec: median(holdings),
    openPositions: ledger.open.length,
    unrealizedPnlUsd: unrealized,
    unpricedOpenPositions: unpriced,
    exposureUsd: exposure,
    tradesPerDay: trades / days,
  };
}
