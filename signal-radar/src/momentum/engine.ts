/**
 * Momentum Detection Engine.
 *
 * Detects EXCEPTIONAL CURRENT ACTIVITY from stored snapshots. It does not
 * predict prices and its output is not a buy signal or a return estimate.
 *
 * Score, fully explainable:
 *   rule points  = triggered ? weight × (0.5 + 0.5 × strength) : 0
 *   strength     = clamp((value − threshold) / (fullAt − threshold), 0, 1)
 *   score        = clamp(Σ rule points − Σ penalty points, 0, 100)
 * A triggered rule is worth between half and all of its weight; weights sum
 * to 100 by default. Every rule, filter and penalty is in the output.
 *
 * Confidence (0–1) describes data coverage, not likelihood:
 *   ruleCoverage   = Σ weights of rules with data / Σ weights
 *   filterCoverage = filters that could be evaluated / all filters
 *   confidence     = ruleCoverage × (0.7 + 0.3 × filterCoverage)
 */
import { WINDOW_MS, clamp01, type WindowId } from './indicators.js';
import { computeAllWindows, type TokenSeries, type WindowMetrics } from './metrics.js';
import type { MomentumThresholds, RuleId } from './thresholds.js';

export const ENGINE_VERSION = 'momentum-v1';
export const MOMENTUM_DISCLAIMER =
  'Meting van uitzonderlijke actuele activiteit. Geen voorspelling, geen koopadvies en geen gegarandeerd rendement.';

export type SignalType = 'MOMENTUM' | 'FILTERED' | 'NO_SIGNAL';

export interface RuleResult {
  id: RuleId;
  label: string;
  window: WindowId;
  value: number | null;
  threshold: number;
  fullAt: number;
  weight: number;
  available: boolean;
  triggered: boolean;
  strength: number;
  points: number;
  /** Human-readable measurement, e.g. "volume +428% (5m: $52.8k vs $10.0k)". */
  detail: string;
}

export type FilterId =
  | 'min_liquidity'
  | 'min_holders'
  | 'min_volume'
  | 'min_transactions'
  | 'min_unique_buyers'
  | 'wash_trading';

export interface FilterResult {
  id: FilterId;
  /** true = passed, false = failed, null = could not be evaluated. */
  passed: boolean | null;
  /** Whether this result blocks the signal. */
  blocking: boolean;
  detail: string;
}

export interface Penalty {
  id: string;
  points: number;
  reason: string;
}

export interface MomentumSignal {
  token: { chain: string; address: string; tokenType: string | null };
  timestamp: string;
  signalType: SignalType;
  engineVersion: string;
  primaryWindow: WindowId;
  /** Threshold layers that applied, e.g. ["default", "solana"]. */
  profile: string[];
  metrics: Record<WindowId, WindowMetrics>;
  triggeredRules: RuleResult[];
  /** Every rule, triggered or not, so the score can be recomputed by hand. */
  rules: RuleResult[];
  filters: FilterResult[];
  penalties: Penalty[];
  score: number;
  confidence: number;
  /** Why the score is what it is: one line per triggered rule. */
  reasons: string[];
  /** Data gaps, excluded trades, failed filters. */
  warnings: string[];
  disclaimer: string;
}

// --- formatting -------------------------------------------------------------------

