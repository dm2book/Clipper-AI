/**
 * SafetyProvider that reads the facts straight from the mint account. This is
 * the authoritative check: RugCheck/GoPlus are second opinions (§E).
 */
import { verdictFromFlags, type SafetyFlag, type SafetyFlags, type SafetyReport } from '../../core/safety.js';
import type { Chain } from '../../core/types.js';
import type { SafetyProvider } from '../interfaces.js';
import type { MintInfo, MintInfoLoader } from './mintInfo.js';

const FAIL_FLAGS: SafetyFlag[] = [
  'mint_authority_active',
  'freeze_authority_active',
  'permanent_delegate',
  'transfer_hook',
  'non_transferable',
  'default_account_frozen',
  'pausable',
];
const WARN_FLAGS: SafetyFlag[] = ['transfer_fee'];

const REASONS: Partial<Record<SafetyFlag, string>> = {
  mint_authority_active: 'mint authority still active (supply can be inflated)',
  freeze_authority_active: 'freeze authority still active (holders can be frozen)',
  permanent_delegate: 'permanent delegate set (tokens can be moved from any holder)',
  transfer_hook: 'transfer hook program set (transfers can be blocked)',
  non_transferable: 'token is non-transferable',
  default_account_frozen: 'new token accounts start frozen',
  pausable: 'token transfers can be paused',
  transfer_fee: 'transfer fee configured',
};

function hasExtension(mint: MintInfo, name: string): Record<string, unknown> | null | undefined {
  const ext = mint.extensions.find((e) => e.extension === name);
  return ext === undefined ? undefined : ext.state;
}

/** Pure: mint facts -> flags. Unknown extension state is judged conservatively. */
export function flagsFromMint(mint: MintInfo): SafetyFlags {
  const transferHook = hasExtension(mint, 'transferHook');
  const delegate = hasExtension(mint, 'permanentDelegate');
  const defaultState = hasExtension(mint, 'defaultAccountState');
  const fee = hasExtension(mint, 'transferFeeConfig');
  const pausable = hasExtension(mint, 'pausableConfig');
  const newerFee = fee?.newerTransferFee as { transferFeeBasisPoints?: unknown } | undefined;
  const feeBps = typeof newerFee?.transferFeeBasisPoints === 'number' ? newerFee.transferFeeBasisPoints : null;
  return {
    mint_authority_active: mint.mintAuthority !== null,
    freeze_authority_active: mint.freezeAuthority !== null,
    // An extension whose authority/program is explicitly null is inert.
    permanent_delegate: delegate === undefined ? false : delegate?.delegate !== null,
    transfer_hook: transferHook === undefined ? false : transferHook?.programId !== null,
    non_transferable: hasExtension(mint, 'nonTransferable') !== undefined,
    default_account_frozen: defaultState === undefined ? false : defaultState?.accountState !== 'initialized',
    pausable: pausable === undefined ? false : pausable === null || pausable.authority !== null || pausable.paused === true,
    transfer_fee: fee === undefined ? false : feeBps === null ? true : feeBps > 0,
  };
}

export class SolanaOnchainSafetyProvider implements SafetyProvider {
  readonly name = 'solana-onchain';
  readonly kind = 'onchain' as const;

  constructor(private readonly mints: Pick<MintInfoLoader, 'get'>) {}

  async check(chain: Chain, address: string, signal?: AbortSignal): Promise<SafetyReport> {
    const mint = await this.mints.get(address, signal);
    const flags = flagsFromMint(mint);
    const reasons = (Object.keys(flags) as SafetyFlag[])
      .filter((f) => flags[f] === true && REASONS[f])
      .map((f) => REASONS[f]!);
    return {
      chain,
      tokenAddress: address,
      provider: this.name,
      kind: this.kind,
      checkedAt: new Date(),
      verdict: verdictFromFlags(flags, FAIL_FLAGS, WARN_FLAGS),
      flags,
      reasons,
      providerScore: null,
      raw: { mintAuthority: mint.mintAuthority, freezeAuthority: mint.freezeAuthority, extensions: mint.extensions },
      tokenInfo: { decimals: mint.decimals, supply: mint.supply, tokenProgram: mint.tokenProgram },
    };
  }
}
