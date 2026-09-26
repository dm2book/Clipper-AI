"""The bot's hot wallet. The key is loaded once and never logged."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from sniper.config import WalletConfig


class Wallet:
    def __init__(self, keypair: Keypair):
        self._keypair = keypair
        self.address = str(keypair.pubkey())

    @classmethod
    def from_config(cls, cfg: WalletConfig) -> "Wallet":
        if cfg.private_key:
            return cls(Keypair.from_base58_string(cfg.private_key.strip()))
        if cfg.keypair_path:
            raw = json.loads(Path(cfg.keypair_path).read_text())
            return cls(Keypair.from_bytes(bytes(raw)))
        raise ValueError("no wallet configured")

    def sign_swap(self, tx_base64: str) -> tuple[str, str]:
        """Sign an unsigned Jupiter swap transaction.

        Returns ``(signature, signed_tx_base64)``. The signature is known before
        anything is sent, which is what makes write-ahead recording possible.
        """
        unsigned = VersionedTransaction.from_bytes(base64.b64decode(tx_base64))
        signed = VersionedTransaction(unsigned.message, [self._keypair])
        return str(signed.signatures[0]), base64.b64encode(bytes(signed)).decode()

    def __repr__(self) -> str:  # never print key material
        return f"Wallet({self.address})"
