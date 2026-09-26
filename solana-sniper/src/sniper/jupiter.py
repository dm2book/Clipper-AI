"""Jupiter swap API: quotes, swap transactions, and the SOL/USD price."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal

import httpx

from sniper.config import JupiterConfig, TradingConfig
from sniper.ratelimit import TokenBucket
from sniper.solana.constants import LAMPORTS_PER_SOL, USDC_DECIMALS, USDC_MINT, WSOL_MINT


class NoRoute(Exception):
    """Jupiter cannot route this pair (no pool yet, not tradable, or closed)."""


class JupiterError(Exception):
    pass


@dataclass(frozen=True)
class Quote:
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    price_impact: Decimal  # as reported by Jupiter, informational only
    raw: dict


@dataclass(frozen=True)
class SwapTx:
    tx_base64: str
    last_valid_block_height: int


class Jupiter:
    def __init__(self, cfg: JupiterConfig, trading: TradingConfig, *,
                 client: httpx.AsyncClient | None = None):
        self._cfg = cfg
        self._trading = trading
        headers = {"x-api-key": cfg.api_key} if cfg.api_key else {}
        self._client = client or httpx.AsyncClient(timeout=cfg.timeout_s, headers=headers)
        self.trading_limiter = TokenBucket(cfg.trading_rps)
        self.analysis_limiter = TokenBucket(cfg.analysis_rps)
        self._sol_price: tuple[float, Decimal] | None = None
        self._sol_price_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict, limiter: TokenBucket) -> dict:
        for attempt in range(3):
            await limiter.acquire()
            try:
                resp = await self._client.get(self._cfg.base_url + path, params=params)
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise JupiterError(str(exc)) from exc
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == 2:
                    raise JupiterError(f"HTTP {resp.status_code}")
                await asyncio.sleep(1 + attempt)
                continue
            try:
                data = resp.json()
            except ValueError:
                raise JupiterError(f"HTTP {resp.status_code}: non-JSON body") from None
            if resp.status_code >= 400:
                code = str(data.get("errorCode") or data.get("error") or "")
                if any(k in code.upper() for k in ("ROUTE", "NOT_TRADABLE", "TRADABLE")):
                    raise NoRoute(code)
                raise JupiterError(f"HTTP {resp.status_code}: {data}")
            return data
        raise AssertionError("unreachable")

    async def quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int,
                    *, for_analysis: bool = False) -> Quote:
        limiter = self.analysis_limiter if for_analysis else self.trading_limiter
        data = await self._get("/quote", {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
            # Fewer exotic hops = fewer surprises when the tx executes.
            "restrictIntermediateTokens": "true",
        }, limiter)
        if not data.get("outAmount") or int(data["outAmount"]) == 0:
            raise NoRoute("empty quote")
        return Quote(
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount=int(data["inAmount"]),
            out_amount=int(data["outAmount"]),
            price_impact=Decimal(str(data.get("priceImpactPct") or "0")),
            raw=data,
        )

    async def swap_transaction(self, quote: Quote, user_public_key: str) -> SwapTx:
        await self.trading_limiter.acquire()
        resp = await self._client.post(self._cfg.base_url + "/swap", json={
            "quoteResponse": quote.raw,
            "userPublicKey": user_public_key,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": {
                "priorityLevelWithMaxLamports": {
                    "maxLamports": self._trading.priority_fee_max_lamports,
                    "priorityLevel": self._trading.priority_level,
                }
            },
        })
        if resp.status_code >= 400:
            raise JupiterError(f"swap HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        return SwapTx(data["swapTransaction"], int(data["lastValidBlockHeight"]))

    async def sol_usd(self, max_age_s: float = 30) -> Decimal:
        """Price of 1 SOL in USD, derived from a SOL→USDC quote and cached."""
        async with self._sol_price_lock:
            now = time.monotonic()
            if self._sol_price and now - self._sol_price[0] < max_age_s:
                return self._sol_price[1]
            q = await self.quote(WSOL_MINT, USDC_MINT, LAMPORTS_PER_SOL, 50)
            price = Decimal(q.out_amount) / Decimal(10 ** USDC_DECIMALS)
            self._sol_price = (now, price)
            return price


def usd_to_lamports(usd: Decimal, sol_usd: Decimal) -> int:
    return int(usd / sol_usd * LAMPORTS_PER_SOL)


def lamports_to_usd(lamports: int, sol_usd: Decimal) -> Decimal:
    return Decimal(lamports) / LAMPORTS_PER_SOL * sol_usd
