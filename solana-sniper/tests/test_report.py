from decimal import Decimal

import pytest

from sniper.analysis.report import (
    TokenReport,
    best_pair,
    count_holders,
    parse_mint_account,
    roundtrip_loss_pct,
    top10_percentage,
)
from sniper.analysis.sources import parse_rugcheck

MINT = "Mint1111111111111111111111111111111111111111"


def test_parse_mint_account_token2022_with_extensions():
    account = {
        "owner": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
        "data": {"parsed": {"type": "mint", "info": {
            "decimals": 9, "supply": "1000000000000000000", "mintAuthority": None,
            "freezeAuthority": "Dev", "isInitialized": True,
            "extensions": [{"extension": "transferFeeConfig", "state": {}}, {"extension": "metadataPointer"}],
        }}},
    }
    r = TokenReport(mint=MINT)
    parse_mint_account(r, account)
    assert r.mint_loaded and r.decimals == 9 and r.supply == 10**18
    assert r.mint_authority is None and r.freeze_authority == "Dev"
    assert r.extensions == ["transferFeeConfig", "metadataPointer"]


def test_parse_mint_account_rejects_non_mint():
    with pytest.raises(ValueError):
        parse_mint_account(TokenReport(mint=MINT), {"owner": "x", "data": {"parsed": {"type": "account", "info": {}}}})


def test_best_pair_picks_most_liquid_solana_pair_for_the_mint():
    pairs = [
        {"chainId": "solana", "baseToken": {"address": MINT}, "quoteToken": {"address": "SOL"}, "liquidity": {"usd": 1000}},
        {"chainId": "solana", "baseToken": {"address": "SOL"}, "quoteToken": {"address": MINT}, "liquidity": {"usd": 5000}},
        {"chainId": "ethereum", "baseToken": {"address": MINT}, "quoteToken": {"address": "X"}, "liquidity": {"usd": 9e9}},
        {"chainId": "solana", "baseToken": {"address": "Other"}, "quoteToken": {"address": "SOL"}, "liquidity": {"usd": 9e9}},
    ]
    assert best_pair(pairs, MINT)["liquidity"]["usd"] == 5000
    assert best_pair([], MINT) is None


def test_count_holders_counts_distinct_owners_with_balance():
    assert count_holders([("a", 1), ("a", 5), ("b", 0), ("c", 2)]) == 2


def test_top10_excludes_pdas_and_listed_owners_and_merges_accounts():
    holdings = [("pool_pda", 500), ("whale", 100), ("whale", 50), ("burn", 200)] + [(f"w{i}", 10) for i in range(15)]
    pct = top10_percentage(
        holdings, 1000, excluded_owners={"burn"}, exclude_program_owned=True,
        is_on_curve=lambda o: o != "pool_pda",
    )
    # whale 150 + nine wallets of 10 = 240 of 1000
    assert pct == Decimal("24.00")


def test_top10_counts_pdas_when_configured():
    pct = top10_percentage([("pool_pda", 500)], 1000, excluded_owners=set(),
                           exclude_program_owned=False, is_on_curve=lambda o: False)
    assert pct == Decimal("50.00")


def test_roundtrip_loss():
    assert roundtrip_loss_pct(1_000_000, 970_000) == Decimal("3.00")
    assert roundtrip_loss_pct(1_000_000, 1_010_000) == Decimal("-1.00")


def test_parse_rugcheck():
    r = parse_rugcheck({"score_normalised": 12, "risks": [{"name": "Mutable metadata", "level": "warn"}]})
    assert r.score_normalised == 12 and r.risks == [("Mutable metadata", "warn")]
    assert parse_rugcheck({}).risks == []
