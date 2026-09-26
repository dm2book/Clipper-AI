from sniper.solana.constants import USDC_MINT, WSOL_MINT
from sniper.solana.txparse import extract_new_mint, owner_deltas

QUOTES = {WSOL_MINT, USDC_MINT}
NEW = "NewToken11111111111111111111111111111111111"


def tb(mint, owner, amount, idx=0):
    return {"accountIndex": idx, "mint": mint, "owner": owner, "uiTokenAmount": {"amount": str(amount)}}


def test_extract_new_mint_from_pool_creation():
    tx = {"meta": {"err": None, "postTokenBalances": [tb(WSOL_MINT, "pool", 1), tb(NEW, "pool", 5), tb(NEW, "dev", 1)]}}
    assert extract_new_mint(tx, QUOTES) == NEW


def test_extract_new_mint_ignores_failed_and_ambiguous():
    assert extract_new_mint({"meta": {"err": {"x": 1}, "postTokenBalances": [tb(NEW, "p", 1)]}}, QUOTES) is None
    two = {"meta": {"err": None, "postTokenBalances": [tb(NEW, "p", 1), tb("Other", "p", 1)]}}
    assert extract_new_mint(two, QUOTES) is None
    assert extract_new_mint({"meta": {"err": None, "postTokenBalances": [tb(WSOL_MINT, "p", 1)]}}, QUOTES) is None


def test_owner_deltas_with_lookup_table_accounts():
    tx = {
        "transaction": {"message": {"accountKeys": [{"pubkey": "me"}, {"pubkey": "pool"}]}},
        "meta": {
            "fee": 15_000,
            "loadedAddresses": {"writable": ["x"], "readonly": []},
            "preBalances": [1_000_000_000, 5, 0],
            "postBalances": [ 897_945_000, 5, 0],
            "preTokenBalances": [],
            "postTokenBalances": [tb(NEW, "me", 12_345), tb(NEW, "pool", 999)],
        },
    }
    d = owner_deltas(tx, "me", NEW)
    assert d.sol_lamports == -102_055_000
    assert d.tokens == 12_345
    assert d.fee_lamports == 15_000
