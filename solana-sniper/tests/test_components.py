import asyncio
import base64
import json

from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.null_signer import NullSigner
from solders.transaction import VersionedTransaction

from sniper.config import DetectionConfig, LaunchSource
from sniper.detector import LaunchDetector, is_launch
from sniper.ratelimit import TokenBucket
from sniper.solana.wallet import Wallet

RAY = LaunchSource(name="ray", program_id="Ray", log_contains=["initialize2"])


def notification(sig, logs, err=None):
    return json.dumps({"method": "logsNotification",
                       "params": {"result": {"value": {"signature": sig, "err": err, "logs": logs}}}})


def test_is_launch():
    assert is_launch(["Program log: initialize2: InitializeInstruction2 {...}"], RAY)
    assert not is_launch(["Program log: ray_log: swap"], RAY)


def test_detector_queues_launches_once_and_skips_failures():
    det = LaunchDetector(DetectionConfig(sources=[RAY], queue_size=2), "ws://x", rpc=None, db=None)
    det._handle(RAY, notification("s1", ["initialize2"]))
    det._handle(RAY, notification("s1", ["initialize2"]))          # duplicate
    det._handle(RAY, notification("s2", ["swap"]))                 # not a launch
    det._handle(RAY, notification("s3", ["initialize2"], err={}))  # failed tx
    det._handle(RAY, json.dumps({"jsonrpc": "2.0", "result": 7, "id": 1}))  # subscription ack
    assert det.queue.qsize() == 1
    det._handle(RAY, notification("s4", ["initialize2"]))
    det._handle(RAY, notification("s5", ["initialize2"]))  # queue full
    assert det.queue.qsize() == 2 and det.dropped == 1


def test_wallet_signs_jupiter_style_unsigned_transaction():
    kp = Keypair()
    msg = MessageV0.try_compile(kp.pubkey(), [], [], Hash.default())
    unsigned = VersionedTransaction(msg, [NullSigner(kp.pubkey())])
    wallet = Wallet(kp)
    sig, signed_b64 = wallet.sign_swap(base64.b64encode(bytes(unsigned)).decode())
    signed = VersionedTransaction.from_bytes(base64.b64decode(signed_b64))
    assert str(signed.signatures[0]) == sig
    assert signed.verify_with_results() == [True]
    assert str(kp) not in repr(wallet)


async def test_token_bucket_paces_requests():
    now = [0.0]
    bucket = TokenBucket(2, burst=1, clock=lambda: now[0])
    await bucket.acquire()
    task = asyncio.create_task(bucket.acquire())
    await asyncio.sleep(0)
    assert not task.done()   # bucket empty, waits ~0.5s
    now[0] = 0.5
    await asyncio.wait_for(task, 2)
