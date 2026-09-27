import { ed25519 } from '@noble/curves/ed25519.js';
import bs58 from 'bs58';

/**
 * Whether a Solana address is a point on the ed25519 curve (a wallet that can
 * sign). Program-derived addresses (pool vaults, bonding curves, lockers) are
 * deliberately off-curve. Same test as web3.js `PublicKey.isOnCurve`.
 */
export function isOnCurve(address: string): boolean {
  try {
    const bytes = bs58.decode(address);
    if (bytes.length !== 32) return false;
    ed25519.Point.fromBytes(bytes);
    return true;
  } catch {
    return false;
  }
}

export function encodeBase58(bytes: Uint8Array): string {
  return bs58.encode(bytes);
}
