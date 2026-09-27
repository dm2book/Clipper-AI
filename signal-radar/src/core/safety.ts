import type { Chain } from './types.js';

export const VERDICTS = ['PASS', 'WARN', 'FAIL', 'UNKNOWN'] as const;
export type SafetyVerdict = (typeof VERDICTS)[number];

/**
 * Provider-neutral risk flags. `true` = the risk is present, `false` = checked
 * and absent, `null`/missing = not checked or not reported.
 */
export const SAFETY_FLAGS = [
  'mint_authority_active',
  'freeze_authority_active',
  'permanent_delegate',
  'transfer_hook',
  'transfer_fee',
  'non_transferable',
  'default_account_frozen',
  'pausable',
  'balance_mutable',
  'closable',
  'metadata_mutable',
  'provider_danger',
] as const;
export type SafetyFlag = (typeof SAFETY_FLAGS)[number];
export type SafetyFlags = Partial<Record<SafetyFlag, boolean | null>>;

export interface TokenInfo {
  decimals: number | null;
  supply: string | null;
  tokenProgram: string | null;
}

export interface SafetyReport {
  chain: Chain;
  tokenAddress: string;
  provider: string;
  kind: 'onchain' | 'external';
  checkedAt: Date;
  verdict: SafetyVerdict;
  flags: SafetyFlags;
  /** Human-readable findings. May contain provider text: treat as untrusted. */
  reasons: string[];
  providerScore: number | null;
  raw: unknown;
  /** On-chain checks also learn decimals/supply/program; external ones leave this null. */
  tokenInfo: TokenInfo | null;
}

export interface SafetySummary {
  verdict: SafetyVerdict;
  onchain: SafetyVerdict;
  /** External providers that answered PASS or WARN. */
  externalOk: number;
  externalChecked: number;
  reasons: string[];
  providers: { provider: string; kind: 'onchain' | 'external'; verdict: SafetyVerdict; checkedAt: Date }[];
}

/**
 * Combine the latest report of each provider (docs/ARCHITECTURE.md §E):
 *  - any FAIL                                  -> FAIL
 *  - on-chain check missing/UNKNOWN            -> UNKNOWN (on-chain facts are authoritative)
 *  - fewer than `minExternal` external answers -> UNKNOWN
 *  - otherwise WARN if anything warned, else PASS
 * Missing data never turns into PASS.
 */
export function aggregateSafety(reports: readonly SafetyReport[], minExternal: number): SafetySummary {
  const providers = reports.map((r) => ({
    provider: r.provider,
    kind: r.kind,
    verdict: r.verdict,
    checkedAt: r.checkedAt,
  }));
  const onchainReports = reports.filter((r) => r.kind === 'onchain');
  const external = reports.filter((r) => r.kind === 'external');
  const onchain: SafetyVerdict = onchainReports.some((r) => r.verdict === 'FAIL')
    ? 'FAIL'
    : onchainReports.find((r) => r.verdict !== 'UNKNOWN')?.verdict ?? 'UNKNOWN';
  const externalOk = external.filter((r) => r.verdict === 'PASS' || r.verdict === 'WARN').length;
  const externalChecked = external.filter((r) => r.verdict !== 'UNKNOWN').length;
  const tag = (r: SafetyReport) => r.reasons.map((reason) => `${r.provider}: ${reason}`);

  const failing = reports.filter((r) => r.verdict === 'FAIL');
  if (failing.length) {
    return { verdict: 'FAIL', onchain, externalOk, externalChecked, reasons: failing.flatMap(tag), providers };
  }
  if (onchain === 'UNKNOWN') {
    return { verdict: 'UNKNOWN', onchain, externalOk, externalChecked, reasons: ['on-chain check not available'], providers };
  }
  if (externalOk < minExternal) {
    return {
      verdict: 'UNKNOWN',
      onchain,
      externalOk,
      externalChecked,
      reasons: [`${externalOk} of ${minExternal} required external safety checks available`],
      providers,
    };
  }
  const warnings = reports.filter((r) => r.verdict === 'WARN');
  return {
    verdict: warnings.length ? 'WARN' : 'PASS',
    onchain,
    externalOk,
    externalChecked,
    reasons: warnings.flatMap(tag),
    providers,
  };
}

/** Verdict from flags: any `fail` flag set -> FAIL, any `warn` flag -> WARN, nothing known -> UNKNOWN. */
export function verdictFromFlags(
  flags: SafetyFlags,
  failFlags: readonly SafetyFlag[],
  warnFlags: readonly SafetyFlag[],
): SafetyVerdict {
  const known = Object.values(flags).filter((v) => v !== null && v !== undefined);
  if (known.length === 0) return 'UNKNOWN';
  if (failFlags.some((f) => flags[f] === true)) return 'FAIL';
  if (warnFlags.some((f) => flags[f] === true)) return 'WARN';
  return 'PASS';
}
