"""End-to-end tests against a real PostgreSQL, with fake chain and APIs.

Run with e.g. ``TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/sniper_test``.
Skipped when the variable is not set. The database is wiped per test.
"""

import asyncio
import base64
import os
from decimal import Decimal

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.null_signer import NullSigner
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from sniper.analysis.analyzer import Analyzer
from sniper.app import MIGRATIONS
from sniper.db import Database
from sniper.executor import Executor
from sniper.jupiter import NoRoute, Quote, SwapTx
from sniper.positions import PositionMonitor
from sniper.recovery import Recovery
from sniper.solana.constants import TOKEN_PROGRAM, WSOL_MINT
from sniper.solana.rpc import RpcError
from sniper.solana.wallet import Wallet

DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")

SOL_USD = Decimal(200)          # $20 = 0.1 SOL = 100_000_000 lamports
TRADE_LAMPORTS = 100_000_000


def new_mint() -> str:
    return str(Keypair().pubkey())


class FakeJupiter:
    def __init__(self):
        self.price: dict[str, Decimal] = {}  # lamports per raw token
        self.no_route: set[str] = set()
        self.payer: str | None = None

    async def sol_usd(self):
        return SOL_USD

    async def quote(self, input_mint, output_mint, amount, slippage_bps, *, for_analysis=False):
        mint = output_mint if input_mint == WSOL_MINT else input_mint
        if mint in self.no_route:
            raise NoRoute("COULD_NOT_FIND_ANY_ROUTE")
        p = self.price[mint]
        out = int(Decimal(amount) / p) if input_mint == WSOL_MINT else int(Decimal(amount) * p)
        return Quote(input_mint, output_mint, amount, out, Decimal(0), {"inAmount": str(amount)})

    async def swap_transaction(self, quote, user_public_key):
        payer = Pubkey.from_string(user_public_key)
        msg = MessageV0.try_compile(payer, [], [], Hash.default())
        tx = VersionedTransaction(msg, [NullSigner(payer)])
        return SwapTx(base64.b64encode(bytes(tx)).decode(), last_valid_block_height=1_000)


class FakeRpc:
    """Just enough chain for the analyzer and the live executor."""

    def __init__(self):
        self.mints: dict[str, dict] = {}
        self.holders: dict[str, list[tuple[str, int]]] = {}
        self.sent: list[str] = []
        self.statuses: dict[str, dict | None] = {}
        self.txs: dict[str, dict] = {}
        self.block_height = 900
        self.send_error: RpcError | None = None
        self.on_send = None  # callback(signature)
        self.db = None
        self.rows_at_send: list[int] = []
        self.token_balances: dict[str, int] = {}

    # analyzer
    async def get_parsed_account(self, mint):
        return self.mints.get(mint)

    async def get_program_accounts(self, program, config):
        mint = config["filters"][0]["memcmp"]["bytes"]
        rows = []
        for owner, amount in self.holders[mint]:
            raw = bytes(Pubkey.from_string(owner)) + amount.to_bytes(8, "little")
            rows.append({"pubkey": "x", "account": {"data": [base64.b64encode(raw).decode(), "base64"]}})
        return rows

    async def get_token_largest_accounts(self, mint):
        top = sorted(self.holders[mint], key=lambda h: -h[1])[:20]
        return [{"address": f"acc:{o}", "amount": str(a)} for o, a in top]

    async def get_multiple_parsed_accounts(self, addresses):
        return [{"data": {"parsed": {"info": {"owner": a.split(":", 1)[1]}}}} for a in addresses]

    # executor
    async def get_balance(self, address):
        return 10 * 10**9

    async def send_raw_transaction(self, tx_b64):
        if self.send_error:
            raise self.send_error
        sig = str(VersionedTransaction.from_bytes(base64.b64decode(tx_b64)).signatures[0])
        self.sent.append(sig)
        if self.db is not None:
            self.rows_at_send.append(await self.db.pool.fetchval(
                "SELECT count(*) FROM trades WHERE signature = $1", sig))
        if self.on_send:
            self.on_send(sig)
        return sig

    async def get_signature_status(self, sig):
        return self.statuses.get(sig)

    async def get_block_height(self):
        return self.block_height

    async def get_transaction(self, sig):
        return self.txs.get(sig)

    async def get_token_balance(self, owner, mint):
        return self.token_balances.get(mint, 0)


def mint_account(mint_authority=None, freeze_authority=None):
    return {"owner": TOKEN_PROGRAM, "data": {"parsed": {"type": "mint", "info": {
        "decimals": 6, "supply": str(10**15), "mintAuthority": mint_authority,
        "freezeAuthority": freeze_authority, "isInitialized": True}}}}


