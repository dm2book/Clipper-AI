import { describe, expect, it } from 'vitest';
import { loadConfig } from '../../../src/config/env.js';
import { DEFAULT_THRESHOLDS, parseOverrides, resolveThresholds } from '../../../src/momentum/thresholds.js';
import { baseEnv } from '../../support/env.js';

describe('momentum thresholds from the environment', () => {
  it('maps the documented variables', () => {
    const c = loadConfig(
      baseEnv({
        VOLUME_SPIKE_THRESHOLD: '300',
        TX_SPIKE_THRESHOLD: '200',
        MARKET_CAP_CHANGE_THRESHOLD: '20',
        MIN_LIQUIDITY: '25000',
        MIN_HOLDERS: '75',
        MOMENTUM_PRIMARY_WINDOW: '15m',
        MOMENTUM_REQUIRE_TRADE_DATA: 'true',
      }),
    );
    expect(c.momentum.defaults).toMatchObject({
      volumeSpikePct: 300,
      txSpikePct: 200,
      marketCapChangePct: 20,
      minLiquidityUsd: 25_000,
      minHolders: 75,
      primaryWindow: '15m',
      requireTradeData: true,
    });
    expect(c.momentum.overrides).toEqual({});
  });

  it('uses the built-in defaults when nothing is set', () => {
    expect(loadConfig(baseEnv()).momentum.defaults).toEqual(DEFAULT_THRESHOLDS);
  });

  it('rejects invalid values and invalid override JSON at startup', () => {
    expect(() => loadConfig(baseEnv({ MOMENTUM_PRIMARY_WINDOW: '2h' }))).toThrow(/MOMENTUM_PRIMARY_WINDOW/);
    expect(() => loadConfig(baseEnv({ MOMENTUM_THRESHOLD_OVERRIDES: '{oops' }))).toThrow(/MOMENTUM_THRESHOLD_OVERRIDES/);
    expect(() =>
      loadConfig(baseEnv({ MOMENTUM_THRESHOLD_OVERRIDES: JSON.stringify({ solana: { buyShare: 0.95 } }) })),
    ).toThrow(/buyShareFull must exceed buyShare/);
  });
});

describe('per chain / token-type thresholds', () => {
  const overrides = parseOverrides(
    JSON.stringify({
      solana: { minLiquidityUsd: 30_000, weights: { holder_growth: 20 } },
      'solana:pumpswap': { volumeSpikePct: 500, minHolders: 100 },
    }),
  );
  const cfg = { defaults: DEFAULT_THRESHOLDS, overrides };

  it('applies layers from general to specific', () => {
    const r = resolveThresholds(cfg, 'solana', 'pumpswap');
    expect(r.profile).toEqual(['default', 'solana', 'solana:pumpswap']);
    expect(r.thresholds).toMatchObject({ minLiquidityUsd: 30_000, volumeSpikePct: 500, minHolders: 100, txSpikePct: 200 });
    // weights merge per rule instead of replacing the whole map
    expect(r.thresholds.weights).toMatchObject({ holder_growth: 20, volume_spike: 20 });
  });

  it('matches token types case-insensitively and ignores unknown ones', () => {
    expect(resolveThresholds(cfg, 'solana', 'PumpSwap').profile).toContain('solana:pumpswap');
    expect(resolveThresholds(cfg, 'solana', 'raydium').thresholds.volumeSpikePct).toBe(300);
    expect(resolveThresholds(cfg, 'base', null).profile).toEqual(['default']);
  });

  it('never mutates the defaults', () => {
    resolveThresholds(cfg, 'solana', 'pumpswap');
    expect(DEFAULT_THRESHOLDS.volumeSpikePct).toBe(300);
    expect(DEFAULT_THRESHOLDS.weights.holder_growth).toBe(10);
  });

  it('validates override keys and fields', () => {
    expect(() => parseOverrides(JSON.stringify({ 'Solana!': {} }))).toThrow(/Solana!/);
    expect(() => parseOverrides(JSON.stringify({ solana: { volumeSpike: 1 } }))).toThrow();
    expect(() => parseOverrides(JSON.stringify({ solana: { minHolders: -1 } }))).toThrow();
    expect(() => parseOverrides(JSON.stringify({ solana: { weights: { made_up: 5 } } }))).toThrow();
    expect(() =>
      resolveThresholds(
        { defaults: DEFAULT_THRESHOLDS, overrides: parseOverrides(JSON.stringify({ solana: { weights: Object.fromEntries(Object.keys(DEFAULT_THRESHOLDS.weights).map((k) => [k, 0])) } })) },
        'solana',
        null,
      ),
    ).toThrow(/weight/);
  });
});
