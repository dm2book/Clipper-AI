import { EventEmitter } from 'node:events';
import { describe, expect, it } from 'vitest';
import type { DiscoveredToken } from '../../src/core/token.js';
import { silentLogger } from '../../src/infra/logger.js';
import { KNOWN_SOURCES, QUOTE_MINTS, WSOL_MINT, resolveSources } from '../../src/providers/solana/constants.js';
import { BoundedQueue, SolanaLogsDiscoveryProvider, type SocketLike } from '../../src/providers/solana/discovery.js';
import type { RpcTransaction } from '../../src/providers/solana/rpcClient.js';
import { blockTimeOf, extractNewMint } from '../../src/providers/solana/txParse.js';

const RAY = KNOWN_SOURCES.raydium_amm_v4!;
const NEW_MINT = 'NewMint111111111111111111111111111111111111';

/** SYNTHETIC notification in the documented logsSubscribe shape. */
function notification(signature: string, logs: string[], err: unknown = null): string {
  return JSON.stringify({
    jsonrpc: '2.0',
    method: 'logsNotification',
    params: { result: { context: { slot: 1 }, value: { signature, err, logs } }, subscription: 7 },
  });
}

/** SYNTHETIC getTransaction result (jsonParsed) with the fields the parser reads. */
function launchTx(mints: string[], err: unknown = null, blockTime: number | null = 1_767_268_800): RpcTransaction {
  return {
    slot: 1,
    blockTime,
    meta: { err, postTokenBalances: mints.map((mint) => ({ mint, owner: 'pool', uiTokenAmount: { amount: '1' } })) },
  };
}

function provider(overrides: Partial<ConstructorParameters<typeof SolanaLogsDiscoveryProvider>[0]> = {}) {
  return new SolanaLogsDiscoveryProvider({
    wsUrl: 'ws://127.0.0.1:9',
    rpc: { getTransaction: async () => launchTx([WSOL_MINT, NEW_MINT]) },
    sources: [RAY],
    quoteMints: QUOTE_MINTS,
    resolverConcurrency: 1,
    logger: silentLogger(),
    fetchDelaysMs: [0, 0, 0],
    queueSize: 2,
    ...overrides,
  });
}

describe('extractNewMint', () => {
  it('returns the single non-quote mint of a successful transaction', () => {
    expect(extractNewMint(launchTx([WSOL_MINT, NEW_MINT, NEW_MINT]), QUOTE_MINTS)).toBe(NEW_MINT);
    expect(blockTimeOf(launchTx([]))?.toISOString()).toBe('2026-01-01T12:00:00.000Z');
  });

  it('refuses to guess', () => {
    expect(extractNewMint(launchTx([WSOL_MINT, NEW_MINT], { InstructionError: [0, 'x'] }), QUOTE_MINTS)).toBeNull();
    expect(extractNewMint(launchTx([NEW_MINT, 'Other11111111111111111111111111111111111111']), QUOTE_MINTS)).toBeNull();
    expect(extractNewMint(launchTx([WSOL_MINT]), QUOTE_MINTS)).toBeNull();
    expect(extractNewMint(null, QUOTE_MINTS)).toBeNull();
  });
});

describe('SolanaLogsDiscoveryProvider.handleMessage', () => {
  it('queues each matching launch once and ignores everything else', () => {
    const p = provider();
    expect(p.handleMessage(RAY, notification('s1', ['Program log: initialize2: InitializeInstruction2 {...}']))).toBe(true);
    p.handleMessage(RAY, notification('s1', ['initialize2'])); // duplicate
    p.handleMessage(RAY, notification('s2', ['Program log: ray_log: swap'])); // not a launch
    p.handleMessage(RAY, notification('s3', ['initialize2'], { InstructionError: [0, 'x'] })); // failed tx
    p.handleMessage(RAY, JSON.stringify({ jsonrpc: '2.0', result: 7, id: 1 })); // subscription ack
    p.handleMessage(RAY, 'not json');
    expect(p.health().details).toMatchObject({ queued: 1, dropped: 0 });
    expect(p.health().lastEventAt).not.toBeNull();
  });

  it('drops (and counts) launches when the queue is full', () => {
    const p = provider();
    for (const s of ['a', 'b', 'c']) p.handleMessage(RAY, notification(s, ['initialize2']));
    expect(p.health().details).toMatchObject({ queued: 2, dropped: 1 });
  });

  it('signals a failed subscription so the socket is recycled', () => {
    const p = provider();
    expect(p.handleMessage(RAY, JSON.stringify({ jsonrpc: '2.0', id: 1, error: { code: -32601, message: 'no' } }))).toBe(false);
  });
});

