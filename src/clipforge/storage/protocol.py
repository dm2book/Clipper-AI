"""The interface every backend satisfies, and the metrics every one reports.

Two implementations: `LocalStorage` (a directory) and `R2Storage` (boto3
against Cloudflare R2 or anything else speaking S3). One suite runs against
both, for the same reason `test_store_contract.py` does — the fast local tests
are only evidence about production because the same assertions pass on the
object store.

## Streams, not bytes

`put_file`/`get_file` take paths and `open` returns a file object. There is no
`put(key, data: bytes)` on purpose: media here is measured in hundreds of
megabytes, and an interface that accepts `bytes` is an interface every caller
will hand a whole video to. Peak memory then scales with worker count times
file size, which is how a four-worker box dies on a long podcast.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, BinaryIO, Iterator, Protocol

from .types import StoredObject, Visibility

__all__ = ["Storage", "StorageMetrics", "OperationStats"]


@dataclass(slots=True)
class OperationStats:
    calls: int = 0
    failures: int = 0
    retries: int = 0
    bytes_moved: int = 0
    seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls, "failures": self.failures,
            "retries": self.retries, "bytes_moved": self.bytes_moved,
            "seconds": round(self.seconds, 3),
            "mean_ms": round(self.seconds * 1000 / self.calls, 1)
            if self.calls else 0.0,
        }


@dataclass
class StorageMetrics:
    """Counters, per operation.

    In-process and reset on restart, which is right for what they are: an
    exporter scrapes them, and a counter this layer tried to persist would be
    a write to storage on every write to storage.

    Retries are counted separately from failures because they answer different
    questions. Failures rising means something is broken; retries rising while
    failures stay flat means something is degraded and the retry budget is
    absorbing it — the state worth catching before it becomes the first.
    """

    operations: dict[str, OperationStats] = field(default_factory=dict)

    def record(
        self, operation: str, *, seconds: float = 0.0, size: int = 0,
        failed: bool = False, retries: int = 0,
    ) -> None:
        stats = self.operations.setdefault(operation, OperationStats())
        stats.calls += 1
        stats.seconds += seconds
        stats.bytes_moved += size
        stats.retries += retries
        if failed:
            stats.failures += 1

    def snapshot(self) -> dict[str, Any]:
        total = OperationStats()
        for stats in self.operations.values():
            total.calls += stats.calls
            total.failures += stats.failures
            total.retries += stats.retries
            total.bytes_moved += stats.bytes_moved
            total.seconds += stats.seconds
        return {
            "operations": {k: v.to_dict() for k, v in
                           sorted(self.operations.items())},
            "total": total.to_dict(),
        }

    def reset(self) -> None:
        self.operations.clear()


class Storage(Protocol):
    """Durable object storage."""

    metrics: StorageMetrics

    @property
    def backend(self) -> str: ...

    def put_file(
        self, key: str, path: str, *, content_type: str = "",
        visibility: Visibility = Visibility.PRIVATE,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject: ...

    def get_file(self, key: str, path: str) -> StoredObject: ...

    def open(self, key: str) -> BinaryIO: ...

    def stat(self, key: str) -> StoredObject: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> bool: ...

    def delete_prefix(self, prefix: str) -> int: ...

    def list(self, prefix: str, limit: int = 1000) -> Iterator[StoredObject]: ...

    def signed_url(
        self, key: str, *, expires_in: timedelta | None = None,
        download_as: str = "",
    ) -> str: ...

    def signed_upload_url(
        self, key: str, *, expires_in: timedelta | None = None,
        content_type: str = "",
    ) -> str: ...

    def public_url(self, key: str) -> str: ...

    def usage(self, prefix: str = "") -> dict[str, Any]: ...
