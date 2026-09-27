import { describe, expect, it } from 'vitest';
import { NotFoundError, PermanentError } from '../../src/core/errors.js';
import { MemoryCache } from '../../src/infra/cache.js';
import { INCINERATOR, TOKEN_2022_PROGRAM, TOKEN_PROGRAM } from '../../src/providers/solana/constants.js';
import { SolanaRpcHolderProvider, decodeOwnerAmount } from '../../src/providers/solana/holders.js';
import { MintInfoLoader, parseMintAccount, type MintInfo } from '../../src/providers/solana/mintInfo.js';
import { SolanaOnchainSafetyProvider, flagsFromMint } from '../../src/providers/solana/onchainSafety.js';
import { isOnCurve } from '../../src/providers/solana/pubkey.js';
import { SolanaRpcClient } from '../../src/providers/solana/rpcClient.js';
import { randomAddress } from '../support/factories.js';
import { scriptedFetch, testHttp } from '../support/http.js';

// A program-derived (off-curve) address: the Raydium AMM v4 pool authority.
const PDA = '5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1';
const MINT = 'Mint1111111111111111111111111111111111111111';

/** SYNTHETIC jsonParsed mint account in the Solana account-decoder shape. */
function mintAccount(info: Record<string, unknown>, owner = TOKEN_PROGRAM) {
  return {
    owner,
    data: { program: 'spl-token', parsed: { type: 'mint', info: { decimals: 6, supply: '1000000000', isInitialized: true, ...info } } },
  };
}

function mintInfo(overrides: Partial<MintInfo> = {}): MintInfo {
  return {
    address: MINT,
    tokenProgram: TOKEN_PROGRAM,
    decimals: 6,
    supply: '1000000',
    mintAuthority: null,
    freezeAuthority: null,
    extensions: [],
    ...overrides,
  };
}

describe('isOnCurve', () => {
  it('separates wallets from program-derived addresses', () => {
    expect(isOnCurve(randomAddress())).toBe(true);
    expect(isOnCurve(PDA)).toBe(false);
    expect(isOnCurve(INCINERATOR)).toBe(false);
    expect(isOnCurve('not-base58!')).toBe(false);
  });
});

describe('parseMintAccount', () => {
  it('reads authorities and Token-2022 extensions', () => {
    const m = parseMintAccount(
      MINT,
      mintAccount(
        {
          mintAuthority: null,
          freezeAuthority: 'Freezer111111111111111111111111111111111111',
          extensions: [{ extension: 'transferFeeConfig', state: { newerTransferFee: { transferFeeBasisPoints: 250 } } }],
        },
        TOKEN_2022_PROGRAM,
      ),
    );
    expect(m).toMatchObject({ tokenProgram: TOKEN_2022_PROGRAM, decimals: 6, supply: '1000000000', mintAuthority: null });
    expect(m.freezeAuthority).toMatch(/^Freezer/);
    expect(m.extensions[0]?.extension).toBe('transferFeeConfig');
  });

  it('rejects anything that is not a parsed mint', () => {
    expect(() => parseMintAccount(MINT, { owner: TOKEN_PROGRAM, data: ['AAAA', 'base64'] })).toThrow(PermanentError);
    expect(() =>
      parseMintAccount(MINT, { owner: TOKEN_PROGRAM, data: { parsed: { type: 'account', info: {} } } }),
    ).toThrow(PermanentError);
  });
});

describe('on-chain safety', () => {
  it('passes a plain mint with revoked authorities', () => {
    expect(flagsFromMint(mintInfo())).toMatchObject({ mint_authority_active: false, freeze_authority_active: false });
  });

  it('fails on active authorities and dangerous extensions, warns on a fee', async () => {
    const withFreeze = new SolanaOnchainSafetyProvider({ get: async () => mintInfo({ freezeAuthority: 'F' }) });
    const report = await withFreeze.check('solana', MINT);
    expect(report.verdict).toBe('FAIL');
    expect(report.reasons.join()).toMatch(/freeze authority/);
    expect(report.tokenInfo).toEqual({ decimals: 6, supply: '1000000', tokenProgram: TOKEN_PROGRAM });

    const ext = (extension: string, state: Record<string, unknown> | null) => mintInfo({ extensions: [{ extension, state }] });
    expect(flagsFromMint(ext('permanentDelegate', { delegate: 'D' })).permanent_delegate).toBe(true);
    expect(flagsFromMint(ext('permanentDelegate', { delegate: null })).permanent_delegate).toBe(false);
    expect(flagsFromMint(ext('transferHook', { programId: 'H', authority: null })).transfer_hook).toBe(true);
    expect(flagsFromMint(ext('transferHook', null)).transfer_hook).toBe(true); // unknown state: conservative
    expect(flagsFromMint(ext('defaultAccountState', { accountState: 'frozen' })).default_account_frozen).toBe(true);
    expect(flagsFromMint(ext('defaultAccountState', { accountState: 'initialized' })).default_account_frozen).toBe(false);
    expect(flagsFromMint(ext('pausableConfig', { authority: null, paused: false })).pausable).toBe(false);
    expect(flagsFromMint(ext('transferFeeConfig', { newerTransferFee: { transferFeeBasisPoints: 0 } })).transfer_fee).toBe(false);

    const withFee = new SolanaOnchainSafetyProvider({
      get: async () => ext('transferFeeConfig', { newerTransferFee: { transferFeeBasisPoints: 100 } }),
    });
    expect((await withFee.check('solana', MINT)).verdict).toBe('WARN');
  });
});

