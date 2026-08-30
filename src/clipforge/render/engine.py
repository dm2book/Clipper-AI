"""Executing render plans.

    engine = RenderEngine(db, "ten_acme", workspace="/var/lib/clipforge/renders")
    engine.enqueue(clip_id, plan, speaker_path, gameplay_path=..., captions=track)
    engine.run(limit=4)

`gameplay.render` builds the filtergraph; this runs it, checks what came out,
and records the result. The two are separate because the graph is a pure
function of the plan and worth testing without spawning a process.

## The output is measured, not assumed

ffmpeg exits zero on plenty of files that are not what was asked for. A
truncated encode, a stream that was silently dropped because a `-map` matched
nothing, a height rounded to an odd number by a `scale`. Every one passes a
"did the process succeed?" check and none is publishable, so the output is
probed and compared against the plan before it counts as a render.

The comparison is deliberately tolerant about duration and strict about
everything else. Encoders land a frame or two either side of the requested
length — that is normal, and failing a render over 16ms would fail most of
them — but geometry, frame rate and the presence of audio are exact, because
each of those being wrong means the composition did not do what the plan said.

## Atomic outputs

ffmpeg writes to `<output>.tmp.mp4` and the file is renamed into place only
after it has been measured. A worker killed mid-encode leaves a `.tmp` nobody
reads, rather than a short file the publisher happily uploads.

## Concurrency

`workers` caps how many ffmpeg processes run at once, and it is not the same
number as the queue's batch size. x264 is already threaded, so four renders on
four cores is slower than four renders queued behind each other; the cap is
there so a machine running 500 uploads a day does not thrash.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Sequence

from ..acquire.http import sha256_file
from ..acquire.probe import MediaProber, find_ffmpeg
from ..acquire.types import MediaProbe
from ..gameplay.render import command, sendcmd_script
from ..publish.types import utcnow
from ..store.records import JobRecord, VideoRecord
from .types import (
    FfmpegMissing,
    OutputRejected,
    RenderFailed,
    RenderRequest,
    RenderResult,
    RenderState,
)

__all__ = ["RenderConfig", "RenderEngine", "RENDER_JOB", "verify_output"]

#: The job kind this engine claims from the shared queue.
RENDER_JOB = "render_video"

#: How far the measured duration may sit from the plan's. Encoders land a
#: frame or two either side; a tighter bound fails renders that are correct.
DURATION_TOLERANCE_S = 0.25


@dataclass(slots=True)
class RenderConfig:
    workspace: str = "/var/lib/clipforge/renders"
    #: A render that has not finished in this long is wedged. The ceiling is
    #: generous because a two-minute clip on a busy box is legitimately slow,
    #: and it exists so one bad input cannot hold a worker for ever.
    timeout_s: float = 1800.0
    max_attempts: int = 3
    retry_base_s: int = 60
    lease_s: int = 3600
    #: Concurrent ffmpeg processes. See the module docstring: x264 is already
    #: threaded, so this is about not thrashing rather than about throughput.
    workers: int = 2
    #: Keep the filtergraph and sendcmd script next to the output. Cheap, and
    #: the difference between "the render looks wrong" being diagnosable and
    #: being a shrug.
    keep_artifacts: bool = True
    ffmpeg: str = ""
    #: x264 preset and quality. The defaults in `gameplay.render` are the
    #: product's — `medium`/18 is where a talking head stops gaining from more
    #: bitrate. Overridable because a machine under load legitimately trades
    #: quality for throughput, and because a test suite cannot spend a minute
    #: of x264 per case.
    preset: str = ""
    crf: int = 0


class RenderEngine:
    """Turns plans into files, and files into `videos` rows."""

    def __init__(
        self,
        database: Any,
        tenant_id: str,
        *,
        config: RenderConfig | None = None,
        prober: MediaProber | None = None,
        clock: Callable[[], datetime] = utcnow,
        runner: Callable[[Sequence[str], float], subprocess.CompletedProcess] | None = None,
        plan_loader: Callable[[dict], Any] | None = None,
        storage: Any | None = None,
    ) -> None:
        if not tenant_id:
            raise ValueError("rendering needs a tenant")
        self.database = database
        self.tenant_id = tenant_id
        #: Durable storage for finished clips. Without it the render exists
        #: only on the container that produced it, and Instagram — which
        #: fetches the file itself — cannot publish it at all.
        self.storage = storage
        self.config = config or RenderConfig()
        self.ffmpeg = self.config.ffmpeg or find_ffmpeg()
        self.prober = prober or MediaProber(ffmpeg=self.ffmpeg or None)
        self.clock = clock
        # Injected so the queue, verification and persistence paths can be
        # tested without spending a minute of x264 per case. The rendering
        # tests do not inject it.
        self._runner = runner or self._spawn
        self._plan_loader = plan_loader
        # Per instance, never on the class. A dict at class scope is shared by
        # every engine in the process — including ones for other tenants — and
        # it grows for the life of the worker.
        self._plans: dict[str, Any] = {}

    # -- queueing ----------------------------------------------------------

    def enqueue(
        self,
        clip_id: str,
        plan: Any,
        speaker_path: str,
        *,
        gameplay_path: str = "",
        subtitles: str = "",
        start_s: float = 0.0,
        source_id: str = "",
        channel_id: str = "",
        priority: int = 100,
    ) -> str:
        """Queue one render. Returns the job id.

        The plan is serialised into the payload rather than held in memory, so
        a worker that picks this up after a restart renders the composition
        that was decided, not one recomputed from since-changed inputs.
        """

        render_id = f"rnd_{uuid.uuid4().hex[:12]}"
        with self.database.unit_of_work(self.tenant_id) as uow:
            job = uow.jobs.enqueue(JobRecord(
                id=f"job_{uuid.uuid4().hex[:12]}",
                tenant_id=self.tenant_id,
                channel_id=channel_id or None,
                kind=RENDER_JOB,
                priority=priority,
                run_after=self.clock(),
                max_attempts=self.config.max_attempts,
                payload={
                    "render_id": render_id,
                    "clip_id": clip_id,
                    "source_id": source_id,
                    "speaker_path": speaker_path,
                    "gameplay_path": gameplay_path,
                    "start_s": start_s,
                    "subtitles": subtitles,
                    "plan": plan.to_dict() if hasattr(plan, "to_dict") else plan,
                    # The plan object itself, kept alive for the in-process
                    # path. A worker that picked the job up from the database
                    # rebuilds it from `plan` above.
                },
                # One render per clip. A clip re-queued while its render is
                # still running is the same piece of work, not a second one.
                dedupe_key=f"render:{clip_id}",
            ))
        self._plans[render_id] = plan
        return job.id

    def run(self, limit: int = 1, *, worker_id: str = "render-1") -> list[RenderResult]:
        """Claim queued renders and run them."""

        now = self.clock()
        with self.database.unit_of_work(self.tenant_id) as uow:
            claimed = uow.jobs.claim(
                worker_id, now, lease_s=self.config.lease_s,
                kinds=(RENDER_JOB,), limit=min(limit, self.config.workers),
            )
        return [self._run_job(job) for job in claimed]

    def _run_job(self, job: JobRecord) -> RenderResult:
        payload = job.payload or {}
        render_id = payload.get("render_id") or f"rnd_{job.id}"
        plan = self._plans.get(render_id)
        if plan is None and self._plan_loader is not None and payload.get("plan"):
            # A worker in a different process from the one that queued this.
            # The serialised plan is in the payload; turning it back into the
            # object needs a loader the caller supplies, because a plan is a
            # composition decision and reconstructing it wrongly is worse than
            # refusing to.
            plan = self._plan_loader(payload["plan"])
        if plan is None or isinstance(plan, dict):
            # A plan that only exists as a dict cannot be rendered: the
            # filtergraph builder needs the object. Said plainly rather than
            # failing deep inside the graph builder with an AttributeError.
            message = (
                f"{render_id}: the plan is only present as serialised data "
                f"and no plan_loader was supplied. Enqueue and run in the "
                f"same process, or pass plan_loader= to rebuild it."
            )
            self._fail(job, message, retry=False)
            return RenderResult(render_id, RenderState.FAILED, error=message)

        directory = os.path.join(self.config.workspace, self.tenant_id, render_id)
        request = RenderRequest(
            render_id=render_id,
            plan=plan,
            speaker_path=payload.get("speaker_path", ""),
            output_path=os.path.join(directory, "clip.mp4"),
            gameplay_path=payload.get("gameplay_path", ""),
            start_s=float(payload.get("start_s", 0.0)),
            clip_id=payload.get("clip_id", ""),
            source_id=payload.get("source_id", ""),
        )
        if subtitles := payload.get("subtitles"):
            os.makedirs(directory, exist_ok=True)
            request.subtitles_path = os.path.join(directory, "captions.ass")
            with open(request.subtitles_path, "w", encoding="utf-8") as handle:
                handle.write(subtitles)

        try:
            result = self.render(request)
        except (RenderFailed, OutputRejected, OSError) as error:
            self._fail(job, str(error), retry=True)
            return RenderResult(render_id, RenderState.FAILED, error=str(error))
        except FfmpegMissing as error:
            self._fail(job, str(error), retry=False)
            return RenderResult(render_id, RenderState.FAILED, error=str(error))

        video = self.persist(request, result)
        result.video_id = video.id
        with self.database.unit_of_work(self.tenant_id) as uow:
            uow.jobs.succeed(job.id, result.to_dict(), self.clock())
        return result

    def _fail(self, job: JobRecord, message: str, *, retry: bool) -> None:
        now = self.clock()
        retry_at = None
        if retry:
            retry_at = now + timedelta(
                seconds=self.config.retry_base_s * (2 ** min(job.attempts, 5))
            )
        with self.database.unit_of_work(self.tenant_id) as uow:
            uow.jobs.fail(job.id, message, retry_at, now)

    # -- rendering ---------------------------------------------------------

    def render(self, request: RenderRequest) -> RenderResult:
        """Run one render to completion. No queue involved.

        Usable directly, which is what the tests do — a render is slow but it
        is not asynchronous, and a synchronous path is far easier to reason
        about than one that only exists behind a worker loop.
        """

        if not self.ffmpeg:
            raise FfmpegMissing(
                "rendering needs ffmpeg — install it, or set CLIPFORGE_FFMPEG"
            )
        if not os.path.exists(request.speaker_path):
            raise RenderFailed(f"no speaker media at {request.speaker_path}")

        self._preflight(request)

        directory = os.path.dirname(os.path.abspath(request.output_path))
        os.makedirs(directory, exist_ok=True)

        plan = request.plan
        sendcmd_path = os.path.join(directory, "camera.cmd")
        with open(sendcmd_path, "w", encoding="utf-8") as handle:
            handle.write(sendcmd_script(plan))

        # Written beside the output, never over it. A killed worker leaves a
        # `.tmp` nobody reads rather than a short file the publisher uploads.
        temporary = f"{request.output_path}.tmp.mp4"
        argv = self._argv(request, sendcmd_path, temporary)

        if self.config.keep_artifacts:
            with open(os.path.join(directory, "ffmpeg.args"), "w") as handle:
                handle.write("\n".join(argv))

        result = RenderResult(request.render_id, RenderState.RENDERING)
        started = time.perf_counter()
        completed = self._runner(argv, self.config.timeout_s)
        result.elapsed_s = time.perf_counter() - started
        result.attempts = 1

        if completed.returncode != 0:
            _discard(temporary)
            result.state = RenderState.FAILED
            result.error = _last_error(completed.stderr)
            raise RenderFailed(f"{request.render_id}: ffmpeg — {result.error}")

        if not os.path.exists(temporary) or os.path.getsize(temporary) == 0:
            _discard(temporary)
            raise OutputRejected(
                f"{request.render_id}: ffmpeg exited 0 and produced no file"
            )

        probe = self.prober.probe(temporary)
        problems = verify_output(probe, plan)
        if problems:
            _discard(temporary)
            raise OutputRejected(f"{request.render_id}: " + "; ".join(problems))

        os.replace(temporary, request.output_path)

        result.state = RenderState.READY
        result.output_path = request.output_path
        result.probe = probe
        result.size_bytes = os.path.getsize(request.output_path)
        result.checksum = sha256_file(request.output_path)
        result.finished_at = self.clock()
        if plan.duration_s:
            result.realtime_ratio = result.elapsed_s / plan.duration_s
        self._store(request, result)
        return result

    def _store(self, request: RenderRequest, result: RenderResult) -> None:
        """Upload the finished clip and record where it went.

        A failure here does not fail the render. The encode is the expensive
        part and it succeeded; the file is still on disk and
        `storage.migrate.backfill` will pick it up. What is *not* done is
        pretending it worked — `storage_ref` stays empty, so anything that
        needs a durable copy can tell.
        """

        if self.storage is None:
            return
        clip_id = request.clip_id or request.render_id
        try:
            from ..storage.migrate import store_render

            ref = store_render(
                self.storage, self.tenant_id, clip_id, result.output_path
            )
            result.storage_ref = str(ref)
        except Exception as error:                          # noqa: BLE001
            result.error = result.error or f"stored locally only: {error}"
            return

        try:
            result.public_url = self.storage.public_url(ref.key)
        except Exception:                                   # noqa: BLE001
            # No public domain on the bucket. TikTok and YouTube are fine
            # without one; Instagram is not, and the capability list says so
            # rather than this failing a render that is otherwise complete.
            result.public_url = ""

    def _preflight(self, request: RenderRequest) -> None:
        """Check the plan against the media it will be applied to.

        The camera path crops the speaker's frame, and its rectangle is in
        *source* pixels — which the plan took from the `SpeakerTrack` it was
        composed with. `SpeakerTrack` defaults to 1920x1080, so a plan built
        without a real track silently assumes that, and applying it to a
        1280x720 file asks ffmpeg to crop a 1080-pixel-tall window out of a
        720-pixel-tall frame.

        ffmpeg's own answer to that is:

            Invalid too big or non positive size for width '996' or height '1080'
            Error reinitializing filters!
            Conversion failed!

        — which names neither the plan, the source, nor the mismatch. Catching
        it here costs one probe and turns it into a sentence that says what to
        fix. Permanent rather than retryable: the same plan against the same
        file fails identically for ever, and the repair is to recompose with
        the real dimensions.
        """

        camera = getattr(request.plan, "camera", None)
        if camera is None:
            return
        # The crop window's size is fixed for the clip; only its position
        # moves. Its far edge is what can overrun the frame, so the check is
        # against the widest keyframe reach rather than the size alone.
        needed_w = getattr(camera, "width", 0)
        needed_h = getattr(camera, "height", 0)
        if not needed_w or not needed_h:
            return
        keyframes = getattr(camera, "keyframes", ()) or ()
        reach_x = max((k.x for k in keyframes), default=0) + needed_w
        reach_y = max((k.y for k in keyframes), default=0) + needed_h

        probe = self.prober.probe(request.speaker_path)
        if not probe.width or not probe.height:
            return  # unmeasurable geometry is the probe's problem, not this one
        if reach_x <= probe.width and reach_y <= probe.height:
            return

        raise OutputRejected(
            f"{request.render_id}: the plan's camera reaches "
            f"{reach_x}x{reach_y} into the speaker's frame (a "
            f"{needed_w}x{needed_h} window), but "
            f"{os.path.basename(request.speaker_path)} is "
            f"{probe.width}x{probe.height}. The plan was composed against a "
            f"different source size — recompose it with a SpeakerTrack whose "
            f"source_width/source_height match the real media."
        )

    def _argv(
        self, request: RenderRequest, sendcmd_path: str, output: str
    ) -> list[str]:
        argv = command(
            request.plan,
            request.speaker_path,
            request.gameplay_path,
            output,
            sendcmd_path,
        )
        argv[0] = self.ffmpeg

        if request.start_s > 0:
            # Before the speaker input, so the seek is by keyframe and cheap.
            # Without it the encoder walks the whole two-hour podcast to reach
            # a clip forty minutes in.
            index = argv.index("-i")
            argv[index:index] = ["-ss", f"{request.start_s:.3f}"]

        if request.subtitles_path:
            argv = _burn_subtitles(argv, request.subtitles_path)
        if self.config.preset:
            argv[argv.index("-preset") + 1] = self.config.preset
        if self.config.crf:
            argv[argv.index("-crf") + 1] = str(self.config.crf)
        return argv

    def _spawn(
        self, argv: Sequence[str], timeout_s: float
    ) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                list(argv), capture_output=True, text=True,
                timeout=timeout_s, check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise RenderFailed(
                f"ffmpeg exceeded {timeout_s:.0f}s and was killed"
            ) from error

    # -- persistence -------------------------------------------------------

    def persist(self, request: RenderRequest, result: RenderResult) -> VideoRecord:
        """Record the rendered asset.

        A `videos` row rather than a field on the clip, because one clip is
        legitimately rendered more than once — a re-render after a caption
        fix, a per-platform variant — and the upload has to reference the
        exact file that was sent.

        Public because `render()` is public and does not call it: a caller
        driving a render directly — which is what the worker's handler does —
        otherwise produces a file with no durable record of what it is. That
        was reachable only through `run()`, the queue path the worker replaced.
        """

        probe = result.probe
        plan = request.plan
        with self.database.unit_of_work(self.tenant_id) as uow:
            record = VideoRecord(
                id=f"vid_{uuid.uuid4().hex[:12]}",
                tenant_id=self.tenant_id,
                clip_id=request.clip_id or None,
                source_id=request.source_id or None,
                state="ready",
                storage_key=result.output_path,
                checksum=result.checksum,
                size_bytes=result.size_bytes,
                duration_s=probe.duration_s if probe and probe.duration_s else 0.0,
                width=probe.width if probe and probe.width else plan.width,
                height=probe.height if probe and probe.height else plan.height,
                fps=int(probe.fps) if probe and probe.fps else plan.fps,
                render_plan=plan.to_dict(),
                rendered_at=result.finished_at or self.clock(),
            )
            return uow.videos.save(record)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_output(probe: MediaProbe, plan: Any) -> list[str]:
    """What is wrong with this render, as human-readable strings.

    Tolerant about duration and strict about everything else. An encoder lands
    a frame or two either side of the requested length and failing over 16ms
    would fail most correct renders; geometry, frame rate and audio being
    wrong each mean the composition did not do what the plan said, and each is
    invisible until the clip is on someone's feed.
    """

    problems: list[str] = []

    if probe.duration_s is None:
        problems.append("the output has no measurable duration")
    elif abs(probe.duration_s - plan.duration_s) > DURATION_TOLERANCE_S:
        problems.append(
            f"duration {probe.duration_s:.2f}s against a planned "
            f"{plan.duration_s:.2f}s"
        )

    if (probe.width, probe.height) != (plan.width, plan.height):
        problems.append(
            f"{probe.width}x{probe.height} against a planned "
            f"{plan.width}x{plan.height}"
        )

    if probe.fps and abs(probe.fps - plan.fps) > 0.5:
        problems.append(f"{probe.fps} fps against a planned {plan.fps}")

    if not probe.has_video:
        problems.append("no video track")
    if not probe.has_audio:
        # The most likely silent failure: a `-map 0:a?` that matched nothing
        # because the speaker file had no audio. Exits zero, produces a
        # perfectly valid silent video, and nobody notices until it is posted.
        problems.append("no audio track — the speaker's audio was not carried")

    return problems


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _burn_subtitles(argv: list[str], subtitles_path: str) -> list[str]:
    """Append an ASS burn-in to the end of the video chain.

    Burned rather than muxed as a soft track: TikTok, Reels and Shorts all
    play with subtitles off by default, so a soft track is a caption nobody
    sees — and captions are load-bearing for retention on muted autoplay.

    The filter is appended to the graph's final `[v]` output rather than
    inserted anywhere clever, so it composites over the finished frame and
    cannot disturb the camera or panel chains.
    """

    index = argv.index("-filter_complex")
    graph = argv[index + 1]
    escaped = subtitles_path.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    argv[index + 1] = f"{graph};[v]subtitles='{escaped}'[vsub]"
    argv[argv.index("[v]")] = "[vsub]"
    return argv


def _discard(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _last_error(stderr: str) -> str:
    """The line of ffmpeg's output that actually says what went wrong.

    ffmpeg puts the diagnosis in the middle of a wall of banner and codec
    statistics, so the tail is not reliably it — the last line mentioning an
    error is.
    """

    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    for line in reversed(lines):
        lowered = line.lower()
        if any(sign in lowered for sign in (
            "error", "invalid", "no such file", "unable to", "failed",
            "not found", "cannot",
        )):
            return line
    return lines[-1] if lines else "no output"
