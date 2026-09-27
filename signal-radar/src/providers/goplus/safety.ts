/**
 * SafetyProvider backed by GoPlus Security — Solana Token Security API (BETA).
 *
 * Endpoint (docs.gopluslabs.io/reference/solanatokensecurityusingget):
 *   GET /api/v1/solana/token_security?contract_addresses={mint}
 *
 * What is used, and how sure we are:
 *  - Envelope `{ code, message, result }` with `code === 1` for success: the
 *    general GoPlus response format.
 *  - Result entries keyed by address with risk fields `mintable`, `freezable`,
 *    `balance_mutable_authority`, `non_transferable`, `transfer_hook`,
 *    `transfer_fee`, `closable`, `metadata_mutable`: field names are
 *    documented; that each carries `status: "0" | "1"` is documented for
 *    mintable/freezable. The exact nesting is NOT verified against a recorded
 *    response. Fields that do not parse count as unknown; if nothing parses
 *    the verdict is UNKNOWN and a schema warning is recorded — never PASS.
 *  - Observed behaviour (third-party report): the Solana route answers only
 *    the FIRST address in `contract_addresses`, so this provider sends one.
 *  - Optional access token sent in the `Authorization` header.
 */
import { z } from 'zod';
import { SchemaError, TransientError } from '../../core/errors.js';
import { verdictFromFlags, type SafetyFlag, type SafetyFlags, type SafetyReport } from '../../core/safety.js';
import type { Chain } from '../../core/types.js';
import type { HttpClient } from '../../infra/http.js';
import type { SafetyProvider } from '../interfaces.js';

export const envelopeSchema = z.looseObject({
  code: z.number(),
  message: z.string().nullable().optional(),
  result: z.record(z.string(), z.unknown()).nullable().optional(),
});

const FIELD_FLAGS: [field: string, flag: SafetyFlag][] = [
  ['mintable', 'mint_authority_active'],
  ['freezable', 'freeze_authority_active'],
  ['balance_mutable_authority', 'balance_mutable'],
  ['non_transferable', 'non_transferable'],
  ['transfer_hook', 'transfer_hook'],
  ['transfer_fee', 'transfer_fee'],
  ['closable', 'closable'],
  ['metadata_mutable', 'metadata_mutable'],
];
const FAIL_FLAGS: SafetyFlag[] = [
  'mint_authority_active',
  'freeze_authority_active',
  'balance_mutable',
  'non_transferable',
  'transfer_hook',
];
const WARN_FLAGS: SafetyFlag[] = ['transfer_fee', 'closable', 'metadata_mutable'];

/** `{ status: "1" }` -> true, `{ status: "0" }` -> false, anything else -> null (unknown). */
export function readStatus(value: unknown): boolean | null {
  if (!value || typeof value !== 'object' || !('status' in value)) return null;
  const s = String((value as { status: unknown }).status);
  return s === '1' ? true : s === '0' ? false : null;
}

/** Pure: result entry -> report fields. */
export function mapEntry(entry: unknown): Pick<SafetyReport, 'verdict' | 'flags' | 'reasons' | 'providerScore'> {
  const flags: SafetyFlags = {};
  const reasons: string[] = [];
  if (entry && typeof entry === 'object') {
    for (const [field, flag] of FIELD_FLAGS) {
      const v = readStatus((entry as Record<string, unknown>)[field]);
      flags[flag] = v;
      if (v === true) reasons.push(field);
    }
  }
  return { verdict: verdictFromFlags(flags, FAIL_FLAGS, WARN_FLAGS), flags, reasons, providerScore: null };
}

export class GoPlusSafetyProvider implements SafetyProvider {
  readonly name = 'goplus';
  readonly kind = 'external' as const;

  constructor(
    private readonly http: HttpClient,
    private readonly baseUrl: string,
    private readonly accessToken: string | null,
  ) {}

  async check(chain: Chain, address: string, signal?: AbortSignal): Promise<SafetyReport> {
    if (chain !== 'solana') throw new Error('goplus solana route only');
    const body = await this.http.requestJson({
      url: `${this.baseUrl}/api/v1/solana/token_security?contract_addresses=${encodeURIComponent(address)}`,
      endpoint: 'solana_token_security',
      schema: envelopeSchema,
      ...(this.accessToken ? { headers: { Authorization: this.accessToken } } : {}),
      ...(signal ? { signal } : {}),
    });
    if (body.code !== 1) {
      // Documented codes other than success are not mapped yet; treat as "try later".
      throw new TransientError(`goplus returned code ${body.code}: ${body.message ?? ''}`.trim());
    }
    const result = body.result ?? {};
    const key = Object.keys(result).find((k) => k === address || k.toLowerCase() === address.toLowerCase());
    const entry = key === undefined ? undefined : result[key];
    const mapped = mapEntry(entry);
    if (entry !== undefined && mapped.verdict === 'UNKNOWN') {
      throw new SchemaError('goplus: none of the expected risk fields could be read', ['result entry shape'], entry);
    }
    return {
      chain,
      tokenAddress: address,
      provider: this.name,
      kind: this.kind,
      checkedAt: new Date(),
      ...mapped,
      raw: entry ?? null,
      tokenInfo: null,
    };
  }
}
