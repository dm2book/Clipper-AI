"""What the analyzer learns about a token, plus the pure helpers that
turn raw RPC/API payloads into those facts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Callable, Iterable


@dataclass
class RugcheckResult:
    score_normalised: int | None
    risks: list[tuple[str, str]]  # (name, level)


@dataclass
class TokenReport:
    mint: str
    # Stage: mint account
    decimals: int | None = None
    supply: int | None = None
    token_program: str | None = None
    mint_authority: str | None = None
    freeze_authority: str | None = None
    extensions: list[str] = field(default_factory=list)
    mint_loaded: bool = False
    # Stage: RugCheck
    rugcheck: RugcheckResult | None = None
    # Stage: DexScreener
    pair_found: bool | None = None
    pool_address: str | None = None
    dex_id: str | None = None
    symbol: str | None = None
    name: str | None = None
    liquidity_usd: Decimal | None = None
    # Stage: holders
    holders: int | None = None
    holders_capped: bool = False
    # Stage: top-10
    top10_pct: Decimal | None = None
    # Stage: Jupiter round-trip
    buy_route: bool | None = None
    sell_route: bool | None = None
    roundtrip_loss_pct: Decimal | None = None
    # stage name -> error message, for stages whose data source failed
    errors: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict:
        def conv(v):
            if isinstance(v, Decimal):
                return str(v)
            if isinstance(v, dict):
                return {k: conv(x) for k, x in v.items()}
            if isinstance(v, (list, tuple)):
                return [conv(x) for x in v]
            return v
        return conv(asdict(self))


def parse_mint_account(report: TokenReport, account: dict) -> None:
    """Fill the mint-stage fields from a jsonParsed ``getAccountInfo`` value."""
    parsed = account["data"]["parsed"]
    if parsed.get("type") != "mint":
        raise ValueError(f"account is not a mint (type={parsed.get('type')})")
    info = parsed["info"]
    report.token_program = account["owner"]
    report.decimals = int(info["decimals"])
    report.supply = int(info["supply"])
    report.mint_authority = info.get("mintAuthority")
    report.freeze_authority = info.get("freezeAuthority")
    report.extensions = [e["extension"] for e in info.get("extensions", []) if "extension" in e]
    report.mint_loaded = True


def best_pair(pairs: Iterable[dict], mint: str) -> dict | None:
    """The Solana pair for ``mint`` with the most USD liquidity."""
    best, best_liq = None, Decimal(-1)
    for p in pairs or []:
        if p.get("chainId") != "solana":
            continue
        if mint not in (p.get("baseToken", {}).get("address"), p.get("quoteToken", {}).get("address")):
            continue
        liq = Decimal(str((p.get("liquidity") or {}).get("usd") or 0))
        if liq > best_liq:
            best, best_liq = p, liq
    return best


def count_holders(accounts: Iterable[tuple[str, int]]) -> int:
    """Distinct owners with a non-zero balance."""
    return len({owner for owner, amount in accounts if amount > 0})


def top10_percentage(
    holdings: Iterable[tuple[str, int]],
    supply: int,
    *,
    excluded_owners: set[str],
    exclude_program_owned: bool,
    is_on_curve: Callable[[str], bool],
) -> Decimal:
    """Share of supply held by the ten largest real holders, in percent.

    ``holdings`` is (owner, raw amount) per token account; one owner with
    several accounts is counted once.
    """
    if supply <= 0:
        raise ValueError("supply must be positive")
    per_owner: dict[str, int] = {}
    for owner, amount in holdings:
        if owner in excluded_owners:
            continue
        if exclude_program_owned and not is_on_curve(owner):
            continue
        per_owner[owner] = per_owner.get(owner, 0) + amount
    top = sorted(per_owner.values(), reverse=True)[:10]
    return (Decimal(sum(top)) * 100 / Decimal(supply)).quantize(Decimal("0.01"))


def roundtrip_loss_pct(lamports_in: int, lamports_back: int) -> Decimal:
    return ((1 - Decimal(lamports_back) / Decimal(lamports_in)) * 100).quantize(Decimal("0.01"))
