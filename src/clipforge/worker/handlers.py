"""The five handlers, over the engines that already existed.

Each one is thin. The work lives in `acquire`, `transcribe`, `gameplay`,
`render`, `publish` and `analytics`, all of which were built and tested before
any of this; a handler's job is to turn a queue row into a call on one of them
and an `Outcome` back out.

## Every handler is idempotent, and each says how

At-least-once delivery is what a leased queue gives you — see `runtime`. So
the interesting sentence in each docstring below is the one that begins "Run
twice", and none of them says "it probably will not happen".

## What a handler must not do

Swallow a failure. A handler that catches everything and returns `Done`
produces a queue that drains beautifully and a product that does nothing, and
the job row — the only durable record — will say it succeeded. `Retry` and
`Fatal` exist so the truth ends up in `last_error`.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Any

from ..store.records import utcnow
from .types import Done, Fatal, JobContext, Outcome, Retry

log = logging.getLogger("clipforge.worker.handlers")

__all__ = [
    "acquisition_handler",
    "transcription_handler",
    "render_handler",
    "publish_handler",
    "analytics_handler",
    "default_handlers",
]


def _service(context: JobContext, name: str) -> Any:
    return getattr(context.services, name, None) if context.services else None


def media_path_for(uow: Any, source_id: str) -> str:
    """Where a source's media actually is on disk.

    Not on `sources`. The downloaded path lives on `acquisition_runs`, which
    is the row acquisition writes — a source can be known (title, rights,
    duration) long before anything has been fetched, and putting the path on
    the source would make "we know about this podcast" and "we have the file"
    the same fact.

    Falls back to the transcription run, which records the media it worked
    from: a source transcribed on one host and rendered on another may have
    no acquisition row visible here.
    """

    for repository in ("acquisitions", "transcriptions"):
        held = getattr(uow, repository, None)
        finder = getattr(held, "for_source", None)
        if finder is None:
            continue
        for run in finder(source_id):
            path = getattr(run, "media_path", "") or ""
            if path:
                return path
    return ""


# ---------------------------------------------------------------------------
# Acquisition
# ---------------------------------------------------------------------------


def acquisition_handler(context: JobContext) -> Outcome:
    """Download and measure a submitted source.

    Run twice: the acquisition engine keys its runs on the source reference
    and returns the existing row rather than downloading again, and `sources`
    carries `unique(tenant_id, fingerprint)` — so a second attempt after a
    crash resolves to the same source rather than a duplicate library entry.
    """

    factory = _service(context, "acquisition_factory")
    if factory is None:
        return Fatal(
            "no acquisition engine is configured on this worker, so this job "
            "can never run here — start a worker with one, or drop the job"
        )

    engine = factory(context.database, context.tenant_id)
    limit = int(context.payload.get("limit", 1))
    try:
        done = engine.run(limit=limit, worker_id=f"job:{context.job.id[:12]}")
    except FileNotFoundError as error:
        return Fatal(f"the media is not where the source says it is: {error}")
    except Exception as error:                              # noqa: BLE001
        return Retry(f"acquisition failed: {type(error).__name__}: {error}")

    if not done:
        return Done("nothing was waiting to be acquired", acquired=0)
    return Done(
        f"acquired {len(done)} source(s)",
        acquired=len(done),
        source_ids=[getattr(a, "source_id", "") for a in done],
    )


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------


def transcription_handler(context: JobContext) -> Outcome:
    """Turn one source's media into word-level timings.

    Run twice: `transcription_runs` has `unique(tenant_id, source_id)` and the
    engine reuses the existing run id, so a repeat overwrites one row rather
    than creating a second. It also checks for an existing transcript first —
    on a paid provider a needless re-run is a second invoice for an answer
    that cannot have changed.
    """

    factory = _service(context, "transcription_factory")
    if factory is None:
        return Fatal("no transcription engine is configured on this worker")

    payload = context.payload
    source_id = payload.get("source_id", "")
    if not source_id:
        return Fatal("the job carries no source_id")

    engine = factory(context.database, context.tenant_id)

    with context.unit_of_work() as uow:
        source = uow.sources.get(source_id)
        if source is None:
            return Fatal(f"source {source_id} no longer exists")
        if source.has_transcript and not payload.get("force"):
            return Done("already transcribed", source_id=source_id,
                        skipped=True)
        media_path = payload.get("media_path") or media_path_for(
            uow, source_id
        )

    if not media_path:
        return Fatal(
            f"source {source_id} has no media_path — it was never acquired, "
            f"so there is nothing to transcribe"
        )

    from ..transcribe.types import PermanentError, ProviderUnavailable

    try:
        transcript = engine.transcribe_source(
            source_id, media_path, language=payload.get("language", ""),
        )
    except ProviderUnavailable as error:
        # A missing model or an unset key is a deployment problem. Retrying
        # eight times will not install anything, but it also is not the job's
        # fault, so it stays retryable and the operator has the window.
        return Retry(f"transcription provider unavailable: {error}", after_s=300)
    except PermanentError as error:
        return Fatal(f"this media cannot be transcribed: {error}")
    except Exception as error:                              # noqa: BLE001
        return Retry(f"transcription failed: {type(error).__name__}: {error}")

    words = len(getattr(transcript, "words", ()) or ())
    return Done(f"{words} words", source_id=source_id, words=words)


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_handler(context: JobContext) -> Outcome:
    """Encode one clip to a real 1080x1920 file and store it.

    This is the step the pipeline never had. `factory.pipeline` composed a
    plan, scheduled a post and stopped — so every scheduled post pointed at a
    path nothing had written.

    Run twice: the output path is derived from the clip id, ffmpeg writes to a
    temporary file and renames, and the storage key is deterministic — so a
    repeat overwrites the same object with the same bytes. The `clips` row is
    updated rather than appended to.
    """

    engine_factory = _service(context, "render_factory")
    if engine_factory is None:
        return Fatal("no render engine is configured on this worker")

    payload = context.payload
    clip_id = payload.get("clip_id", "")
    if not clip_id:
        return Fatal("the job carries no clip_id")

    with context.unit_of_work() as uow:
        clip = uow.clips.get(clip_id)
        if clip is None:
            return Fatal(f"clip {clip_id} no longer exists")
        source = uow.sources.get(clip.source_id)
        if source is None:
            return Fatal(f"clip {clip_id} points at a source that is gone")
        speaker_path = payload.get("speaker_path") or media_path_for(
            uow, clip.source_id
        )
        start_s = clip.start_ms / 1000.0
        duration_s = max(0.1, (clip.end_ms - clip.start_ms) / 1000.0)

    if not speaker_path or not os.path.isfile(speaker_path):
        return Fatal(
            f"the speaker media for {clip_id} is not on this host "
            f"({speaker_path or 'no path'}). A render worker needs the file, "
            f"so either run it where acquisition ran or give it storage."
        )

    # Framing. A failure here is not a failure of the render: the camera
    # solver handles an empty track by producing a static centred crop, and a
    # clip framed conservatively beats a clip not shipped.
    track = None
    tracker = _service(context, "face_tracker")
    if tracker is not None:
        try:
            result = tracker.track_video(
                speaker_path, start_s=start_s, duration_s=duration_s,
            )
            track = result.track
        except Exception as error:                          # noqa: BLE001
            context.logger and context.logger.warning(
                "face tracking failed for %s: %s", clip_id, error
            )

    if track is None:
        track = _static_track(context, clip.source_id, speaker_path)

    from ..gameplay import compose

    plan = compose(
        duration_s,
        track=track,
        assets=tuple(payload.get("assets", ()) or ()),
        word_count=payload.get("word_count", 0),
    )

    engine = engine_factory(context.database, context.tenant_id)
    output_path = payload.get("output_path") or os.path.join(
        payload.get("output_dir", "/tmp/clipforge-renders"), f"{clip_id}.mp4"
    )
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    from ..render import RenderRequest
    from ..render.types import OutputRejected

    context.heartbeat and context.heartbeat()
    try:
        result = engine.render(RenderRequest(
            render_id=f"rnd_{clip_id}",
            plan=plan,
            speaker_path=speaker_path,
            gameplay_path=payload.get("gameplay_path", ""),
            output_path=output_path,
            subtitles_path=payload.get("subtitles_path", ""),
            start_s=start_s,
            clip_id=clip_id,
            source_id=clip.source_id,
        ))
    except OutputRejected as error:
        # The plan does not fit the media. The same plan against the same file
        # fails identically for ever, so this is not worth eight attempts.
        return Fatal(f"the render was rejected: {error}")
    except FileNotFoundError as error:
        return Fatal(f"ffmpeg or an input is missing: {error}")
    except Exception as error:                              # noqa: BLE001
        return Retry(f"render failed: {type(error).__name__}: {error}")

    return Done(
        f"rendered {clip_id}",
        clip_id=clip_id,
        output_path=result.output_path,
        storage_ref=getattr(result, "storage_ref", ""),
        public_url=getattr(result, "public_url", ""),
        size_bytes=getattr(result, "size_bytes", 0),
    )


def _static_track(context: JobContext, source_id: str, media_path: str):
    """An empty track that still knows how big the media is.

    A bare `SpeakerTrack()` defaults to 1920x1080. Composing against that and
    rendering 1280x720 media asks ffmpeg for a crop taller than the frame, and
    `render.engine._preflight` rejects the plan — which is what it is for, and
    which is how this bug was caught rather than shipped.

    So the dimensions are found even when there is nobody to track: from the
    acquisition run first, which recorded them when it measured the download,
    and by probing the file if that row is gone. Only if both fail does this
    fall back to the default, and then the preflight is the backstop.
    """

    from ..gameplay.types import SpeakerTrack

    width = height = 0
    try:
        with context.unit_of_work() as uow:
            for run in uow.acquisitions.for_source(source_id):
                if getattr(run, "width", None) and getattr(run, "height", None):
                    width, height = int(run.width), int(run.height)
                    break
    except Exception:                                       # noqa: BLE001
        pass

    if not width or not height:
        try:
            from ..vision import probe_video

            info = probe_video(media_path)
            width, height = info.width, info.height
        except Exception:                                   # noqa: BLE001
            pass

    if width and height:
        return SpeakerTrack(source_width=width, source_height=height)
    return SpeakerTrack()


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


def publish_handler(context: JobContext) -> Outcome:
    """Hand due posts to the platforms.

    Run twice: every upload carries `unique(tenant_id, idempotency_key)`, the
    publishing engine claims a post before working it, and the platform APIs
    are themselves given an idempotency key — so a repeat resolves to the
    existing remote post rather than a second video on the channel.
    """

    system = _service(context, "publisher")
    transport = _service(context, "transport")
    if system is None:
        return Fatal("no publishing system is configured on this worker")
    if transport is None:
        return Fatal(
            "no upload transport is configured, so nothing can reach a "
            "platform. Configure one rather than draining this queue into "
            "nowhere."
        )

    limit = int(context.payload.get("limit", 5))
    try:
        results = system.tick(transport, now=utcnow(), limit=limit)
    except Exception as error:                              # noqa: BLE001
        return Retry(f"publish pass failed: {type(error).__name__}: {error}")

    published = [r for r in results if getattr(r, "ok", False)]
    failed = [r for r in results if not getattr(r, "ok", False)]
    if failed and not published:
        # The engine's own retry logic has already decided each post's fate;
        # this is about the *pass*. Returning Retry here would double-count
        # attempts against a queue job that is only a trigger.
        return Done(
            f"{len(failed)} post(s) failed; see their own rows",
            published=0, failed=len(failed),
        )
    return Done(
        f"published {len(published)} of {len(results)}",
        published=len(published), failed=len(failed),
    )


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


def analytics_handler(context: JobContext) -> Outcome:
    """Collect platform counters for posts that have been published.

    Run twice: a snapshot is keyed by upload and measurement time, so a repeat
    inside the same window overwrites rather than double-counting. Views are
    a gauge, not a counter, which is what makes that safe.
    """

    source = _service(context, "metric_source")
    if source is None:
        return Fatal(
            "no live metric source is configured. `RecordedSource` is the only "
            "implementation in this build, so there is nothing to collect from "
            "— the Analytics page will stay empty until one exists."
        )

    engine_factory = _service(context, "analytics_factory")
    if engine_factory is None:
        return Fatal("no analytics engine is configured on this worker")

    engine = engine_factory(context.database, context.tenant_id)
    try:
        # `ingest` returns {collected, failed, skipped} and never raises for
        # one post's sake — a platform returning nonsense for a single clip
        # must not cost every other clip a week of data.
        tally = engine.ingest(source, utcnow())
    except Exception as error:                              # noqa: BLE001
        return Retry(f"metric collection failed: {type(error).__name__}: {error}")

    collected = int(tally.get("collected", 0))
    failed = int(tally.get("failed", 0))
    return Done(
        f"collected {collected}, {failed} failed",
        collected=collected, failed=failed,
        skipped=int(tally.get("skipped", 0)),
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def default_handlers() -> dict[str, Any]:
    """Job kind to handler, matching the `JobKind` enum in the schema.

    `detect_clips`, `generate_hooks` and `build_captions` are deliberately
    absent. They are pure functions of a transcript with no I/O, and the
    factory pipeline already runs all three in one pass — giving each its own
    queue row would add three round trips and three lease renewals to buy
    nothing. They are listed in `JobKind` because the schema was written for a
    fully decomposed pipeline; this runtime does not need one.
    """

    return {
        "discover_sources": acquisition_handler,
        "transcribe": transcription_handler,
        "render_video": render_handler,
        "publish_upload": publish_handler,
        "collect_metrics": analytics_handler,
    }
