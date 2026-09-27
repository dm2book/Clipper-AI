import type { AlertSettings } from '../config/env.js';
import { evaluateMomentum, evaluateNewToken, type Decision } from '../core/alertRules.js';
import { buildAlertPayload, dedupeKey, type AlertType, type GateResult, type ScoreSummary } from '../core/alerts.js';
import type { HolderSnapshot } from '../core/holders.js';
import type { MarketSnapshot } from '../core/marketSnapshot.js';
import { aggregateSafety, type SafetySummary } from '../core/safety.js';
import { tokenAge, type Token, type TokenAge } from '../core/token.js';
import { KeyedMutex } from '../infra/concurrency.js';
import type { Pool } from '../infra/db.js';
import type { Logger } from '../infra/logger.js';
import type { Metrics } from '../infra/metrics.js';
import { detectMomentum, type MomentumSignal } from '../momentum/engine.js';
import type { TradeData } from '../momentum/flow.js';
import { resolveThresholds, type ThresholdConfig } from '../momentum/thresholds.js';
import { alertExists, countAlertsSince, createAlertWithEvidence, lastAlert } from '../repositories/alerts.js';
import { latestSafetyReports, recentHolderSnapshots } from '../repositories/enrichment.js';
import { latestSnapshot, recentSnapshots } from '../repositories/snapshots.js';

export interface SignalServiceDeps {
  db: Pool;
  alerts: AlertSettings;
  momentum: ThresholdConfig;
  minExternalSafety: number;
  logger: Logger;
  metrics: Metrics;
  clock?: () => Date;
  /**
   * Per-trade data for a token, or null when no trade stream covers it. Not
   * connected yet (roadmap phase 3): the engine then uses market snapshots
   * and reports unique buyers/sellers and wash checks as "not measured".
   */
  tradeData?: (token: Token, since: Date) => Promise<TradeData | null>;
}

export interface Evaluation {
  skipped?: string;
  newToken?: Decision;
  momentum?: Decision;
  signal?: MomentumSignal;
  alertIds: number[];
}

/** Enough history for the 1h window's before-previous interval, plus tolerance. */
const LOOKBACK_MS = 3 * 3_600_000 + 15 * 60_000;

export function toScoreSummary(s: MomentumSignal): ScoreSummary {
  return {
    value: s.score,
    confidence: s.confidence,
    version: s.engineVersion,
    window: s.primaryWindow,
    components: s.rules.map((r) => ({
      type: r.id,
      label: r.label,
      points: r.points,
      weight: r.weight,
      available: r.available,
      detail: r.detail,
    })),
    penalties: s.penalties.map((p) => ({ reason: p.reason, points: p.points })),
    reasons: s.reasons,
    warnings: s.warnings,
  };
}

/** Runs the Momentum Detection Engine and turns eligible decisions into alerts. */
export class SignalService {
  private readonly mutex = new KeyedMutex();
  private readonly clock: () => Date;

  constructor(private readonly deps: SignalServiceDeps) {
    this.clock = deps.clock ?? (() => new Date());
  }

  /** Re-evaluate with the newest stored snapshot (e.g. right after enrichment). */
  async evaluateLatest(token: Token): Promise<Evaluation> {
    const snapshot = await latestSnapshot(this.deps.db, token.chain, token.address);
    return snapshot ? this.evaluate(token, snapshot) : { skipped: 'no market data', alertIds: [] };
  }

  evaluate(token: Token, snapshot: MarketSnapshot): Promise<Evaluation> {
    // One evaluation per token at a time, so two triggers cannot both pass a cooldown.
    return this.mutex.run(`${token.chain}:${token.address}`, () => this.evaluateLocked(token, snapshot));
  }

