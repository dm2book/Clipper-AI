"""The handlers, over the engines that already existed.

Each one is thin. The work lives in `acquire`, `transcribe`, `gameplay`,
`render`, `publish` and `analytics`, all of which were built and tested before
any of this; a handler's job is to turn a queue row into a call on one of them
and an `Outcome` back out.

## Each one also names its successor

`Done(..., follow_on=[...])` is how acquisition reaches transcription and how a
finished render reaches the publisher — see `chain`. Before it, every stage in
this file ran and stopped, and the pipeline only advanced when somebody queued
the next job by hand.

The status transitions travel with the chaining rather than beside it. A render
that finishes does two things in one breath: it points the booked `uploads`
rows at the file it just wrote and moves them out of `draft`, and it queues the
publish. Splitting those would allow a post to become publishable with nothing
scheduled to publish it, which is the stall this was written to remove.

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
from typing import Any

from ..store.records import utcnow
from . import chain
from .selection import selection_handler
from .types import Done, Fatal, JobContext, Outcome, Retry

log = logging.getLogger("clipforge.worker.handlers")

__all__ = [
    "acquisition_handler",
    "transcription_handler",
    "selection_handler",
    "render_handler",
    "publish_handler",
    "verification_handler",
    "analytics_handler",
    "default_handlers",
]


def _service(context: JobContext, name: str) -> Any:
    return getattr(context.services, name, None) if context.services else None


def _setting(context: JobContext, name: str, default: float) -> float:
    """A tunable, with the module's default when the worker names none."""

    held = _service(context, name)
    return default if held is None else float(held)


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
        # `acquisitions.for_source` returns a tuple and
        # `transcriptions.for_source` returns one record or None — the second
        # is unique on (tenant, source). Normalising here rather than
        # iterating whatever comes back: a bare record is not iterable, and
        # the fallback path would have raised TypeError the first time it was
        # actually needed.
        found = finder(source_id)
        runs = found if isinstance(found, (tuple, list)) else (
            () if found is None else (found,)
        )
        for run in runs:
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

    channel_id = str(context.payload.get("channel_id") or "")
    source_ids = [getattr(a, "source_id", "") for a in done]
    return Done(
        f"acquired {len(done)} source(s)",
        follow_on=[
            chain.transcribe_spec(sid, channel_id=channel_id)
            for sid in source_ids if sid
        ],
        acquired=len(done),
        source_ids=source_ids,
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
            # Still chains. A repeat that returned a bare Done would leave the
            # source transcribed and nothing downstream queued — the stall
            # this whole module exists to prevent, reintroduced on the one
            # path that looks too trivial to matter.
            return Done(
                "already transcribed",
                follow_on=_selection_follow_on(context, source_id, payload),
                source_id=source_id, skipped=True,
            )
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
    return Done(
        f"{words} words",
        follow_on=_selection_follow_on(context, source_id, payload),
        source_id=source_id, words=words,
    )


def _selection_follow_on(
    context: JobContext, source_id: str, payload: dict,
) -> list[Any]:
    """The clip-selection job for this source, if we can name its channel.

    A source with no channel has nothing to select *for* — the niche, the hook
    style and the accounts all come from one. Rather than queue a job that can
    only be fatal, this returns nothing and the transcript sits ready for
    whenever the source is attached.
    """

    channel_id = str(payload.get("channel_id") or "")
    if not channel_id:
        with context.unit_of_work() as uow:
            for run in uow.acquisitions.for_source(source_id):
                if run.channel_id:
                    channel_id = str(run.channel_id)
                    break
    if not channel_id:
        return []
    return [chain.select_spec(source_id, channel_id)]


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_handler(context: JobContext) -> Outcome:
    """Encode one clip to a real 1080x1920 file, and let its posts go.

    This is the step the pipeline never had. `factory.pipeline` composed a
    plan, scheduled a post and stopped — so every scheduled post pointed at a
    path nothing had written.

    Finishing therefore means two things, not one: the file exists, *and* the
    uploads booked against this clip now point at it and have left `draft`.
    They happen together because a post that became publishable with nothing
    queued to publish it is the stall this replaced.

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

    # The same library selection composed against, filtered to the bed this
    # channel's niche asks for. Composing here from a different set than the
    # decision stage used would render a layout nobody chose.
    assets = _beds(context, clip.channel_id)
    plan = compose(
        duration_s,
        track=track,
        assets=assets,
        word_count=payload.get("word_count", 0),
    )
    gameplay_path = payload.get("gameplay_path", "") or _bed_path(assets, plan)

    engine = engine_factory(context.database, context.tenant_id)
    output_path = payload.get("output_path") or chain.render_output_path(
        clip_id, payload.get("output_dir", "")
    )
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    from ..render import RenderRequest
    from ..render.types import OutputRejected

    request = RenderRequest(
        render_id=f"rnd_{clip_id}",
        plan=plan,
        speaker_path=speaker_path,
        gameplay_path=gameplay_path,
        output_path=output_path,
        subtitles_path=payload.get("subtitles_path", ""),
        start_s=start_s,
        clip_id=clip_id,
        source_id=clip.source_id,
    )

    context.heartbeat and context.heartbeat()
    try:
        result = engine.render(request)
    except OutputRejected as error:
        # The plan does not fit the media. The same plan against the same file
        # fails identically for ever, so this is not worth eight attempts.
        return Fatal(f"the render was rejected: {error}")
    except FileNotFoundError as error:
        return Fatal(f"ffmpeg or an input is missing: {error}")
    except Exception as error:                              # noqa: BLE001
        return Retry(f"render failed: {type(error).__name__}: {error}")

    # The durable record of what was produced — dimensions, checksum, storage
    # key, the plan it was rendered from. `render()` does not write it; only
    # the engine's own queue loop did, which this handler replaced.
    try:
        result.video_id = engine.persist(request, result).id
    except Exception as error:                              # noqa: BLE001
        context.logger and context.logger.warning(
            "could not record a videos row for %s: %s", clip_id, error
        )

    promoted, follow_on = _promote_uploads(context, clip, result)
    return Done(
        f"rendered {clip_id}"
        + (f", {len(promoted)} post(s) now publishable" if promoted else ""),
        follow_on=follow_on,
        clip_id=clip_id,
        output_path=result.output_path,
        storage_ref=getattr(result, "storage_ref", ""),
        public_url=getattr(result, "public_url", ""),
        size_bytes=getattr(result, "size_bytes", 0),
        upload_ids=promoted,
    )


def _promote_uploads(
    context: JobContext, clip: Any, result: Any,
) -> tuple[list[str], list[Any]]:
    """Point this clip's booked posts at the file, and make them publishable.

    Two changes to each row, and both matter:

    * the asset gains the path, the public URL and the real byte count, which
      is what the platform adapters read. Until now the spec pointed at
      nothing, because at booking time there was nothing to point at;
    * the state moves `draft` → `scheduled`, which is what lets the calendar's
      `due()` see the post at all. That single transition is the difference
      between a clip that publishes itself and one that waits for a person.

    `run_at` is left exactly where the calendar put it. The publish job is
    queued to fire *then* rather than now — the schedule is a decision the
    calendar already made about cadence and spacing, and finishing a render
    early is not a reason to overrule it.

    Run twice: a row already promoted is rewritten with the same values, and
    the publish follow-on deduplicates on the upload id.
    """

    from dataclasses import replace

    from ..publish.types import PostState
    from ..store.mappers import to_scheduled_post, to_upload_record

    now = utcnow()
    promoted: list[str] = []
    follow_on: list[Any] = []

    try:
        with context.unit_of_work() as uow:
            waiting = [
                row for row in uow.uploads.in_state(
                    PostState.DRAFT.value, PostState.SCHEDULED.value
                )
                if row.clip_id == clip.id
            ]
            for row in waiting:
                post = to_scheduled_post(row)
                asset = post.spec.asset
                metadata = dict(post.spec.metadata)
                metadata["clip_id"] = clip.id
                if getattr(result, "video_id", ""):
                    metadata["video_id"] = result.video_id
                # `replace`, because `MediaAsset` and `PostSpec` are frozen —
                # a spec is what was promised to a platform, and the engine
                # relies on it not changing under a retry. Producing a new one
                # is the supported way to say the promise has been updated.
                post.spec = replace(
                    post.spec,
                    asset=replace(
                        asset,
                        path=result.output_path,
                        public_url=(
                            getattr(result, "public_url", "")
                            or asset.public_url
                        ),
                        size_bytes=(
                            int(result.size_bytes)
                            if getattr(result, "size_bytes", 0)
                            else asset.size_bytes
                        ),
                    ),
                    metadata=metadata,
                )
                post.state = PostState.SCHEDULED

                updated = to_upload_record(
                    post, tenant_id=context.tenant_id,
                    channel_id=row.channel_id,
                    clip_id=clip.id,
                    video_id=getattr(result, "video_id", "") or row.video_id,
                )
                updated.created_at = row.created_at
                uow.uploads.save(updated)

                promoted.append(row.id)
                follow_on.append(chain.publish_spec(
                    row.id, channel_id=row.channel_id,
                    delay_s=(post.run_at - now).total_seconds(),
                ))
    except Exception as error:                              # noqa: BLE001
        # The file is rendered and that is the expensive part. Losing the
        # promotion means the job retries and renders again — wasteful, but
        # the render is idempotent and a silently unpublishable clip is worse.
        raise RuntimeError(
            f"the render of {clip.id} succeeded but its posts could not be "
            f"promoted: {type(error).__name__}: {error}"
        ) from error

    return promoted, follow_on


def _beds(context: JobContext, channel_id: str) -> tuple:
    """The gameplay beds this channel's niche can use, from the library.

    Empty when the niche wants none — cars, luxury and gaming are their own
    visual — and empty when the worker was given no library, in which case
    `compose` falls back to a speaker-only layout rather than failing.
    """

    library = tuple(_service(context, "gameplay_library") or ())
    if not library or not channel_id:
        return ()
    try:
        from ..factory.niches import Niche, profile

        with context.unit_of_work() as uow:
            record = uow.channels.get(channel_id)
        if record is None:
            return ()
        bed = profile(Niche(record.niche)).gameplay_bed
    except Exception:                                       # noqa: BLE001
        return ()
    if bed is None:
        return ()
    return tuple(asset for asset in library if asset.game is bed)


def _bed_path(assets: tuple, plan: Any) -> str:
    """The file behind the bed the plan actually chose.

    Derived from the plan rather than from the first asset in the list: the
    engine picks one, and handing ffmpeg a different recording than the layout
    was solved against produces a crop rectangle that does not match the
    footage.
    """

    game = getattr(plan, "game", None)
    if game is None:
        return ""
    for asset in assets:
        if asset.game is game and asset.path:
            return asset.path
    return ""


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

    payload = context.payload
    channel_id = str(payload.get("channel_id") or "")
    system = _publisher(context, channel_id)
    transport = _service(context, "transport")
    if system is None:
        return Fatal(
            "no publishing system is configured on this worker. A durable one "
            "needs `publisher_factory` and a channel_id on the job; without "
            "either, the booked uploads in the database are invisible here."
        )
    if transport is None:
        return Fatal(
            "no upload transport is configured, so nothing can reach a "
            "platform. Configure one rather than draining this queue into "
            "nowhere."
        )

    limit = int(payload.get("limit", 5))
    now = utcnow()
    try:
        results = system.tick(transport, now=now, limit=limit)
    except Exception as error:                              # noqa: BLE001
        return Retry(f"publish pass failed: {type(error).__name__}: {error}")

    delivered = [r for r in results if getattr(r, "delivered", False)]
    failed = [r for r in results if not getattr(r, "delivered", False)]

    # Verification is chained off delivery, not off success of the pass. A
    # post that reached the platform is exactly the one worth reading back —
    # "the API said yes" and "the video is on the account" are different
    # claims, and the gap between them is where the expensive failures live.
    follow_on = [
        chain.verify_spec(
            r.post_id, channel_id=channel_id, pass_number=1,
            delay_s=_setting(context, "verify_first_s", chain.FIRST_VERIFY_S),
        )
        for r in delivered
    ]

    if not results:
        # Nothing was due. Not an error and not worth a retry: the calendar
        # decides when a post goes out, and this job may simply have been
        # queued for a slot the engine has already served.
        return Done("nothing was due", published=0, failed=0)

    if failed and not delivered:
        # The engine's own retry logic has already decided each post's fate;
        # this is about the *pass*. Returning Retry here would double-count
        # attempts against a queue job that is only a trigger.
        return Done(
            f"{len(failed)} post(s) failed; see their own rows",
            published=0, failed=len(failed),
        )
    return Done(
        f"delivered {len(delivered)} of {len(results)}",
        follow_on=follow_on,
        published=len(delivered), failed=len(failed),
        post_ids=[r.post_id for r in delivered],
    )


def _publisher(context: JobContext, channel_id: str) -> Any:
    """The publishing system this job should work through.

    Prefers the durable one, built per channel, because that is the only kind
    that can see the `uploads` rows selection booked. The injected `publisher`
    is the fallback for a deployment — or a test — that wires its own; a bare
    in-memory `PublishingSystem` has an empty calendar and will cheerfully
    report "published 0 of 0" for ever.
    """

    factory = _service(context, "publisher_factory")
    if factory is not None and channel_id:
        return factory(context.database, context.tenant_id, channel_id)
    return _service(context, "publisher")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _verification_id(row: Any) -> str:
    """What to ask the platform about, which is not always what it returned.

    TikTok's status endpoint is keyed on the `publish_id` from init, while the
    id it hands back on completion is the public post id — a different value.
    Asking about the second gets a truthful "no such publish", which reads as a
    post that does not exist and would have this handler mark a live video
    `needs_attention`. The publish id survives on the attempt's `remote_ref`.

    YouTube and Instagram return the id their read endpoints take, so for them
    the two are the same and `remote_post_id` is used unchanged.
    """

    from ..publish.types import Platform

    if row.platform != Platform.TIKTOK.value:
        return row.remote_post_id

    from ..store.mappers import to_scheduled_post

    try:
        attempts = to_scheduled_post(row).attempts
    except Exception:                                       # noqa: BLE001
        return row.remote_post_id
    for attempt in reversed(attempts):
        if attempt.remote_ref:
            return attempt.remote_ref
    return row.remote_post_id


def verification_handler(context: JobContext) -> Outcome:
    """Ask the platform whether the post it accepted is really there.

    The last link in the chain, and the only one that can take a post *back*.
    Every platform can accept a file and reject the video afterwards — YouTube
    on a rights claim, TikTok on moderation, Instagram on an expired container
    — and none of them tells the uploader. Without this the system's record
    says published and the creator's account says nothing.

    Run twice: it is a read. Nothing is created, the verdict is recomputed
    from the platform's current answer, and the state it writes is a function
    of that answer alone.
    """

    transport = _service(context, "transport")
    if transport is None:
        return Fatal("no transport is configured, so nothing can be verified")

    payload = context.payload
    upload_id = str(payload.get("upload_id") or "")
    if not upload_id:
        return Fatal("the job carries no upload_id")
    channel_id = str(payload.get("channel_id") or "")
    pass_number = int(payload.get("pass_number", 1))

    from ..publish.types import Platform, PostState
    from ..publish.verify import UploadVerifier

    with context.unit_of_work() as uow:
        row = uow.uploads.get(upload_id)
        if row is None:
            return Fatal(f"upload {upload_id} no longer exists")
        channel_id = channel_id or row.channel_id
        state, platform = row.state, row.platform
        account_id = row.account_id
        remote_id = _verification_id(row)

    if state not in (PostState.PUBLISHED.value, PostState.AWAITING_CREATOR.value):
        # Never delivered, or already taken back. Either way there is nothing
        # to confirm, and re-reading it would only add noise.
        return Done(f"{upload_id} is {state}; nothing to verify",
                    verified=False, state=state)
    if not remote_id:
        return Fatal(
            f"upload {upload_id} is {state} with no remote post id — the "
            f"platform never returned one, so nothing was published"
        )

    system = _publisher(context, channel_id)
    tokens = system.tokens.get(account_id) if system is not None else None
    if tokens is None:
        return Retry(
            f"no stored credentials for {account_id}, so {upload_id} cannot "
            f"be verified — reconnect the account",
            after_s=3600.0,
        )

    verdict = UploadVerifier(transport).verify(
        Platform(platform), remote_id, tokens
    )

    if verdict.unknown:
        # An outage is not a missing post. Treating the two the same is how a
        # system decides a live video does not exist and posts it again.
        return Retry(f"could not verify {upload_id}: {verdict.detail}")

    if verdict.pending:
        return Retry(
            f"{upload_id} is still {verdict.state} at the platform",
            after_s=_setting(context, "verify_first_s", chain.FIRST_VERIFY_S),
        )

    if verdict.live:
        follow_on = [chain.metrics_spec(
            upload_id, channel_id=channel_id,
            delay_s=_setting(context, "metrics_delay_s", chain.FIRST_METRICS_S),
        )]
        if pass_number <= 1:
            # The late rejections — claimed audio, delayed moderation — arrive
            # hours after a video looked perfectly fine.
            follow_on.append(chain.verify_spec(
                upload_id, channel_id=channel_id, pass_number=2,
                delay_s=_setting(
                    context, "verify_second_s", chain.SECOND_VERIFY_S
                ),
            ))
        return Done(
            f"{upload_id} is live: {verdict.detail}",
            follow_on=follow_on,
            verified=True, platform_state=verdict.state,
            verification=verdict.to_dict(),
        )

    # Confirmed absent or refused: the one case worth alarming on.
    with context.unit_of_work() as uow:
        row = uow.uploads.get(upload_id)
        if row is not None:
            row.state = PostState.NEEDS_ATTENTION.value
            row.last_error = (
                f"verification pass {pass_number}: {verdict.detail}"
            )[:500]
            uow.uploads.save(row)

    return Done(
        f"{upload_id} is not live: {verdict.detail}",
        verified=False, rejected=True, platform_state=verdict.state,
        verification=verdict.to_dict(),
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

    `generate_hooks` and `build_captions` remain deliberately absent. They are
    pure functions of a transcript with no I/O, and `detect_clips` runs all
    three in one pass through the factory pipeline — giving each its own queue
    row would add two round trips and two lease renewals to buy nothing. They
    are listed in `JobKind` because the schema was written for a fully
    decomposed pipeline; this runtime does not need one.

    The order of the dictionary is the order of the pipeline, which is the
    order to read them in.
    """

    return {
        chain.ACQUIRE: acquisition_handler,
        chain.TRANSCRIBE: transcription_handler,
        chain.SELECT: selection_handler,
        chain.RENDER: render_handler,
        chain.PUBLISH: publish_handler,
        chain.VERIFY: verification_handler,
        chain.METRICS: analytics_handler,
    }
