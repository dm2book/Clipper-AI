import { describe, expect, it } from 'vitest';
import { loadConfig } from '../../src/config/env.js';
import { evaluateMomentum, evaluateNewToken, type NewTokenInput } from '../../src/core/alertRules.js';
import type { SafetySummary } from '../../src/core/safety.js';
import { detectMomentum, type MomentumSignal } from '../../src/momentum/engine.js';
import { DEFAULT_THRESHOLDS } from '../../src/momentum/thresholds.js';
import { baseEnv } from '../support/env.js';
import { makeHolders, makeSnapshot } from '../support/factories.js';

const cfg = loadConfig(baseEnv()).alerts;
const NOW = new Date('2026-01-01T12:10:00Z');
const safe: SafetySummary = { verdict: 'PASS', onchain: 'PASS', externalOk: 1, externalChecked: 1, reasons: [], providers: [] };

function newTokenInput(overrides: Partial<NewTokenInput> = {}): NewTokenInput {
  return {
    now: NOW,
    age: { ms: 6 * 60_000, source: 'onchain' },
    snapshot: makeSnapshot({ observedAt: new Date(NOW.getTime() - 10_000), liquidityUsd: 25_000 }),
    holders: makeHolders({ observedAt: new Date(NOW.getTime() - 30_000), holderCount: 60 }),
    safety: safe,
    ...overrides,
  };
}

const failed = (d: { gates: { name: string; passed: boolean }[] }) => d.gates.filter((g) => !g.passed).map((g) => g.name);

describe('NEW TOKEN rule (age < 10m, liquidity > $20k, ≥ 50 holders, safety passed)', () => {
  it('fires when every gate passes', () => {
    expect(evaluateNewToken(newTokenInput(), cfg).eligible).toBe(true);
  });

  it.each([
    ['leeftijd', { age: { ms: 10 * 60_000, source: 'onchain' as const } }],
    ['leeftijd', { age: null }],
    ['liquiditeit', { snapshot: makeSnapshot({ observedAt: NOW, liquidityUsd: 20_000 }) }],
    ['liquiditeit', { snapshot: makeSnapshot({ observedAt: NOW, liquidityUsd: null }) }],
    ['holders', { holders: makeHolders({ observedAt: NOW, holderCount: 49 }) }],
    ['holders', { holders: null }],
    ['holders', { holders: makeHolders({ observedAt: new Date(NOW.getTime() - 600_000), holderCount: 500 }) }],
    ['veiligheid', { safety: { ...safe, verdict: 'FAIL' as const, reasons: ['solana-onchain: freeze authority'] } }],
    ['veiligheid', { safety: { ...safe, verdict: 'UNKNOWN' as const } }],
    ['actuele marktdata', { snapshot: makeSnapshot({ observedAt: new Date(NOW.getTime() - 120_000), liquidityUsd: 50_000 }) }],
  ])('blocks on %s', (gate, overrides) => {
    const d = evaluateNewToken(newTokenInput(overrides as Partial<NewTokenInput>), cfg);
    expect(d.eligible).toBe(false);
    expect(failed(d)).toEqual([gate]);
  });

  it('accepts a safety WARN when configured to, and explains it', () => {
    const d = evaluateNewToken(newTokenInput({ safety: { ...safe, verdict: 'WARN', reasons: ['rugcheck: Mutable metadata (warn)'] } }), cfg);
    expect(d.eligible).toBe(true);
    expect(d.gates.find((g) => g.name === 'veiligheid')!.detail).toContain('Mutable metadata');
    expect(
      evaluateNewToken(newTokenInput({ safety: { ...safe, verdict: 'WARN' } }), { ...cfg, safetyAllowWarn: false }).eligible,
    ).toBe(false);
  });
});

describe('MOMENTUM rule', () => {
  // A real engine result as the base; only the fields under test are changed.
  const engine = detectMomentum(
    { chain: 'solana', address: 'T', tokenType: null, tokenAgeMs: null, snapshots: [], holders: [], trades: null },
    NOW.getTime(),
    DEFAULT_THRESHOLDS,
  );
  const signal: MomentumSignal = { ...engine, signalType: 'MOMENTUM', score: 72, confidence: 0.9 };
  const base = { now: NOW, snapshot: makeSnapshot({ observedAt: NOW, liquidityUsd: 30_000 }), signal, safety: safe, lastAlert: null };

  it('follows the engine verdict when safety and data freshness are fine', () => {
    expect(evaluateMomentum(base, cfg).eligible).toBe(true);
  });

  it.each([
    ['momentum-engine', { signal: { ...signal, signalType: 'NO_SIGNAL' as const } }],
    ['momentum-engine', { signal: { ...signal, signalType: 'FILTERED' as const } }],
    ['veiligheid', { safety: { ...safe, verdict: 'FAIL' as const } }],
    ['actuele marktdata', { snapshot: makeSnapshot({ observedAt: new Date(NOW.getTime() - 300_000), liquidityUsd: 30_000 }) }],
  ])('blocks on %s', (gate, overrides) => {
    expect(failed(evaluateMomentum({ ...base, ...overrides }, cfg))).toEqual([gate]);
  });

  it('names the filters that blocked the engine', () => {
    const blocked: MomentumSignal = {
      ...signal,
      signalType: 'FILTERED',
      filters: [{ id: 'wash_trading', passed: false, blocking: true, detail: 'x' }],
    };
    const gate = evaluateMomentum({ ...base, signal: blocked }, cfg).gates[0]!;
    expect(gate.detail).toContain('geblokkeerd door wash_trading');
  });

  it('respects the cooldown unless the score escalates', () => {
    const recent = { createdAt: new Date(NOW.getTime() - 5 * 60_000), score: 65 };
    expect(evaluateMomentum({ ...base, lastAlert: recent }, cfg).eligible).toBe(false);
    expect(evaluateMomentum({ ...base, signal: { ...signal, score: 80 }, lastAlert: recent }, cfg).eligible).toBe(true);
    const old = { createdAt: new Date(NOW.getTime() - 20 * 60_000), score: 90 };
    expect(evaluateMomentum({ ...base, lastAlert: old }, cfg).eligible).toBe(true);
  });
});
