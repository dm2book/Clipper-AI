"""The seam between object storage and the tools that need real files.

ffmpeg reads files, yt-dlp writes files, and the MP4 reader seeks. None of them
speaks S3. So every job that touches media follows the same shape:

    with Workspace(storage, tenant_id) as work:
        local = work.fetch(ref)            # object → scratch file
        output = work.path("clip.mp4")     # somewhere to write
        ...run ffmpeg...
        ref = work.publish(output, key)    # scratch file → object

and the scratch directory is removed in a `finally`, including on the exception
path — which is the one that matters, because a failure halfway through a
three-hour podcast has hundreds of megabytes to answer for.

## Why not a FUSE mount

It would let ffmpeg "read from R2" and would turn every seek into a range
request. A codec probing a file seeks dozens of times before decoding a frame,
so the result is a job that works in testing and takes four minutes to start in
production, with no error to explain it. Fetching once is slower to write and
faster to run.

## `sweep` exists because processes die

The context manager cleans up when the block exits. A worker that is killed
mid-job — a spot instance reclaimed, an OOM, a deploy — never exits its block,
and its scratch directory outlives it. `sweep()` is what a worker runs at
startup, and it only removes directories this module created.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

from .types import PermanentStorageError, StorageRef, Visibility, key_for

__all__ = ["Workspace", "sweep", "PREFIX"]

#: Every scratch directory starts with this, and `sweep` will not touch a
#: directory that does not. A sweeper matching anything under /tmp is one
#: mistake away from deleting somebody else's work.
PREFIX = "clipforge-work-"


@dataclass
class Workspace:
    """A scratch directory, and the two moves that cross the boundary."""

    storage: Any
    tenant_id: str
    #: Where scratch directories are made. The default follows the platform's
    #: temp location, which on a container is usually the writable layer.
    base: str = ""
    #: Kept for debugging when something produced a bad file. Off by default:
    #: these are hundreds of megabytes each.
    keep: bool = False
    directory: str = field(default="", init=False)

    def __enter__(self) -> Workspace:
        os.makedirs(self.base or tempfile.gettempdir(), exist_ok=True)
        self.directory = tempfile.mkdtemp(
            prefix=PREFIX, dir=self.base or None
        )
        return self

    def __exit__(self, *_: object) -> None:
        if self.directory and not self.keep:
            shutil.rmtree(self.directory, ignore_errors=True)
        self.directory = ""

    # -- paths -------------------------------------------------------------

    def path(self, name: str) -> str:
        """A path inside the scratch directory."""

        if not self.directory:
            raise PermanentStorageError(
                "workspace used outside its `with` block"
            )
        if os.path.isabs(name) or ".." in name.split(os.sep):
            raise PermanentStorageError(f"{name!r} escapes the workspace")
        full = os.path.join(self.directory, name)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        return full

    # -- object → file -----------------------------------------------------

    def fetch(self, ref: StorageRef | str, name: str = "") -> str:
        """Bring an object into the workspace and return its local path.

        A `StorageRef` that is already local is returned as-is rather than
        copied. That is what makes the migration incremental: rows written
        before it hold filesystem paths, and those keep working while new ones
        hold object keys.
        """

        parsed = ref if isinstance(ref, StorageRef) else StorageRef.parse(ref)
        if parsed.local:
            if not os.path.exists(parsed.key):
                raise PermanentStorageError(
                    f"no local media at {parsed.key!r} — it was written before "
                    f"object storage and the file is gone"
                )
            return parsed.key

        local = self.path(name or parsed.filename or "object")
        self.storage.get_file(parsed.key, local)
        return local

    # -- file → object -----------------------------------------------------

    def publish(
        self, path: str, key: str, *, content_type: str = "",
        visibility: Visibility = Visibility.PRIVATE,
        metadata: dict[str, str] | None = None,
    ) -> StorageRef:
        """Upload a scratch file and return the ref to record."""

        stored = self.storage.put_file(
            key, path, content_type=content_type, visibility=visibility,
            metadata=metadata,
        )
        bucket = getattr(getattr(self.storage, "config", None), "bucket", "")
        if not bucket:
            # Local backend: the ref is the path it landed at, so the value
            # written to the database is what an older reader would expect.
            return StorageRef(key=stored.key, local=False, bucket="local")
        return StorageRef(key=stored.key, bucket=bucket)

    def key(self, *parts: str) -> str:
        """A tenant-scoped key. Always via `key_for`, never by formatting."""
        return key_for(self.tenant_id, *parts)


def sweep(base: str = "", older_than_s: float = 6 * 3600) -> int:
    """Remove abandoned scratch directories. Returns how many went.

    Only directories whose name starts with `PREFIX`, and only ones older than
    the cutoff — a running job's directory is young, and deleting it would
    fail the job it belongs to in a way that looks like a storage error.
    """

    root = base or tempfile.gettempdir()
    if not os.path.isdir(root):
        return 0
    cutoff = time.time() - older_than_s
    removed = 0
    for name in os.listdir(root):
        if not name.startswith(PREFIX):
            continue
        path = os.path.join(root, name)
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed
