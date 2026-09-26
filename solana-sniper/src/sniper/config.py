"""Configuration: one YAML file, validated before anything starts.

Secrets never live in the YAML itself. The file references them as
``${ENV_VAR}`` (or ``${ENV_VAR:-default}``) and they are substituted from the
environment at load time.
"""

from __future__ import annotations

import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from sniper.solana.constants import INCINERATOR, USDC_MINT, USDT_MINT, WSOL_MINT


class _Strict(BaseModel):
    # A typo in a key is a config error, not a silently ignored setting.
    model_config = ConfigDict(extra="forbid")


class RpcConfig(_Strict):
    http_url: str
    ws_url: str
    requests_per_second: float = Field(10, gt=0)
    timeout_s: float = Field(15, gt=0)


class LaunchSource(_Strict):
    name: str
    program_id: str
    # A notification counts as a pool launch when any log line contains any
    # of these substrings.
    log_contains: list[str]
    enabled: bool = True


class DetectionConfig(_Strict):
    sources: list[LaunchSource]
    queue_size: int = Field(1000, gt=0)
    resolver_workers: int = Field(4, gt=0)
    quote_mints: list[str] = [WSOL_MINT, USDC_MINT, USDT_MINT]
    # Health turns red when no notification arrived for this long.
    stale_after_s: int = Field(300, gt=0)


class RugcheckConfig(_Strict):
    enabled: bool = True
    base_url: str = "https://api.rugcheck.xyz/v1"
    reject_levels: list[str] = ["danger"]
    # score_normalised runs 0 (clean) .. 100 (worst). None disables the limit.
    max_score_normalised: int | None = 50
    requests_per_second: float = Field(2, gt=0)


class FiltersConfig(_Strict):
    min_liquidity_usd: Decimal = Decimal(25_000)
    min_holders: int = 100
    max_top10_pct: Decimal = Decimal(40)
    require_mint_authority_revoked: bool = True
    require_freeze_authority_revoked: bool = True
    forbidden_extensions: list[str] = [
        "permanentDelegate",
        "transferHook",
        "transferFeeConfig",
        "nonTransferable",
        "defaultAccountState",
        "pausableConfig",
    ]
    max_roundtrip_loss_pct: Decimal = Decimal(10)
    # Pool vaults, bonding curves and lockers are owned by program-derived
    # (off-curve) addresses. Counting them would make every token fail top-10.
    exclude_program_owned_from_top10: bool = True
    excluded_holder_owners: list[str] = [INCINERATOR]
    denylist_mints: list[str] = []
    rugcheck: RugcheckConfig = RugcheckConfig()


class AnalysisConfig(_Strict):
    workers: int = Field(3, gt=0)
    recheck_interval_s: int = Field(20, gt=0)
    watch_window_s: int = Field(1800, gt=0)
    holders_method: Literal["rpc", "helius_das"] = "rpc"
    # Beyond this many token accounts the holder check simply passes the
    # minimum; scanning further only costs RPC credits.
    max_holder_accounts_scan: int = Field(20_000, gt=0)


class TradingConfig(_Strict):
    mode: Literal["paper", "live"] = "paper"
    trade_size_usd: Decimal = Field(Decimal(20), gt=0)
    max_open_positions: int = Field(3, gt=0)
    max_buys_per_hour: int = Field(6, gt=0)
    max_daily_loss_usd: Decimal = Field(Decimal(60), gt=0)
    min_sol_reserve: Decimal = Field(Decimal("0.05"), ge=0)
    buy_slippage_bps: int = Field(500, gt=0, le=5000)
    sell_slippage_bps: int = Field(1500, gt=0, le=5000)
    priority_level: Literal["medium", "high", "veryHigh"] = "veryHigh"
    priority_fee_max_lamports: int = Field(1_000_000, ge=0)
    confirm_poll_s: float = Field(2, gt=0)


