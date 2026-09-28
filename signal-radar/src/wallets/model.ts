import type { Chain } from '../core/types.js';

/**
 * One on-chain movement of a token for one wallet, normalised across sources.
 *
 *  buy / sell           a swap; `valueUsd` is the USD value of the swap leg
 *  transfer_in / _out   tokens moved without a trade (another wallet, a CEX,
 *                       an airdrop…). A transfer is NOT a sale: it never
 *                       realises PnL, and tokens received this way have an
 *                       unknown cost basis.
 *
 * Failed transactions are recorded (for audit) but ignored everywhere else.
 */
export type WalletEventKind = 'buy' | 'sell' | 'transfer_in' | 'transfer_out';

export interface WalletEvent {
  chain: Chain;
  signature: string;
  /** Position of the instruction within the transaction (one tx can hold several swaps). */
  ixIndex: number;
  wallet: string;
  tokenAddress: string;
  kind: WalletEventKind;
  status: 'success' | 'failed';
  /** Raw token amount moved; always positive. */
  amountRaw: bigint;
  /** USD value of the swap; null when the source could not price it. Ignored for transfers. */
  valueUsd: number | null;
  blockTime: Date;
  slot: number | null;
  source: string;
}

/** Identity of an event: the same instruction for the same wallet is the same event. */
export function eventKey(e: Pick<WalletEvent, 'chain' | 'signature' | 'ixIndex' | 'wallet'>): string {
  return `${e.chain}:${e.signature}:${e.ixIndex}:${e.wallet}`;
}

export function isSwap(e: Pick<WalletEvent, 'kind'>): boolean {
  return e.kind === 'buy' || e.kind === 'sell';
}
