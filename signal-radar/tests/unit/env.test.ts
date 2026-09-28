import { describe, expect, it } from 'vitest';
import { ConfigError, loadConfig } from '../../src/config/env.js';
import { baseEnv } from '../support/env.js';

describe('loadConfig', () => {
  it('applies the documented defaults, including the requested alert filters', () => {
    const c = loadConfig(baseEnv());
    expect(c.alerts.newToken).toEqual({ maxAgeMinutes: 10, minLiquidityUsd: 20_000, minHolders: 50 });
    expect(c.momentum.defaults.minScore).toBe(60);
    expect(c.solana.discoverySources).toEqual(['raydium_amm_v4', 'raydium_cpmm', 'pumpswap']);
    expect(c.redisUrl).toBeNull();
    expect(c.discord.webhookUrl).toBeNull();
    expect(c.rugcheck.enabled).toBe(true);
    expect(c.tiers.snapshotIntervalSec).toEqual({ HOT: 15, WARM: 60, COOL: 300 });
  });

  it('coerces numbers and booleans from strings', () => {
    const c = loadConfig(
      baseEnv({ NEW_TOKEN_MIN_HOLDERS: '75', RUGCHECK_ENABLED: 'false', DEXSCREENER_REQUESTS_PER_MINUTE: '60' }),
    );
    expect(c.alerts.newToken.minHolders).toBe(75);
    expect(c.rugcheck.enabled).toBe(false);
    expect(c.dexscreener.requestsPerMinute).toBe(60);
  });

  it('treats empty values as unset', () => {
    const c = loadConfig(baseEnv({ REDIS_URL: '', DISCORD_WEBHOOK_URL: '  ' }));
    expect(c.redisUrl).toBeNull();
    expect(c.discord.webhookUrl).toBeNull();
  });

  it('reports every missing or invalid variable by name without echoing values', () => {
    const secret = 'wss://mainnet.example/?api-key=SUPER-SECRET';
    try {
      loadConfig({ SOLANA_RPC_HTTP_URL: secret, DEXSCREENER_REQUESTS_PER_MINUTE: '9999' });
      expect.unreachable();
    } catch (err) {
      expect(err).toBeInstanceOf(ConfigError);
      const msg = (err as ConfigError).message;
      expect(msg).toContain('DATABASE_URL');
      expect(msg).toContain('SOLANA_RPC_WS_URL');
      expect(msg).toContain('SOLANA_RPC_HTTP_URL');
      expect(msg).toContain('DEXSCREENER_REQUESTS_PER_MINUTE');
      expect(msg).not.toContain('SUPER-SECRET');
    }
  });

  it('only accepts real Discord webhook URLs', () => {
    expect(() => loadConfig(baseEnv({ DISCORD_WEBHOOK_URL: 'https://evil.example/api/webhooks/1/abc' }))).toThrow(
      /DISCORD_WEBHOOK_URL/,
    );
    const c = loadConfig(baseEnv({ DISCORD_WEBHOOK_URL: 'https://discord.com/api/webhooks/123456/abc-DEF_1' }));
    expect(c.discord.webhookUrl).toBe('https://discord.com/api/webhooks/123456/abc-DEF_1');
  });

  it('parses custom discovery sources from JSON', () => {
    const c = loadConfig(
      baseEnv({
        DISCOVERY_CUSTOM_SOURCES: JSON.stringify([
          { name: 'my_dex', programId: '11111111111111111111111111111111', logPatterns: ['Instruction: InitPool'] },
        ]),
      }),
    );
    expect(c.solana.customSources[0]?.name).toBe('my_dex');
    expect(() => loadConfig(baseEnv({ DISCOVERY_CUSTOM_SOURCES: '{not json' }))).toThrow(/DISCOVERY_CUSTOM_SOURCES/);
  });

  it('rejects inconsistent tier boundaries', () => {
    expect(() => loadConfig(baseEnv({ TIER_HOT_MINUTES: '60', TIER_WARM_MINUTES: '30' }))).toThrow(/TIER_WARM_MINUTES/);
  });

  it('maps every Wallet Intelligence criterion and threshold from the environment', () => {
    const w = 'Wa11et1111111111111111111111111111111111111';
    const c = loadConfig(
      baseEnv({
        WALLET_MIN_CLOSED_POSITIONS: '40',
        WALLET_MIN_WIN_RATE: '0.6',
        WALLET_MIN_AVG_RETURN_PCT: '-5',
        WHALE_MIN_TRADE_USD: '50000',
        CLUSTER_WINDOW_MINUTES: '5',
        CLUSTER_MIN_WALLETS: '4',
        TRACKED_WALLETS: `${w}, ${w}`,
        WALLET_IGNORE_LIST: w,
      }),
    );
    expect(c.wallets.criteria).toMatchObject({ minClosedPositions: 40, minWinRate: 0.6, minAvgReturnPct: -5 });
    expect(c.wallets.activity).toMatchObject({ whaleMinTradeUsd: 50_000, clusterWindowMinutes: 5, clusterMinWallets: 4, ignoredWallets: [w] });
    expect(c.wallets.manualWallets).toEqual([w]);
  });

  it('rejects invalid wallet settings', () => {
    expect(() => loadConfig(baseEnv({ TRACKED_WALLETS: 'not-an-address' }))).toThrow(/TRACKED_WALLETS/);
    expect(() => loadConfig(baseEnv({ WALLET_MIN_WIN_RATE: '1.5' }))).toThrow(/WALLET_MIN_WIN_RATE/);
    // a per-wallet cap below 1/N cannot be satisfied
    expect(() => loadConfig(baseEnv({ CLUSTER_MIN_WALLETS: '2', CLUSTER_MAX_WALLET_SHARE: '0.3' }))).toThrow(/CLUSTER_MAX_WALLET_SHARE/);
    expect(() => loadConfig(baseEnv({ WALLET_EVENT_RETENTION_DAYS: '30' }))).toThrow(/WALLET_EVENT_RETENTION_DAYS/);
  });
});