export function fmtUsd(v: number): string {
  const a = Math.abs(v);
  if (a >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (a >= 1_000) return `$${(v / 1_000).toFixed(1)}k`;
  return `$${v.toFixed(0)}`;
}
const fmtPct = (v: number) => `${v >= 0 ? '+' : ''}${v.toFixed(0)}%`;
const fmtNum = (v: number) => (Number.isInteger(v) ? String(v) : v.toFixed(1));
const round1 = (v: number) => Math.round(v * 10) / 10;

// --- rules -------------------------------------------------------------------------

interface RuleSpec {
  id: RuleId;
  label: string;
  threshold: (t: MomentumThresholds) => number;
  fullAt: (t: MomentumThresholds) => number;
  value: (m: WindowMetrics) => number | null;
  describe: (m: WindowMetrics, value: number) => string;
}

const pctRule = (
  id: RuleId,
  label: string,
  threshold: (t: MomentumThresholds) => number,
  value: (m: WindowMetrics) => number | null,
  describe: (m: WindowMetrics, value: number) => string,
): RuleSpec => ({ id, label, threshold, fullAt: (t) => threshold(t) * t.fullStrengthFactor, value, describe });

const RULES: RuleSpec[] = [
  pctRule('volume_spike', 'Volume-spike', (t) => t.volumeSpikePct, (m) => m.volumeUsd.pct, (m, v) =>
    `volume ${fmtPct(v)} (${m.window}: ${fmtUsd(m.volumeUsd.current!)} vs ${fmtUsd(m.volumeUsd.previous!)})`),
  pctRule('tx_spike', 'Transactiegroei', (t) => t.txSpikePct, (m) => m.transactions.pct, (m, v) =>
    `transactions ${fmtPct(v)} (${m.window}: ${fmtNum(m.transactions.current!)} vs ${fmtNum(m.transactions.previous!)})`),
  pctRule('buyer_growth', 'Groei unieke kopers', (t) => t.buyerGrowthPct, (m) => m.uniqueBuyers.pct, (m, v) =>
    `unique buyers ${fmtPct(v)} (${m.window}: ${m.uniqueBuyers.current} vs ${m.uniqueBuyers.previous})`),
  pctRule('holder_growth', 'Holdergroei', (t) => t.holderGrowthPct, (m) => m.holders.pct, (m, v) =>
    `holder growth ${fmtPct(v)} (${m.window}: ${m.holders.previous} → ${m.holders.current})`),
  pctRule('liquidity_growth', 'Liquiditeitsgroei', (t) => t.liquidityGrowthPct, (m) => m.liquidityUsd.pct, (m, v) =>
    `liquidity ${fmtPct(v)} (${m.window}: ${fmtUsd(m.liquidityUsd.previous!)} → ${fmtUsd(m.liquidityUsd.current!)})`),
  pctRule('market_cap_change', 'Market-cap-verandering', (t) => t.marketCapChangePct, (m) => m.marketCapUsd.pct, (m, v) =>
    `${m.marketCapUsd.basis === 'fdv' ? 'FDV' : 'market cap'} ${fmtPct(v)} (${m.window}: ${fmtUsd(m.marketCapUsd.previous!)} → ${fmtUsd(m.marketCapUsd.current!)})`),
  {
    id: 'buy_pressure',
    label: 'Aandeel aankopen',
    threshold: (t) => t.buyShare,
    fullAt: (t) => t.buyShareFull,
    value: (m) => m.buyShare,
    describe: (m, v) => `buy share ${(v * 100).toFixed(0)}% (${m.window}: ${fmtNum(m.buys.current!)} buys / ${fmtNum(m.sells.current!)} sells)`,
  },
  pctRule('volume_acceleration', 'Volume-acceleratie', (t) => t.volumeAccelerationPct, (m) => m.volumeUsd.acceleration, (m, v) =>
    `volume acceleration ${v >= 0 ? '+' : ''}${v.toFixed(0)} pp (${m.window})`),
  pctRule('tx_acceleration', 'Transactie-acceleratie', (t) => t.txAccelerationPct, (m) => m.transactions.acceleration, (m, v) =>
    `transaction acceleration ${v >= 0 ? '+' : ''}${v.toFixed(0)} pp (${m.window})`),
];

function evaluateRule(spec: RuleSpec, m: WindowMetrics, t: MomentumThresholds): RuleResult {
  const threshold = spec.threshold(t);
  const fullAt = spec.fullAt(t);
  const weight = t.weights[spec.id];
  const value = spec.value(m);
  const available = value !== null && Number.isFinite(value);
  const triggered = available && value >= threshold;
  const strength = triggered ? clamp01((value - threshold) / (fullAt - threshold)) : 0;
  return {
    id: spec.id,
    label: spec.label,
    window: m.window,
    // 3 decimals: enough for fractions (buy share) and percentages alike
    value: available ? Math.round(value * 1000) / 1000 : null,
    threshold,
    fullAt,
    weight,
    available,
    triggered,
    strength: Math.round(strength * 100) / 100,
    points: triggered ? round1(weight * (0.5 + 0.5 * strength)) : 0,
    detail: available ? spec.describe(m, value) : `${spec.label}: geen data (${m.window})`,
  };
}

// --- filters -------------------------------------------------------------------------

function evaluateFilters(m: WindowMetrics, t: MomentumThresholds): FilterResult[] {
  const hasTrades = m.flowSource === 'trades';
  const required = (id: FilterId, value: number | null, min: number, describe: (v: number) => string): FilterResult => ({
    id,
    passed: value === null ? false : value >= min,
    blocking: value === null || value < min,
    detail: value === null ? `${id}: onbekend (min ${min})` : describe(value),
  });
  const filters: FilterResult[] = [
    required('min_liquidity', m.liquidityUsd.current, t.minLiquidityUsd, (v) => `liquiditeit ${fmtUsd(v)} (min ${fmtUsd(t.minLiquidityUsd)})`),
    required('min_holders', m.holders.current, t.minHolders, (v) => `${v} holders (min ${t.minHolders})`),
    required('min_volume', m.volumeUsd.current, t.minWindowVolumeUsd, (v) =>
      `volume ${m.window} ${fmtUsd(v)} (min ${fmtUsd(t.minWindowVolumeUsd)})`),
    required('min_transactions', m.transactions.current, t.minWindowTransactions, (v) =>
      `${fmtNum(v)} transacties in ${m.window} (min ${t.minWindowTransactions})`),
  ];

  // Trade-based filters: without trade data they cannot run.
  if (!hasTrades) {
    const detail = `niet gemeten: geen trade-data (flow uit ${m.flowSource ?? 'geen bron'})`;
    filters.push({ id: 'min_unique_buyers', passed: null, blocking: t.requireTradeData, detail });
    filters.push({ id: 'wash_trading', passed: null, blocking: t.requireTradeData, detail });
    return filters;
  }
  filters.push(
    required('min_unique_buyers', m.uniqueBuyers.current, t.minUniqueBuyers, (v) =>
      `${v} unieke kopers in ${m.window} (min ${t.minUniqueBuyers})`),
  );
  const wash = m.wash;
  const suspicious: string[] = [];
  if (wash) {
    if (wash.topWalletsShare > t.washMaxTopWalletsShare) {
      suspicious.push(`top-3 wallets ${(wash.topWalletsShare * 100).toFixed(0)}% van het volume`);
    }
    if (wash.roundTripShare > t.washMaxRoundTripShare) {
      suspicious.push(`${(wash.roundTripShare * 100).toFixed(0)}% van het volume heen-en-terug door dezelfde wallets`);
    }
    if (wash.transactionsPerWallet > t.washMaxTransactionsPerWallet) {
      suspicious.push(`${wash.transactionsPerWallet.toFixed(1)} transacties per wallet`);
    }
  }
  filters.push({
    id: 'wash_trading',
    passed: wash ? suspicious.length === 0 : null,
    blocking: suspicious.length > 0 || (!wash && t.requireTradeData),
    detail: !wash ? 'geen trades in het venster' : suspicious.length ? `wash-trading-vermoeden: ${suspicious.join('; ')}` : 'geen wash-trading-patroon',
  });
  return filters;
}

// --- engine -----------------------------------------------------------------------------

export function detectMomentum(
  series: TokenSeries,
  nowMs: number,
  thresholds: MomentumThresholds,
  profile: string[] = ['default'],
): MomentumSignal {
  const t = thresholds;
  const metrics = computeAllWindows(series, nowMs, {
    singleTradeMaxShare: t.singleTradeMaxShare,
    singleTradeMedianMultiple: t.singleTradeMedianMultiple,
  });
  const m = metrics[t.primaryWindow];

  const rules = RULES.map((spec) => evaluateRule(spec, m, t));
  const triggered = rules.filter((r) => r.triggered).sort((a, b) => b.points - a.points);
  const filters = evaluateFilters(m, t);

  const penalties: Penalty[] = [];
  if (m.liquidityUsd.pct !== null && m.liquidityUsd.pct <= -t.liquidityDropPct) {
    penalties.push({ id: 'liquidity_decline', points: 20, reason: `liquidity ${fmtPct(m.liquidityUsd.pct)} in ${m.window}` });
  }
  if (m.marketCapUsd.pct !== null && m.marketCapUsd.pct <= -t.marketCapChangePct) {
    penalties.push({ id: 'market_cap_decline', points: 10, reason: `market cap ${fmtPct(m.marketCapUsd.pct)} in ${m.window}` });
  }

  const rawScore = rules.reduce((a, r) => a + r.points, 0) - penalties.reduce((a, p) => a + p.points, 0);
  const score = round1(Math.min(100, Math.max(0, rawScore)));
  const totalWeight = rules.reduce((a, r) => a + r.weight, 0);
  const ruleCoverage = totalWeight > 0 ? rules.filter((r) => r.available).reduce((a, r) => a + r.weight, 0) / totalWeight : 0;
  const filterCoverage = filters.filter((f) => f.passed !== null).length / filters.length;
  const confidence = Math.round(ruleCoverage * (0.7 + 0.3 * filterCoverage) * 100) / 100;

  const qualifies = score >= t.minScore && triggered.length >= t.minTriggeredRules && confidence >= t.minConfidence;
  const blocked = filters.filter((f) => f.blocking);
  const signalType: SignalType = !qualifies ? 'NO_SIGNAL' : blocked.length ? 'FILTERED' : 'MOMENTUM';

  const warnings: string[] = [];
  if (m.flowSource === null) warnings.push(`geen flow-data voor ${m.window} (volume/transacties onbekend)`);
  if (m.flowSource !== 'trades') warnings.push('unieke kopers/verkopers en wash-trading niet gemeten: geen trade-data');
  if (m.flowNormalizedFromSec !== null && Math.abs(m.flowNormalizedFromSec - WINDOW_MS[m.window] / 1000) > 1) {
    warnings.push(`flow ${m.window} geschaald vanaf een meetspanne van ${m.flowNormalizedFromSec}s`);
  }
  if (m.volumeUsd.excludedTrades > 0) {
    warnings.push(`${m.volumeUsd.excludedTrades} extreme trade(s) uitgesloten van volume (${fmtUsd(m.volumeUsd.excludedVolumeUsd)})`);
  }
  for (const f of blocked) warnings.push(`filter ${f.id}: ${f.detail}`);
  for (const p of penalties) warnings.push(`aftrek −${p.points}: ${p.reason}`);

  return {
    token: { chain: series.chain, address: series.address, tokenType: series.tokenType },
    timestamp: new Date(nowMs).toISOString(),
    signalType,
    engineVersion: ENGINE_VERSION,
    primaryWindow: t.primaryWindow,
    profile,
    metrics,
    triggeredRules: triggered,
    rules,
    filters,
    penalties,
    score,
    confidence,
    reasons: triggered.map((r) => r.detail),
    warnings,
    disclaimer: MOMENTUM_DISCLAIMER,
  };
}

/** Plain-text explanation, e.g. for logs: "Momentum Score: 82" + reasons. */
export function explain(signal: MomentumSignal): string {
  const lines = [
    `Momentum Score: ${Math.round(signal.score)} (${signal.signalType}, confidence ${signal.confidence})`,
    'Reasons:',
    ...signal.reasons.map((r) => `* ${r}`),
  ];
  if (signal.warnings.length) lines.push('Warnings:', ...signal.warnings.map((w) => `* ${w}`));
  lines.push(signal.disclaimer);
  return lines.join('\n');
}
