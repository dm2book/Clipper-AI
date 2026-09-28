/**
 * Wallet Intelligence: stores wallet events, keeps per-wallet statistics and
 * classification up to date, and turns notable activity into alerts.
 *
 *  WHALE           a single large swap (size only — says nothing about skill)
 *  TRACKED_WALLET  a swap by a wallet on the watchlist (manual, or meeting the
 *                  configured, measurable criteria)
 *  TRACKED_CLUSTER several tracked wallets buying the same token within a
 *                  short window, with a per-wallet share cap
 *
 * Alerts are only created for tokens the radar tracks (the `tokens` table),
 * for fresh events (a backfill never alerts) and for successful swaps.
 */
import type { AlertSettings } from '../config/env.js';
import { dedupeKey, type WalletAlertPayload, type WalletAlertType, type WalletStatsSummary } from '../core/alerts.js';
import { errorMessage } from '../core/errors.js';
import type { Chain } from '../core/types.js';
import { KeyedMutex } from '../infra/concurrency.js';
import type { Pool } from '../infra/db.js';
import type { Logger } from '../infra/logger.js';
import type { Metrics } from '../infra/metrics.js';
import type { WalletActivitySource, WalletProvider } from '../providers/interfaces.js';
import { countAlertsSince, countWalletAlertsSince, createAlertWithEvidence, lastAlert } from '../repositories/alerts.js';
import { latestSnapshot } from '../repositories/snapshots.js';
import { getToken } from '../repositories/tokens.js';
import {
  applyCriteriaTracking,
  getTrackedWallet,
  getWalletProfile,
  insertWalletEvents,
  listTrackedWallets,
  saveWalletProfile,
  syncManualWallets,
  trackedBuys,
  walletEvents,
  walletsDueForStats,
  type WalletProfile,
} from '../repositories/wallets.js';
import { classifyActivity, detectCluster } from '../wallets/activity.js';
import { classifyWallet } from '../wallets/classify.js';
import type { WalletIntelligenceConfig } from '../wallets/criteria.js';
import { buildLedger, unrealizedPnl } from '../wallets/ledger.js';
import type { WalletEvent } from '../wallets/model.js';
import { computeStats, type PriceInfo, type WalletStats } from '../wallets/stats.js';

export interface WalletIntelligenceDeps {
  db: Pool;
  chain: Chain;
  config: WalletIntelligenceConfig;
  alerts: Pick<AlertSettings, 'maxPerHour' | 'marketDataMaxAgeSec'>;
  isOnCurve: (address: string) => boolean;
  logger: Logger;
  metrics: Metrics;
  source?: WalletActivitySource;
  history?: WalletProvider;
  clock?: () => Date;
}

export interface IngestResult {
  received: number;
  inserted: number;
  duplicates: number;
  invalid: number;
  alertIds: number[];
}

const HOUR = 3_600_000;
const DAY = 86_400_000;

const WHALE_NOTE = 'Whale-activiteit gaat over de omvang van één transactie, niet over de kwaliteit van de trader.';
const HISTORY_NOTE = 'Statistieken zijn historisch en gemeten over het genoemde venster: geen voorspelling en geen garantie.';

/** Structural checks on events from an external source; bad ones are dropped, not stored. */
export function validateEvent(e: WalletEvent): string | null {
  if (!e.signature || !e.wallet || !e.tokenAddress) return 'missing signature, wallet or token';
  if (!Number.isInteger(e.ixIndex) || e.ixIndex < 0) return 'invalid instruction index';
  if (typeof e.amountRaw !== 'bigint' || e.amountRaw < 0n) return 'invalid amount';
  if (e.valueUsd !== null && !(Number.isFinite(e.valueUsd) && e.valueUsd >= 0)) return 'invalid USD value';
  if (!(e.blockTime instanceof Date) || Number.isNaN(e.blockTime.getTime())) return 'invalid block time';
  if (!['buy', 'sell', 'transfer_in', 'transfer_out'].includes(e.kind)) return 'unknown kind';
  if (e.status !== 'success' && e.status !== 'failed') return 'unknown status';
  return null;
}

