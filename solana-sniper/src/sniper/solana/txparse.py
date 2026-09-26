"""Pure helpers over jsonParsed ``getTransaction`` results."""

from __future__ import annotations

from dataclasses import dataclass


def _account_keys(tx: dict) -> list[str]:
    keys = tx["transaction"]["message"]["accountKeys"]
    out = [k["pubkey"] if isinstance(k, dict) else k for k in keys]
    # Versioned transactions append addresses loaded from lookup tables.
    loaded = tx.get("meta", {}).get("loadedAddresses") or {}
    return out + list(loaded.get("writable", [])) + list(loaded.get("readonly", []))


def extract_new_mint(tx: dict, quote_mints: set[str]) -> str | None:
    """The launched token in a pool-creation transaction.

    A new pool pairs exactly one token with a quote asset (SOL/USDC/USDT).
    Anything else (two unknown mints, none) is not a launch we trade.
    """
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None
    mints = {b["mint"] for b in (meta.get("postTokenBalances") or []) if "mint" in b}
    candidates = mints - quote_mints
    if len(candidates) != 1:
        return None
    return candidates.pop()


def _owner_token_total(balances: list[dict], owner: str, mint: str) -> int:
    return sum(
        int(b["uiTokenAmount"]["amount"])
        for b in balances or []
        if b.get("owner") == owner and b.get("mint") == mint
    )


@dataclass(frozen=True)
class Deltas:
    sol_lamports: int   # change of the owner's SOL balance (fees included)
    tokens: int         # change of the owner's raw token balance
    fee_lamports: int   # transaction fee paid (base + priority)


def owner_deltas(tx: dict, owner: str, mint: str) -> Deltas:
    meta = tx["meta"]
    keys = _account_keys(tx)
    idx = keys.index(owner)
    sol = int(meta["postBalances"][idx]) - int(meta["preBalances"][idx])
    tokens = (_owner_token_total(meta.get("postTokenBalances"), owner, mint)
              - _owner_token_total(meta.get("preTokenBalances"), owner, mint))
    return Deltas(sol, tokens, int(meta.get("fee", 0)))
