from dataclasses import replace
from decimal import Decimal

import pytest

from sniper.analysis.filters import Status, evaluate
from sniper.analysis.report import RugcheckResult, TokenReport

MINT = "Mint1111111111111111111111111111111111111111"


def good_report(**overrides) -> TokenReport:
    r = TokenReport(
        mint=MINT, decimals=6, supply=10**15, token_program="Tokenkeg", mint_loaded=True,
        rugcheck=RugcheckResult(score_normalised=5, risks=[("Low amount of LP providers", "warn")]),
        pair_found=True, pool_address="Pool", dex_id="raydium", symbol="GOOD",
        liquidity_usd=Decimal(40_000), holders=250, top10_pct=Decimal("22.5"),
        buy_route=True, sell_route=True, roundtrip_loss_pct=Decimal("1.2"),
    )
    return replace(r, **overrides)


def status_of(verdict, name):
    return next(c.status for c in verdict.checks if c.name == name)


def test_everything_good_passes(settings):
    v = evaluate(good_report(), settings.filters)
    assert v.status == Status.PASS, v.reason
    assert all(c.status == Status.PASS for c in v.checks)


@pytest.mark.parametrize("overrides, check", [
    ({"mint_authority": "Dev"}, "mint_authority"),
    ({"freeze_authority": "Dev"}, "freeze_authority"),
    ({"extensions": ["metadataPointer", "permanentDelegate"]}, "token_extensions"),
    ({"rugcheck": RugcheckResult(10, [("Freeze Authority still enabled", "danger")])}, "rugcheck"),
    ({"rugcheck": RugcheckResult(80, [])}, "rugcheck"),
    ({"roundtrip_loss_pct": Decimal("35")}, "tradable"),
])
def test_scam_signals_reject(settings, overrides, check):
    v = evaluate(good_report(**overrides), settings.filters)
    assert v.status == Status.REJECT
    assert status_of(v, check) == Status.REJECT


@pytest.mark.parametrize("overrides, check", [
    ({"liquidity_usd": Decimal(24_999)}, "liquidity"),
    ({"holders": 99}, "holders"),
    ({"top10_pct": Decimal(40)}, "top10"),          # "less than 40%" is strict
    ({"pair_found": False, "liquidity_usd": None}, "pool_active"),
    ({"sell_route": False, "roundtrip_loss_pct": None}, "tradable"),
])
def test_soft_conditions_wait(settings, overrides, check):
    v = evaluate(good_report(**overrides), settings.filters)
    assert v.status == Status.WAIT
    assert status_of(v, check) == Status.WAIT


def test_boundaries_that_pass(settings):
    v = evaluate(good_report(liquidity_usd=Decimal(25_000), holders=100, top10_pct=Decimal("39.99")),
                 settings.filters)
    assert v.status == Status.PASS


def test_unmeasured_data_is_never_a_pass(settings):
    v = evaluate(TokenReport(mint=MINT), settings.filters)
    assert v.status == Status.WAIT
    assert status_of(v, "holders") == Status.UNKNOWN
    assert not v.blocking  # nothing measured yet, so nothing blocks the next stage


def test_failed_source_waits(settings):
    r = good_report(holders=None)
    r.errors["holders"] = "timeout"
    v = evaluate(r, settings.filters)
    assert v.status == Status.WAIT
    assert status_of(v, "holders") == Status.WAIT


def test_denylist_rejects(settings):
    settings.filters.denylist_mints.append(MINT)
    assert evaluate(good_report(), settings.filters).status == Status.REJECT


def test_rugcheck_can_be_disabled(settings):
    settings.filters.rugcheck.enabled = False
    v = evaluate(good_report(rugcheck=None), settings.filters)
    assert v.status == Status.PASS
