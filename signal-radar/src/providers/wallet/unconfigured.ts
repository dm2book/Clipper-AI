import { NotConfiguredError } from '../../core/errors.js';
import type { WalletProvider, WalletTrade } from '../interfaces.js';

/**
 * NOT CONNECTED YET — roadmap phase 4 (docs/ARCHITECTURE.md §G.4).
 *
 * Wallet tracking needs per-wallet trade history. Candidate sources (to be
 * chosen and verified): a Helius Enhanced Transactions / trade stream, or the
 * radar's own `trades` table once phase 3 records swaps. Until then this
 * placeholder makes the gap explicit instead of returning empty data that
 * would look like "this wallet never trades".
 */
export class UnconfiguredWalletProvider implements WalletProvider {
  readonly name = 'unconfigured';
  readonly available = false;

  async getTrades(): Promise<WalletTrade[]> {
    throw new NotConfiguredError(
      'WalletProvider is not connected yet (roadmap phase 4, see docs/PROVIDERS.md)',
    );
  }
}
