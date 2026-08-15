"""A directory, behind the same interface.

Kept, not deprecated. It is what the tests run against for everything that is
not specifically about R2, it is what a single-machine deployment should use,
and it is the reference the object store is checked against — one suite runs
over both, so the fast local results are evidence about the remote one.

What it cannot do is the honest part: `public_url` raises. There is no web
server in this repository, so a local file has no URL Instagram could fetch,
and returning a `file://` would be a URL-shaped thing that fails at Meta's
fetcher instead of here.
"""

from __future__ import annotations

import mimetypes
import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, BinaryIO, Iterator

from .protocol import StorageMetrics
from .types import (
    ObjectNotFound,
    PermanentStorageError,
    StoredObject,
    Visibility,
)

__all__ = ["LocalStorage"]


@dataclass
class LocalStorage:
    root: str
    metrics: StorageMetrics = field(default_factory=StorageMetrics)
    #: Set when a static server fronts `root`, which makes `public_url`
    #: answerable. Empty by default because usually nothing does.
    public_base_url: str = ""

    def __post_init__(self) -> None:
        os.makedirs(self.root, exist_ok=True)

    @property
    def backend(self) -> str:
        return "local"

    def _path(self, key: str) -> str:
        """Resolve a key inside the root, refusing anything that escapes it.

        `key_for` already refuses traversal, but this is the last line before
        a write reaches the filesystem and a key can arrive from a database
        row written by an older version. The cost is one `realpath`.
        """

        if not key:
            raise PermanentStorageError("empty storage key")
        candidate = os.path.realpath(os.path.join(self.root, key))
        root = os.path.realpath(self.root)
        if candidate != root and not candidate.startswith(root + os.sep):
            raise PermanentStorageError(
                f"{key!r} resolves outside the storage root"
            )
        return candidate

    def put_file(
        self, key: str, path: str, *, content_type: str = "",
        visibility: Visibility = Visibility.PRIVATE,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        if not os.path.exists(path):
            raise PermanentStorageError(f"nothing to upload at {path!r}")
        destination = self._path(key)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        size = os.path.getsize(path)
        # Copied then renamed, so a reader never sees a half-written object.
        scratch = f"{destination}.part"
        shutil.copyfile(path, scratch)
        os.replace(scratch, destination)
        self.metrics.record("put_file", size=size)
        del visibility, metadata      # no ACLs and no metadata on a filesystem
        return self.stat(key)

    def get_file(self, key: str, path: str) -> StoredObject:
        source = self._path(key)
        if not os.path.exists(source):
            raise ObjectNotFound(f"no object at {key!r}")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        shutil.copyfile(source, path)
        stored = self.stat(key)
        self.metrics.record("get_file", size=stored.size_bytes)
        return stored

    def open(self, key: str) -> BinaryIO:
        source = self._path(key)
        if not os.path.exists(source):
            raise ObjectNotFound(f"no object at {key!r}")
        self.metrics.record("open")
        return open(source, "rb")     # noqa: SIM115 — the caller closes it

    def stat(self, key: str) -> StoredObject:
        path = self._path(key)
        if not os.path.exists(path):
            raise ObjectNotFound(f"no object at {key!r}")
        info = os.stat(path)
        guessed, _ = mimetypes.guess_type(path)
        return StoredObject(
            key=key,
            size_bytes=info.st_size,
            etag=f"{int(info.st_mtime)}-{info.st_size}",
            content_type=guessed or "application/octet-stream",
            modified_at=datetime.fromtimestamp(info.st_mtime, UTC),
        )

    def exists(self, key: str) -> bool:
        return os.path.exists(self._path(key))

    def delete(self, key: str) -> bool:
        path = self._path(key)
        if not os.path.exists(path):
            return False
        os.remove(path)
        self.metrics.record("delete")
        self._prune(os.path.dirname(path))
        return True

    def delete_prefix(self, prefix: str) -> int:
        if not prefix:
            raise PermanentStorageError(
                "refusing to delete an empty prefix — that is everything"
            )
        removed = 0
        for stored in list(self.list(prefix, limit=0)):
            if self.delete(stored.key):
                removed += 1
        self.metrics.record("delete_prefix")
        return removed

    def _prune(self, directory: str) -> None:
        """Remove directories a delete emptied, stopping at the root."""
        root = os.path.realpath(self.root)
        while directory.startswith(root + os.sep) and directory != root:
            try:
                os.rmdir(directory)
            except OSError:
                return
            directory = os.path.dirname(directory)

    def list(self, prefix: str, limit: int = 1000) -> Iterator[StoredObject]:
        root = os.path.realpath(self.root)
        seen = 0
        for base, _, names in os.walk(root):
            for name in sorted(names):
                if name.endswith(".part"):
                    continue
                key = os.path.relpath(os.path.join(base, name), root)
                key = key.replace(os.sep, "/")
                if not key.startswith(prefix):
                    continue
                yield self.stat(key)
                seen += 1
                if limit and seen >= limit:
                    return

    def signed_url(
        self, key: str, *, expires_in: timedelta | None = None,
        download_as: str = "",
    ) -> str:
        """There is nothing to sign against a filesystem.

        Returns a `file://` URL, which is honest — it is where the bytes are —
        and useless to a browser, which is also honest. The alternative is
        inventing a signature no verifier exists for.
        """

        del expires_in, download_as
        if not self.exists(key):
            raise ObjectNotFound(f"no object at {key!r}")
        return f"file://{self._path(key)}"

    def signed_upload_url(
        self, key: str, *, expires_in: timedelta | None = None,
        content_type: str = "",
    ) -> str:
        del key, expires_in, content_type
        raise PermanentStorageError(
            "local storage cannot issue upload URLs — there is no endpoint to "
            "PUT to. Configure R2 for direct browser uploads."
        )

    def public_url(self, key: str) -> str:
        if not self.public_base_url:
            raise PermanentStorageError(
                "local storage has no public URL. Instagram fetches media "
                "itself over the internet, so Reels cannot publish from a "
                "local directory — this is the gap R2 exists to close."
            )
        return f"{self.public_base_url.rstrip('/')}/{key}"

    def usage(self, prefix: str = "") -> dict[str, Any]:
        total = 0
        count = 0
        largest: StoredObject | None = None
        for stored in self.list(prefix, limit=0):
            total += stored.size_bytes
            count += 1
            if largest is None or stored.size_bytes > largest.size_bytes:
                largest = stored
        return {
            "prefix": prefix,
            "objects": count,
            "bytes": total,
            "gigabytes": round(total / 1e9, 3),
            "largest": largest.to_dict() if largest else None,
            "backend": self.backend,
        }
