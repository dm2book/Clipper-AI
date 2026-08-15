"""What an object is, where it lives, and how to name it.

## Local disk does not go away, and pretending otherwise would be a lie

ffmpeg reads files. yt-dlp writes files. The MP4 box reader seeks. None of them
speaks S3, and wrapping them in a FUSE mount trades a clear failure for a slow
mysterious one.

So the migration is not "delete every local path". It is a change of *system of
record*: R2 holds the durable copy, local disk holds a scratch copy for as long
as a process is working on it, and `workspace.py` is the seam. Anything on
local disk after a job finishes is a bug, and `sweep()` is what finds it.

## Keys start with the tenant

`ten_acme/sources/src_123/media.mp4`. Not for tidiness — for blast radius. A
bug that builds the wrong key lands in the same tenant's prefix, an IAM policy
can be scoped by prefix, and a per-tenant deletion is a prefix delete rather
than a query. A key that started with `sources/` would make every one of those
impossible to express.

`StorageRef` parses both `r2://bucket/key` and a bare local path, because the
`media_path` column already holds thousands of the latter and a migration that
requires a backfill before anything works is a migration nobody runs.
"""

from __future__ import annotations

import enum
import posixpath
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

__all__ = [
    "StorageError", "TransientStorageError", "PermanentStorageError",
    "ObjectNotFound", "StoredObject", "StorageRef", "Visibility",
    "key_for", "SCHEME",
]

SCHEME = "r2"

#: Anything outside this is refused before it reaches the wire. S3 permits far
#: more, but a key with a newline or a `../` in it is a bug on the way to being
#: a security problem, and the cost of refusing is one regex.
_SAFE_KEY = re.compile(r"^[A-Za-z0-9!\-_.*'()/]+$")


class StorageError(Exception):
    """Something went wrong with storage."""


class TransientStorageError(StorageError):
    """Worth another attempt: a timeout, a 500, a throttle."""


class PermanentStorageError(StorageError):
    """Not worth another attempt: bad credentials, a malformed key, 404."""


class ObjectNotFound(PermanentStorageError):
    """No object at that key."""


class Visibility(str, enum.Enum):
    """Whether an object is reachable without a signature.

    `PUBLIC` is not a convenience. Instagram's Graph API fetches the file
    itself from a URL it is given, with no credentials, so a Reel cannot
    publish from a private object however well the URL is signed — Meta's
    fetcher will not present a signature it was never given. This is the only
    reason the distinction exists.
    """

    PRIVATE = "private"
    PUBLIC = "public"


@dataclass(slots=True)
class StoredObject:
    """One object, as the store describes it."""

    key: str
    size_bytes: int = 0
    etag: str = ""
    content_type: str = ""
    modified_at: datetime | None = None
    #: Small user metadata, stored alongside. Kept short: S3 caps the total
    #: header size and a large value fails at PUT with an unhelpful error.
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "size_bytes": self.size_bytes,
            "etag": self.etag,
            "content_type": self.content_type,
            "modified_at": (
                self.modified_at.isoformat() if self.modified_at else None
            ),
        }


@dataclass(frozen=True, slots=True)
class StorageRef:
    """Where a piece of media is, whichever backend holds it.

    Two forms, and both have to work: `r2://bucket/key` for anything written
    since the migration, and a bare filesystem path for everything written
    before it. A ref that only understood the new form would need every row
    backfilled before a single job could run.
    """

    key: str
    bucket: str = ""
    #: True when this is a filesystem path rather than an object key.
    local: bool = False

    @classmethod
    def parse(cls, value: str) -> StorageRef:
        if not value:
            raise PermanentStorageError("empty storage reference")
        if value.startswith(f"{SCHEME}://"):
            rest = value[len(SCHEME) + 3:]
            bucket, _, key = rest.partition("/")
            if not bucket or not key:
                raise PermanentStorageError(
                    f"{value!r} is not {SCHEME}://bucket/key"
                )
            return cls(key=key, bucket=bucket)
        # Anything else is a path. Deliberately permissive: this is the form
        # already in the database.
        return cls(key=value, local=True)

    def __str__(self) -> str:
        if self.local:
            return self.key
        return f"{SCHEME}://{self.bucket}/{self.key}"

    @property
    def filename(self) -> str:
        return posixpath.basename(self.key)


def key_for(tenant_id: str, *parts: str) -> str:
    """Build a tenant-scoped key, refusing anything that could escape it.

    The traversal check is the point. `key_for(tenant, "..", "..", "other")`
    would otherwise produce a key reaching another tenant's prefix, and object
    stores have no directory semantics to stop it — the key is just a string,
    and `a/../b` is a *different object* from `b`, not the same one, which
    means the guard has to be here rather than left to the store.
    """

    if not tenant_id:
        raise PermanentStorageError("a storage key needs a tenant")
    cleaned: list[str] = [tenant_id]
    for part in parts:
        piece = str(part).strip("/")
        if not piece:
            continue
        if ".." in piece.split("/"):
            raise PermanentStorageError(
                f"{part!r} contains a path traversal and would leave the "
                f"tenant's prefix"
            )
        cleaned.append(piece)

    key = "/".join(cleaned)
    if not _SAFE_KEY.match(key):
        raise PermanentStorageError(
            f"unsafe characters in storage key {key!r} — allowed: letters, "
            f"digits and !-_.*'()/"
        )
    if len(key.encode()) > 1024:
        raise PermanentStorageError("storage key is over the 1024-byte limit")
    return key
