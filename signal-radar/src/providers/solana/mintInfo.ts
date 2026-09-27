/**
 * The mint account, read via jsonParsed getAccountInfo. Field names follow
 * the Solana account-decoder output for SPL Token and Token-2022 (including
 * the Token-2022 `extensions` list). Shared, cached, by the on-chain safety
 * check and the holder provider so one enrichment reads it once.
 */
import { NotFoundError, PermanentError } from '../../core/errors.js';
import { cached, type Cache, SingleFlight } from '../../infra/cache.js';
import type { SolanaRpcClient } from './rpcClient.js';

export interface MintExtension {
  extension: string;
  state: Record<string, unknown> | null;
}

export interface MintInfo {
  address: string;
  tokenProgram: string;
  decimals: number;
  supply: string;
  mintAuthority: string | null;
  freezeAuthority: string | null;
  extensions: MintExtension[];
}

/** Pure: jsonParsed account -> MintInfo. Throws PermanentError if it is not a mint. */
export function parseMintAccount(address: string, account: { owner: string; data: unknown }): MintInfo {
  const data = account.data as { parsed?: { type?: unknown; info?: Record<string, unknown> } } | undefined;
  const parsed = data && typeof data === 'object' && !Array.isArray(data) ? data.parsed : undefined;
  if (!parsed || parsed.type !== 'mint' || !parsed.info) {
    throw new PermanentError(`${address} is not a parsed SPL mint account`);
  }
  const info = parsed.info;
  const decimals = Number(info.decimals);
  const supply = typeof info.supply === 'string' && /^\d+$/.test(info.supply) ? info.supply : null;
  if (!Number.isInteger(decimals) || supply === null) {
    throw new PermanentError(`${address}: mint account without valid decimals/supply`);
  }
  const extensions: MintExtension[] = [];
  if (Array.isArray(info.extensions)) {
    for (const e of info.extensions as unknown[]) {
      if (e && typeof e === 'object' && typeof (e as { extension?: unknown }).extension === 'string') {
        const state = (e as { state?: unknown }).state;
        extensions.push({
          extension: (e as { extension: string }).extension,
          state: state && typeof state === 'object' ? (state as Record<string, unknown>) : null,
        });
      }
    }
  }
  const str = (v: unknown) => (typeof v === 'string' && v.length > 0 ? v : null);
  return {
    address,
    tokenProgram: account.owner,
    decimals,
    supply,
    mintAuthority: str(info.mintAuthority),
    freezeAuthority: str(info.freezeAuthority),
    extensions,
  };
}

export class MintInfoLoader {
  private readonly flight = new SingleFlight();

  constructor(
    private readonly rpc: Pick<SolanaRpcClient, 'getParsedAccount'>,
    private readonly cache: Cache,
    private readonly ttlMs = 30_000,
  ) {}

  get(address: string, signal?: AbortSignal): Promise<MintInfo> {
    return cached(this.cache, this.flight, `mint:solana:${address}`, this.ttlMs, async () => {
      const account = await this.rpc.getParsedAccount(address, signal);
      if (!account) throw new NotFoundError(`mint ${address} not found`);
      return parseMintAccount(address, account);
    });
  }
}
