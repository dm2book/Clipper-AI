"""Choosing a backend from the environment.

There is no default backend and no fallback. A deployment that meant to use R2
and mistyped a variable gets an error at startup, not a working system quietly
writing media to a container's disk — which looks fine until the next deploy,
when a customer's library is gone.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

from .local import LocalStorage
from .r2 import R2Config, R2Storage
from .types import PermanentStorageError

__all__ = ["ENV_PREFIX", "storage_from_env", "describe_environment", "BACKENDS"]

ENV_PREFIX = "CLIPFORGE_STORAGE_"
R2_PREFIX = "CLIPFORGE_R2_"
BACKENDS = ("r2", "local")


def _env(name: str, default: str = "", prefix: str = ENV_PREFIX) -> str:
    return os.environ.get(f"{prefix}{name}", default).strip()


def storage_from_env() -> Any:
    backend = _env("BACKEND").lower()
    if not backend:
        raise PermanentStorageError(
            f"no storage backend configured — set {ENV_PREFIX}BACKEND to one "
            f"of: {', '.join(BACKENDS)}. There is deliberately no default: "
            f"falling back to a local directory would lose a customer's media "
            f"on the next deploy without an error anywhere."
        )
    if backend == "local":
        root = _env("LOCAL_ROOT") or "/var/lib/clipforge/media"
        return LocalStorage(root=root, public_base_url=_env("PUBLIC_BASE_URL"))
    if backend == "r2":
        return R2Storage(r2_config_from_env())
    raise PermanentStorageError(
        f"unknown storage backend {backend!r} — expected one of "
        f"{', '.join(BACKENDS)}"
    )


def r2_config_from_env() -> R2Config:
    ttl = _env("SIGNED_URL_TTL_S", prefix=R2_PREFIX)
    return R2Config(
        bucket=_env("BUCKET", prefix=R2_PREFIX),
        account_id=_env("ACCOUNT_ID", prefix=R2_PREFIX),
        endpoint_url=_env("ENDPOINT_URL", prefix=R2_PREFIX),
        # Read from the environment and never written anywhere — not to a log,
        # not to a run record, not into `describe_environment`.
        access_key_id=_env("ACCESS_KEY_ID", prefix=R2_PREFIX),
        secret_access_key=_env("SECRET_ACCESS_KEY", prefix=R2_PREFIX),
        public_base_url=_env("PUBLIC_BASE_URL", prefix=R2_PREFIX),
        signed_url_ttl=timedelta(seconds=int(ttl)) if ttl else timedelta(hours=1),
    )


def describe_environment() -> dict[str, Any]:
    """What is configured, for a startup log. No secret is echoed.

    The key *names* appear and their values never do — only whether they are
    set, which is the question an operator is actually asking.
    """

    backend = _env("BACKEND").lower()
    report: dict[str, Any] = {
        "env_prefix": ENV_PREFIX,
        "backend": backend or None,
        "backends": list(BACKENDS),
    }

    if backend == "r2":
        config = r2_config_from_env()
        problems = []
        if not config.bucket:
            problems.append(f"{R2_PREFIX}BUCKET is not set")
        if not config.access_key_id or not config.secret_access_key:
            problems.append(
                f"{R2_PREFIX}ACCESS_KEY_ID and {R2_PREFIX}SECRET_ACCESS_KEY "
                f"are required"
            )
        if not config.endpoint_url and not config.account_id:
            problems.append(
                f"{R2_PREFIX}ACCOUNT_ID or {R2_PREFIX}ENDPOINT_URL is required"
            )
        if not config.public_base_url:
            problems.append(
                f"{R2_PREFIX}PUBLIC_BASE_URL is not set, so Instagram cannot "
                f"publish — it fetches media itself and cannot present a "
                f"signature"
            )
        report.update({
            "bucket": config.bucket or None,
            "endpoint": config.endpoint_url or (
                f"https://<account>.r2.cloudflarestorage.com"
                if config.account_id else None
            ),
            "credentials_present": bool(
                config.access_key_id and config.secret_access_key
            ),
            "public_base_url": config.public_base_url or None,
            "ready": not problems,
            "problems": problems,
        })
    elif backend == "local":
        report.update({
            "root": _env("LOCAL_ROOT") or "/var/lib/clipforge/media",
            "ready": True,
            "problems": [
                "local storage has no public URL, so Instagram cannot publish",
                "media is lost when the container is replaced",
            ],
        })
    else:
        report.update({
            "ready": False,
            "problems": [f"{ENV_PREFIX}BACKEND is not set"],
        })
    return report