function summarize(p: WalletProfile, windowDays: number): WalletStatsSummary {
  return {
    classification: p.classification,
    criteriaVersion: p.criteriaVersion,
    windowDays,
    reliableClosedPositions: p.stats.reliableClosedPositions,
    winRate: p.stats.winRate,
    winRateLowerBound: p.stats.winRateLowerBound,
    avgReturnPct: p.stats.avgReturnPct,
    realizedPnlUsd: p.stats.realizedPnlUsd,
    avgHoldingSec: p.stats.avgHoldingSec,
    computedAt: p.computedAt.toISOString(),
  };
}

export class WalletIntelligenceService {
  private readonly mutex = new KeyedMutex();
  private readonly clock: () => Date;

  constructor(private readonly deps: WalletIntelligenceDeps) {
    this.clock = deps.clock ?? (() => new Date());
  }

  /** Syncs the manual watchlist and starts the live source, if one is connected. */
  async start(): Promise<void> {
    const { source, logger } = this.deps;
    await this.syncWatchlist();
    if (!source?.available) {
      logger.warn('wallet activity source not connected: Wallet Intelligence stores and alerts nothing until one is (docs/PROVIDERS.md)');
      return;
    }
    await source.start(async (events) => {
      try {
        await this.ingest(events);
      } catch (err) {
        logger.error({ err: errorMessage(err), events: events.length }, 'wallet event ingestion failed');
      }
    });
  }

  async stop(): Promise<void> {
    await this.deps.source?.stop();
  }

  /** Stores new events and evaluates the fresh, successful swaps among them. */
  async ingest(events: readonly WalletEvent[]): Promise<IngestResult> {
    const { db, logger, metrics } = this.deps;
    const valid: WalletEvent[] = [];
    let invalid = 0;
    for (const e of events) {
      const problem = validateEvent(e);
      if (problem) {
        invalid++;
        logger.warn({ signature: e.signature, problem }, 'invalid wallet event dropped');
      } else valid.push(e);
    }
    const inserted = await insertWalletEvents(db, valid);
    metrics.walletEvents.inc({ outcome: 'inserted' }, inserted.length);
    metrics.walletEvents.inc({ outcome: 'duplicate' }, valid.length - inserted.length);
    metrics.walletEvents.inc({ outcome: 'invalid' }, invalid);

    const alertIds: number[] = [];
    for (const e of inserted) {
      if (e.status !== 'success' || (e.kind !== 'buy' && e.kind !== 'sell')) continue;
      const ids = await this.mutex.run(`${e.chain}:${e.tokenAddress}`, () => this.evaluate(e));
      alertIds.push(...ids);
    }
    return { received: events.length, inserted: inserted.length, duplicates: valid.length - inserted.length, invalid, alertIds };
  }

