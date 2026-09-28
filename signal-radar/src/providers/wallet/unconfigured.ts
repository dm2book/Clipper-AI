import { NotConfiguredError } from '../../core/errors.js';
import type { WalletEvent } from '../../wallets/model.js';
import type { WalletActivitySource, WalletProvider } from '../interfaces.js';

/*
 * NOT CONNECTED YET — Wallet Intelligence data sources.
 *
 * The module (src/wallets/, WalletIntelligenceService) is complete and tested
 * on synthetic events, but no source of real wallet events is wired in.
 * Candidates (to be chosen and verified against their documentation, then
 * contract-tested with recorded responses like the other providers): a
 * parsed-transaction stream such as Helius Enhanced Transactions/webhooks, or
 * the radar's own swap decoder once roadmap phase 3 records trades.
 * Until then these placeholders make the gap explicit instead of returning
 * empty data that would look like "this wallet never trades".
 */

export class UnconfiguredWalletProvider implements WalletProvider {
  readonly name = 'unconfigured';
  readonly available = false;

  async getHistory(): Promise<WalletEvent[]> {
    throw new NotConfiguredError('WalletProvider is not connected yet (see docs/PROVIDERS.md, Wallet Intelligence)');
  }
}

export class UnconfiguredWalletActivitySource implements WalletActivitySource {
  readonly name = 'unconfigured';
  readonly available = false;

  async start(): Promise<void> {
    // Nothing to start: the app logs that wallet activity is not connected.
  }
  async stop(): Promise<void> {}
  setWatchlist(): void {}
}
