/**
 * Transparent Signal Score (docs/ARCHITECTURE.md §G.1):
 *
 *   score = clamp( Σ weight_i · strength_i  −  Σ penalty_j , 0, 100 )
 *   strength_i = clamp( (metric_i − floor_i) / (full_i − floor_i), 0, 1 )
 *
 * No hidden factors, no model. The score measures how unusual the measured
 * activity is — NOT the probability that the price will rise.
 * Changing any number here means a new version string.
 */
import type { SafetyVerdict } from './safety.js';
import type { Measurement, SignalType } from './signals.js';

export interface ComponentSpec {
  type: SignalType;
  label: string;
  weight: number;
  floor: number;
  full: number;
}

export interface ScoringSpec {
  version: string;
  components: ComponentSpec[];
  penalties: {
    safetyWarn: number;
    top10AtOrAbovePct: number;
    top10: number;
    liquidityDropPct: number; // e.g. -20
    liquidityDrop: number;
  };
  /** A component counts as "active" at this strength. */
  activeStrength: number;
}

export const SCORING_V1: ScoringSpec = {
  version: 'scoring-v1',
  components: [
    { type: 'volume_spike', label: 'Volume-spike 5m', weight: 25, floor: 2, full: 8 },
    { type: 'tx_growth', label: 'Transactiegroei 5m', weight: 15, floor: 1.5, full: 5 },
    { type: 'liquidity_growth', label: 'Liquiditeitsgroei', weight: 20, floor: 10, full: 100 },
    { type: 'mcap_change', label: 'Market-cap-verandering', weight: 15, floor: 20, full: 200 },
    { type: 'buy_count_ratio', label: 'Aandeel aankopen 5m', weight: 10, floor: 0.55, full: 0.75 },
    { type: 'holder_growth', label: 'Holdergroei', weight: 15, floor: 10, full: 100 },
  ],
  penalties: { safetyWarn: 10, top10AtOrAbovePct: 40, top10: 15, liquidityDropPct: -20, liquidityDrop: 20 },
  activeStrength: 0.5,
};

export interface ScoreComponent {
  type: SignalType;
  label: string;
  weight: number;
  available: boolean;
  strength: number;
  points: number;
  metric: number | null;
  detail: string;
}

export interface ScoreResult {
  version: string;
  score: number;
  /** Share of total weight that had data (0–1). */
  confidence: number;
  activeComponents: number;
  components: ScoreComponent[];
  penalties: { reason: string; points: number }[];
}

export interface PenaltyInput {
  safetyVerdict: SafetyVerdict;
  top10Pct: number | null;
  liquidityChange5mPct: number | null;
}

export function strength(metric: number, floor: number, full: number): number {
  if (full === floor) return metric >= full ? 1 : 0;
  return Math.min(1, Math.max(0, (metric - floor) / (full - floor)));
}

const round1 = (v: number) => Math.round(v * 10) / 10;

export function computeScore(measurements: readonly Measurement[], input: PenaltyInput, spec: ScoringSpec = SCORING_V1): ScoreResult {
  const totalWeight = spec.components.reduce((a, c) => a + c.weight, 0);
  const byType = new Map(measurements.map((m) => [m.type, m]));
  const components: ScoreComponent[] = spec.components.map((c) => {
    const m = byType.get(c.type);
    const available = Boolean(m?.available && m.metric !== null);
    const s = available && m!.qualifies ? strength(m!.metric!, c.floor, c.full) : 0;
    return {
      type: c.type,
      label: c.label,
      weight: c.weight,
      available,
      strength: round1(s * 100) / 100,
      points: round1(c.weight * s),
      metric: m?.metric ?? null,
      detail: m ? (available && !m.qualifies ? `${m.detail} (onder absolute minimum)` : m.detail) : 'niet gemeten',
    };
  });

  const penalties: { reason: string; points: number }[] = [];
  const p = spec.penalties;
  if (input.safetyVerdict === 'WARN') penalties.push({ reason: 'veiligheidswaarschuwing', points: p.safetyWarn });
  if (input.top10Pct !== null && input.top10Pct >= p.top10AtOrAbovePct) {
    penalties.push({ reason: `top-10 bezit ${input.top10Pct.toFixed(1)}% (≥ ${p.top10AtOrAbovePct}%)`, points: p.top10 });
  }
  if (input.liquidityChange5mPct !== null && input.liquidityChange5mPct <= p.liquidityDropPct) {
    penalties.push({ reason: `liquiditeit ${input.liquidityChange5mPct.toFixed(1)}% in 5m`, points: p.liquidityDrop });
  }

  const raw = components.reduce((a, c) => a + c.points, 0) - penalties.reduce((a, x) => a + x.points, 0);
  const availableWeight = components.filter((c) => c.available).reduce((a, c) => a + c.weight, 0);
  return {
    version: spec.version,
    score: round1(Math.min(100, Math.max(0, raw))),
    confidence: Math.round((availableWeight / totalWeight) * 100) / 100,
    activeComponents: components.filter((c) => c.strength >= spec.activeStrength).length,
    components,
    penalties,
  };
}
