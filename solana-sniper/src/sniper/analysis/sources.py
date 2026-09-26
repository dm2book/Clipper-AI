"""Off-chain data sources: DexScreener (pools, liquidity) and RugCheck."""

from __future__ import annotations

import httpx

from sniper.analysis.report import RugcheckResult
from sniper.config import DexscreenerConfig, RugcheckConfig
from sniper.ratelimit import TokenBucket


class SourceError(Exception):
    pass


class DexScreener:
    def __init__(self, cfg: DexscreenerConfig, client: httpx.AsyncClient | None = None):
        self._cfg = cfg
        self._client = client or httpx.AsyncClient(timeout=10)
        self._limiter = TokenBucket(cfg.requests_per_second)

    async def close(self) -> None:
        await self._client.aclose()

    async def pairs_for_token(self, mint: str) -> list[dict]:
        await self._limiter.acquire()
        try:
            resp = await self._client.get(f"{self._cfg.base_url}/tokens/v1/solana/{mint}")
        except httpx.TransportError as exc:
            raise SourceError(f"dexscreener: {exc}") from exc
        if resp.status_code != 200:
            raise SourceError(f"dexscreener HTTP {resp.status_code}")
        data = resp.json()
        # The endpoint returns a bare list; older variants wrap it in {"pairs": [...]}.
        return data if isinstance(data, list) else (data.get("pairs") or [])


class RugCheck:
    def __init__(self, cfg: RugcheckConfig, client: httpx.AsyncClient | None = None):
        self._cfg = cfg
        self._client = client or httpx.AsyncClient(timeout=10)
        self._limiter = TokenBucket(cfg.requests_per_second)

    async def close(self) -> None:
        await self._client.aclose()

    async def summary(self, mint: str) -> RugcheckResult:
        await self._limiter.acquire()
        try:
            resp = await self._client.get(f"{self._cfg.base_url}/tokens/{mint}/report/summary")
        except httpx.TransportError as exc:
            raise SourceError(f"rugcheck: {exc}") from exc
        if resp.status_code != 200:
            raise SourceError(f"rugcheck HTTP {resp.status_code}")
        return parse_rugcheck(resp.json())


def parse_rugcheck(data: dict) -> RugcheckResult:
    risks = [(str(r.get("name", "?")), str(r.get("level", "")).lower()) for r in data.get("risks") or []]
    score = data.get("score_normalised")
    return RugcheckResult(score_normalised=int(score) if score is not None else None, risks=risks)
