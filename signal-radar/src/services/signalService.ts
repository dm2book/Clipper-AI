import type { AlertSettings } from '../config/env.js';
import { evaluateMomentum, evaluateNewToken, type Decision } from '../core/alertRules.js';
import { buildAlertPayload, dedupeKey, type AlertType, type GateResult } from '../core/alerts.js';
import type { HolderSnapshot } from '../core/holders.js';
import type { MarketSnapshot } from '../core/marketSnapshot.js';
import { aggregateSafety, type SafetySummary } from '../core/safety.js';
import { SCORING_V1, computeScore, type ScoreResult, type ScoringSpec } from '../core/scoring.js';
import { DETECTOR_VERSION, liquidityChange5mPct, measureAll, type Measurement } from '../core/signals.js';
import { tokenAge, type Token, type TokenAge } from '../core/token.js';
import { KeyedMutex } from '../infra/concurrency.js';
import type { Pool } from '../infra/db.js';
import type { Logger } from '../infra/logger.js';
import type { Metrics } from '../infra/metrics.js';
import { alertExists, countAlertsSince, createAlertWithEvidence, lastAlert } from '../repositories/alerts.js';
import { latestSafetyReports, recentHolderSnapshots } from '../repositories/enrichment.js';
import { latestSnapshot, recentSnapshots } from '../repositories/snapshots.js';

export interface SignalServiceDeps {
  db: Pool;
  alerts: AlertSettings;
  minExternalSafety: number;
  logger: Logger;
  metrics: Metrics;
  spec?: ScoringSpec;
  clock?: () => Date;
}

export interface Evaluation {
  skipped?: string;
  newToken?: Decision;
  momentum?: Decision;
  score?: ScoreResult;
  alertIds: number[];
}

const HISTORY_MS = 20 * 60_000;

/** Measures signals, scores them and turns eligible decisions into alerts. */
export class SignalService {
  private readonly mutex = new KeyedMutex();
  private readonly spec: ScoringSpec;
  private readonly clock: () => Date;

  constructor(private readonly deps: SignalServiceDeps) {
    this.spec = deps.spec ?? SCORING_V1;
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
    const { db, alerts } = this.deps;
    const now = this.clock();
    const needed = Math.min(alerts.newToken.minLiquidityUsd, alerts.momentum.minLiquidityUsd);
    // Cheap exit: without enough liquidity no alert can pass its gates.
    if (snapshot.liquidityUsd === null || snapshot.liquidityUsd < needed) {
      return { skipped: 'liquidity below every alert threshold', alertIds: [] };
    }

    const since = new Date(now.getTime() - HISTORY_MS);
    const [history, holders, reports] = await Promise.all([
      recentSnapshots(db, token.chain, token.address, since),
      recentHolderSnapshots(db, token.chain, token.address, since),
      latestSafetyReports(db, token.chain, token.address),
    ]);
    const earlier = history.filter((s) => s.observedAt.getTime() < snapshot.observedAt.getTime());
    const safety = aggregateSafety(reports, this.deps.minExternalSafety);
    const age = tokenAge(token, snapshot.pairCreatedAt, now);
    const ctx = { now, current: snapshot, history: earlier, holders, tokenAgeMs: age?.ms ?? null };
    const measurements = measureAll(ctx);
    const score = computeScore(
      measurements,
      { safetyVerdict: safety.verdict, top10Pct: holders[0]?.top10Pct ?? null, liquidityChange5mPct: liquidityChange5mPct(ctx) },
      this.spec,
    );
    const result: Evaluation = { score, alertIds: [] };
    const common = { token, snapshot, holders: holders[0] ?? null, safety, age, measurements, now };

    const newKey = dedupeKey('NEW_TOKEN', token.chain, token.address);
    if (!(await alertExists(db, newKey))) {
      result.newToken = evaluateNewToken({ now, age, snapshot, holders: holders[0] ?? null, safety }, alerts);
      if (result.newToken.eligible) {
        const id = await this.createAlert('NEW_TOKEN', newKey, common, null, result.newToken.gates);
        if (id !== null) result.alertIds.push(id);
      }
    }

    result.momentum = evaluateMomentum(
      { now, snapshot, score, safety, lastAlert: await lastAlert(db, token.chain, token.address, 'MOMENTUM') },
      alerts,
    );
    if (result.momentum.eligible) {
      const key = dedupeKey('MOMENTUM', token.chain, token.address, now.getTime());
      const id = await this.createAlert('MOMENTUM', key, common, score, result.momentum.gates);
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
      measurements: Measurement[];
      now: Date;
    },
    score: ScoreResult | null,
    gates: GateResult[],
  ): Promise<number | null> {
    const { db, alerts, logger, metrics } = this.deps;
    const recent = await countAlertsSince(db, new Date(c.now.getTime() - 3_600_000));
    const suppressedReason = recent >= alerts.maxPerHour ? `limit of ${alerts.maxPerHour} alerts per hour reached` : null;
    const payload = buildAlertPayload({
      type,
      token: { chain: c.token.chain, address: c.token.address, symbol: c.token.symbol ?? c.snapshot.symbol, name: c.token.name ?? c.snapshot.name },
      now: c.now,
      age: c.age,
      snapshot: c.snapshot,
      holders: c.holders,
      safety: c.safety,
      score,
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
        score,
        measurements: score ? c.measurements : [],
        detectorVersion: DETECTOR_VERSION,
        now: c.now,
      });
    } finally {
      client.release();
    }
    if (id !== null) {
      const status = suppressedReason ? 'SUPPRESSED' : 'PENDING';
      metrics.alerts.inc({ type, status });
      logger.info({ alertId: id, type, token: c.token.address, score: score?.score ?? null, status }, 'alert created');
    }
    return id;
  }
}
