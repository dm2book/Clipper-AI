/** Chains the radar understands. The data model carries `chain` everywhere so
 * adding one later means adding providers, not migrating data. */
export const CHAINS = ['solana'] as const;
export type Chain = (typeof CHAINS)[number];

export interface TokenRef {
  chain: Chain;
  address: string;
}

export function tokenKey(ref: TokenRef): string {
  return `${ref.chain}:${ref.address}`;
}
