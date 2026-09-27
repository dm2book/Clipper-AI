/**
 * Test-only builders. These produce SYNTHETIC domain objects for unit and
 * integration tests; they are not provider responses and not real market data.
 */
import { ed25519 } from '@noble/curves/ed25519.js';
import bs58 from 'bs58';
import type { HolderSnapshot } from '../../src/core/holders.js';
import type { MarketSnapshot } from '../../src/core/marketSnapshot.js';
import type { SafetyReport } from '../../src/core/safety.js';
import type { DiscoveredToken } from '../../src/core/token.js';

/** A fresh, valid (on-curve) Solana address. */
export function randomAddress(): string {
  return bs58.encode(ed25519.getPublicKey(ed25519.utils.randomSecretKey()));
}

export type DeepPartial<T> = { [K in keyof T]?: T[K] extends Date ? T[K] : T[K] extends object ? DeepPartial<T[K]> : T[K] };

export type SnapshotOverrides = DeepPartial<MarketSnapshot>;

export function makeSnapshot(overrides: SnapshotOverrides = {}): MarketSnapshot {
  const base: MarketSnapshot = {
    chain: 'solana',
    tokenAddress: 'Token11111111111111111111111111111111111111',
    pairAddress: 'Pair111111111111111111111111111111111111111',
    dexId: 'raydium',
    quoteAddress: 'So11111111111111111111111111111111111111112',
    source: 'test',
    observedAt: new Date('2026-01-01T12:00:00Z'),
    priceUsd: 0.001,
    liquidityUsd: 30_000,
    fdvUsd: 1_000_000,
    marketCapUsd: 1_000_000,
    volumeUsd: { m5: 1_000, h1: 12_000, h6: null, h24: null },
    txns: { m5: { buys: 10, sells: 10 }, h1: { buys: 100, sells: 100 }, h24: { buys: null, sells: null } },
    priceChangePct: { m5: 0, h1: 0, h24: null },
    pairCreatedAt: null,
    symbol: 'TEST',
    name: 'Test Token',
  };
  return {
    ...base,
    ...overrides,
    volumeUsd: { ...base.volumeUsd, ...overrides.volumeUsd },
    txns: {
      m5: { ...base.txns.m5, ...overrides.txns?.m5 },
      h1: { ...base.txns.h1, ...overrides.txns?.h1 },
      h24: { ...base.txns.h24, ...overrides.txns?.h24 },
    },
    priceChangePct: { ...base.priceChangePct, ...overrides.priceChangePct },
  } as MarketSnapshot;
}

export function makeDiscovered(overrides: Partial<DiscoveredToken> = {}): DiscoveredToken {
  return {
    chain: 'solana',
    address: randomAddress(),
    createdAtChain: new Date(),
    source: 'test',
    reference: 'test-signature',
    ...overrides,
  };
}

export function makeSafety(overrides: Partial<SafetyReport> = {}): SafetyReport {
  return {
    chain: 'solana',
    tokenAddress: 'Token11111111111111111111111111111111111111',
    provider: 'test',
    kind: 'external',
    checkedAt: new Date(),
    verdict: 'PASS',
    flags: {},
    reasons: [],
    providerScore: null,
    raw: null,
    tokenInfo: null,
    ...overrides,
  };
}

export function makeHolders(overrides: Partial<HolderSnapshot> = {}): HolderSnapshot {
  return {
    chain: 'solana',
    tokenAddress: 'Token11111111111111111111111111111111111111',
    observedAt: new Date(),
    holderCount: 120,
    holderCountCapped: false,
    top10Pct: 25,
    method: 'test',
    topHolders: [],
    ...overrides,
  };
}
