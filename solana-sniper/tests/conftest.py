from pathlib import Path

import pytest

from sniper.config import parse_settings

ROOT = Path(__file__).resolve().parents[1]

ENV = {
    "RPC_HTTP_URL": "http://rpc.invalid",
    "RPC_WS_URL": "ws://rpc.invalid",
    "TELEGRAM_BOT_TOKEN": "1:abc",
    "TELEGRAM_CHAT_ID": "42",
    "DATABASE_URL": "postgresql://x@localhost/x",
}


@pytest.fixture
def settings():
    return parse_settings((ROOT / "config.example.yaml").read_text(), ENV)
