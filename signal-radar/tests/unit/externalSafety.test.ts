import { describe, expect, it } from 'vitest';
import { NotConfiguredError, NotFoundError, SchemaError, TransientError } from '../../src/core/errors.js';
import { GoPlusSafetyProvider, mapEntry, readStatus } from '../../src/providers/goplus/safety.js';
import { RugCheckSafetyProvider, mapSummary } from '../../src/providers/rugcheck/safety.js';
import { UnconfiguredWalletProvider } from '../../src/providers/wallet/unconfigured.js';
import { scriptedFetch, testHttp } from '../support/http.js';

const MINT = 'Mint1111111111111111111111111111111111111111';

// All provider bodies below are SYNTHETIC, shaped after the documented fields.

describe('RugCheck', () => {
  it('bases the verdict on risk levels only', () => {
    expect(mapSummary({ risks: [{ name: 'Freeze Authority still enabled', level: 'danger' }] }).verdict).toBe('FAIL');
    expect(mapSummary({ risks: [{ name: 'Mutable metadata', level: 'warn' }] })).toMatchObject({
      verdict: 'WARN',
      reasons: ['Mutable metadata (warn)'],
    });
    expect(mapSummary({ risks: [], score_normalised: 99 })).toMatchObject({ verdict: 'PASS', providerScore: 99 });
    expect(mapSummary({ score: 1 }).verdict).toBe('UNKNOWN');
  });

  it('calls the summary endpoint with the API key and maps 404 to NotFound', async () => {
    const { fetchFn, calls } = scriptedFetch([{ status: 200, body: { score_normalised: 3, risks: [] } }, { status: 404 }]);
    const p = new RugCheckSafetyProvider(testHttp(fetchFn), 'https://api.rugcheck.xyz/v1', 'key-123');
    const report = await p.check('solana', MINT);
    expect(report).toMatchObject({ provider: 'rugcheck', kind: 'external', verdict: 'PASS' });
    expect(calls[0]!.url).toBe(`https://api.rugcheck.xyz/v1/tokens/${MINT}/report/summary`);
    expect(calls[0]!.headers['x-api-key']).toBe('key-123');
    await expect(p.check('solana', MINT)).rejects.toBeInstanceOf(NotFoundError);
  });
});

describe('GoPlus', () => {
  it('reads status flags and treats anything else as unknown', () => {
    expect(readStatus({ status: '1', authority: [] })).toBe(true);
    expect(readStatus({ status: '0' })).toBe(false);
    expect(readStatus({ status: 1 })).toBe(true);
    expect(readStatus('1')).toBeNull();
    expect(readStatus(undefined)).toBeNull();
  });

  it('maps risk fields to a verdict', () => {
    expect(mapEntry({ mintable: { status: '0' }, freezable: { status: '0' } }).verdict).toBe('PASS');
    expect(mapEntry({ mintable: { status: '1' }, freezable: { status: '0' } }).verdict).toBe('FAIL');
    expect(mapEntry({ mintable: { status: '0' }, metadata_mutable: { status: '1' } }).verdict).toBe('WARN');
    expect(mapEntry({}).verdict).toBe('UNKNOWN');
  });

  it('sends one address, checks the envelope, and never passes unreadable data', async () => {
    const { fetchFn, calls } = scriptedFetch([
      { status: 200, body: { code: 1, message: 'OK', result: { [MINT]: { mintable: { status: '0' }, freezable: { status: '0' } } } } },
      { status: 200, body: { code: 4029, message: 'too many requests' } },
      { status: 200, body: { code: 1, message: 'OK', result: { [MINT]: { something_else: true } } } },
      { status: 200, body: { code: 1, message: 'OK', result: {} } },
    ]);
    const p = new GoPlusSafetyProvider(testHttp(fetchFn), 'https://api.gopluslabs.io', null);
    expect((await p.check('solana', MINT)).verdict).toBe('PASS');
    expect(calls[0]!.url).toBe(`https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses=${MINT}`);
    await expect(p.check('solana', MINT)).rejects.toBeInstanceOf(TransientError);
    await expect(p.check('solana', MINT)).rejects.toBeInstanceOf(SchemaError);
    expect((await p.check('solana', MINT)).verdict).toBe('UNKNOWN'); // token not known to GoPlus
  });
});

describe('WalletProvider placeholder', () => {
  it('fails loudly instead of returning empty history', async () => {
    const w = new UnconfiguredWalletProvider();
    expect(w.available).toBe(false);
    await expect(w.getHistory()).rejects.toBeInstanceOf(NotConfiguredError);
  });
});
