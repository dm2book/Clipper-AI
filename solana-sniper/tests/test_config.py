from decimal import Decimal

import pytest
from pydantic import ValidationError

from sniper.config import parse_settings

from conftest import ENV, ROOT

EXAMPLE = (ROOT / "config.example.yaml").read_text()


def test_example_config_loads_with_the_requested_limits(settings):
    assert settings.trading.mode == "paper"
    assert settings.trading.trade_size_usd == Decimal(20)
    assert settings.trading.max_open_positions == 3
    assert settings.exits.stop_loss_pct == 20
    assert settings.exits.take_profit_pct == 50
    assert settings.exits.trailing_stop_pct == 15
    assert settings.filters.min_liquidity_usd == 25000
    assert settings.filters.min_holders == 100
    assert settings.filters.max_top10_pct == 40
    assert settings.telegram.allowed_chat_ids == [42]
    assert settings.wallet.private_key is None
    assert settings.jupiter.api_key is None


def test_missing_required_secret_is_an_error():
    env = dict(ENV)
    del env["RPC_HTTP_URL"]
    with pytest.raises(ValueError, match="RPC_HTTP_URL"):
        parse_settings(EXAMPLE, env)


def test_live_mode_requires_a_wallet():
    with pytest.raises(ValidationError, match="wallet"):
        parse_settings(EXAMPLE.replace("mode: paper", "mode: live"), ENV)


def test_live_mode_with_wallet_is_accepted():
    s = parse_settings(EXAMPLE.replace("mode: paper", "mode: live"), {**ENV, "SOLANA_PRIVATE_KEY": "x"})
    assert s.trading.mode == "live"


def test_unknown_keys_are_rejected():
    with pytest.raises(ValidationError):
        parse_settings(EXAMPLE.replace("  trade_size_usd: 20", "  trade_size_usd: 20\n  trade_sise_usd: 99"), ENV)


def test_out_of_range_values_are_rejected():
    with pytest.raises(ValidationError):
        parse_settings(EXAMPLE.replace("stop_loss_pct: 20", "stop_loss_pct: 120"), ENV)
