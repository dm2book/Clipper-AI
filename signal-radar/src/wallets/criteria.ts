/**
 * Every threshold of the Wallet Intelligence module. Nothing here is a
 * judgement of quality until a wallet meets ALL classification criteria,
 * and even then the label only says "meets the configured criteria".
 */
export interface WalletCriteria {
  version: string;
  statsWindowDays: number;
  /** Minimum reliable closed positions before a wallet is judged at all. */
  minClosedPositions: number;
  minWinRate: number;
  /** Wilson 90% lower bound: keeps a lucky streak on a small sample out. */
  minWinRateLowerBound: number;
  minAvgReturnPct: number;
  minMedianReturnPct: number;
  minRealizedPnlUsd: number;
  /** No single position may account for more than this share of all profit. */
  maxLargestWinShare: number;
  /** Above this, the wallet trades like a bot/market maker and is not classified. */
  maxTradesPerDay: number;
  autoTrackQualified: boolean;
}

export interface ActivityThresholds {
  /** A single swap at or above this USD value is whale activity. */
  whaleMinTradeUsd: number;
  /** …or at or above this share of the pool's liquidity… */
  whaleMinLiquidityShare: number;
  /** …provided it is at least this large in USD. */
  whaleLiquidityShareFloorUsd: number;
  /** Per wallet, at most this many whale alerts per hour (one wallet cannot flood the channel). */
  whaleMaxAlertsPerWalletPerHour: number;
  /** Tracked-wallet swaps below this USD value are not alerted. */
  trackedMinTradeUsd: number;
  clusterWindowMinutes: number;
  clusterMinWallets: number;
  /** Largest share of a cluster's (capped) USD any single wallet may contribute. */
  clusterMaxWalletShare: number;
  clusterCooldownMinutes: number;
  /** Pool vaults, routers and other program-owned (off-curve) accounts are not wallets. */
  excludeProgramOwned: boolean;
  /** Addresses never treated as whales or tracked wallets (CEX hot wallets, bridges, …). */
  ignoredWallets: string[];
}

export interface WalletIntelligenceConfig {
  criteria: WalletCriteria;
  activity: ActivityThresholds;
  /** Wallets to follow regardless of statistics (source: manual). */
  manualWallets: string[];
  /** Events older than this when they arrive (backfill, delayed source) are stored but never alerted. */
  alertMaxEventAgeSec: number;
  /** Tracked wallets' statistics are recomputed at least this often (the window moves). */
  statsRefreshMinutes: number;
  /** Wallet events are kept this long (at least the statistics window). */
  eventRetentionDays: number;
}

export const DEFAULT_CRITERIA: WalletCriteria = {
  version: 'wallet-criteria-v1',
  statsWindowDays: 90,
  minClosedPositions: 20,
  minWinRate: 0.55,
  minWinRateLowerBound: 0.4,
  minAvgReturnPct: 10,
  minMedianReturnPct: 0,
  minRealizedPnlUsd: 5_000,
  maxLargestWinShare: 0.5,
  maxTradesPerDay: 100,
  autoTrackQualified: true,
};

export const DEFAULT_ACTIVITY: ActivityThresholds = {
  whaleMinTradeUsd: 25_000,
  whaleMinLiquidityShare: 0.05,
  whaleLiquidityShareFloorUsd: 5_000,
  whaleMaxAlertsPerWalletPerHour: 3,
  trackedMinTradeUsd: 500,
  clusterWindowMinutes: 3,
  clusterMinWallets: 3,
  clusterMaxWalletShare: 0.4,
  clusterCooldownMinutes: 30,
  excludeProgramOwned: true,
  ignoredWallets: [],
};
