import { describe, expect, it } from 'vitest';
import { silentLogger } from '../../src/infra/logger.js';
import {
  DexScreenerMarketDataProvider,
  pairSchema,
  pairToSnapshot,
  selectPairs,
  type DexScreenerPair,
} from '../../src/providers/dexscreener/marketData.js';
import { scriptedFetch, testHttp } from '../support/http.js';

const TOKEN = 'Token11111111111111111111111111111111111111';
const SOL = 'So11111111111111111111111111111111111111112';

/**
 * SYNTHETIC pair object built from the documented DexScreener pair fields.
 * Not a recorded response: values are arbitrary test numbers.
 */
function pair(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    chainId: 'solana',
    dexId: 'raydium',
    url: 'https://dexscreener.com/solana/pair',
    pairAddress: 'PairA',
    baseToken: { address: TOKEN, name: 'Test', symbol: 'TST' },
    quoteToken: { address: SOL, name: 'Wrapped SOL', symbol: 'SOL' },
    priceNative: '0.0000001',
    priceUsd: '0.00002',
    txns: { m5: { buys: 40, sells: 12 }, h1: { buys: 300, sells: 120 }, h6: { buys: 1, sells: 1 }, h24: { buys: 1, sells: 1 } },
    volume: { m5: 15000, h1: 90000, h6: 90000, h24: 90000 },
    priceChange: { m5: 12.5, h1: 80 },
    liquidity: { usd: 42000, base: 1, quote: 1 },
    fdv: 250000,
    marketCap: 240000,
    pairCreatedAt: 1_767_268_800_000,
    ...overrides,
  };
}
const parse = (p: Record<string, unknown>): DexScreenerPair => pairSchema.parse(p);

describe('DexScreener mapping', () => {
  it('maps the documented pair fields to a snapshot', () => {
    const s = pairToSnapshot(parse(pair()), 'solana', new Date('2026-01-01T12:05:00Z'));
    expect(s).toMatchObject({
      tokenAddress: TOKEN,
      pairAddress: 'PairA',
      dexId: 'raydium',
      quoteAddress: SOL,
      source: 'dexscreener',
      priceUsd: 0.00002,
      liquidityUsd: 42000,
      fdvUsd: 250000,
      marketCapUsd: 240000,
      volumeUsd: { m5: 15000, h1: 90000, h6: 90000, h24: 90000 },
      txns: { m5: { buys: 40, sells: 12 }, h1: { buys: 300, sells: 120 } },
      priceChangePct: { m5: 12.5, h1: 80, h24: null },
      symbol: 'TST',
    });
    expect(s.pairCreatedAt?.toISOString()).toBe('2026-01-01T12:00:00.000Z');
  });

  it('keeps missing fields as unknown instead of zero', () => {
    const s = pairToSnapshot(
      parse(pair({ liquidity: undefined, marketCap: undefined, volume: { h24: 5 }, txns: undefined, priceUsd: undefined })),
      'solana',
      new Date(),
    );
    expect(s.liquidityUsd).toBeNull();
    expect(s.marketCapUsd).toBeNull();
    expect(s.priceUsd).toBeNull();
    expect(s.volumeUsd.m5).toBeNull();
    expect(s.txns.m5).toEqual({ buys: null, sells: null });
  });

  it('picks the most liquid pair where the token is the base token', () => {
    const pairs = [
      parse(pair({ pairAddress: 'small', liquidity: { usd: 1_000 } })),
      parse(pair({ pairAddress: 'big', liquidity: { usd: 90_000 } })),
      parse(pair({ pairAddress: 'as-quote', baseToken: { address: SOL }, quoteToken: { address: TOKEN }, liquidity: { usd: 9e9 } })),
      parse(pair({ pairAddress: 'other-chain', chainId: 'base', liquidity: { usd: 9e9 } })),
    ];
    const best = selectPairs(pairs, 'solana', new Set([TOKEN]));
    expect(best.get(TOKEN)?.pairAddress).toBe('big');
    expect(selectPairs(pairs.slice(2), 'solana', new Set([TOKEN])).size).toBe(0);
  });
});

describe('DexScreenerMarketDataProvider', () => {
  it('batches at most 30 addresses per request and skips invalid pairs', async () => {
    const addresses = Array.from({ length: 31 }, (_, i) => `Addr${i.toString().padStart(40, '0')}`);
    const { fetchFn, calls } = scriptedFetch((call) => {
      const requested = decodeURIComponent(call.url.split('/tokens/v1/solana/')[1]!).split(',');
      return {
        status: 200,
        body: [
          ...requested.slice(0, 1).map((a) => pair({ baseToken: { address: a, symbol: 'X' } })),
          { chainId: 'solana', broken: true },
        ],
      };
    });
    const provider = new DexScreenerMarketDataProvider(testHttp(fetchFn), 'https://api.dexscreener.com', silentLogger());
    const result = await provider.getSnapshots('solana', addresses);
    expect(calls).toHaveLength(2);
    expect(calls[0]!.url.split(',')).toHaveLength(30);
    expect(calls[1]!.url).toContain(addresses[30]);
    expect([...result.keys()]).toEqual([addresses[0], addresses[30]]);
  });
});
