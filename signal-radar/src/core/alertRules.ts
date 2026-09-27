/**
 * Alert decisions (docs/ARCHITECTURE.md §G.6) as pure functions. Every gate
 * is reported with its outcome and the measured value, so an alert (or its
 * absence) can always be explained.
 */
import type { AlertSettings } from '../config/env.js';
import type { GateResult } from './alerts.js';
import type { HolderSnapshot } from './holders.js';
import { snapshotAgeMs, type MarketSnapshot } from './marketSnapshot.js';
import type { SafetySummary } from './safety.js';
import type { MomentumSignal } from '../momentum/engine.js';
import type { TokenAge } from './token.js';

export interface Decision {
  eligible: boolean;
  gates: GateResult[];
}

function safetyGate(safety: SafetySummary, allowWarn: boolean): GateResult {
  const ok = safety.verdict === 'PASS' || (allowWarn && safety.verdict === 'WARN');
  return {
    name: 'veiligheid',
    passed: ok,
    detail: `${safety.verdict}${safety.reasons.length ? `: ${safety.reasons.slice(0, 3).join('; ')}` : ''}`,
  };
}

function freshnessGate(snapshot: MarketSnapshot, now: Date, maxAgeSec: number): GateResult {
  const ageSec = snapshotAgeMs(snapshot, now) / 1000;
  return { name: 'actuele marktdata', passed: ageSec <= maxAgeSec, detail: `${ageSec.toFixed(0)}s oud (max ${maxAgeSec}s)` };
}

export interface NewTokenInput {
  now: Date;
  age: TokenAge | null;
  snapshot: MarketSnapshot;
  holders: HolderSnapshot | null;
  safety: SafetySummary;
}

/** NEW TOKEN: age < max, liquidity > min, holders ≥ min, safety passed, data fresh. */
export function evaluateNewToken(input: NewTokenInput, cfg: AlertSettings): Decision {
  const { now, age, snapshot, holders, safety } = input;
  const maxAgeMs = cfg.newToken.maxAgeMinutes * 60_000;
  const liq = snapshot.liquidityUsd;
  const holdersAgeSec = holders ? (now.getTime() - holders.observedAt.getTime()) / 1000 : null;
  const gates: GateResult[] = [
    age === null
      ? { name: 'leeftijd', passed: false, detail: 'onbekend' }
      : {
          name: 'leeftijd',
          passed: age.ms >= 0 && age.ms < maxAgeMs,
          detail: `${(age.ms / 60_000).toFixed(1)} min (${age.source === 'onchain' ? 'on-chain' : 'volgens marktdata'}, max ${cfg.newToken.maxAgeMinutes})`,
        },
    {
      name: 'liquiditeit',
      passed: liq !== null && liq > cfg.newToken.minLiquidityUsd,
      detail: liq === null ? 'onbekend' : `$${Math.round(liq)} (min > $${cfg.newToken.minLiquidityUsd})`,
    },
    {
      name: 'holders',
      passed: holders !== null && holders.holderCount >= cfg.newToken.minHolders && holdersAgeSec! <= cfg.holdersMaxAgeSec,
      detail:
        holders === null
          ? 'nog niet gemeten'
          : `${holders.holderCount} (min ${cfg.newToken.minHolders}; meting ${holdersAgeSec!.toFixed(0)}s oud)`,
    },
    safetyGate(safety, cfg.safetyAllowWarn),
    freshnessGate(snapshot, now, cfg.marketDataMaxAgeSec),
  ];
  return { eligible: gates.every((g) => g.passed), gates };
}

export interface MomentumInput {
  now: Date;
  snapshot: MarketSnapshot;
  /** Output of the Momentum Detection Engine for this token. */
  signal: MomentumSignal;
  safety: SafetySummary;
  /** The last MOMENTUM alert for this token, if any. */
  lastAlert: { createdAt: Date; score: number | null } | null;
}

/**
 * MOMENTUM alert: the engine must report MOMENTUM (score, rule breadth,
 * confidence and false-positive filters are its job); on top of that the
 * alert needs passing safety, fresh data, and must respect the cooldown
 * unless the score escalates.
 */
export function evaluateMomentum(input: MomentumInput, cfg: AlertSettings): Decision {
  const { now, snapshot, signal, safety, lastAlert } = input;
  const blocked = signal.filters.filter((f) => f.blocking).map((f) => f.id);
  const gates: GateResult[] = [
    {
      name: 'momentum-engine',
      passed: signal.signalType === 'MOMENTUM',
      detail:
        `${signal.signalType}: score ${signal.score}, ${signal.triggeredRules.length} regels, ` +
        `confidence ${signal.confidence}${blocked.length ? `, geblokkeerd door ${blocked.join(', ')}` : ''}`,
    },
    safetyGate(safety, cfg.safetyAllowWarn),
    freshnessGate(snapshot, now, cfg.marketDataMaxAgeSec),
  ];
  if (lastAlert) {
    const sinceMin = (now.getTime() - lastAlert.createdAt.getTime()) / 60_000;
    const inCooldown = sinceMin < cfg.cooldownMinutes;
    const escalates = lastAlert.score !== null && signal.score >= lastAlert.score + cfg.escalationPoints;
    gates.push({
      name: 'cooldown',
      passed: !inCooldown || escalates,
      detail: inCooldown
        ? escalates
          ? `escalatie: ${signal.score} ≥ ${lastAlert.score} + ${cfg.escalationPoints}`
          : `vorige alert ${sinceMin.toFixed(1)} min geleden (cooldown ${cfg.cooldownMinutes} min)`
        : 'buiten cooldown',
    });
  }
  return { eligible: gates.every((g) => g.passed), gates };
}
