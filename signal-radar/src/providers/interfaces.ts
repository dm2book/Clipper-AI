/**
 * Provider interfaces (docs/ARCHITECTURE.md §E). Services depend only on
 * these; nothing outside `src/providers/` knows a provider's URL or field
 * names, so any implementation can be swapped without touching the pipeline.
 *
 * Status of each implementation: docs/PROVIDERS.md.
 */
import type { AlertPayload } from '../core/alerts.js';
import type { HolderSnapshot } from '../core/holders.js';
import type { MarketSnapshot } from '../core/marketSnapshot.js';
import type { SafetyReport } from '../core/safety.js';
import type { DiscoveredToken } from '../core/token.js';
import type { Chain } from '../core/types.js';
import type { WalletEvent } from '../wallets/model.js';

export interface DiscoveryHealth {
  connected: boolean;
  lastEventAt: Date | null;
  details?: Record<string, unknown>;
}

/** Finds new tokens as they launch and reports them through `handler`. */
export interface TokenDiscoveryProvider {
  readonly name: string;
  /** Starts background work and returns once it is running. */
  start(handler: (token: DiscoveredToken) => Promise<void>): Promise<void>;
  /** Stops all background work; resolves when it has stopped. */
  stop(): Promise<void>;
  health(): DiscoveryHealth;
}

/** Market data per token (price, liquidity, volume, transactions). */
export interface MarketDataProvider {
  readonly name: string;
  /** How many tokens one `getSnapshots` call may contain. */
  readonly maxBatchSize: number;
  /**
   * Snapshots for the requested tokens. A token missing from the result is
   * "not indexed (yet)", which is normal for brand-new tokens — not an error.
   */
  getSnapshots(chain: Chain, addresses: readonly string[], signal?: AbortSignal): Promise<Map<string, MarketSnapshot>>;
}

/** One source of safety facts about a token (on-chain or an external service). */
export interface SafetyProvider {
  readonly name: string;
  readonly kind: 'onchain' | 'external';
  /** Throws NotFoundError when the provider does not know the token yet. */
  check(chain: Chain, address: string, signal?: AbortSignal): Promise<SafetyReport>;
}

/** Holder count and concentration. */
export interface HolderProvider {
  readonly name: string;
  getHolders(chain: Chain, address: string, signal?: AbortSignal): Promise<HolderSnapshot>;
}

/**
 * Wallet history (Wallet Intelligence): used to backfill a wallet that was
 * just added to the watchlist, so its statistics do not start from zero.
 * NOT CONNECTED YET: see providers/wallet/unconfigured.ts and docs/PROVIDERS.md.
 */
export interface WalletProvider {
  readonly name: string;
  readonly available: boolean;
  /** Every swap/transfer of `wallet` since `since`, failed transactions included (status 'failed'). */
  getHistory(chain: Chain, wallet: string, since: Date, signal?: AbortSignal): Promise<WalletEvent[]>;
}

/**
 * Live wallet-level swaps and transfers (Wallet Intelligence). Each event
 * must carry its signature + instruction index (dedupe), status, raw amount
 * and — only when the source actually knows it — the USD value.
 * NOT CONNECTED YET: see providers/wallet/unconfigured.ts and docs/PROVIDERS.md.
 */
export interface WalletActivitySource {
  readonly name: string;
  readonly available: boolean;
  start(handler: (events: WalletEvent[]) => Promise<void>): Promise<void>;
  stop(): Promise<void>;
  /** The watchlist; a source that sees every swap may use it only for priority. */
  setWatchlist(chain: Chain, wallets: readonly string[]): void;
}

/** Delivers an alert somewhere (Discord, console, …). */
export interface NotificationProvider {
  readonly name: string;
  send(alert: AlertPayload, signal?: AbortSignal): Promise<{ messageId: string | null }>;
}
