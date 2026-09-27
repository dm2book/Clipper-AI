/**
 * SafetyProvider backed by RugCheck (Solana only).
 *
 * Endpoint (api.rugcheck.xyz/swagger):  GET /v1/tokens/{mint}/report/summary
 * Optional API key sent as `X-API-KEY`.
 *
 * What is used, and how sure we are:
 *  - `risks[]` with `name`, `level` (e.g. "warn", "danger"): documented; the
 *    verdict is based ONLY on these levels.
 *  - `score_normalised`: stored for reference, NOT used for decisions. Public
 *    descriptions disagree on its direction (higher = riskier vs. safer), so
 *    gating on it would be guesswork until verified with recorded responses.
 *  - Rate limits are not documented publicly; the default budget is
 *    conservative (RUGCHECK_REQUESTS_PER_MINUTE).
 * STATUS: not yet run against a recorded live response (docs/PROVIDERS.md).
 */
import { z } from 'zod';
import type { SafetyReport } from '../../core/safety.js';
import type { Chain } from '../../core/types.js';
import type { HttpClient } from '../../infra/http.js';
import type { SafetyProvider } from '../interfaces.js';

export const summarySchema = z.looseObject({
  score: z.number().nullable().optional(),
  score_normalised: z.number().nullable().optional(),
  risks: z
    .array(
      z.looseObject({
        name: z.string(),
        level: z.string().nullable().optional(),
        description: z.string().nullable().optional(),
      }),
    )
    .nullable()
    .optional(),
});
export type RugCheckSummary = z.infer<typeof summarySchema>;

const FAIL_LEVELS = new Set(['danger', 'critical']);
const WARN_LEVELS = new Set(['warn', 'warning']);

/** Pure: summary -> report fields. */
export function mapSummary(summary: RugCheckSummary): Pick<SafetyReport, 'verdict' | 'flags' | 'reasons' | 'providerScore'> {
  const providerScore = summary.score_normalised ?? summary.score ?? null;
  if (!Array.isArray(summary.risks)) {
    return { verdict: 'UNKNOWN', flags: {}, reasons: ['summary has no risk list'], providerScore };
  }
  const level = (l: string | null | undefined) => (l ?? '').toLowerCase();
  const danger = summary.risks.filter((r) => FAIL_LEVELS.has(level(r.level)));
  const warn = summary.risks.filter((r) => WARN_LEVELS.has(level(r.level)));
  return {
    verdict: danger.length ? 'FAIL' : warn.length ? 'WARN' : 'PASS',
    flags: { provider_danger: danger.length > 0 },
    reasons: [...danger, ...warn].map((r) => `${r.name} (${level(r.level)})`),
    providerScore,
  };
}

export class RugCheckSafetyProvider implements SafetyProvider {
  readonly name = 'rugcheck';
  readonly kind = 'external' as const;

  constructor(
    private readonly http: HttpClient,
    private readonly baseUrl: string,
    private readonly apiKey: string | null,
  ) {}

  async check(chain: Chain, address: string, signal?: AbortSignal): Promise<SafetyReport> {
    if (chain !== 'solana') throw new Error('rugcheck only supports solana');
    const summary = await this.http.requestJson({
      url: `${this.baseUrl}/tokens/${encodeURIComponent(address)}/report/summary`,
      endpoint: 'report_summary',
      schema: summarySchema,
      ...(this.apiKey ? { headers: { 'X-API-KEY': this.apiKey } } : {}),
      ...(signal ? { signal } : {}),
    });
    return {
      chain,
      tokenAddress: address,
      provider: this.name,
      kind: this.kind,
      checkedAt: new Date(),
      ...mapSummary(summary),
      raw: summary,
      tokenInfo: null,
    };
  }
}
