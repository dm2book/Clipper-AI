"""Clip selection, made durable.

This is the stage the chain was missing. `factory.pipeline` could already take
a transcript and produce a moment, a hook, a caption track and a post spec —
in memory, inside one process, returning a `WorkItem` that nothing wrote down.
Run from a worker it stopped there: no `clips` row, no `uploads` row, nothing
for a renderer to find. "Source → clip selection → scheduled" ended here, and
the reason it ended here is that scheduled meant *an object in a variable*.

So this module runs the same pipeline and then commits what it produced:

* a `clips` row, with the moment's scores, every generated hook and the
  caption track — the record of what was chosen and why;
* one `uploads` row per connected platform, booked on the calendar in `draft`;
* a `render_video` job for the clip.

## Draft, not scheduled, and the distinction is load-bearing

A booked post whose file does not exist yet must not be publishable. The
calendar's `due()` ignores `DRAFT`, so booking in `DRAFT` means no publisher
can ever claim a post before its render finishes — not because the timing
usually works out, but because the state machine will not let it. The render's
own completion is what promotes the row to `SCHEDULED`, which is the transition
that makes the post real.

Booking now rather than after the render is deliberate: the calendar's spacing
and quota checks are the things that decide whether a clip is worth rendering
at all, and finding out after ffmpeg has run is finding out too late.

## Running it twice does nothing twice

The guard is a lookup, not a lock: if this source already has a clip for this
channel, the pipeline is not run again and the same `render_video` follow-on is
re-emitted. That matters more here than in most handlers, because the pipeline
is *not* idempotent by construction — its first stage refuses a source the
channel has already used, so a naive second run would return BLOCKED, emit no
follow-on, and stop the chain with every row saying success.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from ..factory.pipeline import Pipeline, PipelineConfig, Stage
from ..publish.types import PostState, utcnow
from ..store.records import ClipRecord
from .chain import render_output_path, render_spec
from .types import Done, Fatal, JobContext, Outcome, Retry

__all__ = ["selection_handler", "LEAD_TIME", "SLOT_STEP"]


#: How far ahead the first booking is placed. Long enough that a render and an
#: upload have time to finish before the slot arrives, short enough that a
#: creator who submits a source in the morning sees it go out the same day.
LEAD_TIME = timedelta(hours=6)
#: How far to move when a slot clashes with the account's spacing floor.
SLOT_STEP = timedelta(hours=4)
#: How many slots to try before giving up and reporting the clash.
SLOT_TRIES = 8


def selection_handler(context: JobContext) -> Outcome:
    """Transcript → clip → booked posts → a queued render.

    Fatal on anything a retry cannot change: no source row, no channel, no
    transcript. Retry on a pipeline that threw, because that is either a bug
    or a transient store failure and neither is worth burning the clip over.
    """

    payload = context.payload
    source_id = str(payload.get("source_id") or "")
    if not source_id:
        return Fatal("no source_id in the payload")

    channel_id = str(payload.get("channel_id") or "")
    now = utcnow()

    with context.unit_of_work() as uow:
        source_record = uow.sources.get(source_id)
        if source_record is None:
            return Fatal(f"source {source_id} does not exist")

        if not channel_id:
            channel_id = _channel_from_acquisition(uow, source_id)
        if not channel_id:
            return Fatal(
                f"source {source_id} is not attached to a channel, and clip "
                f"selection needs one — the niche, the hook style and the "
                f"accounts to post to all come from it"
            )

        channel_record = uow.channels.get(channel_id)
        if channel_record is None:
            return Fatal(f"channel {channel_id} does not exist")

        # The replay path. See the module docstring: the pipeline refuses a
        # source the channel has already used, so a second run must not reach
        # it or the chain stops on a BLOCKED with nothing queued behind it.
        done = [
            clip for clip in uow.clips.for_source(source_id)
            if clip.channel_id == channel_id
        ]
        if done:
            return Done(
                f"{source_id} was already selected for {channel_id}",
                follow_on=[
                    render_spec(clip.id, channel_id=channel_id,
                                source_id=source_id)
                    for clip in done
                ],
                clip_ids=[clip.id for clip in done],
                replayed=True,
            )

        run = uow.transcriptions.for_source(source_id)
        transcript_payload = dict(getattr(run, "transcript", None) or {})
        state = getattr(run, "state", "")

    if run is None:
        return Fatal(
            f"source {source_id} has no transcription run — transcription "
            f"must finish before a clip can be chosen from it"
        )
    if state != "succeeded":
        return Fatal(
            f"the transcription of {source_id} is {state!r}, not succeeded"
        )

    words = _words(transcript_payload)
    if not words:
        return Fatal(
            f"the stored transcript for {source_id} has no word timings, so "
            f"there is nothing to cut on"
        )

    channel = _channel(context, channel_record)
    source = _source(source_record)

    pipeline = Pipeline(PipelineConfig(
        # Business, motivation, AI and history all want a gameplay bed, and
        # the compose stage blocks a source outright when the library has
        # none. A worker with no beds therefore produces nothing for four of
        # the seven niches — which is worth knowing rather than discovering
        # from a channel that never posts.
        gameplay_library=tuple(
            getattr(context.services, "gameplay_library", ()) or ()
        ),
        # Framing is the renderer's job and needs the media, which may not be
        # on this host. Running a detector here would either load a model to
        # throw the result away or fail on a file it cannot see.
        disable_face_tracking=True,
    ))
    item = pipeline.run(channel, source, transcript_words=words, now=now)

    # Charged whatever the outcome, as the factory does. A source blocked at
    # the quality floor still paid for transcription, detection, hooks and
    # captions on the way there, and a budget that only counts successes
    # reports a channel as affordable while it burns money on rejects.
    channel.budget.charge(item.cost_cents)

    if item.stage is Stage.FAILED:
        _record_failure(context, channel, item.reason, now)
        return Retry(f"the pipeline failed on {source_id}: {item.reason}")
    if item.stage is Stage.BLOCKED:
        _record_blocked(context, channel, item.reason)
        # Done, not Fatal: a rights gate or a quality floor refusing a source
        # is the system working. It emits no follow-on because there is
        # nothing downstream to do, and the reason is on the job row.
        return Done(f"blocked: {item.reason}", blocked=True,
                    reason=item.reason)
    if item.stage is not Stage.SCHEDULED:
        return Retry(f"the pipeline stopped at {item.stage.value}")

    clip_id = _clip_id(channel_id, source_id, item)
    booked, problems = _book(context, channel, item, clip_id, now)

    with context.unit_of_work() as uow:
        uow.clips.save(_clip_record(
            context.tenant_id, channel_id, source_id, clip_id, item
        ))
        uow.sources.mark_used(channel_id, source_id, now)

    _record_success(context, channel, source.fingerprint)

    detail = f"clip {clip_id}: {len(booked)} post(s) booked"
    if problems:
        detail = f"{detail} ({'; '.join(problems)})"
    return Done(
        detail,
        follow_on=[render_spec(clip_id, channel_id=channel_id,
                               source_id=source_id)],
        clip_id=clip_id,
        upload_ids=booked,
        virality=item.moment.scores.virality if item.moment else None,
        problems=problems or None,
    )


# ---------------------------------------------------------------------------
# Reading what the pipeline needs
# ---------------------------------------------------------------------------


def _channel_from_acquisition(uow: Any, source_id: str) -> str:
    """The channel that asked for this source, if acquisition recorded one."""

    for run in uow.acquisitions.for_source(source_id):
        if run.channel_id:
            return str(run.channel_id)
    return ""


def _words(payload: dict[str, Any]) -> list[Any]:
    from ..transcribe.engine import transcript_from_dict
    from ..transcribe.pipeline import to_timed_words

    return to_timed_words(transcript_from_dict(payload))


def _source(record: Any):
    from ..store.mappers import to_source

    return to_source(record)


def _channel(context: JobContext, record: Any):
    """The channel, with its accounts and its used-source set attached.

    Through `DurableChannelBook` rather than `to_channel` directly, because a
    channel without its accounts produces a work item with no post specs and
    an unhelpful "no connected accounts" — the accounts live in their own
    table and something has to join them.
    """

    from ..store.durable import DurableChannelBook

    book = DurableChannelBook(
        context.database, context.tenant_id,
        project_id=getattr(record, "project_id", "") or "",
    )
    return book[record.id]


# ---------------------------------------------------------------------------
# Writing down what it produced
# ---------------------------------------------------------------------------


def _clip_id(channel_id: str, source_id: str, item: Any) -> str:
    """Derived, not random.

    Two runs over the same transcript choose the same moment, so they should
    name the same clip — and then `render:<clip_id>` deduplicates the render
    even if the replay guard above is somehow bypassed.
    """

    moment = item.moment
    span = f"{moment.candidate.start_ms}-{moment.candidate.end_ms}"
    raw = f"{channel_id}|{source_id}|{span}"
    return f"cl_{hashlib.blake2b(raw.encode(), digest_size=6).hexdigest()}"


def _clip_record(
    tenant_id: str, channel_id: str, source_id: str, clip_id: str, item: Any,
) -> ClipRecord:
    moment = item.moment
    hook = item.best_hook
    candidate = moment.candidate
    track = item.caption_track

    return ClipRecord(
        id=clip_id,
        tenant_id=tenant_id,
        channel_id=channel_id,
        source_id=source_id,
        start_ms=candidate.start_ms,
        end_ms=candidate.end_ms,
        duration_s=candidate.duration_ms / 1000.0,
        title=(hook.text[:200] if hook else ""),
        transcript=getattr(candidate, "text", "") or "",
        virality_score=float(moment.scores.virality),
        scores=moment.scores.as_dict(),
        features={k: round(v, 4) for k, v in moment.features.items()},
        signals=[
            signal.value
            for signal, weight in sorted(
                moment.signals.items(), key=lambda kv: -kv[1]
            )
            if weight > 0
        ],
        hook_text=(hook.text if hook else ""),
        hook_type=(hook.hook_type.value if hook else ""),
        predicted_lift=(hook.estimate.lift if hook else 0.0),
        hook_candidates=[h.to_dict() for h in item.hooks],
        caption_track=(track.to_dict() if hasattr(track, "to_dict") else None),
    )


def _book(
    context: JobContext, channel: Any, item: Any, clip_id: str,
    now: datetime,
) -> tuple[list[str], list[str]]:
    """Put the item's posts on the calendar, in `draft`.

    Returns the upload ids booked and the problems that stopped the rest. A
    channel with no publisher configured books nothing and says so — the clip
    and its render still happen, which is the honest behaviour for a
    deployment that has not connected an account yet.
    """

    services = context.services
    factory = getattr(services, "publisher_factory", None)
    if factory is None:
        return [], ["no publisher is configured, so nothing was booked"]

    publisher = factory(context.database, context.tenant_id, channel.channel_id)

    booked: list[str] = []
    problems: list[str] = []
    lead = getattr(services, "lead_time_s", None)
    lead = LEAD_TIME.total_seconds() if lead is None else float(lead)
    run_at = now + timedelta(seconds=lead)

    from ..publish.engine import ScheduleError

    for platform, spec in zip(channel.platforms, item.post_specs):
        account_id = channel.accounts.get(platform, "")
        if not account_id:
            problems.append(f"{platform.value}: no account connected")
            continue

        prepared = _prepare(spec, clip_id)
        slot = run_at
        for _ in range(SLOT_TRIES):
            try:
                post = publisher.schedule(account_id, prepared, slot)
            except ScheduleError as error:
                if _spacing_only(error):
                    # The floor exists so a platform does not read a channel
                    # as a bot. Moving is the right answer; refusing is not.
                    slot = slot + SLOT_STEP
                    continue
                problems.append(f"{platform.value}: {error}")
                break
            except KeyError as error:
                problems.append(f"{platform.value}: unknown account {error}")
                break

            # Booked, and deliberately not publishable yet.
            post.state = PostState.DRAFT
            publisher.calendar.persist(post)
            booked.append(post.post_id)
            break
        else:
            problems.append(
                f"{platform.value}: no free slot within "
                f"{SLOT_TRIES * SLOT_STEP.total_seconds() / 3600:.0f} hours"
            )

    return booked, problems


def _prepare(spec: Any, clip_id: str) -> Any:
    """The spec, pointed at the clip that will be rendered for it.

    The asset id becomes the clip id so the idempotency key the publishing
    engine derives is stable across a re-book, and `clip_id` goes into the
    metadata so the `uploads` row carries the link — that is how a finished
    render finds the posts waiting on it.
    """

    asset = replace(
        spec.asset,
        asset_id=clip_id,
        # Where the render *will* put the file, not where a file is. The
        # calendar's validator requires a path — a post with nowhere to read
        # bytes from is not a post — and the render overwrites this with the
        # path the encoder actually returned before the upload leaves `draft`.
        # So no publisher ever reads the prediction.
        path=render_output_path(clip_id),
        public_url="",
    )
    metadata = dict(spec.metadata)
    metadata["clip_id"] = clip_id
    return replace(spec, asset=asset, metadata=metadata)


def _spacing_only(error: Any) -> bool:
    problems = getattr(error, "problems", None) or []
    return bool(problems) and all("minutes of" in p for p in problems)


# ---------------------------------------------------------------------------
# Channel bookkeeping
#
# The factory does this around its own cycle. A worker running one source at a
# time has to do it too, or a channel's spend, its breaker and its used-source
# set stop moving the moment the pipeline is driven from a queue.
# ---------------------------------------------------------------------------


def _save_channel(context: JobContext, channel: Any) -> None:
    from ..store.durable import DurableChannelBook

    with context.unit_of_work() as uow:
        record = uow.channels.get(channel.channel_id)
        project_id = getattr(record, "project_id", "") or ""
    book = DurableChannelBook(
        context.database, context.tenant_id, project_id=project_id
    )
    book[channel.channel_id] = channel


def _record_success(
    context: JobContext, channel: Any, fingerprint: str
) -> None:
    channel.health.record_success()
    channel.used_fingerprints.add(fingerprint)
    _save_channel(context, channel)


def _record_blocked(context: JobContext, channel: Any, reason: str) -> None:
    channel.health.record_blocked(reason)
    _save_channel(context, channel)


def _record_failure(
    context: JobContext, channel: Any, reason: str, now: datetime
) -> None:
    channel.health.record_failure(reason, now)
    _save_channel(context, channel)
