/**
 * Wallet classification against configured, measurable criteria. The result
 * lists every criterion with its value and threshold, so "why is this wallet
 * tracked?" always has a concrete answer. There is deliberately no
 * "smart money" label: QUALIFIED means "met the configured criteria over the
 * stats window", nothing more — past results do not predict future ones.
 */
import type { WalletCriteria } from './criteria.js';
import type { WalletStats } from './stats.js';

export type WalletClass = 'INSUFFICIENT_HISTORY' | 'BOT_LIKE' | 'NOT_QUALIFIED' | 'QUALIFIED';

export interface CriterionResult {
  id: string;
  label: string;
  value: number | null;
  threshold: number;
  comparison: '>=' | '<=';
  passed: boolean;
}

export interface Classification {
  classification: WalletClass;
  criteriaVersion: string;
  criteria: CriterionResult[];
  /** Readable one-liner, e.g. "voldoet aan volgcriteria wallet-criteria-v1 (34 posities, …)". */
  summary: string;
}

const pct = (v: number | null) => (v === null ? '—' : `${(v * 100).toFixed(0)}%`);

export function classifyWallet(stats: WalletStats, c: WalletCriteria): Classification {
  const check = (
    id: string,
    label: string,
    value: number | null,
    threshold: number,
    comparison: '>=' | '<=',
  ): CriterionResult => ({
    id,
    label,
    value,
    threshold,
    comparison,
    passed: value !== null && (comparison === '>=' ? value >= threshold : value <= threshold),
  });
  const criteria: CriterionResult[] = [
    check('closed_positions', 'betrouwbare gesloten posities', stats.reliableClosedPositions, c.minClosedPositions, '>='),
    check('trades_per_day', 'trades per dag', stats.tradesPerDay, c.maxTradesPerDay, '<='),
    check('win_rate', 'winrate', stats.winRate, c.minWinRate, '>='),
    check('win_rate_lower_bound', 'winrate ondergrens (90% Wilson)', stats.winRateLowerBound, c.minWinRateLowerBound, '>='),
    check('avg_return', 'gemiddelde return %', stats.avgReturnPct, c.minAvgReturnPct, '>='),
    check('median_return', 'mediaan return %', stats.medianReturnPct, c.minMedianReturnPct, '>='),
    check('realized_pnl', 'gerealiseerde PnL USD', stats.realizedPnlUsd, c.minRealizedPnlUsd, '>='),
    check('largest_win_share', 'aandeel grootste winst in totale winst', stats.largestWinShare, c.maxLargestWinShare, '<='),
  ];

  let classification: WalletClass;
  if (!criteria[0]!.passed) classification = 'INSUFFICIENT_HISTORY';
  else if (!criteria[1]!.passed) classification = 'BOT_LIKE';
  else classification = criteria.every((x) => x.passed) ? 'QUALIFIED' : 'NOT_QUALIFIED';

  const sample = `${stats.reliableClosedPositions} posities in ${c.statsWindowDays}d`;
  const summary =
    classification === 'QUALIFIED'
      ? `voldoet aan volgcriteria ${c.version} (${sample}, winrate ${pct(stats.winRate)}, gem. return ${stats.avgReturnPct?.toFixed(0)}%)`
      : classification === 'INSUFFICIENT_HISTORY'
        ? `te weinig historie (${sample}, minimaal ${c.minClosedPositions})`
        : classification === 'BOT_LIKE'
          ? `handelt als bot (${stats.tradesPerDay.toFixed(0)} trades/dag)`
          : `voldoet niet aan: ${criteria.filter((x) => !x.passed).map((x) => x.label).join(', ')}`;
  return { classification, criteriaVersion: c.version, criteria, summary };
}