  private async evaluateLocked(token: Token, snapshot: MarketSnapshot): Promise<Evaluation> {
    const { db, alerts, logger } = this.deps;
    const now = this.clock();
    const { thresholds, profile } = resolveThresholds(this.deps.momentum, token.chain, snapshot.dexId);
    const needed = Math.min(alerts.newToken.minLiquidityUsd, thresholds.minLiquidityUsd);
    // Cheap exit: without enough liquidity no alert can pass its gates.
    if (snapshot.liquidityUsd === null || snapshot.liquidityUsd < needed) {
      return { skipped: 'liquidity below every alert threshold', alertIds: [] };
    }

    const since = new Date(now.getTime() - LOOKBACK_MS);
    const [history, holders, reports, trades] = await Promise.all([
      recentSnapshots(db, token.chain, token.address, since),
      recentHolderSnapshots(db, token.chain, token.address, since),
      latestSafetyReports(db, token.chain, token.address),
      this.deps.tradeData ? this.deps.tradeData(token, since) : Promise.resolve(null),
    ]);
    const snapshots = history.some((s) => s.observedAt.getTime() === snapshot.observedAt.getTime())
      ? history
      : [snapshot, ...history];
    const safety = aggregateSafety(reports, this.deps.minExternalSafety);
    const age = tokenAge(token, snapshot.pairCreatedAt, now);

    const signal = detectMomentum(
      {
        chain: token.chain,
        address: token.address,
        tokenType: snapshot.dexId,
        tokenAgeMs: age?.ms ?? null,
        snapshots,
        holders,
        trades,
      },
      now.getTime(),
      thresholds,
      profile,
    );
    if (signal.signalType !== 'NO_SIGNAL') {
      logger.debug(
        { token: token.address, signalType: signal.signalType, score: signal.score, reasons: signal.reasons },
        'momentum engine result',
      );
    }

    const result: Evaluation = { signal, alertIds: [] };
    const common = { token, snapshot, holders: holders[0] ?? null, safety, age, now };

    const newKey = dedupeKey('NEW_TOKEN', token.chain, token.address);
    if (!(await alertExists(db, newKey))) {
      result.newToken = evaluateNewToken({ now, age, snapshot, holders: holders[0] ?? null, safety }, alerts);
      if (result.newToken.eligible) {
        const id = await this.createAlert('NEW_TOKEN', newKey, common, null, result.newToken.gates);
        if (id !== null) result.alertIds.push(id);
      }
    }

    result.momentum = evaluateMomentum(
      { now, snapshot, signal, safety, lastAlert: await lastAlert(db, token.chain, token.address, 'MOMENTUM') },
      alerts,
    );
    if (result.momentum.eligible) {
      const key = dedupeKey('MOMENTUM', token.chain, token.address, now.getTime());
      const id = await this.createAlert('MOMENTUM', key, common, signal, result.momentum.gates);
      if (id !== null) result.alertIds.push(id);
    }
    return result;
  }

  private async createAlert(
    type: AlertType,
    key: string,
    c: {
      token: Token;
      snapshot: MarketSnapshot;
      holders: HolderSnapshot | null;
      safety: SafetySummary;
      age: TokenAge | null;
      now: Date;
    },
    signal: MomentumSignal | null,
    gates: GateResult[],
  ): Promise<number | null> {
    const { db, alerts, logger, metrics } = this.deps;
    const recent = await countAlertsSince(db, new Date(c.now.getTime() - 3_600_000));
    const suppressedReason = recent >= alerts.maxPerHour ? `limit of ${alerts.maxPerHour} alerts per hour reached` : null;
    const payload = buildAlertPayload({
      type,
      token: {
        chain: c.token.chain,
        address: c.token.address,
        symbol: c.token.symbol ?? c.snapshot.symbol,
        name: c.token.name ?? c.snapshot.name,
      },
      now: c.now,
      age: c.age,
      snapshot: c.snapshot,
      holders: c.holders,
      safety: c.safety,
      score: signal ? toScoreSummary(signal) : null,
      gates,
    });
    const client = await db.connect();
    let id: number | null;
    try {
      id = await createAlertWithEvidence(client, {
        chain: c.token.chain,
        tokenAddress: c.token.address,
        type,
        dedupeKey: key,
        status: suppressedReason ? 'SUPPRESSED' : 'PENDING',
        suppressedReason,
        payload,
        momentum: signal,
      });
    } finally {
      client.release();
    }
    if (id !== null) {
      const status = suppressedReason ? 'SUPPRESSED' : 'PENDING';
      metrics.alerts.inc({ type, status });
      logger.info({ alertId: id, type, token: c.token.address, score: signal?.score ?? null, status }, 'alert created');
    }
    return id;
  }
}