describe('SolanaLogsDiscoveryProvider.resolve', () => {
  it('retries until the transaction is visible', async () => {
    let calls = 0;
    const p = provider({
      rpc: {
        getTransaction: async () => (++calls < 3 ? null : launchTx([WSOL_MINT, NEW_MINT])),
      },
    });
    const token = await p.resolve({ source: 'raydium_amm_v4', signature: 'sig' }, new AbortController().signal);
    expect(calls).toBe(3);
    expect(token).toEqual({
      chain: 'solana',
      address: NEW_MINT,
      createdAtChain: new Date('2026-01-01T12:00:00Z'),
      source: 'raydium_amm_v4',
      reference: 'sig',
    });
  });

  it('gives up quietly when the transaction never appears', async () => {
    const p = provider({ rpc: { getTransaction: async () => null } });
    expect(await p.resolve({ source: 'x', signature: 'sig' }, new AbortController().signal)).toBeNull();
  });
});

/** A fake socket that records what the provider sends. */
class FakeSocket extends EventEmitter implements SocketLike {
  sent: string[] = [];
  send(data: string) {
    this.sent.push(data);
  }
  ping() {}
  terminate() {
    this.emit('close');
  }
  close() {
    this.emit('close');
  }
}

describe('SolanaLogsDiscoveryProvider lifecycle', () => {
  it('subscribes per program, resolves launches to tokens, and stops cleanly', async () => {
    const sockets: FakeSocket[] = [];
    const found: DiscoveredToken[] = [];
    const p = provider({
      socketFactory: () => {
        const s = new FakeSocket();
        sockets.push(s);
        setImmediate(() => s.emit('open'));
        return s;
      },
    });
    await p.start(async (t) => void found.push(t));
    await new Promise((r) => setImmediate(r));
    expect(p.health().connected).toBe(true);
    const subscribe = JSON.parse(sockets[0]!.sent[0]!);
    expect(subscribe).toMatchObject({ method: 'logsSubscribe', params: [{ mentions: [RAY.programId] }, { commitment: 'confirmed' }] });

    sockets[0]!.emit('message', Buffer.from(notification('launch-1', ['initialize2'])));
    await new Promise((r) => setTimeout(r, 20));
    expect(found.map((t) => t.address)).toEqual([NEW_MINT]);

    await p.stop();
    expect(p.health().connected).toBe(false);
  });
});

describe('BoundedQueue', () => {
  it('hands items to waiters and honours abort', async () => {
    const q = new BoundedQueue<number>(1);
    const ctrl = new AbortController();
    const waiting = q.take(ctrl.signal);
    expect(q.push(1)).toBe(true);
    await expect(waiting).resolves.toBe(1);
    const aborted = q.take(ctrl.signal);
    ctrl.abort();
    await expect(aborted).rejects.toBeDefined();
    expect(q.push(2)).toBe(true);
    expect(q.push(3)).toBe(false);
  });
});

describe('resolveSources', () => {
  it('rejects unknown names and appends custom sources', () => {
    expect(() => resolveSources(['nope'], [])).toThrow(/unknown discovery source/);
    const s = resolveSources(['pumpswap'], [{ name: 'mine', programId: 'P'.repeat(32), logPatterns: ['Init'] }]);
    expect(s.map((x) => x.name)).toEqual(['pumpswap', 'mine']);
  });
});
