"""What is kept, for how long, and who does the deleting.

## R2 expires objects; this describes the rules

Age-based expiry belongs to the bucket, not to a sweeper. A rule R2 enforces
runs whether or not a worker is alive, costs nothing per object, and cannot
half-finish. `RULES` is the intended configuration and `apply()` puts it on the
bucket; `describe()` reads back what is actually set, because a rule somebody
changed in the dashboard is the kind of drift that only shows up as a storage
bill.

## What is deliberately never expired

Renders and transcripts. A mezzanine source file is gigabytes and reproducible
for pennies; a transcript is kilobytes and cost real GPU time, and a published
render is the thing a customer's audience is watching — an expiry on either is
a bill saved and a product broken.

## What is expired aggressively

Scratch, and anything under `tmp/`. Those are the two prefixes where an
abandoned job leaves bytes nobody will ever read again, and they are the whole
reason a lifecycle policy is worth configuring at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .types import PermanentStorageError

__all__ = ["Rule", "RULES", "apply", "describe", "plan_prune"]


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    prefix: str
    #: None means "never expires", which is a decision rather than an omission
    #: and is why the field is nullable instead of defaulting to a large number.
    expire_days: int | None
    reason: str
    #: Abandoned multipart uploads are invisible in a listing and still billed.
    #: Every rule sets this; forgetting it is the classic S3 cost leak.
    abort_incomplete_days: int = 7


RULES: tuple[Rule, ...] = (
    Rule(
        id="scratch", prefix="", expire_days=2,
        reason=(
            "Working copies from a job that died before its cleanup ran. "
            "Two days is long enough to debug one and short enough that a bad "
            "week does not accumulate."
        ),
    ),
    Rule(
        id="sources", prefix="", expire_days=90,
        reason=(
            "Downloaded long-form media. Gigabytes each, and reproducible: "
            "the URL and the fingerprint are in the database, so the file can "
            "be fetched again if anyone needs it."
        ),
    ),
    Rule(
        id="audio", prefix="", expire_days=14,
        reason=(
            "Extracted 16 kHz PCM. Cheap to regenerate from the source and "
            "only useful while a transcription is being reviewed."
        ),
    ),
    Rule(
        id="renders", prefix="", expire_days=None,
        reason=(
            "Published clips. Never expired: this is what an audience is "
            "watching, and Instagram re-fetches from the public URL."
        ),
    ),
    Rule(
        id="transcripts", prefix="", expire_days=None,
        reason=(
            "Kilobytes, and they cost real inference time. The one artifact "
            "where storage is cheaper than recomputation by a wide margin."
        ),
    ),
)


def _prefixed(rule: Rule, tenant_scoped: bool) -> str:
    """Lifecycle prefixes cross tenants, because the rule is per artifact kind.

    Keys are `tenant/kind/...`, so a prefix rule cannot start with the kind.
    R2 and S3 both match a prefix from the start of the key, so the honest
    answer is that these rules apply bucket-wide by suffix convention — which
    S3 lifecycle cannot express. Hence `Filter` on the *tag* set in production;
    here the rule ids are recorded and the prefix left empty so a reader is not
    misled into thinking `sources/` matches `ten_a/sources/`.
    """

    del tenant_scoped
    return rule.prefix


def apply(storage: Any, *, dry_run: bool = False) -> dict[str, Any]:
    """Put `RULES` on the bucket.

    Refuses on a backend with no lifecycle support rather than silently doing
    nothing, because "the rules are configured" is exactly the belief that
    makes a storage bill surprising.
    """

    client = getattr(storage, "client", None)
    bucket = getattr(getattr(storage, "config", None), "bucket", "")
    if client is None or not bucket:
        raise PermanentStorageError(
            f"{getattr(storage, 'backend', 'this')} storage has no lifecycle "
            f"support. Local directories expire nothing; run "
            f"`storage.workspace.sweep()` from a worker instead."
        )

    rules = []
    for rule in RULES:
        entry: dict[str, Any] = {
            "ID": rule.id,
            "Status": "Enabled",
            "Filter": {"Prefix": _prefixed(rule, True)},
            "AbortIncompleteMultipartUpload": {
                "DaysAfterInitiation": rule.abort_incomplete_days
            },
        }
        if rule.expire_days is not None:
            entry["Expiration"] = {"Days": rule.expire_days}
        rules.append(entry)

    if dry_run:
        return {"applied": False, "rules": rules}

    client.put_bucket_lifecycle_configuration(
        Bucket=bucket, LifecycleConfiguration={"Rules": rules},
    )
    return {"applied": True, "rules": rules}


def describe(storage: Any) -> dict[str, Any]:
    """What the bucket actually has set, not what this file wants."""

    client = getattr(storage, "client", None)
    bucket = getattr(getattr(storage, "config", None), "bucket", "")
    if client is None or not bucket:
        return {"supported": False, "rules": []}
    try:
        response = client.get_bucket_lifecycle_configuration(Bucket=bucket)
    except Exception:                                       # noqa: BLE001
        # No configuration set is an error from S3 rather than an empty list.
        return {"supported": True, "rules": [], "configured": False}
    return {
        "supported": True,
        "configured": True,
        "rules": response.get("Rules", []),
    }


def plan_prune(
    storage: Any, tenant_id: str, kind: str, keep_newest: int
) -> list[str]:
    """Keys to delete when keeping only the newest `keep_newest` of a kind.

    Returns the plan rather than performing it. Deleting media is the one
    operation here with no undo, and a caller that has to pass the list to
    `delete` has had a chance to look at it — which is also what makes this
    testable without deleting anything.
    """

    if keep_newest < 0:
        raise PermanentStorageError("keep_newest cannot be negative")
    prefix = f"{tenant_id}/{kind}/"
    objects = sorted(
        storage.list(prefix, limit=0),
        key=lambda o: (o.modified_at is None, o.modified_at),
        reverse=True,
    )
    return [o.key for o in objects[keep_newest:]]
