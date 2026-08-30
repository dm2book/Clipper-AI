"""What follows what, and how the queue is told.

The stages were all here. Acquisition wrote sources, transcription wrote
transcripts, the factory picked moments, the renderer produced files and the
publisher could post them. What was missing was the wiring: each stage finished
and stopped, and the only thing that could start the next one was a person
opening a queue console. This module is that wiring.

## One place that knows the order

Every dedupe key in the pipeline is built here, and that is the point of the
module rather than a detail of it. The keys are what make the chain idempotent:
`render:cl_123` queued twice is one render, because the second `enqueue` finds
the first row and returns it. If those keys were built at each call site they
would drift — one caller including the attempt number, another the timestamp —
and the chain would quietly fan out instead of converging.

A dedupe key is permanent within a tenant. `render:cl_123` resolves to the same
row a month later, whatever state it reached, so a clip is rendered at most
once for as long as its job row exists. That is the intended reading of "one
render per clip", and the escape hatch for a job that died is
`monitor.requeue_dead`, which resets the existing row rather than inserting a
rival.

## Delays are part of the contract

`verify_upload` is queued with a delay because the platforms are asynchronous
in a way that has nothing to do with our queue: YouTube accepts every byte and
then transcodes, and asking it thirty seconds later gets an honest "processing"
that proves nothing. The first verification pass waits minutes and the second
waits hours, so a video rejected for claimed audio — which is the common case,
and can take an hour to surface — is still caught.
"""

from __future__ import annotations

import os.path
import uuid
from datetime import datetime, timedelta
from typing import Any, Sequence

from ..store.records import JobRecord
from .types import JobSpec

__all__ = [
    "ACQUIRE",
    "TRANSCRIBE",
    "SELECT",
    "RENDER",
    "PUBLISH",
    "VERIFY",
    "METRICS",
    "PIPELINE_ORDER",
    "DEFAULT_RENDER_DIR",
    "enqueue",
    "render_output_path",
    "transcribe_spec",
    "select_spec",
    "render_spec",
    "publish_spec",
    "verify_spec",
    "metrics_spec",
]


ACQUIRE = "discover_sources"
TRANSCRIBE = "transcribe"
SELECT = "detect_clips"
RENDER = "render_video"
PUBLISH = "publish_upload"
VERIFY = "verify_upload"
METRICS = "collect_metrics"

#: The chain, in order, for anything that wants to display or reason about it.
PIPELINE_ORDER: tuple[str, ...] = (
    ACQUIRE, TRANSCRIBE, SELECT, RENDER, PUBLISH, VERIFY, METRICS,
)

#: How long after publishing to ask the platform whether the post is really
#: there. Two passes: one past the usual transcode window, one past the
#: rights-claim window that catches YouTube's late rejections.
FIRST_VERIFY_S = 15 * 60.0
SECOND_VERIFY_S = 6 * 3600.0

#: How long after a post goes live before the first metrics reading. Earlier
#: than this and every platform reports zeros for a video nobody has been
#: shown yet, which is a reading that looks like a failure and is not.
FIRST_METRICS_S = 3600.0


#: Where a render lands when nothing says otherwise. Shared with clip
#: selection, which needs to name the file *before* it exists — see
#: `render_output_path`.
DEFAULT_RENDER_DIR = "/tmp/clipforge-renders"


def render_output_path(clip_id: str, output_dir: str = "") -> str:
    """The file this clip's render will produce.

    Derived from the clip id, which is what makes a render idempotent: the
    same clip rendered twice overwrites one file rather than accumulating
    two. Selection uses it as the booked post's asset path before anything has
    been encoded — the upload sits in `draft` until the render replaces it
    with the path the encoder actually returned, so the prediction is never
    what a publisher reads.
    """

    return os.path.join(output_dir or DEFAULT_RENDER_DIR, f"{clip_id}.mp4")


def enqueue(
    uow: Any,
    tenant_id: str,
    specs: Sequence[JobSpec],
    now: datetime,
) -> list[str]:
    """Queue each spec, returning the job ids — existing ones included.

    Called from inside the caller's transaction on purpose. The runtime uses
    the transaction that marks a job succeeded, so the successor is queued if
    and only if the predecessor is recorded done.

    Deduplication is the store's: `jobs.enqueue` returns the row that already
    holds the key rather than inserting a second. So this is safe to call
    again with the same specs, which is exactly what a reaped lease causes.
    """

    queued: list[str] = []
    for spec in specs:
        record = JobRecord(
            id=f"job_{uuid.uuid4().hex[:16]}",
            tenant_id=tenant_id,
            channel_id=spec.channel_id or None,
            kind=spec.kind,
            payload=dict(spec.payload),
            priority=spec.priority,
            max_attempts=spec.max_attempts,
            run_after=now + timedelta(seconds=spec.delay_s),
            dedupe_key=spec.dedupe_key or None,
        )
        queued.append(uow.jobs.enqueue(record).id)
    return queued