def healthy_holders(n=150):
    pool = str(Pubkey.find_program_address([b"pool"], Pubkey.from_string(TOKEN_PROGRAM))[0])
    wallets = [(str(Keypair().pubkey()), 10**12) for _ in range(n)]  # 0.1% each
    return [(pool, 4 * 10**14)] + wallets


class FakeDex:
    def __init__(self):
        self.liquidity: dict[str, int] = {}

    async def pairs_for_token(self, mint):
        if mint not in self.liquidity:
            return []
        return [{"chainId": "solana", "dexId": "raydium", "pairAddress": "Pool",
                 "baseToken": {"address": mint, "symbol": "TST", "name": "Test"},
                 "quoteToken": {"address": WSOL_MINT}, "liquidity": {"usd": self.liquidity[mint]}}]


@pytest.fixture
async def db():
    database = await Database.connect(DB_URL, min_size=1, max_size=5)
    await database.pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await database.migrate(MIGRATIONS)
    yield database
    await database.close()


@pytest.fixture
def paper(settings, db):
    settings.filters.rugcheck.enabled = False
    jup, rpc, dex = FakeJupiter(), FakeRpc(), FakeDex()
    ex = Executor(settings, db, jup, rpc, wallet=None)
    analyzer = Analyzer(settings, db, rpc, jup, dex, None, ex)
    monitor = PositionMonitor(settings, db, jup, ex)
    return settings, db, jup, rpc, dex, ex, analyzer, monitor


def list_token(jup, rpc, dex, mint, *, liquidity=40_000, price=Decimal(100)):
    rpc.mints[mint] = mint_account()
    rpc.holders[mint] = healthy_holders()
    dex.liquidity[mint] = liquidity
    jup.price[mint] = price


async def settle_background(monitor):
    await asyncio.gather(*list(monitor._tasks))


# --- database basics ---------------------------------------------------------

async def test_migrations_are_idempotent_and_lock_is_exclusive(db):
    assert await db.migrate(MIGRATIONS) == []
    assert await db.acquire_instance_lock()
    other = await Database.connect(DB_URL, min_size=1, max_size=2)
    try:
        assert not await other.acquire_instance_lock()
    finally:
        await other.close()


async def test_claim_hands_each_token_to_one_worker(db):
    for _ in range(3):
        await db.insert_token(new_mint(), "test", None)
    a, b = await asyncio.gather(db.claim_due_tokens(2, 60), db.claim_due_tokens(2, 60))
    claimed = [r["mint"] for r in a] + [r["mint"] for r in b]
    assert len(claimed) == 3 and len(set(claimed)) == 3
    assert await db.claim_due_tokens(5, 60) == []


# --- full pipeline in paper mode -------------------------------------------------

async def test_token_waits_then_passes_and_is_bought(paper):
    settings, db, jup, rpc, dex, ex, analyzer, _ = paper
    mint = new_mint()
    list_token(jup, rpc, dex, mint, liquidity=10_000)
    assert await db.insert_token(mint, "test", "sig")
    assert not await db.insert_token(mint, "test", "sig")  # dedupe

    v = await analyzer.process(mint)
    assert v.status.value == "WAIT" and "liquidity" in v.reason
    assert await db.pool.fetchval("SELECT status FROM tokens WHERE mint=$1", mint) == "WATCHING"

    dex.liquidity[mint] = 30_000
    v = await analyzer.process(mint)
    assert v.status.value == "PASS", v.reason
    pos = (await db.positions_by_status("OPEN"))[0]
    assert pos["entry_lamports"] == TRADE_LAMPORTS
    assert int(pos["initial_amount"]) == TRADE_LAMPORTS // 100
    assert await db.pool.fetchval("SELECT status FROM tokens WHERE mint=$1", mint) == "BOUGHT"
    bodies = [r["body"] for r in await db.next_notifications(20)]
    assert any("slaagt voor alle filters" in b for b in bodies)
    assert any("Gekocht" in b for b in bodies)


async def test_scam_token_is_rejected_for_good(paper):
    settings, db, jup, rpc, dex, ex, analyzer, _ = paper
    mint = new_mint()
    list_token(jup, rpc, dex, mint)
    rpc.mints[mint] = mint_account(freeze_authority="Dev")
    await db.insert_token(mint, "test", None)
    v = await analyzer.process(mint)
    assert v.status.value == "REJECT"
    assert await db.pool.fetchval("SELECT status FROM tokens WHERE mint=$1", mint) == "REJECTED"
    assert await db.count_active_positions() == 0


async def test_concentrated_supply_waits(paper):
    settings, db, jup, rpc, dex, ex, analyzer, _ = paper
    mint = new_mint()
    list_token(jup, rpc, dex, mint)
    rpc.holders[mint].append((str(Keypair().pubkey()), 5 * 10**14))  # one wallet holds 50%
    await db.insert_token(mint, "test", None)
    v = await analyzer.process(mint)
    assert v.status.value == "WAIT" and "top10" in v.reason


