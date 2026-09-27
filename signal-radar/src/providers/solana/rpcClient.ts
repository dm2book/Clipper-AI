/**
 * Solana JSON-RPC over HTTP — only the standard methods the radar needs
 * (https://solana.com/docs/rpc). Any RPC provider works; public endpoints are
 * too throttled for production and often refuse getProgramAccounts.
 */
import { z, type ZodType } from 'zod';
import { PermanentError, TransientError } from '../../core/errors.js';
import type { HttpClient } from '../../infra/http.js';

// Codes that mean "try again" rather than "your request is wrong".
const TRANSIENT_RPC_CODES = new Set([-32004, -32005, -32007, -32014, -32016]);

const rpcEnvelope = z.object({
  jsonrpc: z.literal('2.0'),
  id: z.union([z.number(), z.string(), z.null()]),
  result: z.unknown().optional(),
  error: z.object({ code: z.number(), message: z.string() }).optional(),
});

// --- response schemas (standard RPC shapes, validated loosely) --------------------

const tokenBalance = z.looseObject({
  mint: z.string(),
  owner: z.string().optional(),
  uiTokenAmount: z.looseObject({ amount: z.string() }),
});

export const transactionSchema = z
  .looseObject({
    slot: z.number(),
    blockTime: z.number().nullable().optional(),
    meta: z
      .looseObject({
        err: z.unknown(),
        postTokenBalances: z.array(tokenBalance).nullable().optional(),
      })
      .nullable(),
  })
  .nullable();
export type RpcTransaction = z.infer<typeof transactionSchema>;

const parsedAccount = z.looseObject({
  owner: z.string(),
  data: z.union([
    z.looseObject({ parsed: z.looseObject({ type: z.string().optional(), info: z.record(z.string(), z.unknown()) }), program: z.string().optional() }),
    z.array(z.string()),
    z.string(),
  ]),
});
export type ParsedAccount = z.infer<typeof parsedAccount>;

const accountInfoSchema = z.looseObject({ value: parsedAccount.nullable() });
const multipleAccountsSchema = z.looseObject({ value: z.array(parsedAccount.nullable()) });
const largestAccountsSchema = z.looseObject({
  value: z.array(z.looseObject({ address: z.string(), amount: z.string() })),
});
const programAccountsSchema = z.array(
  z.looseObject({ pubkey: z.string(), account: z.looseObject({ data: z.tuple([z.string(), z.string()]) }) }),
);

export class SolanaRpcClient {
  private nextId = 1;

  constructor(
    private readonly http: HttpClient,
    private readonly url: string,
  ) {}

  call<T>(method: string, params: unknown[], schema: ZodType<T>, opts: { signal?: AbortSignal; timeoutMs?: number } = {}): Promise<T> {
    return this.http.request({
      method: 'POST',
      url: this.url,
      endpoint: method,
      body: { jsonrpc: '2.0', id: this.nextId++, method, params },
      schema: rpcEnvelope,
      idempotent: true, // every method used here is a read
      ...(opts.signal ? { signal: opts.signal } : {}),
      ...(opts.timeoutMs ? { timeoutMs: opts.timeoutMs } : {}),
      unwrap: (envelope) => {
        if (envelope.error) {
          const { code, message } = envelope.error;
          if (TRANSIENT_RPC_CODES.has(code)) throw new TransientError(`rpc ${method}: ${code} ${message}`);
          throw new PermanentError(`rpc ${method}: ${code} ${message}`);
        }
        const parsed = schema.safeParse(envelope.result);
        if (!parsed.success) {
          throw new PermanentError(
            `rpc ${method}: unexpected result shape (${parsed.error.issues[0]?.message ?? 'invalid'})`,
          );
        }
        return parsed.data;
      },
    });
  }

  getTransaction(signature: string, signal?: AbortSignal): Promise<RpcTransaction> {
    return this.call(
      'getTransaction',
      [signature, { encoding: 'jsonParsed', maxSupportedTransactionVersion: 0, commitment: 'confirmed' }],
      transactionSchema,
      signal ? { signal } : {},
    );
  }

  async getParsedAccount(address: string, signal?: AbortSignal): Promise<ParsedAccount | null> {
    const res = await this.call(
      'getAccountInfo',
      [address, { encoding: 'jsonParsed', commitment: 'confirmed' }],
      accountInfoSchema,
      signal ? { signal } : {},
    );
    return res.value;
  }

  async getMultipleParsedAccounts(addresses: readonly string[], signal?: AbortSignal): Promise<(ParsedAccount | null)[]> {
    const out: (ParsedAccount | null)[] = [];
    for (let i = 0; i < addresses.length; i += 100) {
      const res = await this.call(
        'getMultipleAccounts',
        [addresses.slice(i, i + 100), { encoding: 'jsonParsed', commitment: 'confirmed' }],
        multipleAccountsSchema,
        signal ? { signal } : {},
      );
      out.push(...res.value);
    }
    return out;
  }

  async getTokenLargestAccounts(mint: string, signal?: AbortSignal): Promise<{ address: string; amount: string }[]> {
    const res = await this.call(
      'getTokenLargestAccounts',
      [mint, { commitment: 'confirmed' }],
      largestAccountsSchema,
      signal ? { signal } : {},
    );
    return res.value;
  }

  getProgramAccounts(
    program: string,
    config: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<z.infer<typeof programAccountsSchema>> {
    return this.call('getProgramAccounts', [program, config], programAccountsSchema, {
      ...(signal ? { signal } : {}),
      timeoutMs: 30_000,
    });
  }
}