class ExitConfig(_Strict):
    stop_loss_pct: Decimal = Field(Decimal(20), gt=0, lt=100)
    take_profit_pct: Decimal = Field(Decimal(50), gt=0)
    take_profit_sell_fraction: Decimal = Field(Decimal("0.5"), gt=0, le=1)
    trailing_stop_pct: Decimal = Field(Decimal(15), gt=0, lt=100)
    trailing_activate_after_tp: bool = True
    max_hold_minutes: int = Field(0, ge=0)  # 0 = no limit
    monitor_interval_s: float = Field(5, gt=0)
    max_sell_failures: int = Field(5, gt=0)


class JupiterConfig(_Strict):
    base_url: str = "https://lite-api.jup.ag/swap/v1"
    api_key: str | None = None
    # Two separate budgets so screening can never delay an exit.
    trading_rps: float = Field(1.0, gt=0)
    analysis_rps: float = Field(0.5, gt=0)
    timeout_s: float = Field(10, gt=0)


class DexscreenerConfig(_Strict):
    base_url: str = "https://api.dexscreener.com"
    requests_per_second: float = Field(4, gt=0)


class TelegramConfig(_Strict):
    enabled: bool = True
    bot_token: str | None = None
    notify_chat_id: int | None = None
    allowed_chat_ids: list[int] = []
    commands_enabled: bool = True
    notify_on_detected: bool = False
    notify_on_rejected: bool = False
    notify_on_passed: bool = True


class DatabaseConfig(_Strict):
    url: str
    min_pool: int = 2
    max_pool: int = 10


class WalletConfig(_Strict):
    private_key: str | None = None   # base58, as exported by Phantom/Solflare
    keypair_path: str | None = None  # JSON byte array, as written by solana-keygen


class HealthConfig(_Strict):
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080


class Settings(_Strict):
    rpc: RpcConfig
    detection: DetectionConfig
    analysis: AnalysisConfig = AnalysisConfig()
    filters: FiltersConfig = FiltersConfig()
    trading: TradingConfig = TradingConfig()
    exits: ExitConfig = ExitConfig()
    jupiter: JupiterConfig = JupiterConfig()
    dexscreener: DexscreenerConfig = DexscreenerConfig()
    telegram: TelegramConfig = TelegramConfig()
    database: DatabaseConfig
    wallet: WalletConfig = WalletConfig()
    health: HealthConfig = HealthConfig()
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        if self.trading.mode == "live" and not (self.wallet.private_key or self.wallet.keypair_path):
            raise ValueError("live mode needs wallet.private_key or wallet.keypair_path")
        if self.telegram.enabled:
            if not self.telegram.bot_token or self.telegram.notify_chat_id is None:
                raise ValueError("telegram.enabled needs bot_token and notify_chat_id")
            if self.telegram.commands_enabled and not self.telegram.allowed_chat_ids:
                raise ValueError("telegram.commands_enabled needs allowed_chat_ids")
        if not any(s.enabled for s in self.detection.sources):
            raise ValueError("detection.sources: enable at least one source")
        return self


_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(text: str, env: dict[str, str]) -> str:
    def sub(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        if name in env and env[name] != "":
            return env[name]
        if default is not None:
            return default
        raise ValueError(f"config references ${{{name}}} but it is not set")

    return _ENV.sub(sub, text)


def _expand_tree(node, env: dict[str, str]):
    """Substitute ${VAR} in string values only, after YAML parsing, so
    comments and keys are never touched. A value that expands to "" (from
    ``${VAR:-}``) is dropped so the field falls back to its default."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            v = _expand_tree(v, env)
            if v != "":
                out[k] = v
        return out
    if isinstance(node, list):
        return [_expand_tree(v, env) for v in node]
    if isinstance(node, str):
        return _expand(node, env)
    return node


def parse_settings(text: str, env: dict[str, str] | None = None) -> Settings:
    data = yaml.safe_load(text) or {}
    expanded = _expand_tree(data, dict(os.environ) if env is None else env)
    return Settings.model_validate(expanded)


def load_settings(path: str | Path) -> Settings:
    return parse_settings(Path(path).read_text(encoding="utf-8"))
