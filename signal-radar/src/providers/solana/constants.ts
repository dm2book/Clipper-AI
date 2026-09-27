import type { CustomDiscoverySource } from '../../config/env.js';

export const WSOL_MINT = 'So11111111111111111111111111111111111111112';
export const USDC_MINT = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v';
export const USDT_MINT = 'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB';
export const QUOTE_MINTS: ReadonlySet<string> = new Set([WSOL_MINT, USDC_MINT, USDT_MINT]);

export const TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA';
export const TOKEN_2022_PROGRAM = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb';

/** Tokens sent here are burned; the address is not a holder. */
export const INCINERATOR = '1nc1nerator11111111111111111111111111111111';

export interface DiscoverySource extends CustomDiscoverySource {
  /** Whether the log pattern has been verified against a real launch. */
  patternVerified: boolean;
}

/**
 * DEX programs whose pool-creation transactions announce a new token.
 * Program IDs are the public mainnet addresses. The log patterns are the
 * instruction names these programs log on pool creation; they are NOT yet
 * verified against a recorded transaction in this project (see
 * docs/PROVIDERS.md) — confirm them on an explorer before relying on them,
 * and override via DISCOVERY_CUSTOM_SOURCES if a program changes.
 */
export const KNOWN_SOURCES: Readonly<Record<string, DiscoverySource>> = {
  raydium_amm_v4: {
    name: 'raydium_amm_v4',
    programId: '675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8',
    logPatterns: ['initialize2'],
    patternVerified: false,
  },
  raydium_cpmm: {
    name: 'raydium_cpmm',
    programId: 'CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C',
    logPatterns: ['Instruction: Initialize'],
    patternVerified: false,
  },
  pumpswap: {
    name: 'pumpswap',
    programId: 'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA',
    logPatterns: ['Instruction: CreatePool'],
    patternVerified: false,
  },
  pumpfun: {
    name: 'pumpfun',
    programId: '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P',
    logPatterns: ['Instruction: Create'],
    patternVerified: false,
  },
};

export function resolveSources(names: readonly string[], custom: readonly CustomDiscoverySource[]): DiscoverySource[] {
  const out: DiscoverySource[] = [];
  for (const name of names) {
    const known = KNOWN_SOURCES[name];
    if (!known) throw new Error(`unknown discovery source "${name}" (known: ${Object.keys(KNOWN_SOURCES).join(', ')})`);
    out.push(known);
  }
  for (const c of custom) out.push({ ...c, patternVerified: false });
  return out;
}