  private async evaluate(e: WalletEvent): Promise<number[]> {
    const { db, config, alerts, logger } = this.deps;
    const now = this.clock();
    if (now.getTime() - e.blockTime.getTime() > config.alertMaxEventAgeSec * 1000) return []; // backfill / late delivery
    const token = await getToken(db, e.chain, e.tokenAddress);
    if (!token) return []; // stored for statistics; the radar does not track this token

    const snapshot = await latestSnapshot(db, e.chain, e.tokenAddress);
    const fresh = snapshot && now.getTime() - snapshot.observedAt.getTime() <= alerts.marketDataMaxAgeSec * 1000 ? snapshot : null;
    const tracked = await getTrackedWallet(db, e.chain, e.wallet);
    const decision = classifyActivity(
      e,
      { liquidityUsd: fresh?.liquidityUsd ?? null, tracked, isOnCurve: this.deps.isOnCurve },
      config.activity,
    );
    if (decision.skipped || (!decision.whale && !decision.tracked)) return [];

    const type: WalletAlertType = decision.tracked ? 'TRACKED_WALLET' : 'WHALE';
    const profile = decision.tracked ? await getWalletProfile(db, e.chain, e.wallet) : null;
    const exposure = await this.positionAfter(e, token.decimals, fresh?.priceUsd ?? null);
    const notes: string[] = [];
    if (decision.whale && type === 'WHALE') notes.push(WHALE_NOTE);
    if (type === 'TRACKED_WALLET') notes.push(HISTORY_NOTE);
    if (exposure && !exposure.complete) notes.push('Positie onvolledig bekend: deel kwam via transfer of van vóór de opgeslagen historie.');
    if (!fresh) notes.push('Geen actuele marktdata: liquiditeit en positiewaarde onbekend.');

    const payload: WalletAlertPayload = {
      type,
      chain: e.chain,
      tokenAddress: e.tokenAddress,
      symbol: token.symbol ?? snapshot?.symbol ?? null,
      name: token.name ?? snapshot?.name ?? null,
      decidedAt: now.toISOString(),
      trade: {
        wallet: e.wallet,
        side: e.kind as 'buy' | 'sell',
        valueUsd: e.valueUsd!,
        amountRaw: e.amountRaw.toString(),
        signature: e.signature,
        blockTime: e.blockTime.toISOString(),
        source: e.source,
      },
      whaleReason: decision.whaleReason,
      tracked: tracked
        ? { source: tracked.source, reason: tracked.reason, stats: profile ? summarize(profile, config.criteria.statsWindowDays) : null }
        : null,
      exposure,
      cluster: null,
      market: fresh
        ? {
            source: fresh.source,
            observedAt: fresh.observedAt.toISOString(),
            priceUsd: fresh.priceUsd,
            liquidityUsd: fresh.liquidityUsd,
            marketCapUsd: fresh.marketCapUsd,
          }
        : null,
      notes,
    };

    let suppressed: string | null = null;
    if (type === 'WHALE') {
      const perWallet = await countWalletAlertsSince(db, e.wallet, 'WHALE', new Date(now.getTime() - HOUR));
      if (perWallet >= config.activity.whaleMaxAlertsPerWalletPerHour) {
        suppressed = `limit of ${config.activity.whaleMaxAlertsPerWalletPerHour} whale alerts per wallet per hour reached`;
      }
    }
    const ids: number[] = [];
    const id = await this.createAlert(type, dedupeKey(type, e.chain, e.tokenAddress, `${e.signature}:${e.ixIndex}:${e.wallet}`), payload, e.wallet, suppressed, now);
    if (id !== null) ids.push(id);

    if (decision.tracked && e.kind === 'buy') {
      const clusterId = await this.checkCluster(e, payload, now).catch((err: unknown) => {
        logger.error({ err: errorMessage(err), token: e.tokenAddress }, 'cluster check failed');
        return null;
      });
      if (clusterId !== null) ids.push(clusterId);
    }
    return ids;
  }