# ---------------------------------------------------------------------------
# The specs, one per edge of the chain
# ---------------------------------------------------------------------------


def transcribe_spec(source_id: str, *, channel_id: str = "") -> JobSpec:
    """Acquisition finished → transcribe what it downloaded."""

    return JobSpec(
        kind=TRANSCRIBE,
        payload={"source_id": source_id, "channel_id": channel_id},
        dedupe_key=f"transcribe:{source_id}",
        channel_id=channel_id,
        # Ahead of renders and publishes: transcription gates everything after
        # it, and a queue that renders yesterday's clips before transcribing
        # today's source has the longest possible time-to-first-clip.
        priority=50,
    )


def select_spec(source_id: str, channel_id: str) -> JobSpec:
    """Transcript stored → pick the moment, write the clip, book the post."""

    return JobSpec(
        kind=SELECT,
        payload={"source_id": source_id, "channel_id": channel_id},
        dedupe_key=f"select:{channel_id}:{source_id}",
        channel_id=channel_id,
        priority=60,
    )


def render_spec(
    clip_id: str,
    *,
    channel_id: str = "",
    source_id: str = "",
    output_dir: str = "",
) -> JobSpec:
    """A clip exists and its post is booked → produce the file.

    The key is the clip alone. A clip booked to three platforms is one render
    shared by three uploads, not three identical files.
    """

    payload: dict[str, Any] = {"clip_id": clip_id}
    if channel_id:
        payload["channel_id"] = channel_id
    if source_id:
        payload["source_id"] = source_id
    if output_dir:
        payload["output_dir"] = output_dir
    return JobSpec(
        kind=RENDER,
        payload=payload,
        dedupe_key=f"render:{clip_id}",
        channel_id=channel_id,
    )


def publish_spec(
    upload_id: str, *, channel_id: str = "", delay_s: float = 0.0,
) -> JobSpec:
    """The file exists → send it to the platform, at the slot it was booked.

    `delay_s` is normally the distance to the post's `run_at`, not zero. The
    calendar decided when this should go out — cadence, spacing, the account's
    quiet hours — and publishing the moment ffmpeg finishes would throw all of
    that away and post a channel's whole backlog in one burst.
    """

    return JobSpec(
        kind=PUBLISH,
        payload={"upload_id": upload_id, "channel_id": channel_id},
        dedupe_key=f"publish:{upload_id}",
        channel_id=channel_id,
        delay_s=max(0.0, delay_s),
    )


def verify_spec(
    upload_id: str,
    *,
    channel_id: str = "",
    pass_number: int = 1,
    delay_s: float | None = None,
) -> JobSpec:
    """The platform said it worked → go back and check that it did.

    `pass_number` is in the key rather than a retry count because the two
    passes are different questions asked at different times, and collapsing
    them would mean the second never runs: the key of the first is already
    taken.
    """

    if delay_s is None:
        delay_s = FIRST_VERIFY_S if pass_number <= 1 else SECOND_VERIFY_S
    return JobSpec(
        kind=VERIFY,
        payload={
            "upload_id": upload_id,
            "channel_id": channel_id,
            "pass_number": pass_number,
        },
        dedupe_key=f"verify:{upload_id}:{pass_number}",
        channel_id=channel_id,
        delay_s=delay_s,
        # Lower than a render's: verification is a cheap read and holding it
        # behind an hour of ffmpeg is how a rejected video stays undetected.
        priority=40,
    )


def metrics_spec(
    upload_id: str,
    *,
    channel_id: str = "",
    delay_s: float = FIRST_METRICS_S,
) -> JobSpec:
    """The post is confirmed live → start measuring it."""

    return JobSpec(
        kind=METRICS,
        payload={"upload_id": upload_id, "channel_id": channel_id},
        dedupe_key=f"metrics:{upload_id}",
        channel_id=channel_id,
        delay_s=delay_s,
        priority=120,
    )
