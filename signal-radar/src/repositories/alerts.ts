import type { AlertPayload, AlertType } from '../core/alerts.js';
import type { ScoreResult } from '../core/scoring.js';
import type { Measurement } from '../core/signals.js';
import type { Chain } from '../core/types.js';
import { date, num, type PoolClient, type Queryable } from '../infra/db.js';

export type AlertStatus = 'PENDING' | 'SENT' | 'FAILED' | 'SUPPRESSED';

export interface StoredAlert {
  id: number;
  chain: Chain;
  tokenAddress: string;
  type: AlertType;
  status: AlertStatus;
  score: number | null;
  attempts: number;
  createdAt: Date;
  payload: AlertPayload;
}

interface AlertRow {
  id: string;
  chain: Chain;
  token_address: string;
  type: AlertType;
  status: AlertStatus;
  score: string | null;
  attempts: number;
  created_at: Date;
  payload: AlertPayload;
}

const toAlert = (r: AlertRow): StoredAlert => ({
  id: Number(r.id),
  chain: r.chain,
  tokenAddress: r.token_address,
  type: r.type,
  status: r.status,
  score: num(r.score),
  attempts: r.attempts,
  createdAt: date(r.created_at)!,
  payload: r.payload,
});

export async function alertExists(db: Queryable, dedupeKey: string): Promise<boolean> {
  const { rowCount } = await db.query('SELECT 1 FROM alerts WHERE dedupe_key = $1', [dedupeKey]);
  return (rowCount ?? 0) > 0;
}

export async function lastAlert(
  db: Queryable,
  chain: Chain,
  address: string,
  type: AlertType,
): Promise<{ createdAt: Date; score: number | null } | null> {
  const { rows } = await db.query<{ created_at: Date; score: string | null }>(
    `SELECT created_at, score FROM alerts
     WHERE chain = $1 AND token_address = $2 AND type = $3 AND status IN ('PENDING', 'SENT')
     ORDER BY created_at DESC LIMIT 1`,
    [chain, address, type],
  );
  return rows[0] ? { createdAt: date(rows[0].created_at)!, score: num(rows[0].score) } : null;
}

export async function countAlertsSince(db: Queryable, since: Date): Promise<number> {
  const { rows } = await db.query<{ n: string }>(
    `SELECT count(*) AS n FROM alerts WHERE created_at >= $1 AND status IN ('PENDING', 'SENT')`,
    [since],
  );
  return Number(rows[0]!.n);
}

/**
 * Stores the score, the signals that were active, and the alert in one
 * transaction: an alert never exists without its evidence. Returns the alert
 * id, or null when the dedupe key already existed.
 */
export async function createAlertWithEvidence(
  client: PoolClient,
  a: {
    chain: Chain;
    tokenAddress: string;
    type: AlertType;
    dedupeKey: string;
    status: 'PENDING' | 'SUPPRESSED';
    suppressedReason: string | null;
    payload: AlertPayload;
    score: ScoreResult | null;
    measurements: readonly Measurement[];
    detectorVersion: string;
    now: Date;
  },
): Promise<number | null> {
  await client.query('BEGIN');
  try {
    let scoreId: number | null = null;
    if (a.score) {
      const { rows } = await client.query<{ id: string }>(
        `INSERT INTO scores (chain, token_address, computed_at, score, confidence, scoring_version, components, penalties)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id`,
        [
          a.chain,
          a.tokenAddress,
          a.now,
          a.score.score,
          a.score.confidence,
          a.score.version,
          JSON.stringify(a.score.components),
          JSON.stringify(a.score.penalties),
        ],
      );
      scoreId = Number(rows[0]!.id);
      const strengths = new Map(a.score.components.map((c) => [c.type, c.strength]));
      for (const m of a.measurements.filter((x) => x.available)) {
        await client.query(
          `INSERT INTO signals (chain, token_address, score_id, type, detected_at, window_label, value, baseline, metric, strength, evidence, detector_version)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)`,
          [
            a.chain,
            a.tokenAddress,
            scoreId,
            m.type,
            a.now,
            m.window,
            m.value,
            m.baseline,
            m.metric,
            strengths.get(m.type) ?? null,
            JSON.stringify({ ...m.evidence, detail: m.detail, qualifies: m.qualifies }),
            a.detectorVersion,
          ],
        );
      }
    }
    const { rows } = await client.query<{ id: string }>(
      `INSERT INTO alerts (chain, token_address, type, score_id, score, dedupe_key, status, suppressed_reason, payload)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
       ON CONFLICT (dedupe_key) DO NOTHING RETURNING id`,
      [
        a.chain,
        a.tokenAddress,
        a.type,
        scoreId,
        a.score?.score ?? null,
        a.dedupeKey,
        a.status,
        a.suppressedReason,
        JSON.stringify(a.payload),
      ],
    );
    if (!rows[0]) {
      await client.query('ROLLBACK');
      return null;
    }
    await client.query('COMMIT');
    return Number(rows[0].id);
  } catch (err) {
    await client.query('ROLLBACK');
    throw err;
  }
}

/** Outbox claim: like the token queues, leased so a crashed sender's alerts come back. */
export async function claimPendingAlerts(db: Queryable, limit: number, leaseSec: number): Promise<StoredAlert[]> {
  const { rows } = await db.query<AlertRow>(
    `WITH due AS (
       SELECT id FROM alerts
       WHERE status = 'PENDING' AND next_attempt_at <= now()
       ORDER BY created_at
       LIMIT $1
       FOR UPDATE SKIP LOCKED
     )
     UPDATE alerts a SET next_attempt_at = now() + make_interval(secs => $2::double precision)
     FROM due WHERE a.id = due.id
     RETURNING a.id, a.chain, a.token_address, a.type, a.status, a.score, a.attempts, a.created_at, a.payload`,
    [limit, leaseSec],
  );
  return rows.map(toAlert);
}

export async function markAlertSent(db: Queryable, id: number, messageId: string | null): Promise<void> {
  await db.query(
    `UPDATE alerts SET status = 'SENT', sent_at = now(), attempts = attempts + 1, provider_message_id = $2, last_error = NULL
     WHERE id = $1`,
    [id, messageId],
  );
}

export async function markAlertRetry(db: Queryable, id: number, error: string, retryInSec: number): Promise<void> {
  await db.query(
    `UPDATE alerts SET attempts = attempts + 1, last_error = $2,
       next_attempt_at = now() + make_interval(secs => $3::double precision)
     WHERE id = $1`,
    [id, error.slice(0, 1_000), retryInSec],
  );
}

export async function markAlertFailed(db: Queryable, id: number, error: string): Promise<void> {
  await db.query(`UPDATE alerts SET status = 'FAILED', attempts = attempts + 1, last_error = $2 WHERE id = $1`, [
    id,
    error.slice(0, 1_000),
  ]);
}

export async function getAlert(db: Queryable, id: number): Promise<(StoredAlert & { lastError: string | null; providerMessageId: string | null }) | null> {
  const { rows } = await db.query<AlertRow & { last_error: string | null; provider_message_id: string | null }>(
    'SELECT * FROM alerts WHERE id = $1',
    [id],
  );
  const r = rows[0];
  return r ? { ...toAlert(r), lastError: r.last_error, providerMessageId: r.provider_message_id } : null;
}

export async function outboxBacklog(db: Queryable): Promise<number> {
  const { rows } = await db.query<{ n: string }>(`SELECT count(*) AS n FROM alerts WHERE status = 'PENDING'`);
  return Number(rows[0]!.n);
}
