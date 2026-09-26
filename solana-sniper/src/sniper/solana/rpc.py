"""Minimal Solana JSON-RPC client: only the calls the bot uses."""

from __future__ import annotations

import asyncio
import itertools
import logging
import random
from typing import Any

import httpx

from sniper.ratelimit import TokenBucket

log = logging.getLogger(__name__)


class RpcError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


# Codes that mean "try again later", not "your request is wrong".
_RETRYABLE_RPC_CODES = {-32004, -32005, -32007, -32014, -32016, 429}


class SolanaRpc:
    def __init__(self, url: str, limiter: TokenBucket, *, timeout_s: float = 15,
                 client: httpx.AsyncClient | None = None, max_attempts: int = 4):
        self._url = url
        self._limiter = limiter
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._ids = itertools.count(1)
        self._max_attempts = max_attempts

    async def close(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, params: list[Any] | dict | None = None) -> Any:
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or []}
        for attempt in range(1, self._max_attempts + 1):
            await self._limiter.acquire()
            try:
                resp = await self._client.post(self._url, json=body)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise RpcError(f"HTTP {resp.status_code}", resp.status_code)
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    err = data["error"]
                    raise RpcError(err.get("message", str(err)), err.get("code"))
                return data.get("result")
            except (httpx.TransportError, RpcError) as exc:
                retryable = isinstance(exc, httpx.TransportError) or (
                    isinstance(exc, RpcError) and (exc.code in _RETRYABLE_RPC_CODES or (exc.code or 0) >= 500)
                )
                if not retryable or attempt == self._max_attempts:
                    raise
                delay = min(8.0, 0.5 * 2 ** (attempt - 1)) * (0.5 + random.random())
                log.debug("rpc %s failed (%s), retry in %.1fs", method, exc, delay)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    # --- typed helpers --------------------------------------------------

    async def get_transaction(self, signature: str) -> dict | None:
        return await self.call("getTransaction", [signature, {
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 0,
            "commitment": "confirmed",
        }])

    async def get_parsed_account(self, address: str) -> dict | None:
        res = await self.call("getAccountInfo", [address, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        return res["value"] if res else None

    async def get_multiple_parsed_accounts(self, addresses: list[str]) -> list[dict | None]:
        out: list[dict | None] = []
        for i in range(0, len(addresses), 100):
            res = await self.call("getMultipleAccounts", [addresses[i:i + 100], {"encoding": "jsonParsed"}])
            out.extend(res["value"])
        return out

    async def get_token_largest_accounts(self, mint: str) -> list[dict]:
        res = await self.call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        return res["value"]

    async def get_program_accounts(self, program: str, config: dict) -> list[dict]:
        return await self.call("getProgramAccounts", [program, config])

    async def get_balance(self, address: str) -> int:
        res = await self.call("getBalance", [address, {"commitment": "confirmed"}])
        return int(res["value"])

    async def get_token_balance(self, owner: str, mint: str) -> int:
        """Sum of the owner's raw balance across all token accounts for ``mint``."""
        res = await self.call("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
        return sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in res["value"])

    async def get_block_height(self) -> int:
        return int(await self.call("getBlockHeight", [{"commitment": "confirmed"}]))

    async def get_signature_status(self, signature: str) -> dict | None:
        res = await self.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
        return res["value"][0]

    async def send_raw_transaction(self, tx_base64: str) -> str:
        # Preflight simulation catches obvious failures (slippage already
        # exceeded, insufficient funds) before paying for a landed failure.
        return await self.call("sendTransaction", [tx_base64, {
            "encoding": "base64",
            "skipPreflight": False,
            "preflightCommitment": "confirmed",
            "maxRetries": 3,
        }])

    async def das(self, method: str, params: dict) -> Any:
        """Helius-style DAS call (named params instead of a positional list)."""
        return await self.call(method, params)
