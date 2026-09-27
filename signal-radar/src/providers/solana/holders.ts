/**
 * HolderProvider on standard Solana RPC.
 *  - count:  getProgramAccounts on the token program, filtered by mint, with a
 *            dataSlice of owner (32 bytes @32) + amount (u64 LE @64)
 *  - top-10: getTokenLargestAccounts + the owners of those accounts
 * Note: many RPC providers restrict getProgramAccounts; see docs/PROVIDERS.md.
 */
import { countHolders, top10Percentage, type HolderSnapshot, type Holding } from '../../core/holders.js';
import type { Chain } from '../../core/types.js';
import type { HolderProvider } from '../interfaces.js';
import { INCINERATOR, TOKEN_PROGRAM } from './constants.js';
import type { MintInfoLoader } from './mintInfo.js';
import { encodeBase58, isOnCurve } from './pubkey.js';
import type { SolanaRpcClient } from './rpcClient.js';

/** Pure: decode a base64 dataSlice of (owner[32], amount u64 LE). */
export function decodeOwnerAmount(base64: string): Holding | null {
  const bytes = Buffer.from(base64, 'base64');
  if (bytes.length < 40) return null;
  return { owner: encodeBase58(bytes.subarray(0, 32)), amount: bytes.readBigUInt64LE(32) };
}

export interface RpcHolderProviderOptions {
  rpc: Pick<SolanaRpcClient, 'getProgramAccounts' | 'getTokenLargestAccounts' | 'getMultipleParsedAccounts'>;
  mints: Pick<MintInfoLoader, 'get'>;
  excludedOwners?: readonly string[];
  excludeProgramOwned?: boolean;
}

export class SolanaRpcHolderProvider implements HolderProvider {
  readonly name = 'solana-rpc';
  private readonly excluded: ReadonlySet<string>;

  constructor(private readonly opts: RpcHolderProviderOptions) {
    this.excluded = new Set([INCINERATOR, ...(opts.excludedOwners ?? [])]);
  }

  async getHolders(chain: Chain, address: string, signal?: AbortSignal): Promise<HolderSnapshot> {
    const mint = await this.opts.mints.get(address, signal);
    const filters: Record<string, unknown>[] = [{ memcmp: { offset: 0, bytes: address } }];
    // Classic SPL token accounts are exactly 165 bytes; Token-2022 accounts vary.
    if (mint.tokenProgram === TOKEN_PROGRAM) filters.push({ dataSize: 165 });
    const accounts = await this.opts.rpc.getProgramAccounts(
      mint.tokenProgram,
      { encoding: 'base64', commitment: 'confirmed', filters, dataSlice: { offset: 32, length: 40 } },
      signal,
    );
    const holdings = accounts.map((a) => decodeOwnerAmount(a.account.data[0])).filter((h): h is Holding => h !== null);

    const largest = await this.opts.rpc.getTokenLargestAccounts(address, signal);
    const owners = await this.opts.rpc.getMultipleParsedAccounts(
      largest.map((l) => l.address),
      signal,
    );
    const top: Holding[] = [];
    largest.forEach((l, i) => {
      const data = owners[i]?.data as { parsed?: { info?: { owner?: unknown } } } | undefined;
      const owner = data?.parsed?.info?.owner;
      if (typeof owner === 'string' && /^\d+$/.test(l.amount)) top.push({ owner, amount: BigInt(l.amount) });
    });

    const supply = BigInt(mint.supply);
    const concentration =
      supply > 0n
        ? top10Percentage(top, supply, {
            excludedOwners: this.excluded,
            excludeProgramOwned: this.opts.excludeProgramOwned ?? true,
            isOnCurve,
          })
        : null;

    return {
      chain,
      tokenAddress: address,
      observedAt: new Date(),
      holderCount: countHolders(holdings),
      holderCountCapped: false,
      top10Pct: concentration?.pct ?? null,
      method: 'rpc_getProgramAccounts',
      topHolders: concentration?.top ?? [],
    };
  }
}
