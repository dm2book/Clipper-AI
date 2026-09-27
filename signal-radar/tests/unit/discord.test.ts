import { describe, expect, it } from 'vitest';
import type { AlertPayload } from '../../src/core/alerts.js';
import { DISCLAIMER, looksSpoofed, renderAlert, sanitize } from '../../src/providers/discord/formatter.js';
import { DiscordWebhookProvider } from '../../src/providers/discord/webhook.js';
import { scriptedFetch, testHttp } from '../support/http.js';

const ADDRESS = 'Token11111111111111111111111111111111111111';

/** SYNTHETIC alert payload for rendering tests. */
function payload(overrides: Partial<AlertPayload> = {}): AlertPayload {
  return {
    type: 'NEW_TOKEN',
    chain: 'solana',
    tokenAddress: ADDRESS,
    symbol: 'TST',
    name: 'Test Token',
    decidedAt: '2026-01-01T12:05:00.000Z',
    age: { seconds: 312, source: 'onchain' },
    market: {
      source: 'dexscreener',
      observedAt: '2026-01-01T12:04:55.000Z',
      dexId: 'raydium',
      pairAddress: 'Pair',
      priceUsd: 0.00002,
      liquidityUsd: 42_000,
      marketCapUsd: 240_000,
      fdvUsd: 250_000,
      volumeM5Usd: 15_000,
      volumeH1Usd: 90_000,
      buysM5: 40,
      sellsM5: 12,
    },
    holders: { count: 84, capped: false, top10Pct: 23.4, observedAt: '2026-01-01T12:04:00.000Z' },
    safety: { verdict: 'WARN', reasons: ['rugcheck: Mutable metadata (warn)'], providers: [{ provider: 'rugcheck', verdict: 'WARN' }] },
    score: null,
    gates: [],
    ...overrides,
  };
}

describe('sanitize', () => {
  it('neutralises mentions, markdown, links and invisible characters', () => {
    expect(sanitize('@everyone')).toBe('@​everyone');
    expect(sanitize('**BOLD** [click](https://evil.example)')).not.toMatch(/(?<!\\)[*[\]()]/);
    expect(sanitize('https://evil.example')).toContain('https\\:');
    expect(sanitize('a​b‮c\u0000d')).toBe('abcd');
    expect(sanitize('x'.repeat(100), 10)).toHaveLength(10);
    expect(sanitize(null)).toBe('—');
  });

  it('flags lookalike tickers', () => {
    expect(looksSpoofed('USDC')).toBe(false);
    expect(looksSpoofed('USDС')).toBe(true); // Cyrillic С
  });
});

describe('renderAlert', () => {
  it('disables mentions, carries the disclaimer and fixed links only', () => {
    const body = renderAlert(payload({ symbol: '@here', name: '[free](https://evil.example)' }), 'Signal Radar');
    expect(body.allowed_mentions).toEqual({ parse: [] });
    const embed = body.embeds[0];
    expect(embed.footer.text).toBe(DISCLAIMER);
    expect(embed.url).toBe(`https://dexscreener.com/solana/${ADDRESS}`);
    const text = JSON.stringify(embed);
    expect(text).not.toContain('https://evil.example');
    expect(text).toContain('@​here');
    expect(embed.fields.find((f) => f.name === 'Leeftijd')?.value).toBe('5m 12s (on-chain)');
    expect(embed.fields.find((f) => f.name === 'Holders')?.value).toBe('84 · top-10 23.4%');
  });

  it('explains a momentum score component by component', () => {
    const body = renderAlert(
      payload({
        type: 'MOMENTUM',
        score: {
          value: 72.5,
          confidence: 0.85,
          version: 'v1',
          components: [
            { type: 'volume_spike', label: 'Volume-spike 5m', points: 25, weight: 25, available: true, detail: '$15k vs $1.9k (8.0×)' },
            { type: 'holder_growth', label: 'Holdergroei', points: 0, weight: 15, available: false, detail: 'geen data' },
          ],
          penalties: [{ reason: 'top-10 bezit ≥ 40%', points: 15 }],
        },
      }),
      'Signal Radar',
    );
    const embed = body.embeds[0];
    expect(embed.title).toContain('score 73/100');
    const breakdown = embed.fields.find((f) => f.name === 'Score-opbouw')!.value;
    expect(breakdown).toContain('Volume-spike 5m');
    expect(breakdown).toContain('25.0/25');
    expect(breakdown).toContain('Geen data: Holdergroei');
    expect(breakdown).toContain('−15');
  });

  it('stays within Discord limits with hostile input', () => {
    const body = renderAlert(payload({ safety: { verdict: 'FAIL', reasons: Array(50).fill('x'.repeat(2000)), providers: [] } }), 'u');
    const e = body.embeds[0];
    const total = e.title.length + e.footer.text.length + e.fields.reduce((a, f) => a + f.name.length + f.value.length, 0);
    expect(total).toBeLessThanOrEqual(6000);
    expect(e.fields.every((f) => f.value.length <= 1024)).toBe(true);
  });

  it('does not build links from an invalid address', () => {
    const e = renderAlert(payload({ tokenAddress: 'javascript:alert(1)' }), 'u').embeds[0];
    expect(e.url).toBeUndefined();
    expect(JSON.stringify(e)).not.toContain('javascript');
  });
});

describe('DiscordWebhookProvider', () => {
  it('posts with wait=true, retries a 429 and returns the message id', async () => {
    const { fetchFn, calls } = scriptedFetch([
      { status: 429, body: { message: 'You are being rate limited.', retry_after: 0.001, global: false } },
      { status: 200, body: { id: '1234567890', channel_id: '1' } },
    ]);
    const p = new DiscordWebhookProvider(testHttp(fetchFn), 'https://discord.com/api/webhooks/1/tok', 'Signal Radar');
    await expect(p.send(payload())).resolves.toEqual({ messageId: '1234567890' });
    expect(calls).toHaveLength(2);
    expect(calls[0]!.url).toBe('https://discord.com/api/webhooks/1/tok?wait=true');
    expect(calls[0]!.method).toBe('POST');
    expect(calls[0]!.body).toMatchObject({ allowed_mentions: { parse: [] }, username: 'Signal Radar' });
  });
});