  private async checkCluster(e: WalletEvent, base: WalletAlertPayload, now: Date): Promise<number | null> {
    const { db, config } = this.deps;
    const a = config.activity;
    const from = new Date(e.blockTime.getTime() - a.clusterWindowMinutes * 60_000);
    const buys = await trackedBuys(db, e.chain, e.tokenAddress, from, e.blockTime, a.trackedMinTradeUsd);
    const cluster = detectCluster(buys, e.blockTime, a);
    if (!cluster) return null;
    const last = await lastAlert(db, e.chain, e.tokenAddress, 'TRACKED_CLUSTER');
    if (last && now.getTime() - last.createdAt.getTime() < a.clusterCooldownMinutes * 60_000) return null;

    const notes = [HISTORY_NOTE];
    if (cluster.cappedWallets > 0) {
      notes.push(`${cluster.cappedWallets} wallet(s) begrensd tot ${(a.clusterMaxWalletShare * 100).toFixed(0)}% van het gewogen totaal.`);
    }
    const payload: WalletAlertPayload = {
      ...base,
      type: 'TRACKED_CLUSTER',
      trade: null,
      whaleReason: null,
      tracked: null,
      exposure: null,
      cluster: {
        wallets: cluster.wallets.map((w) => ({ wallet: w.wallet, buyUsd: w.buyUsd, sharePct: w.sharePct })),
        totalUsd: cluster.totalUsd,
        cappedTotalUsd: cluster.cappedTotalUsd,
        maxWalletShare: a.clusterMaxWalletShare,
        windowMinutes: cluster.windowMinutes,
        firstAt: cluster.firstAt.toISOString(),
        lastAt: cluster.lastAt.toISOString(),
      },
      notes,
    };
    const key = dedupeKey('TRACKED_CLUSTER', e.chain, e.tokenAddress, cluster.firstAt.getTime());
    return this.createAlert('TRACKED_CLUSTER', key, payload, null, null, now);
  }

  private async createAlert(
    type: WalletAlertType,
    key: string,
    payload: WalletAlertPayload,
    wallet: string | null,
    suppressedReason: string | null,
    now: Date,
  ): Promise<number | null> {
    const { db, alerts, logger, metrics } = this.deps;
    if (!suppressedReason && (await countAlertsSince(db, new Date(now.getTime() - HOUR))) >= alerts.maxPerHour) {
      suppressedReason = `limit of ${alerts.maxPerHour} alerts per hour reached`;
    }
    const client = await db.connect();
    let id: number | null;
    try {
      id = await createAlertWithEvidence(client, {
        chain: payload.chain,
        tokenAddress: payload.tokenAddress,
        type,
        dedupeKey: key,
        status: suppressedReason ? 'SUPPRESSED' : 'PENDING',
        suppressedReason,
        payload,
        momentum: null,
        wallet,
      });
    } finally {
      client.release();
    }
    if (id !== null) {
      const status = suppressedReason ? 'SUPPRESSED' : 'PENDING';
      metrics.alerts.inc({ type, status });
      logger.info({ alertId: id, type, token: payload.tokenAddress, wallet, status }, 'wallet alert created');
    }
    return id;
  }

  /** The wallet's position in the event's token after the event, from stored events. */
  private async positionAfter(e: WalletEvent, decimals: number | null, priceUsd: number | null): Promise<WalletAlertPayload['exposure']> {
    const since = new Date(e.blockTime.getTime() - this.deps.config.criteria.statsWindowDays * DAY);
    const events = (await walletEvents(this.deps.db, e.chain, e.wallet, since, e.tokenAddress)).filter(
      (x) => x.blockTime <= e.blockTime,
    );
    const pos = buildLedger(events).open.find((p) => p.tokenAddress === e.tokenAddress);
    if (!pos) return { qtyRaw: '0', costUsd: 0, valueUsd: 0, complete: true };
    return {
      qtyRaw: pos.qtyRaw.toString(),
      costUsd: pos.remainingCostUsd,
      valueUsd: decimals === null || priceUsd === null ? null : (Number(pos.qtyRaw) / 10 ** decimals) * priceUsd,
      complete: pos.reliable,
    };
  }

  /** Open positions of a wallet with their current value where a fresh price is known. */
  async exposure(chain: Chain, wallet: string) {
    const { db, config } = this.deps;
    const now = this.clock();
    const events = await walletEvents(db, chain, wallet, new Date(now.getTime() - config.criteria.statsWindowDays * DAY));
    const open = buildLedger(events).open;
    const prices = await this.prices(chain, open.map((p) => p.tokenAddress), now);
    return open.map((p) => {
      const price = prices.get(p.tokenAddress);
      const u = unrealizedPnl(p, price?.priceUsd ?? null, price?.decimals ?? null);
      return { ...p, valueUsd: u?.valueUsd ?? null, unrealizedPnlUsd: u?.pnlUsd ?? null };
    });
  }

