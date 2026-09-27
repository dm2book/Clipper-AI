/**
 * Records REAL provider responses for contract tests.
 *
 *   npm run record-fixtures -- <token-address> [<token-address> ...]
 *
 * Run on a machine with network access (the providers' hosts must be
 * reachable). Responses are written unmodified to
 * tests/fixtures/recorded/<provider>/<address>.json together with the request
 * URL (credentials redacted) and the time of recording. API keys are sent as
 * headers/query parameters and never written to disk.
 *
 * The contract tests (tests/unit/recordedFixtures.test.ts) then check every
 * mapper against these real responses. Until you record some, those tests
 * are skipped and the mappers are only verified against the documentation.
 */
import { mkdir, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { redactUrl } from '../src/infra/redact.js';

const OUT = join(dirname(fileURLToPath(import.meta.url)), '..', 'tests', 'fixtures', 'recorded');
const BASE58 = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;

interface Target {
  provider: string;
  url: string;
  init?: RequestInit;
}

function targets(address: string): Target[] {
  const env = process.env;
  const out: Target[] = [
    {
      provider: 'dexscreener',
      url: `${(env.DEXSCREENER_BASE_URL ?? 'https://api.dexscreener.com').replace(/\/+$/, '')}/tokens/v1/solana/${address}`,
    },
    {
      provider: 'rugcheck',
      url: `${(env.RUGCHECK_BASE_URL ?? 'https://api.rugcheck.xyz/v1').replace(/\/+$/, '')}/tokens/${address}/report/summary`,
      ...(env.RUGCHECK_API_KEY ? { init: { headers: { 'X-API-KEY': env.RUGCHECK_API_KEY } } } : {}),
    },
    {
      provider: 'goplus',
      url: `${(env.GOPLUS_BASE_URL ?? 'https://api.gopluslabs.io').replace(/\/+$/, '')}/api/v1/solana/token_security?contract_addresses=${address}`,
      ...(env.GOPLUS_ACCESS_TOKEN ? { init: { headers: { Authorization: env.GOPLUS_ACCESS_TOKEN } } } : {}),
    },
  ];
  if (env.SOLANA_RPC_HTTP_URL) {
    out.push({
      provider: 'solana-rpc',
      url: env.SOLANA_RPC_HTTP_URL,
      init: {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          jsonrpc: '2.0',
          id: 1,
          method: 'getAccountInfo',
          params: [address, { encoding: 'jsonParsed', commitment: 'confirmed' }],
        }),
      },
    });
  }
  return out;
}

async function main(): Promise<void> {
  const addresses = process.argv.slice(2);
  if (!addresses.length || addresses.some((a) => !BASE58.test(a))) {
    console.error('usage: npm run record-fixtures -- <solana-token-address> [...]');
    process.exit(1);
  }
  for (const address of addresses) {
    for (const t of targets(address)) {
      const recordedAt = new Date().toISOString();
      try {
        const res = await fetch(t.url, { ...t.init, signal: AbortSignal.timeout(15_000) });
        const text = await res.text();
        let body: unknown;
        try {
          body = JSON.parse(text);
        } catch {
          body = text;
        }
        const file = join(OUT, t.provider, `${address}.json`);
        await mkdir(dirname(file), { recursive: true });
        await writeFile(
          file,
          `${JSON.stringify({ meta: { provider: t.provider, url: redactUrl(t.url), status: res.status, recordedAt }, body }, null, 2)}\n`,
        );
        console.log(`${t.provider.padEnd(12)} ${res.status} -> ${file}`);
      } catch (err) {
        console.error(`${t.provider.padEnd(12)} failed: ${err instanceof Error ? err.message : String(err)}`);
      }
    }
  }
}

void main();
