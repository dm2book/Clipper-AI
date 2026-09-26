from dataclasses import replace
from decimal import Decimal

import pytest

from sniper.risk import RiskSnapshot, check_buy

OK = RiskSnapshot(paused=False, active_positions=0, buys_last_hour=0, daily_pnl_usd=Decimal(0),
                  sol_balance_lamports=10**9, trade_lamports=10**8, reserve_lamports=5 * 10**7)


def test_allowed(settings):
    assert check_buy(OK, settings.trading).allowed


@pytest.mark.parametrize("change, fragment", [
    ({"paused": True}, "paused"),
    ({"active_positions": 3}, "max open positions"),
    ({"buys_last_hour": 6}, "per hour"),
    ({"daily_pnl_usd": Decimal(-60)}, "daily loss"),
    ({"sol_balance_lamports": 10**8 + 5 * 10**7 - 1}, "insufficient SOL"),
    ({"mint_denied": True}, "already traded"),
])
def test_blocked(settings, change, fragment):
    d = check_buy(replace(OK, **change), settings.trading)
    assert not d.allowed and fragment in d.reason


def test_paper_mode_skips_balance_check(settings):
    assert check_buy(replace(OK, sol_balance_lamports=None), settings.trading).allowed


def test_two_positions_still_allow_a_third(settings):
    assert check_buy(replace(OK, active_positions=2), settings.trading).allowed
