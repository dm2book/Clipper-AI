"""Analyzer workers: claim due tokens, gather data, apply the filters."""

from __future__ import annotations

import asyncio
import base64
import logging
from decimal import Decimal
from html import escape

from solders.pubkey import Pubkey

from sniper.analysis.filters import Status, Verdict, evaluate
from sniper.analysis.report import (
    TokenReport,
    best_pair,
    count_holders,
    parse_mint_account,
    roundtrip_loss_pct,
    top10_percentage,
)
from sniper.analysis.sources import DexScreener, RugCheck
from sniper.config import Settings
from sniper.db import Database
from sniper.executor import Executor
from sniper.jupiter import Jupiter, NoRoute, usd_to_lamports
from sniper.solana.constants import TOKEN_PROGRAM, WSOL_MINT
from sniper.solana.rpc import SolanaRpc

log = logging.getLogger(__name__)


def is_on_curve(address: str) -> bool:
    return Pubkey.from_string(address).is_on_curve()


class Analyzer:
    def __init__(self, settings: Settings, db: Database, rpc: SolanaRpc, jupiter: Jupiter,
                 dexscreener: DexScreener, rugcheck: RugCheck | None, executor: Executor):
        self.cfg = settings
        self.db = db
        self.rpc = rpc
        self.jup = jupiter
        self.ds = dexscreener
        self.rc = rugcheck
        self.ex = executor

    # --- worker loop --------------------------------------------------------

    async def run_worker(self, n: int) -> None:
        a = self.cfg.analysis
        lease = max(a.recheck_interval_s, 60)
        while True:
            try:
                if n == 0:
                    expired = await self.db.expire_tokens(a.watch_window_s)
                    if expired:
                        log.info("expired %d tokens", expired)
                rows = await self.db.claim_due_tokens(1, lease)
                if not rows:
                    await asyncio.sleep(1)
                    continue
                await self.process(rows[0]["mint"])
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("analyzer worker %d failed", n)
                await asyncio.sleep(2)

    async def process(self, mint: str) -> Verdict:
        report = TokenReport(mint=mint)
        verdict = await self.analyse(report)
        a = self.cfg.analysis
        status = {Status.PASS: "PASSED", Status.REJECT: "REJECTED", Status.WAIT: "WATCHING"}[verdict.status]
        await self.db.record_analysis(
            mint, verdict=verdict.status.value, status=status, reason=verdict.reason[:1000],
            checks=verdict.to_json(), report=report.to_json(), next_check_in_s=a.recheck_interval_s,
            liquidity_usd=report.liquidity_usd, holders=report.holders, top10_pct=report.top10_pct,
            symbol=report.symbol, name=report.name, pool_address=report.pool_address, decimals=report.decimals,
        )
        label = escape(report.symbol or mint[:8])
        t = self.cfg.telegram
        if verdict.status == Status.REJECT:
            log.info("rejected %s: %s", mint, verdict.reason)
            if t.notify_on_rejected:
                await self.db.notify(f"❌ <b>{label}</b> afgewezen: {escape(verdict.reason[:300])}", priority=2)
        elif verdict.status == Status.PASS:
            log.info("PASSED %s", mint)
            await self.db.event("TOKEN_PASSED", mint=mint, data=report.to_json())
            if t.notify_on_passed:
                await self.db.notify(self._passed_message(report, label), priority=1, dedupe_key=f"passed:{mint}")
            try:
                await self.ex.try_buy(mint, report.decimals, report.symbol)
            except Exception as exc:
                # e.g. no SOL price because Jupiter is down; the token is not retried.
                log.exception("buy of %s aborted", mint)
                await self.db.set_token_status(mint, "SKIPPED", f"buy aborted: {exc}"[:500])
        return verdict

    def _passed_message(self, r: TokenReport, label: str) -> str:
        return (
            f"✅ <b>{label}</b> ({escape(r.name or '')}) slaagt voor alle filters\n"
            f"Liquiditeit: ${r.liquidity_usd:,.0f} · Holders: {r.holders}{'+' if r.holders_capped else ''} · "
            f"Top-10: {r.top10_pct}%\n"
            f"Round-trip: {r.roundtrip_loss_pct}% · DEX: {escape(r.dex_id or '?')}\n"
            f"<code>{r.mint}</code>\nhttps://dexscreener.com/solana/{r.mint}"
        )

    # --- staged analysis ------------------------------------------------------

    async def analyse(self, report: TokenReport) -> Verdict:
        """Cheapest and most decisive stages first; stop at the first stage
        that leaves a WAIT or REJECT, so a hopeless token costs few requests."""
        stages = [
            ("mint", self._stage_mint),
            ("rugcheck", self._stage_rugcheck),
            ("dexscreener", self._stage_dexscreener),
            ("holders", self._stage_holders),
            ("top10", self._stage_top10),
            ("roundtrip", self._stage_roundtrip),
        ]
        verdict = evaluate(report, self.cfg.filters)
        if verdict.status == Status.REJECT:
            return verdict
        for name, stage in stages:
            try:
                await stage(report)
            except Exception as exc:  # a data source failing is a WAIT, never a PASS
                log.debug("stage %s failed for %s: %s", name, report.mint, exc)
                report.errors[name] = str(exc)[:200] or type(exc).__name__
            verdict = evaluate(report, self.cfg.filters)
            if verdict.blocking:
                return verdict
        return verdict

    async def _stage_mint(self, r: TokenReport) -> None:
        account = await self.rpc.get_parsed_account(r.mint)
        if account is None:
            raise ValueError("mint account not found")
        parse_mint_account(r, account)

    async def _stage_rugcheck(self, r: TokenReport) -> None:
        if self.rc is None:
            return
        r.rugcheck = await self.rc.summary(r.mint)

    async def _stage_dexscreener(self, r: TokenReport) -> None:
        pair = best_pair(await self.ds.pairs_for_token(r.mint), r.mint)
        r.pair_found = pair is not None
        if pair is None:
            return
        side = pair["baseToken"] if pair["baseToken"]["address"] == r.mint else pair["quoteToken"]
        r.pool_address = pair.get("pairAddress")
        r.dex_id = pair.get("dexId")
        r.symbol = side.get("symbol")
        r.name = side.get("name")
        r.liquidity_usd = Decimal(str((pair.get("liquidity") or {}).get("usd") or 0))

    async def _stage_holders(self, r: TokenReport) -> None:
        if self.cfg.analysis.holders_method == "helius_das":
            owners, capped = await self._holders_das(r.mint)
        else:
            owners, capped = await self._holders_rpc(r.mint, r.token_program or TOKEN_PROGRAM)
        r.holders = count_holders(owners)
        r.holders_capped = capped

    async def _holders_rpc(self, mint: str, program: str) -> tuple[list[tuple[str, int]], bool]:
        filters: list[dict] = [{"memcmp": {"offset": 0, "bytes": mint}}]
        if program == TOKEN_PROGRAM:
            filters.append({"dataSize": 165})  # Token-2022 accounts vary in size
        # Only fetch owner (32 bytes @32) + amount (u64 @64).
        rows = await self.rpc.get_program_accounts(program, {
            "encoding": "base64", "commitment": "confirmed",
            "filters": filters, "dataSlice": {"offset": 32, "length": 40},
        })
        out = []
        for row in rows:
            raw = base64.b64decode(row["account"]["data"][0])
            out.append((str(Pubkey.from_bytes(raw[:32])), int.from_bytes(raw[32:40], "little")))
        return out, False

    async def _holders_das(self, mint: str) -> tuple[list[tuple[str, int]], bool]:
        out: list[tuple[str, int]] = []
        page = 1
        limit = self.cfg.analysis.max_holder_accounts_scan
        while len(out) < limit:
            res = await self.rpc.das("getTokenAccounts", {"mint": mint, "page": page, "limit": 1000})
            accounts = res.get("token_accounts") or []
            out.extend((a["owner"], int(a["amount"])) for a in accounts)
            if len(accounts) < 1000:
                return out, False
            page += 1
        return out, True

    async def _stage_top10(self, r: TokenReport) -> None:
        largest = await self.rpc.get_token_largest_accounts(r.mint)
        infos = await self.rpc.get_multiple_parsed_accounts([a["address"] for a in largest])
        holdings = []
        for acc, info in zip(largest, infos):
            if info is None:
                continue
            owner = info["data"]["parsed"]["info"]["owner"]
            holdings.append((owner, int(acc["amount"])))
        f = self.cfg.filters
        excluded = set(f.excluded_holder_owners)
        r.top10_pct = top10_percentage(
            holdings, r.supply, excluded_owners=excluded,
            exclude_program_owned=f.exclude_program_owned_from_top10, is_on_curve=is_on_curve,
        )

    async def _stage_roundtrip(self, r: TokenReport) -> None:
        sol_usd = await self.jup.sol_usd()
        lamports = usd_to_lamports(self.cfg.trading.trade_size_usd, sol_usd)
        slip = self.cfg.trading.buy_slippage_bps
        try:
            buy = await self.jup.quote(WSOL_MINT, r.mint, lamports, slip, for_analysis=True)
        except NoRoute:
            r.buy_route = False
            return
        r.buy_route = True
        try:
            back = await self.jup.quote(r.mint, WSOL_MINT, buy.out_amount, slip, for_analysis=True)
        except NoRoute:
            r.sell_route = False
            return
        r.sell_route = True
        r.roundtrip_loss_pct = roundtrip_loss_pct(lamports, back.out_amount)
