/**
 * TEST-ONLY provider mocks. They implement the production interfaces with
 * scripted, synthetic data so the pipeline can be exercised end to end
 * without network access. Never wired into the application.
 */
import type { AlertPayload } from '../../src/core/alerts.js';
import { TransientError } from '../../src/core/errors.js';
import type { HolderSnapshot } from '../../src/core/holders.js';
import type { MarketSnapshot } from '../../src/core/marketSnapshot.js';
import type { SafetyReport, SafetyVerdict } from '../../src/core/safety.js';
import type { DiscoveredToken } from '../../src/core/token.js';
import type { Chain } from '../../src/core/types.js';
import type {
  DiscoveryHealth,
  HolderProvider,
  MarketDataProvider,
  NotificationProvider,
  SafetyProvider,
  TokenDiscoveryProvider,
  WalletProvider,
  WalletTrade,
} from '../../src/providers/interfaces.js';
import { makeHolders, makeSafety, makeSnapshot, type SnapshotOverrides } from './factories.js';

export class MockDiscoveryProvider implements TokenDiscoveryProvider {
  readonly name = 'mock-discovery';
  private handler: ((t: DiscoveredToken) => Promise<void>) | null = null;
  stopped = false;

  async start(handler: (t: DiscoveredToken) => Promise<void>): Promise<void> {
    this.handler = handler;
  }
  async stop(): Promise<void> {
    this.stopped = true;
    this.handler = null;
  }
  health(): DiscoveryHealth {
    return { connected: this.handler !== null, lastEventAt: null };
  }
  async emit(token: DiscoveredToken): Promise<void> {
    if (!this.handler) throw new Error('mock discovery not started');
    await this.handler(token);
  }
}

export class MockMarketDataProvider implements MarketDataProvider {
  readonly name = 'mock-market';
  readonly maxBatchSize = 30;
  /** Per address: a snapshot factory; missing = not indexed. */
  readonly markets = new Map<string, (now: Date) => SnapshotOverrides>();
  failNext = 0;
  calls = 0;

  async getSnapshots(chain: Chain, addresses: readonly string[]): Promise<Map<string, MarketSnapshot>> {
    this.calls++;
    if (this.failNext > 0) {
      this.failNext--;
      throw new TransientError('mock market data outage');
    }
    const now = new Date();
    const out = new Map<string, MarketSnapshot>();
    for (const address of addresses) {
      const make = this.markets.get(address);
      if (make) out.set(address, makeSnapshot({ chain, tokenAddress: address, source: this.name, observedAt: now, ...make(now) }));
    }
    return out;
  }
}

export class MockSafetyProvider implements SafetyProvider {
  readonly verdicts = new Map<string, SafetyVerdict>();
  calls = 0;

  constructor(
    readonly name: string,
    readonly kind: 'onchain' | 'external',
    private readonly fallback: SafetyVerdict = 'PASS',
  ) {}

  async check(chain: Chain, address: string): Promise<SafetyReport> {
    this.calls++;
    const verdict = this.verdicts.get(address) ?? this.fallback;
    return makeSafety({
      chain,
      tokenAddress: address,
      provider: this.name,
      kind: this.kind,
      verdict,
      reasons: verdict === 'PASS' ? [] : [`mock ${verdict.toLowerCase()}`],
      checkedAt: new Date(),
      tokenInfo: this.kind === 'onchain' ? { decimals: 6, supply: '1000000000000', tokenProgram: 'MockProgram' } : null,
    });
  }
}

export class MockHolderProvider implements HolderProvider {
  readonly name = 'mock-holders';
  readonly counts = new Map<string, number>();

  async getHolders(chain: Chain, address: string): Promise<HolderSnapshot> {
    return makeHolders({ chain, tokenAddress: address, observedAt: new Date(), holderCount: this.counts.get(address) ?? 0, method: 'mock' });
  }
}

export class MockNotificationProvider implements NotificationProvider {
  readonly name = 'mock-notifier';
  readonly sent: AlertPayload[] = [];
  /** Errors to throw on the next sends, in order. */
  readonly failures: Error[] = [];

  async send(alert: AlertPayload): Promise<{ messageId: string | null }> {
    const failure = this.failures.shift();
    if (failure) throw failure;
    this.sent.push(alert);
    return { messageId: `mock-${this.sent.length}` };
  }
}

export class MockWalletProvider implements WalletProvider {
  readonly name = 'mock-wallet';
  readonly available = true;
  readonly trades: WalletTrade[] = [];

  async getTrades(chain: Chain, wallet: string, since: Date): Promise<WalletTrade[]> {
    return this.trades.filter((t) => t.chain === chain && t.wallet === wallet && t.blockTime >= since);
  }
}
