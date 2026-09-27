/**
 * Contract tests against REAL, recorded provider responses
 * (`npm run record-fixtures -- <address>`). Skipped until fixtures exist:
 * until then the mappers are verified only against the documentation.
 */
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { envelopeSchema, mapEntry } from '../../src/providers/goplus/safety.js';
import { pairSchema, pairToSnapshot } from '../../src/providers/dexscreener/marketData.js';
import { mapSummary, summarySchema } from '../../src/providers/rugcheck/safety.js';
import { parseMintAccount } from '../../src/providers/solana/mintInfo.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures', 'recorded');

function recorded(provider: string): { file: string; status: number; body: unknown }[] {
  const dir = join(ROOT, provider);
  if (!existsSync(dir)) return [];
  return readdirSync(dir)
    .filter((f) => f.endsWith('.json'))
    .map((f) => {
      const data = JSON.parse(readFileSync(join(dir, f), 'utf8')) as { meta: { status: number }; body: unknown };
      return { file: f, status: data.meta.status, body: data.body };
    })
    .filter((r) => r.status === 200);
}

const dexscreener = recorded('dexscreener');
const rugcheck = recorded('rugcheck');
const goplus = recorded('goplus');
const rpc = recorded('solana-rpc');

describe.skipIf(!dexscreener.length)('DexScreener mapper vs recorded responses', () => {
  it.each(dexscreener.map((r) => [r.file, r.body] as const))('%s', (_file, body) => {
    expect(Array.isArray(body)).toBe(true);
    for (const item of body as unknown[]) {
      const pair = pairSchema.parse(item);
      const s = pairToSnapshot(pair, 'solana', new Date());
      expect(s.tokenAddress).toBe(pair.baseToken.address);
      if (s.liquidityUsd !== null) expect(s.liquidityUsd).toBeGreaterThanOrEqual(0);
    }
  });
});

describe.skipIf(!rugcheck.length)('RugCheck mapper vs recorded responses', () => {
  it.each(rugcheck.map((r) => [r.file, r.body] as const))('%s', (_file, body) => {
    const summary = summarySchema.parse(body);
    // A real summary must carry a risk list; otherwise the mapper would only ever say UNKNOWN.
    expect(Array.isArray(summary.risks)).toBe(true);
    expect(['PASS', 'WARN', 'FAIL']).toContain(mapSummary(summary).verdict);
  });
});

describe.skipIf(!goplus.length)('GoPlus mapper vs recorded responses', () => {
  it.each(goplus.map((r) => [r.file, r.body] as const))('%s', (file, body) => {
    const envelope = envelopeSchema.parse(body);
    expect(envelope.code).toBe(1);
    const address = file.replace(/\.json$/, '');
    const entry = envelope.result?.[address];
    if (entry !== undefined) {
      // If GoPlus knows the token, at least one expected risk field must be readable.
      expect(mapEntry(entry).verdict).not.toBe('UNKNOWN');
    }
  });
});

describe.skipIf(!rpc.length)('Solana mint parser vs recorded getAccountInfo', () => {
  it.each(rpc.map((r) => [r.file, r.body] as const))('%s', (file, body) => {
    const value = (body as { result?: { value?: { owner: string; data: unknown } | null } }).result?.value;
    if (value) {
      const mint = parseMintAccount(file.replace(/\.json$/, ''), value);
      expect(mint.decimals).toBeGreaterThanOrEqual(0);
    }
  });
});

it('records which contracts are verified', () => {
  // Informational: shows up in the test output.
  expect({ dexscreener: dexscreener.length, rugcheck: rugcheck.length, goplus: goplus.length, rpc: rpc.length }).toBeDefined();
});
