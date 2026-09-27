/**
 * TokenDiscoveryProvider on standard Solana RPC:
 *   logsSubscribe (WebSocket, one per DEX program)
 *     -> log lines match a pool-creation pattern
 *     -> getTransaction(signature)
 *     -> the one non-quote mint in the transaction is the new token
 */
import WebSocket from 'ws';
import { errorMessage, isAbortError } from '../../core/errors.js';
import type { DiscoveredToken } from '../../core/token.js';
import type { Logger } from '../../infra/logger.js';
import type { Metrics } from '../../infra/metrics.js';
import { redactUrl } from '../../infra/redact.js';
import { sleep } from '../../infra/retry.js';
import type { DiscoveryHealth, TokenDiscoveryProvider } from '../interfaces.js';
import type { DiscoverySource } from './constants.js';
import type { SolanaRpcClient } from './rpcClient.js';
import { blockTimeOf, extractNewMint } from './txParse.js';

/** Minimal socket surface, so tests can drive the provider without a network. */
export interface SocketLike {
  on(event: 'open', cb: () => void): unknown;
  on(event: 'message', cb: (data: WebSocket.RawData) => void): unknown;
  on(event: 'pong', cb: () => void): unknown;
  on(event: 'error', cb: (err: Error) => void): unknown;
  on(event: 'close', cb: () => void): unknown;
  send(data: string): void;
  ping(): void;
  terminate(): void;
  close(): void;
}

export interface SolanaLogsDiscoveryOptions {
  wsUrl: string;
  rpc: Pick<SolanaRpcClient, 'getTransaction'>;
  sources: DiscoverySource[];
  quoteMints: ReadonlySet<string>;
  resolverConcurrency: number;
  logger: Logger;
  metrics?: Metrics;
  queueSize?: number;
  socketFactory?: (url: string) => SocketLike;
  /** Delays between getTransaction attempts: "confirmed" can precede visibility. */
  fetchDelaysMs?: number[];
  heartbeatMs?: number;
}

interface PendingLaunch {
  source: string;
  signature: string;
}

/** Bounded FIFO queue; `take` waits for an item. */
export class BoundedQueue<T> {
  private readonly items: T[] = [];
  private readonly waiters: ((item: T) => void)[] = [];

  constructor(readonly capacity: number) {}

  get size(): number {
    return this.items.length;
  }

  /** False when full: dropping new work beats unbounded memory. */
  push(item: T): boolean {
    const waiter = this.waiters.shift();
    if (waiter) {
      waiter(item);
      return true;
    }
    if (this.items.length >= this.capacity) return false;
    this.items.push(item);
    return true;
  }

  take(signal: AbortSignal): Promise<T> {
    const item = this.items.shift();
    if (item !== undefined) return Promise.resolve(item);
    return new Promise((resolve, reject) => {
      if (signal.aborted) return reject(signal.reason);
      const waiter = (value: T) => {
        signal.removeEventListener('abort', onAbort);
        resolve(value);
      };
      const onAbort = () => {
        const i = this.waiters.indexOf(waiter);
        if (i >= 0) this.waiters.splice(i, 1);
        reject(signal.reason);
      };
      this.waiters.push(waiter);
      signal.addEventListener('abort', onAbort, { once: true });
    });
  }
}

export class SolanaLogsDiscoveryProvider implements TokenDiscoveryProvider {
  readonly name = 'solana-logs';
  private readonly queue: BoundedQueue<PendingLaunch>;
  private readonly seen = new Map<string, number>();
  private readonly connected = new Set<string>();
  private readonly sockets = new Set<SocketLike>();
  private readonly tasks: Promise<void>[] = [];
  private abort = new AbortController();
  private handler: ((token: DiscoveredToken) => Promise<void>) | null = null;
  private lastEventAt: Date | null = null;
  private dropped = 0;

  constructor(private readonly opts: SolanaLogsDiscoveryOptions) {
    this.queue = new BoundedQueue(opts.queueSize ?? 1_000);
  }

  async start(handler: (token: DiscoveredToken) => Promise<void>): Promise<void> {
    if (this.handler) throw new Error('discovery already started');
    this.handler = handler;
    this.abort = new AbortController();
    for (const source of this.opts.sources) {
      if (!source.patternVerified) {
        this.opts.logger.warn(
          { source: source.name, patterns: source.logPatterns },
          'discovery log pattern not verified against a recorded launch (see docs/PROVIDERS.md)',
        );
      }
      this.tasks.push(this.runSubscription(source));
    }
    for (let i = 0; i < this.opts.resolverConcurrency; i++) this.tasks.push(this.runResolver());
  }

  async stop(): Promise<void> {
    this.abort.abort();
    for (const ws of this.sockets) ws.terminate();
    await Promise.allSettled(this.tasks);
    this.tasks.length = 0;
    this.handler = null;
  }

  health(): DiscoveryHealth {
    return {
      connected: this.connected.size > 0,
      lastEventAt: this.lastEventAt,
      details: { subscriptions: [...this.connected], queued: this.queue.size, dropped: this.dropped },
    };
  }