async def test_max_three_open_positions(paper):
    settings, db, jup, rpc, dex, ex, analyzer, _ = paper
    mints = [new_mint() for _ in range(4)]
    for m in mints:
        list_token(jup, rpc, dex, m)
        await db.insert_token(m, "test", None)
    await asyncio.gather(*(analyzer.process(m) for m in mints))
    assert await db.count_active_positions() == 3
    statuses = sorted(await db.pool.fetchval(
        "SELECT array_agg(status) FROM tokens"))
    assert statuses == ["BOUGHT", "BOUGHT", "BOUGHT", "SKIPPED"]


async def test_take_profit_then_trailing_stop(paper):
    settings, db, jup, rpc, dex, ex, analyzer, monitor = paper
    mint = new_mint()
    list_token(jup, rpc, dex, mint, price=Decimal(100))
    await db.insert_token(mint, "test", None)
    await analyzer.process(mint)
    pid = (await db.positions_by_status("OPEN"))[0]["id"]

    jup.price[mint] = Decimal(150)          # +50% -> sell half
    await monitor.tick(); await settle_background(monitor)
    pos = await db.get_position(pid)
    assert pos["status"] == "OPEN" and pos["tp_hit"]
    assert int(pos["remaining_amount"]) == 500_000

    jup.price[mint] = Decimal(200)          # new peak
    await monitor.tick(); await settle_background(monitor)
    jup.price[mint] = Decimal(171)          # above 200 * 0.85
    await monitor.tick(); await settle_background(monitor)
    assert (await db.get_position(pid))["status"] == "OPEN"

    jup.price[mint] = Decimal(170)          # trailing stop
    await monitor.tick(); await settle_background(monitor)
    pos = await db.get_position(pid)
    assert pos["status"] == "CLOSED" and pos["close_reason"] == "TRAILING_STOP"
    # 0.5 * 1.5 + 0.5 * 1.7 = 1.6x the 0.1 SOL stake
    assert pos["realized_lamports"] == 160_000_000
    assert await db.realized_pnl_lamports_since(pos["opened_at"]) == 60_000_000


async def test_stop_loss_closes_everything(paper):
    settings, db, jup, rpc, dex, ex, analyzer, monitor = paper
    mint = new_mint()
    list_token(jup, rpc, dex, mint, price=Decimal(100))
    await db.insert_token(mint, "test", None)
    await analyzer.process(mint)
    jup.price[mint] = Decimal(79)
    await monitor.tick(); await settle_background(monitor)
    pos = (await db.positions_by_status("CLOSED"))[0]
    assert pos["close_reason"] == "STOP_LOSS"
    assert pos["realized_lamports"] == 79_000_000


async def test_daily_loss_limit_blocks_buys(paper):
    settings, db, jup, rpc, dex, ex, analyzer, monitor = paper
    settings.trading.max_daily_loss_usd = Decimal(4)   # one stop-loss loses ~$4.20
    first, second = new_mint(), new_mint()
    for m in (first, second):
        list_token(jup, rpc, dex, m)
        await db.insert_token(m, "test", None)
    await analyzer.process(first)
    jup.price[first] = Decimal(79)
    await monitor.tick(); await settle_background(monitor)
    await analyzer.process(second)
    row = await db.pool.fetchrow("SELECT status, last_reason FROM tokens WHERE mint=$1", second)
    assert row["status"] == "SKIPPED" and "daily loss" in row["last_reason"]


# --- live execution path (fake chain) ------------------------------------------

@pytest.fixture
def live(settings, db):
    settings.trading.mode = "live"
    settings.trading.confirm_poll_s = 0.01
    jup, rpc = FakeJupiter(), FakeRpc()
    wallet = Wallet(Keypair())
    ex = Executor(settings, db, jup, rpc, wallet)
    return settings, db, jup, rpc, wallet, ex


def confirmed_tx(wallet, mint, sol_delta, token_delta, fee=5_000):
    return {
        "transaction": {"message": {"accountKeys": [{"pubkey": wallet.address}]}},
        "meta": {"err": None, "fee": fee, "preBalances": [10**10], "postBalances": [10**10 + sol_delta],
                 "preTokenBalances": [], "postTokenBalances": [
                     {"mint": mint, "owner": wallet.address, "uiTokenAmount": {"amount": str(token_delta)}}]},
    }


