/**
 * MarketDataProvider backed by the DexScreener public API.
 *
 * Endpoint (docs.dexscreener.com/api/reference):
 *   GET /tokens/v1/{chainId}/{tokenAddresses}   — up to 30 comma-separated addresses,
 *                                                 300 requests/minute, no API key
 * Returns an array of pairs. The fields read here (baseToken, quoteToken,
 * priceUsd, liquidity.usd, fdv, marketCap, volume.*, txns.*.buys/sells,
 * priceChange.*, pairCreatedAt) are the documented pair fields.
 *
 * STATUS: endpoint and field names are taken from the documentation; this
 * mapper has not yet been run against a recorded live response
 * (`npm run record-fixtures`, docs/PROVIDERS.md). Every field is validated
 * and optional, so a change shows up as missing data + a schema warning,
 * never as a wrong number.
 */
import { z } from 'zod';
import type { MarketSnapshot } from '../../core/marketSnapshot.js';
import type { Chain } from '../../core/types.js';
import type { HttpClient } from '../../infra/http.js';
import type { Logger } from '../../infra/logger.js';
import type { MarketDataProvider } from '../interfaces.js';

const CHAIN_IDS: Record<Chain, string> = { solana: 'solana' };
const MAX_ADDRESSES = 30;

const nullableNumber = z.number().nullable().optional();
const counts = z.looseObject({ buys: nullableNumber, sells: nullableNumber }).nullable().optional();

export const pairSchema = z.looseObject({
  chainId: z.string(),
  dexId: z.string().nullable().optional(),
  pairAddress: z.string(),
  baseToken: z.looseObject({
    address: z.string(),
    name: z.string().nullable().optional(),
    symbol: z.string().nullable().optional(),
  }),
  quoteToken: z.looseObject({ address: z.string() }),
  priceUsd: z.string().nullable().optional(),
  liquidity: z.looseObject({ usd: nullableNumber }).nullable().optional(),
  fdv: nullableNumber,
  marketCap: nullableNumber,
  volume: z.looseObject({ m5: nullableNumber, h1: nullableNumber, h6: nullableNumber, h24: nullableNumber }).nullable().optional(),
  txns: z.looseObject({ m5: counts, h1: counts, h24: counts }).nullable().optional(),
  priceChange: z.looseObject({ m5: nullableNumber, h1: nullableNumber, h24: nullableNumber }).nullable().optional(),
  pairCreatedAt: nullableNumber,
});
export type DexScreenerPair = z.infer<typeof pairSchema>;

const responseSchema = z.array(z.unknown());

const n = (v: number | null | undefined): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);

/** Pure: pair -> snapshot. Only called for pairs where the token is the BASE token. */
export function pairToSnapshot(pair: DexScreenerPair, chain: Chain, observedAt: Date): MarketSnapshot {
  const price = pair.priceUsd === null || pair.priceUsd === undefined ? null : Number(pair.priceUsd);
  const window = (w: 'm5' | 'h1' | 'h24') => ({ buys: n(pair.txns?.[w]?.buys), sells: n(pair.txns?.[w]?.sells) });
  return {
    chain,
    tokenAddress: pair.baseToken.address,
    pairAddress: pair.pairAddress,
    dexId: pair.dexId ?? null,
    quoteAddress: pair.quoteToken.address,
    source: 'dexscreener',
    observedAt,
    priceUsd: price !== null && Number.isFinite(price) ? price : null,
    liquidityUsd: n(pair.liquidity?.usd),
    fdvUsd: n(pair.fdv),
    marketCapUsd: n(pair.marketCap),
    volumeUsd: { m5: n(pair.volume?.m5), h1: n(pair.volume?.h1), h6: n(pair.volume?.h6), h24: n(pair.volume?.h24) },
    txns: { m5: window('m5'), h1: window('h1'), h24: window('h24') },
    priceChangePct: { m5: n(pair.priceChange?.m5), h1: n(pair.priceChange?.h1), h24: n(pair.priceChange?.h24) },
    pairCreatedAt: typeof pair.pairCreatedAt === 'number' ? new Date(pair.pairCreatedAt) : null,
    symbol: pair.baseToken.symbol ?? null,
    name: pair.baseToken.name ?? null,
  };
}

/**
 * Pure: pick, per requested token, its most liquid pair where it is the base
 * token. Pairs where it is only the quote token are skipped: their price
 * refers to the other token.
 */
export function selectPairs(
  pairs: readonly DexScreenerPair[],
  chainId: string,
  requested: ReadonlySet<string>,
): Map<string, DexScreenerPair> {
  const best = new Map<string, DexScreenerPair>();
  for (const p of pairs) {
    if (p.chainId !== chainId || !requested.has(p.baseToken.address)) continue;
    const current = best.get(p.baseToken.address);
    if (!current || (n(p.liquidity?.usd) ?? -1) > (n(current.liquidity?.usd) ?? -1)) best.set(p.baseToken.address, p);
  }
  return best;
}

export class DexScreenerMarketDataProvider implements MarketDataProvider {
  readonly name = 'dexscreener';
  readonly maxBatchSize = MAX_ADDRESSES;

  constructor(
    private readonly http: HttpClient,
    private readonly baseUrl: string,
    private readonly logger: Logger,
  ) {}

  async getSnapshots(chain: Chain, addresses: readonly string[], signal?: AbortSignal): Promise<Map<string, MarketSnapshot>> {
    const out = new Map<string, MarketSnapshot>();
    const chainId = CHAIN_IDS[chain];
    for (let i = 0; i < addresses.length; i += MAX_ADDRESSES) {
      const batch = addresses.slice(i, i + MAX_ADDRESSES);
      const raw = await this.http.requestJson({
        url: `${this.baseUrl}/tokens/v1/${chainId}/${batch.map(encodeURIComponent).join(',')}`,
        endpoint: 'tokens',
        schema: responseSchema,
        ...(signal ? { signal } : {}),
      });
      const observedAt = new Date();
      const pairs: DexScreenerPair[] = [];
      let invalid = 0;
      for (const item of raw) {
        const parsed = pairSchema.safeParse(item);
        if (parsed.success) pairs.push(parsed.data);
        else invalid++;
      }
      if (invalid) this.logger.warn({ provider: this.name, invalid }, 'skipped pairs that failed validation');
      for (const [address, pair] of selectPairs(pairs, chainId, new Set(batch))) {
        out.set(address, pairToSnapshot(pair, chain, observedAt));
      }
    }
    return out;
  }
}