  /**
   * Handles one WebSocket message. Returns false when the subscription itself
   * failed (the caller then closes the socket, which triggers a backed-off
   * reconnect). Public for tests.
   */
  handleMessage(source: DiscoverySource, raw: string): boolean {
    let msg: unknown;
    try {
      msg = JSON.parse(raw);
    } catch {
      this.opts.logger.warn({ source: source.name }, 'discarding non-JSON websocket message');
      return true;
    }
    if (!msg || typeof msg !== 'object') return true;
    const m = msg as { method?: string; error?: unknown; result?: unknown; params?: { result?: { value?: unknown } } };
    if (m.error) {
      this.opts.logger.error({ source: source.name, error: m.error }, 'logsSubscribe returned an error');
      return false;
    }
    if (m.method !== 'logsNotification') {
      if (typeof m.result === 'number') this.opts.logger.info({ source: source.name }, 'subscribed to program logs');
      return true;
    }
    const value = m.params?.result?.value as { signature?: unknown; err?: unknown; logs?: unknown } | undefined;
    if (!value || typeof value.signature !== 'string' || !Array.isArray(value.logs)) return true;
    this.lastEventAt = new Date();
    if (value.err !== null && value.err !== undefined) return true;
    const logs = value.logs.filter((l): l is string => typeof l === 'string');
    if (!logs.some((line) => source.logPatterns.some((p) => line.includes(p)))) return true;
    if (!this.remember(value.signature)) return true;
    if (!this.queue.push({ source: source.name, signature: value.signature })) {
      this.dropped++;
      this.opts.metrics?.discoveryDropped.inc();
    }
    return true;
  }

  /** Resolves one queued launch to a token. Public for tests. */
  async resolve(launch: PendingLaunch, signal: AbortSignal): Promise<DiscoveredToken | null> {
    let tx = null;
    for (const delay of this.opts.fetchDelaysMs ?? [0, 500, 1_000, 2_000, 4_000]) {
      if (delay) await sleep(delay, signal);
      tx = await this.opts.rpc.getTransaction(launch.signature, signal);
      if (tx) break;
    }
    if (!tx) {
      this.opts.logger.debug({ signature: launch.signature }, 'launch transaction not retrievable');
      return null;
    }
    const mint = extractNewMint(tx, this.opts.quoteMints);
    if (!mint) return null;
    return { chain: 'solana', address: mint, createdAtChain: blockTimeOf(tx), source: launch.source, reference: launch.signature };
  }

  private remember(signature: string): boolean {
    if (this.seen.has(signature)) return false;
    const now = Date.now();
    this.seen.set(signature, now);
    if (this.seen.size > 50_000) {
      for (const [sig, at] of this.seen) {
        if (now - at > 10 * 60_000) this.seen.delete(sig);
      }
    }
    return true;
  }

  private async runResolver(): Promise<void> {
    const signal = this.abort.signal;
    while (!signal.aborted) {
      let launch: PendingLaunch;
      try {
        launch = await this.queue.take(signal);
      } catch {
        return;
      }
      try {
        const token = await this.resolve(launch, signal);
        if (token && this.handler) await this.handler(token);
      } catch (err) {
        if (signal.aborted || isAbortError(err)) return;
        this.opts.logger.warn({ signature: launch.signature, err: errorMessage(err) }, 'resolving launch failed');
      }
    }
  }

  private async runSubscription(source: DiscoverySource): Promise<void> {
    const signal = this.abort.signal;
    let backoffMs = 1_000;
    while (!signal.aborted) {
      const opened = await this.connectOnce(source);
      if (signal.aborted) return;
      if (opened) backoffMs = 1_000;
      const delay = Math.round(backoffMs * (0.5 + Math.random()));
      this.opts.logger.warn({ source: source.name, delayMs: delay }, 'program log subscription closed; reconnecting');
      backoffMs = Math.min(backoffMs * 2, 60_000);
      try {
        await sleep(delay, signal);
      } catch {
        return;
      }
    }
  }

  /** Resolves when the socket closes; true if it had connected successfully. */
  private connectOnce(source: DiscoverySource): Promise<boolean> {
    return new Promise((resolve) => {
      const factory = this.opts.socketFactory ?? ((url: string) => new WebSocket(url, { handshakeTimeout: 10_000 }));
      let ws: SocketLike;
      try {
        ws = factory(this.opts.wsUrl);
      } catch (err) {
        this.opts.logger.warn({ source: source.name, err: errorMessage(err) }, 'websocket creation failed');
        resolve(false);
        return;
      }
      this.sockets.add(ws);
      let opened = false;
      let alive = true;
      // A socket can stall without closing; a missed pong forces a reconnect.
      const heartbeat = setInterval(() => {
        if (!alive) {
          ws.terminate();
          return;
        }
        alive = false;
        ws.ping();
      }, this.opts.heartbeatMs ?? 30_000);

      ws.on('open', () => {
        opened = true;
        this.connected.add(source.name);
        ws.send(
          JSON.stringify({
            jsonrpc: '2.0',
            id: 1,
            method: 'logsSubscribe',
            params: [{ mentions: [source.programId] }, { commitment: 'confirmed' }],
          }),
        );
      });
      ws.on('pong', () => {
        alive = true;
      });
      ws.on('message', (data) => {
        alive = true;
        if (!this.handleMessage(source, data.toString())) ws.close();
      });
      ws.on('error', (err) => {
        this.opts.logger.warn(
          { source: source.name, url: redactUrl(this.opts.wsUrl), err: err.message },
          'program log websocket error',
        );
      });
      ws.on('close', () => {
        clearInterval(heartbeat);
        this.connected.delete(source.name);
        this.sockets.delete(ws);
        resolve(opened);
      });
    });
  }
}
