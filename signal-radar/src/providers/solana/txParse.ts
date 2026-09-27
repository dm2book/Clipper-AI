import type { RpcTransaction } from './rpcClient.js';

/**
 * The launched token in a pool-creation transaction: a new pool pairs exactly
 * one token with a quote asset (SOL/USDC/USDT). Anything else — a failed
 * transaction, two unknown mints, no token balances — is not a launch we can
 * attribute, and is skipped rather than guessed.
 */
export function extractNewMint(tx: RpcTransaction, quoteMints: ReadonlySet<string>): string | null {
  if (!tx?.meta || tx.meta.err !== null) return null;
  const mints = new Set((tx.meta.postTokenBalances ?? []).map((b) => b.mint));
  const candidates = [...mints].filter((m) => !quoteMints.has(m));
  return candidates.length === 1 ? candidates[0]! : null;
}

export function blockTimeOf(tx: RpcTransaction): Date | null {
  return typeof tx?.blockTime === 'number' ? new Date(tx.blockTime * 1000) : null;
}
