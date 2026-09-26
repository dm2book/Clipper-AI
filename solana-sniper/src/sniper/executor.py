"""Buys and sells via Jupiter, crash-safe.

The ordering that makes a crash at any point recoverable:

1. build the swap transaction and sign it locally -> the signature is known
2. record the trade as PENDING with that signature   (write-ahead)
3. send it
4. poll until it is confirmed, failed, or its blockhash has expired
5. read the real amounts from the confirmed transaction and settle

A PENDING trade is never re-sent. On restart, recovery resumes at step 4.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from html import escape

from sniper.config import Settings
from sniper.db import Database, hour_ago, utc_midnight
from sniper.exits import sell_amount
from sniper.jupiter import Jupiter, Quote, lamports_to_usd, usd_to_lamports
from sniper.risk import RiskSnapshot, check_buy
from sniper.solana.constants import LAMPORTS_PER_SOL, WSOL_MINT
from sniper.solana.rpc import RpcError, SolanaRpc
from sniper.solana.txparse import owner_deltas
from sniper.solana.wallet import Wallet

log = logging.getLogger(__name__)

CONFIRMED, FAILED, EXPIRED = "CONFIRMED", "FAILED", "EXPIRED"

# sendTransaction errors after which the transaction was certainly not sent:
# preflight simulation failed, or signature verification failed.
_NEVER_FORWARDED = {-32002, -32003}


@dataclass(frozen=True)
class Outcome:
    status: str                 # CONFIRMED / FAILED
    lamports: int | None = None  # swap leg in SOL
    tokens: int | None = None
    fee_lamports: int | None = None
    error: str | None = None


def sol(lamports: int | None) -> str:
    return f"{Decimal(lamports or 0) / LAMPORTS_PER_SOL:.4f} SOL"


class Executor:
    def __init__(self, settings: Settings, db: Database, jupiter: Jupiter, rpc: SolanaRpc,
                 wallet: Wallet | None):
        self.cfg = settings
        self.db = db
        self.jup = jupiter
        self.rpc = rpc
        self.wallet = wallet
        self.paper = settings.trading.mode == "paper"
        if not self.paper and wallet is None:
            raise ValueError("live mode requires a wallet")
        # Serialises "check limits + create position" so concurrent analyzer
        # workers cannot both take the last free slot.
        self._buy_lock = asyncio.Lock()
        # Signatures this process is currently confirming; recovery leaves them alone.
        self.in_flight: set[str] = set()
        # Positions with a buy or sell running in this process.
        self.busy: set[int] = set()

    # --- risk --------------------------------------------------------------

    async def risk_snapshot(self, mint: str, trade_lamports: int, sol_usd: Decimal) -> RiskSnapshot:
        pnl = await self.db.realized_pnl_lamports_since(utc_midnight())
        balance = None
        if not self.paper:
            balance = await self.rpc.get_balance(self.wallet.address)
        traded_before = await self.db.traded_before(mint)
        return RiskSnapshot(
            paused=await self.db.is_paused(),
            active_positions=await self.db.count_active_positions(),
            buys_last_hour=await self.db.buys_since(hour_ago()),
            daily_pnl_usd=lamports_to_usd(pnl, sol_usd),
            sol_balance_lamports=balance,
            trade_lamports=trade_lamports,
            reserve_lamports=int(self.cfg.trading.min_sol_reserve * LAMPORTS_PER_SOL),
            mint_denied=bool(traded_before),
        )

    # --- buy ---------------------------------------------------------------

    async def try_buy(self, mint: str, decimals: int, symbol: str | None) -> int | None:
        """Buy ``trade_size_usd`` of ``mint`` if the risk gate allows it.
        Returns the position id when a position was opened."""
        label = escape(symbol or mint[:8])
        async with self._buy_lock:
            sol_usd = await self.jup.sol_usd()
            lamports = usd_to_lamports(self.cfg.trading.trade_size_usd, sol_usd)
            decision = check_buy(await self.risk_snapshot(mint, lamports, sol_usd), self.cfg.trading)
            if not decision.allowed:
                await self.db.set_token_status(mint, "SKIPPED", decision.reason)
                await self.db.event("BUY_SKIPPED", mint=mint, data={"reason": decision.reason})
                await self.db.notify(f"⏭️ <b>{label}</b> niet gekocht: {escape(decision.reason)}", priority=2)
                return None
            pid = await self.db.create_opening_position(mint, decimals)
            if pid is None:  # another live position in this mint
                return None
            self.busy.add(pid)
        try:
            return await self._buy(pid, mint, label, lamports, sol_usd)
        finally:
            self.busy.discard(pid)

    async def _buy(self, pid: int, mint: str, label: str, lamports: int, sol_usd: Decimal) -> int | None:
        try:
            quote = await self.jup.quote(WSOL_MINT, mint, lamports, self.cfg.trading.buy_slippage_bps)
            outcome = await self._execute(pid, "BUY", "ENTRY", quote)
        except Exception as exc:
            if await self.db.has_pending_trade(pid):
                # The transaction may be in flight; recovery settles it.
                log.exception("buy of %s interrupted; left to recovery", mint)
                return None
            await self._buy_failed(pid, mint, label, str(exc) or type(exc).__name__)
            return None
        if outcome.status != CONFIRMED:
            await self._buy_failed(pid, mint, label, outcome.error or "not confirmed")
            return None
        await self._open_position(pid, mint, label, outcome, sol_usd)
        return pid

    async def _open_position(self, pid: int, mint: str, label: str, outcome: Outcome,
                             sol_usd: Decimal | None) -> None:
        if not outcome.tokens or outcome.tokens <= 0:
            await self._buy_failed(pid, mint, label, "confirmed, but no tokens received")
            return
        entry_price = Decimal(outcome.lamports) / Decimal(outcome.tokens)
        await self.db.update_position(
            pid, status="OPEN", entry_lamports=outcome.lamports,
            entry_usd=lamports_to_usd(outcome.lamports, sol_usd) if sol_usd else None,
            initial_amount=outcome.tokens, remaining_amount=outcome.tokens,
            entry_price=entry_price, last_price=entry_price,
        )
        await self.db.set_token_status(mint, "BOUGHT")
        await self.db.event("BUY_CONFIRMED", mint=mint, position_id=pid, data={
            "lamports": outcome.lamports, "tokens": outcome.tokens, "fee": outcome.fee_lamports})
        x = self.cfg.exits
        await self.db.notify(
            f"🟢 <b>Gekocht {label}</b>{' (paper)' if self.paper else ''}\n"
            f"Inzet: {sol(outcome.lamports)} (+{sol(outcome.fee_lamports)} fees)\n"
            f"SL −{x.stop_loss_pct}% · TP +{x.take_profit_pct}% · trailing {x.trailing_stop_pct}%\n"
            f"<code>{mint}</code>\nPositie #{pid}",
            priority=1, dedupe_key=f"buy:{pid}",
        )

    async def _buy_failed(self, pid: int, mint: str, label: str, error: str) -> None:
        await self.db.update_position(pid, status="FAILED", closed_at=datetime.now(timezone.utc), close_reason=error[:500])
        await self.db.event("BUY_FAILED", mint=mint, position_id=pid, data={"error": error})
        await self.db.notify(f"⚠️ Aankoop <b>{label}</b> mislukt: {escape(error[:300])}", priority=0,
                             dedupe_key=f"buyfail:{pid}")

    # --- sell --------------------------------------------------------------

    async def sell(self, pid: int, fraction: Decimal, reason: str) -> bool:
        """Sell ``fraction`` of the remaining tokens. Returns True on success."""
        if pid in self.busy or not await self.db.claim_for_sell(pid):
            return False  # already closing, closed, or not ours to touch
        self.busy.add(pid)
        try:
            return await self._sell(pid, fraction, reason)
        finally:
            self.busy.discard(pid)

    async def _sell(self, pid: int, fraction: Decimal, reason: str) -> bool:
        pos = await self.db.get_position(pid)
        label = escape(pos["symbol"] or pos["mint"][:8])
        remaining = int(pos["remaining_amount"])
        amount = sell_amount(remaining, fraction)
        if amount <= 0:
            await self.db.update_position(pid, status="OPEN")
            return False
        try:
            quote = await self.jup.quote(pos["mint"], WSOL_MINT, amount, self.cfg.trading.sell_slippage_bps)
            outcome = await self._execute(pid, "SELL", reason, quote)
        except Exception as exc:
            if await self.db.has_pending_trade(pid):
                log.exception("sell of position %s interrupted; left to recovery", pid)
                return False
            outcome = Outcome(FAILED, error=str(exc) or type(exc).__name__)
        if outcome.status != CONFIRMED:
            await self._sell_failed(pos, label, reason, outcome.error or "not confirmed")
            return False
        await self._apply_sell(pos, label, reason, outcome)
        return True

    async def _apply_sell(self, pos, label: str, reason: str, outcome: Outcome) -> None:
        pid = pos["id"]
        remaining = max(0, int(pos["remaining_amount"]) - int(outcome.tokens))
        realized = int(pos["realized_lamports"]) + int(outcome.lamports)
        fields = dict(remaining_amount=remaining, realized_lamports=realized, sell_failures=0)
        if reason == "TAKE_PROFIT":
            fields["tp_hit"] = True
            fields["peak_price"] = Decimal(outcome.lamports) / Decimal(outcome.tokens)
        closed = remaining == 0
        if closed:
            fields.update(status="CLOSED", closed_at=datetime.now(timezone.utc), close_reason=reason)
        else:
            fields["status"] = "OPEN"
        await self.db.update_position(pid, **fields)
        await self.db.event("SELL_CONFIRMED", mint=pos["mint"], position_id=pid, data={
            "reason": reason, "lamports": outcome.lamports, "tokens": outcome.tokens, "closed": closed})
        entry = int(pos["entry_lamports"])
        sold_share = Decimal(outcome.tokens) / Decimal(int(pos["initial_amount"]))
        leg_cost = Decimal(entry) * sold_share
        leg_pct = (Decimal(outcome.lamports) / leg_cost - 1) * 100 if leg_cost else Decimal(0)
        lines = [
            f"{'🔴' if reason in ('STOP_LOSS', 'TRAILING_STOP', 'MAX_HOLD_TIME') else '💰'} "
            f"<b>Verkocht {label}</b> — {reason}{' (paper)' if self.paper else ''}",
            f"Ontvangen: {sol(outcome.lamports)} ({leg_pct:+.1f}% op dit deel)",
        ]
        if closed:
            fees = await self.db.position_fees(pid)
            net = realized - entry - fees
            lines.append(f"Positie gesloten. Netto resultaat: {sol(net)} (fees incl.)")
        else:
            lines.append(f"Rest in positie: {remaining} (ruw) — trailing stop actief")
        await self.db.notify("\n".join(lines), priority=0 if reason == "STOP_LOSS" else 1,
                             dedupe_key=f"sell:{pid}:{outcome.lamports}:{outcome.tokens}")

    async def _sell_failed(self, pos, label: str, reason: str, error: str) -> None:
        pid = pos["id"]
        failures = int(pos["sell_failures"]) + 1
        await self.db.update_position(pid, status="OPEN", sell_failures=failures)
        await self.db.event("SELL_FAILED", mint=pos["mint"], position_id=pid,
                            data={"reason": reason, "error": error, "failures": failures})
        if failures == self.cfg.exits.max_sell_failures:
            await self.db.notify(
                f"🚨 <b>{label}</b>: verkoop ({reason}) {failures}× mislukt — handmatig ingrijpen nodig.\n"
                f"Laatste fout: {escape(error[:300])}\nPositie #{pid}",
                priority=0, dedupe_key=f"sellstuck:{pid}")

    # --- shared execution path ------------------------------------------------

    async def _execute(self, pid: int, side: str, reason: str, quote: Quote) -> Outcome:
        if self.paper:
            return await self._execute_paper(pid, side, reason, quote)

        swap = await self.jup.swap_transaction(quote, self.wallet.address)
        signature, signed = self.wallet.sign_swap(swap.tx_base64)
        tid = await self.db.insert_trade(
            position_id=pid, side=side, reason=reason, signature=signature, paper=False,
            quoted_in=quote.in_amount, quoted_out=quote.out_amount,
            last_valid_block_height=swap.last_valid_block_height,
        )
        self.in_flight.add(signature)
        try:
            return await self._send_and_settle(tid, signature, signed, swap.last_valid_block_height, side, quote)
        finally:
            self.in_flight.discard(signature)

    async def _send_and_settle(self, tid: int, signature: str, signed: str, lvbh: int,
                               side: str, quote: Quote) -> Outcome:
        try:
            await self.rpc.send_raw_transaction(signed)
        except RpcError as exc:
            # Preflight/signature rejections mean the tx was never forwarded.
            # Anything ambiguous (timeouts) falls through to status polling.
            if exc.code in _NEVER_FORWARDED:
                await self.db.settle_trade(tid, status=FAILED, error=f"preflight: {exc}")
                return Outcome(FAILED, error=f"preflight: {exc}")
            log.warning("send %s raised %s; polling for status", signature, exc)
        except Exception as exc:  # network: the tx may or may not have left
            log.warning("send %s raised %s; polling for status", signature, exc)
        return await self.await_and_settle(tid, signature, lvbh,
                                           side=side, mint=quote.output_mint if side == "BUY" else quote.input_mint,
                                           quoted_in=quote.in_amount)

    async def _execute_paper(self, pid: int, side: str, reason: str, quote: Quote) -> Outcome:
        tid = await self.db.insert_trade(
            position_id=pid, side=side, reason=reason, signature=f"paper-{uuid.uuid4()}", paper=True,
            quoted_in=quote.in_amount, quoted_out=quote.out_amount, last_valid_block_height=None,
        )
        if side == "BUY":
            outcome = Outcome(CONFIRMED, lamports=quote.in_amount, tokens=quote.out_amount, fee_lamports=0)
        else:
            outcome = Outcome(CONFIRMED, lamports=quote.out_amount, tokens=quote.in_amount, fee_lamports=0)
        await self.db.settle_trade(tid, status=CONFIRMED, lamports=outcome.lamports,
                                   token_amount=outcome.tokens, fee_lamports=0)
        return outcome

    async def wait_for_signature(self, signature: str, last_valid_block_height: int | None) -> tuple[str, str | None]:
        """Poll until the signature is confirmed, failed on chain, or can no
        longer land because its blockhash expired."""
        poll = self.cfg.trading.confirm_poll_s
        while True:
            try:
                st = await self.rpc.get_signature_status(signature)
                if st is not None:
                    if st.get("err") is not None:
                        return FAILED, f"on-chain error: {st['err']}"
                    if st.get("confirmationStatus") in ("confirmed", "finalized"):
                        return CONFIRMED, None
                elif last_valid_block_height is not None:
                    if await self.rpc.get_block_height() > last_valid_block_height:
                        # One last look: it may have landed in the final slot.
                        st = await self.rpc.get_signature_status(signature)
                        if st is None:
                            return EXPIRED, "blockhash expired, transaction did not land"
                        continue
            except RpcError as exc:
                log.warning("status poll for %s failed: %s", signature, exc)
            await asyncio.sleep(poll)

    async def await_and_settle(self, tid: int, signature: str, last_valid_block_height: int | None, *,
                               side: str, mint: str, quoted_in: int) -> Outcome:
        status, error = await self.wait_for_signature(signature, last_valid_block_height)
        if status != CONFIRMED:
            await self.db.settle_trade(tid, status=FAILED, error=error)
            return Outcome(FAILED, error=error)

        tx = None
        for _ in range(10):
            tx = await self.rpc.get_transaction(signature)
            if tx is not None:
                break
            await asyncio.sleep(1)
        if tx is None:
            # Landed but not yet queryable. Leave PENDING; recovery settles it.
            raise RuntimeError(f"confirmed transaction {signature} not retrievable yet")

        d = owner_deltas(tx, self.wallet.address, mint)
        if side == "BUY":
            # ExactIn swap: the quoted input is exactly what went into the swap;
            # whatever else left the wallet is fees and token-account rent.
            outcome = Outcome(CONFIRMED, lamports=quoted_in, tokens=d.tokens,
                              fee_lamports=max(0, -d.sol_lamports - quoted_in))
        else:
            outcome = Outcome(CONFIRMED, lamports=d.sol_lamports + d.fee_lamports, tokens=-d.tokens,
                              fee_lamports=d.fee_lamports)
        await self.db.settle_trade(tid, status=CONFIRMED, lamports=outcome.lamports,
                                   token_amount=outcome.tokens, fee_lamports=outcome.fee_lamports)
        return outcome