describe('SolanaRpcClient', () => {
  it('unwraps results and classifies RPC errors', async () => {
    const { fetchFn, calls } = scriptedFetch([
      { status: 200, body: { jsonrpc: '2.0', id: 1, error: { code: -32005, message: 'Node is behind' } } },
      { status: 200, body: { jsonrpc: '2.0', id: 2, result: { context: { slot: 1 }, value: mintAccount({}) } } },
    ]);
    const rpc = new SolanaRpcClient(testHttp(fetchFn), 'http://rpc');
    // -32005 ("node is behind") arrives inside an HTTP 200 and is retried like a 5xx.
    const account = await rpc.getParsedAccount(MINT);
    expect(account?.owner).toBe(TOKEN_PROGRAM);
    expect(calls).toHaveLength(2);
    expect(calls[0]!.body).toMatchObject({ jsonrpc: '2.0', method: 'getAccountInfo', params: [MINT, { encoding: 'jsonParsed' }] });

    const bad = scriptedFetch([{ status: 200, body: { jsonrpc: '2.0', id: 1, error: { code: -32602, message: 'Invalid param' } } }]);
    await expect(new SolanaRpcClient(testHttp(bad.fetchFn), 'http://rpc').getParsedAccount(MINT)).rejects.toBeInstanceOf(
      PermanentError,
    );
  });
});

describe('MintInfoLoader', () => {
  it('caches the mint account and reports unknown mints', async () => {
    let calls = 0;
    const loader = new MintInfoLoader(
      {
        getParsedAccount: async (address: string) => {
          calls++;
          return address === MINT ? mintAccount({}) : null;
        },
      },
      new MemoryCache(),
    );
    await Promise.all([loader.get(MINT), loader.get(MINT)]);
    await loader.get(MINT);
    expect(calls).toBe(1);
    await expect(loader.get('Missing')).rejects.toBeInstanceOf(NotFoundError);
  });
});

describe('SolanaRpcHolderProvider', () => {
  function slice(owner: string, amount: bigint): string {
    const buf = Buffer.alloc(40);
    Buffer.from(bs58decode(owner)).copy(buf, 0);
    buf.writeBigUInt64LE(amount, 32);
    return buf.toString('base64');
  }

  it('decodes owner/amount slices', () => {
    const owner = randomAddress();
    expect(decodeOwnerAmount(slice(owner, 12345n))).toEqual({ owner, amount: 12345n });
    expect(decodeOwnerAmount('AAAA')).toBeNull();
  });

  it('counts distinct funded owners and excludes the pool from top-10', async () => {
    const wallets = Array.from({ length: 12 }, () => randomAddress());
    const accounts = [
      { owner: PDA, amount: 400_000n }, // pool vault
      { owner: wallets[0]!, amount: 100_000n },
      { owner: wallets[0]!, amount: 50_000n }, // same owner, second account
      ...wallets.slice(1).map((w) => ({ owner: w, amount: 10_000n })),
      { owner: randomAddress(), amount: 0n }, // emptied account
    ];
    let gpaConfig: Record<string, unknown> | undefined;
    const provider = new SolanaRpcHolderProvider({
      mints: { get: async () => mintInfo({ supply: '1000000' }) },
      rpc: {
        getProgramAccounts: async (program, config) => {
          expect(program).toBe(TOKEN_PROGRAM);
          gpaConfig = config;
          return accounts.map((a, i) => ({ pubkey: `acc${i}`, account: { data: [slice(a.owner, a.amount), 'base64'] as [string, string] } }));
        },
        getTokenLargestAccounts: async () => accounts.map((a, i) => ({ address: `acc${i}`, amount: a.amount.toString() })),
        getMultipleParsedAccounts: async (addresses) =>
          addresses.map((addr) => ({ owner: TOKEN_PROGRAM, data: { parsed: { info: { owner: accounts[Number(addr.slice(3))]!.owner } } } })),
      },
    });
    const snap = await provider.getHolders('solana', MINT);
    expect(snap.holderCount).toBe(13); // PDA + 12 wallets; the empty account does not count
    // top-10 without the pool: 150k + 9 × 10k = 240k of 1M
    expect(snap.top10Pct).toBe(24);
    expect(snap.topHolders[0]).toMatchObject({ owner: wallets[0], amount: '150000', pct: 15 });
    expect(gpaConfig).toMatchObject({ filters: [{ memcmp: { offset: 0, bytes: MINT } }, { dataSize: 165 }], dataSlice: { offset: 32, length: 40 } });
  });
});

// Local helper: decode base58 via the same library the code uses.
import bs58 from 'bs58';
function bs58decode(s: string): Uint8Array {
  return bs58.decode(s);
}
