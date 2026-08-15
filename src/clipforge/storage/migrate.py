"""Wiring durable storage into the engines that were written before it.

Each engine already has a place where media enters or leaves it — acquisition
finishes a download, transcription needs a file to extract audio from,
rendering produces one, publishing needs a URL. This module attaches to those
places rather than rewriting the engines, for two reasons: the engines are
tested and working, and a change that reaches into all four at once is one
nobody can review.

## The three moves

* **After acquisition**, `store_acquisition` uploads the downloaded file and
  rewrites `media_path` to a `r2://` ref. The local copy is then scratch.
* **Before transcription or rendering**, `materialise` brings whatever the ref
  points at back onto disk — and returns local paths unchanged, so rows
  written before the migration keep working.
* **After rendering**, `store_render` uploads the clip and produces the
  `MediaAsset` the publisher needs, including the public URL Instagram fetches.

## Backfill is offered, not required

`backfill` walks rows whose `media_path` is still a filesystem path and uploads
the files that still exist. It is safe to run repeatedly and it skips what is
gone. A migration that demanded a completed backfill before anything worked is
one that gets deferred and then run under pressure.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .types import (
    ObjectNotFound,
    PermanentStorageError,
    StorageRef,
    Visibility,
    key_for,
)
from .workspace import Workspace

__all__ = [
    "materialise", "store_acquisition", "store_render", "asset_for",
    "backfill", "BackfillReport",
]


def materialise(storage: Any, tenant_id: str, ref: str, work: Workspace) -> str:
    """A local path for whatever `ref` names.

    The one function every consumer of media calls. A local ref comes back
    unchanged; an object is fetched into the workspace.
    """

    del tenant_id
    return work.fetch(StorageRef.parse(ref))


def store_acquisition(
    storage: Any, tenant_id: str, source_id: str, path: str,
) -> StorageRef:
    """Upload a downloaded source and return the ref to record.

    Private: source media is somebody else's copyrighted long-form video and
    has no business being world-readable. Only the rendered clip becomes
    public, and only because Instagram cannot fetch anything else.
    """

    key = key_for(tenant_id, "sources", source_id, os.path.basename(path))
    stored = storage.put_file(
        key, path, visibility=Visibility.PRIVATE,
        metadata={"source_id": source_id, "tenant": tenant_id},
    )
    return _ref(storage, stored.key)


def store_render(
    storage: Any, tenant_id: str, clip_id: str, path: str,
) -> StorageRef:
    """Upload a finished clip. Public, because Instagram fetches it itself."""

    key = key_for(tenant_id, "renders", clip_id, os.path.basename(path))
    stored = storage.put_file(
        key, path, content_type="video/mp4", visibility=Visibility.PUBLIC,
        metadata={"clip_id": clip_id, "tenant": tenant_id},
    )
    return _ref(storage, stored.key)


def asset_for(
    storage: Any, ref: StorageRef | str, asset_id: str, **fields: Any
) -> Any:
    """Build the `MediaAsset` the publisher needs.

    `public_url` is filled from the store, and left empty when the backend
    cannot produce one — which is exactly the state Instagram refuses, and it
    refuses it at *schedule* time with a message naming the cause rather than
    at 6am with a fetch failure from Meta.
    """

    from ..publish.types import MediaAsset

    parsed = ref if isinstance(ref, StorageRef) else StorageRef.parse(ref)
    public = ""
    if not parsed.local:
        try:
            public = storage.public_url(parsed.key)
        except PermanentStorageError:
            # Reported by the capability list rather than raised here: a
            # YouTube or TikTok post does not need a public URL and should
            # not fail because Instagram would have.
            public = ""

    return MediaAsset(
        asset_id=asset_id,
        path=parsed.key if parsed.local else str(parsed),
        public_url=public,
        **fields,
    )


def _ref(storage: Any, key: str) -> StorageRef:
    bucket = getattr(getattr(storage, "config", None), "bucket", "")
    return StorageRef(key=key, bucket=bucket or "local")


@dataclass(slots=True)
class BackfillReport:
    considered: int = 0
    uploaded: int = 0
    already_remote: int = 0
    missing: int = 0
    failed: int = 0
    bytes_moved: int = 0
    errors: list[str] = None            # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "considered": self.considered, "uploaded": self.uploaded,
            "already_remote": self.already_remote, "missing": self.missing,
            "failed": self.failed, "bytes_moved": self.bytes_moved,
            "errors": self.errors[:20],
        }


def backfill(
    database: Any, storage: Any, tenant_id: str, *, dry_run: bool = False,
    delete_local: bool = False,
) -> BackfillReport:
    """Move media written before the migration into object storage.

    Idempotent: a row already holding an `r2://` ref is counted and skipped, so
    an interrupted run is resumed by running it again.

    `delete_local` is off by default and should stay off until a run has been
    inspected. The upload is verifiable — the object is there or it is not —
    but a delete is not reversible, and the whole point of this system is that
    nothing important is lost.
    """

    report = BackfillReport()

    with database.unit_of_work(tenant_id) as uow:
        runs = list(uow.acquisitions.all())

    for run in runs:
        if not run.media_path:
            continue
        report.considered += 1
        parsed = StorageRef.parse(run.media_path)
        if not parsed.local:
            report.already_remote += 1
            continue
        if not os.path.exists(parsed.key):
            report.missing += 1
            continue

        try:
            size = os.path.getsize(parsed.key)
            if dry_run:
                report.uploaded += 1
                report.bytes_moved += size
                continue
            ref = store_acquisition(
                storage, tenant_id, run.source_id or run.id, parsed.key
            )
            with database.unit_of_work(tenant_id) as uow:
                held = uow.acquisitions.get(run.id)
                if held is not None:
                    held.media_path = str(ref)
                    uow.acquisitions.save(held)
            report.uploaded += 1
            report.bytes_moved += size
            if delete_local:
                os.remove(parsed.key)
        except Exception as error:                          # noqa: BLE001
            report.failed += 1
            report.errors.append(f"{run.id}: {type(error).__name__}: {error}")

    return report


def verify(storage: Any, database: Any, tenant_id: str) -> dict[str, Any]:
    """Check that every recorded ref actually resolves.

    Run after a backfill and on a schedule. A `media_path` pointing at nothing
    is invisible until a job tries to use it, which is usually days later and
    reads as a transcription failure.
    """

    with database.unit_of_work(tenant_id) as uow:
        runs = [r for r in uow.acquisitions.all() if r.media_path]

    ok, missing, local = 0, [], 0
    for run in runs:
        parsed = StorageRef.parse(run.media_path)
        if parsed.local:
            local += 1
            if not os.path.exists(parsed.key):
                missing.append(run.media_path)
            continue
        try:
            storage.stat(parsed.key)
            ok += 1
        except ObjectNotFound:
            missing.append(run.media_path)
    return {
        "checked": len(runs), "resolved": ok, "still_local": local,
        "missing": missing[:50], "missing_count": len(missing),
    }