async def test_live_buy_records_signature_before_sending(live):
    settings, db, jup, rpc, wallet, ex = live
    mint = new_mint()
    jup.price[mint] = Decimal(100)
    await db.insert_token(mint, "test", None)

    rpc.db = db

    def on_send(sig):
        rpc.statuses[sig] = {"confirmationStatus": "confirmed", "err": None}
        # 0.1 SOL into the swap + 2.04M rent + 5k fee; 998_000 tokens after slippage
        rpc.txs[sig] = confirmed_tx(wallet, mint, -(TRADE_LAMPORTS + 2_039_280 + 5_000), 998_000)
    rpc.on_send = on_send

    pid = await ex.try_buy(mint, 6, "TST")
    trade = await db.pool.fetchrow("SELECT * FROM trades WHERE position_id=$1", pid)
    assert trade["signature"] == rpc.sent[0] and trade["status"] == "CONFIRMED"
    assert rpc.rows_at_send == [1]  # the trade row existed before the tx left
    assert trade["lamports"] == TRADE_LAMPORTS and trade["fee_lamports"] == 2_044_280
    pos = await db.get_position(pid)
    assert pos["status"] == "OPEN" and int(pos["initial_amount"]) == 998_000


async def test_live_preflight_failure_fails_cleanly(live):
    settings, db, jup, rpc, wallet, ex = live
    mint = new_mint()
    jup.price[mint] = Decimal(100)
    await db.insert_token(mint, "test", None)
    rpc.send_error = RpcError("Transaction simulation failed: slippage", -32002)
    assert await ex.try_buy(mint, 6, "TST") is None
    assert await db.pool.fetchval("SELECT status FROM trades") == "FAILED"
    assert await db.pool.fetchval("SELECT status FROM positions") == "FAILED"
    assert await db.count_active_positions() == 0


async def test_expired_blockhash_marks_trade_failed(live):
    settings, db, jup, rpc, wallet, ex = live
    mint = new_mint()
    jup.price[mint] = Decimal(100)
    await db.insert_token(mint, "test", None)
    rpc.block_height = 1_001  # past lastValidBlockHeight, signature never seen
    assert await ex.try_buy(mint, 6, "TST") is None
    trade = await db.pool.fetchrow("SELECT * FROM trades")
    assert trade["status"] == "FAILED" and "expired" in trade["error"]


async def test_recovery_settles_a_trade_interrupted_by_a_crash(live):
    settings, db, jup, rpc, wallet, ex = live
    mint = new_mint()
    jup.price[mint] = Decimal(100)
    await db.insert_token(mint, "test", None)

    # Simulate the process dying right after sending.
    class Crash(BaseException):
        pass

    def crash(sig):
        raise Crash
    rpc.on_send = crash
    with pytest.raises(Crash):
        await ex.try_buy(mint, 6, "TST")
    sig = await db.pool.fetchval("SELECT signature FROM trades WHERE status='PENDING'")
    assert sig is not None  # write-ahead: recorded although never confirmed

    # "Restart": fresh executor; meanwhile the tx did land.
    rpc.on_send = None
    rpc.statuses[sig] = {"confirmationStatus": "finalized", "err": None}
    rpc.txs[sig] = confirmed_tx(wallet, mint, -(TRADE_LAMPORTS + 5_000), 1_000_000)
    rpc.token_balances[mint] = 1_000_000
    ex2 = Executor(settings, db, jup, rpc, wallet)
    await Recovery(db, ex2, rpc).run(startup=True)

    assert await db.pool.fetchval("SELECT status FROM trades") == "CONFIRMED"
    assert len(rpc.sent) == 1  # never re-sent
    pos = await db.pool.fetchrow("SELECT * FROM positions")
    assert pos["status"] == "OPEN" and int(pos["initial_amount"]) == 1_000_000


async def test_recovery_marks_tokens_sold_outside_the_bot(live):
    settings, db, jup, rpc, wallet, ex = live
    mint = new_mint()
    await db.insert_token(mint, "test", None)
    pid = await db.create_opening_position(mint, 6)
    await db.update_position(pid, status="OPEN", initial_amount=1000, remaining_amount=1000,
                             entry_lamports=1, entry_price=Decimal(1))
    rpc.token_balances[mint] = 0
    await Recovery(db, ex, rpc).run(startup=True)
    pos = await db.get_position(pid)
    assert pos["status"] == "CLOSED" and pos["close_reason"] == "EXTERNAL"


async def test_startup_recovery_resets_orphaned_states(paper):
    settings, db, jup, rpc, dex, ex, analyzer, monitor = paper
    a, b = new_mint(), new_mint()
    for m in (a, b):
        await db.insert_token(m, "test", None)
    opening = await db.create_opening_position(a, 6)
    closing = await db.create_opening_position(b, 6)
    await db.update_position(closing, status="CLOSING", remaining_amount=5, initial_amount=5,
                             entry_lamports=1, entry_price=Decimal(1))
    await Recovery(db, ex, rpc).run(startup=True)
    assert (await db.get_position(opening))["status"] == "FAILED"
    assert (await db.get_position(closing))["status"] == "OPEN"
