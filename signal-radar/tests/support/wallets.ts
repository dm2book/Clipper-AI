/**
 * Test-only builders for wallet events. SYNTHETIC data: not provider
 * responses and not real transactions.
 */
import type { WalletEvent } from '../../src/wallets/model.js';

export const WALLET = 'Wa11et1111111111111111111111111111111111111';
export const TOKEN = 'Token11111111111111111111111111111111111111';
export const T0 = new Date('2026-01-01T00:00:00Z');

let seq = 0;

/** `minutes` after T0; amounts are raw token units (bigint). */
export function ev(
  kind: WalletEvent['kind'],
  amountRaw: bigint | number,
  valueUsd: number | null,
  minutes: number,
  overrides: Partial<WalletEvent> = {},
): WalletEvent {
  seq++;
  return {
    chain: 'solana',
    signature: `sig${seq}`,
    ixIndex: 0,
    wallet: WALLET,
    tokenAddress: TOKEN,
    kind,
    status: 'success',
    amountRaw: BigInt(amountRaw),
    valueUsd,
    blockTime: new Date(T0.getTime() + minutes * 60_000),
    slot: null,
    source: 'test',
    ...overrides,
  };
}

/** A complete round trip: buy `cost` then sell everything for `proceeds`. */
export function roundTrip(token: string, cost: number, proceeds: number, startMinute: number, holdMinutes = 60): WalletEvent[] {
  return [
    ev('buy', 1_000, cost, startMinute, { tokenAddress: token }),
    ev('sell', 1_000, proceeds, startMinute + holdMinutes, { tokenAddress: token }),
  ];
}
