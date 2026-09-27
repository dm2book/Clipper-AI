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
});
