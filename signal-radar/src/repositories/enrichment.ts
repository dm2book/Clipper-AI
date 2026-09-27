import type { HolderSnapshot } from '../core/holders.js';
import type { SafetyReport, SafetyVerdict } from '../core/safety.js';
import type { Chain } from '../core/types.js';
import { date, int, num, type Queryable } from '../infra/db.js';

export async function insertSafetyReport(db: Queryable, r: SafetyReport): Promise<void> {
  await db.query(
    `INSERT INTO safety_reports (chain, token_address, provider, kind, checked_at, verdict, flags, reasons, provider_score, raw)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)`,
    [
      r.chain,
      r.tokenAddress,
      r.provider,
      r.kind,
      r.checkedAt,
      r.verdict,
      JSON.stringify(r.flags),
      JSON.stringify(r.reasons),
      r.providerScore,
      r.raw === undefined ? null : JSON.stringify(r.raw),
    ],
  );
}

interface SafetyRow {
  chain: Chain;
  token_address: string;
  provider: string;
  kind: 'onchain' | 'external';
  checked_at: Date;
  verdict: SafetyVerdict;
  flags: SafetyReport['flags'];
  reasons: string[];
  provider_score: string | null;
  raw: unknown;
}

/** The newest report per provider. */
export async function latestSafetyReports(db: Queryable, chain: Chain, address: string): Promise<SafetyReport[]> {
  const { rows } = await db.query<SafetyRow>(
    `SELECT DISTINCT ON (provider) chain, token_address, provider, kind, checked_at, verdict, flags, reasons, provider_score, raw
     FROM safety_reports WHERE chain = $1 AND token_address = $2
     ORDER BY provider, checked_at DESC`,
    [chain, address],
  );
  return rows.map((r) => ({
    chain: r.chain,
    tokenAddress: r.token_address,
    provider: r.provider,
    kind: r.kind,
    checkedAt: date(r.checked_at)!,
    verdict: r.verdict,
    flags: r.flags,
    reasons: r.reasons,
    providerScore: num(r.provider_score),
    raw: r.raw,
    tokenInfo: null,
  }));
}

export async function insertHolderSnapshot(db: Queryable, h: HolderSnapshot): Promise<void> {
  await db.query(
    `INSERT INTO holder_snapshots (chain, token_address, observed_at, holder_count, holder_count_capped, top10_pct, method, top_holders)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
    [
      h.chain,
      h.tokenAddress,
      h.observedAt,
      h.holderCount,
      h.holderCountCapped,
      h.top10Pct,
      h.method,
      JSON.stringify(h.topHolders),
    ],
  );
}

interface HolderRow {
  chain: Chain;
  token_address: string;
  observed_at: Date;
  holder_count: number;
  holder_count_capped: boolean;
  top10_pct: string | null;
  method: string;
  top_holders: HolderSnapshot['topHolders'];
}

/** Newest first. */
export async function recentHolderSnapshots(db: Queryable, chain: Chain, address: string, since: Date): Promise<HolderSnapshot[]> {
  const { rows } = await db.query<HolderRow>(
    `SELECT * FROM holder_snapshots WHERE chain = $1 AND token_address = $2 AND observed_at >= $3
     ORDER BY observed_at DESC LIMIT 100`,
    [chain, address, since],
  );
  return rows.map((r) => ({
    chain: r.chain,
    tokenAddress: r.token_address,
    observedAt: date(r.observed_at)!,
    holderCount: int(r.holder_count)!,
    holderCountCapped: r.holder_count_capped,
    top10Pct: num(r.top10_pct),
    method: r.method,
    topHolders: r.top_holders,
  }));
}

export async function deleteHolderSnapshotsBefore(db: Queryable, cutoff: Date): Promise<number> {
  const { rowCount } = await db.query('DELETE FROM holder_snapshots WHERE observed_at < $1', [cutoff]);
  return rowCount ?? 0;
}

export async function recordProviderError(
  db: Queryable,
  e: { provider: string; endpoint: string; kind: string; message: string; sample: unknown },
): Promise<void> {
  await db.query(
    `INSERT INTO provider_errors (provider, endpoint, kind, message, sample) VALUES ($1, $2, $3, $4, $5)`,
    [e.provider, e.endpoint, e.kind, e.message.slice(0, 2_000), e.sample === undefined ? null : JSON.stringify(e.sample)],
  );
}

export async function deleteProviderErrorsBefore(db: Queryable, cutoff: Date): Promise<number> {
  const { rowCount } = await db.query('DELETE FROM provider_errors WHERE occurred_at < $1', [cutoff]);
  return rowCount ?? 0;
}