  private async prices(chain: Chain, tokens: readonly string[], now: Date): Promise<Map<string, PriceInfo>> {
    const out = new Map<string, PriceInfo>();
    for (const address of tokens) {
      const [token, snap] = await Promise.all([getToken(this.deps.db, chain, address), latestSnapshot(this.deps.db, chain, address)]);
      const fresh = snap && now.getTime() - snap.observedAt.getTime() <= this.deps.alerts.marketDataMaxAgeSec * 1000;
      out.set(address, { priceUsd: fresh ? snap.priceUsd : null, decimals: token?.decimals ?? null });
    }
    return out;
  }

  /**
   * Recomputes a wallet's statistics over the configured window, classifies
   * it and — if enabled — adds it to or removes it from the criteria watchlist.
   */
  async recomputeStats(chain: Chain, wallet: string): Promise<{ stats: WalletStats; profile: WalletProfile; tracking: 'added' | 'removed' | null }> {
    const { db, config, logger } = this.deps;
    const now = this.clock();
    const from = new Date(now.getTime() - config.criteria.statsWindowDays * DAY);
    const ledger = buildLedger(await walletEvents(db, chain, wallet, from));
    const prices = await this.prices(chain, ledger.open.map((p) => p.tokenAddress), now);
    const stats = computeStats(ledger, { chain, wallet, from, to: now, prices });
    const result = classifyWallet(stats, config.criteria);
    const profile: WalletProfile = {
      stats,
      classification: result.classification,
      criteriaVersion: result.criteriaVersion,
      computedAt: now,
    };
    await saveWalletProfile(db, chain, wallet, profile);
    let tracking: 'added' | 'removed' | null = null;
    if (config.criteria.autoTrackQualified && !config.activity.ignoredWallets.includes(wallet)) {
      tracking = await applyCriteriaTracking(db, chain, wallet, result.classification === 'QUALIFIED', result.summary);
      if (tracking) {
        logger.info({ wallet, tracking, classification: result.classification, summary: result.summary }, 'criteria watchlist changed');
        await this.pushWatchlist(chain);
      }
    }
    return { stats, profile, tracking };
  }

  /** Scheduler job: recompute statistics of wallets with new events (and tracked ones periodically). */
  async statsOnce(signal: AbortSignal): Promise<boolean> {
    const batch = 25;
    const due = await walletsDueForStats(this.deps.db, batch, this.deps.config.statsRefreshMinutes * 60);
    for (const w of due) {
      if (signal.aborted) break;
      try {
        await this.recomputeStats(w.chain, w.address);
      } catch (err) {
        this.deps.logger.error({ wallet: w.address, err: errorMessage(err) }, 'wallet statistics failed');
      }
    }
    return due.length === batch;
  }

  /** Applies TRACKED_WALLETS, backfills manual wallets when a history provider exists, updates the source. */
  async syncWatchlist(): Promise<void> {
    const { db, chain, config, history, logger } = this.deps;
    await syncManualWallets(db, chain, config.manualWallets);
    if (history?.available) {
      const since = new Date(this.clock().getTime() - config.criteria.statsWindowDays * DAY);
      for (const wallet of config.manualWallets) {
        try {
          const r = await this.ingest(await history.getHistory(chain, wallet, since));
          logger.info({ wallet, inserted: r.inserted }, 'wallet history backfilled');
        } catch (err) {
          logger.warn({ wallet, err: errorMessage(err) }, 'wallet history backfill failed');
        }
      }
    }
    await this.pushWatchlist(chain);
  }

  private async pushWatchlist(chain: Chain): Promise<void> {
    const list = await listTrackedWallets(this.deps.db, chain);
    this.deps.source?.setWatchlist(chain, list.map((w) => w.address));
  }
}
