"""Video in, `SpeakerTrack` out.

This is the module the rest of the system calls. Everything above it — the
detector, the sampler, the tracker — exists to make this one function honest:

    result = FaceTrackEngine().track_video("clip.mp4")
    plan = GameplayEngine().compose(duration_s=..., track=result.track)

## What this replaces

`factory/pipeline.py` passed `SpeakerTrack()` — empty — into every compose, with
the comment "no face track in this path". Two things followed from that, and
both were real:

1. **Every clip was framed by a static centred crop.** `camera.solve` handles an
   empty track correctly and says so (`tracking="static"`), but a centred crop
   of a 16:9 interview into 9:16 cuts both people in half. The camera solver's
   entire deadband-follow-cut apparatus was unreachable.
2. **The source size was a guess.** `SpeakerTrack` defaults to 1920x1080, so a
   plan composed against 1280x720 media asked ffmpeg for a crop taller than the
   frame. `render.engine._preflight` exists to catch exactly that, which is to
   say the default was already known to be producing broken plans.

Both are fixed by the same fact: this engine probes the real file, so even the
no-faces fallback returns a track with true dimensions.

## Failure is a fallback, never an exception

A source with no faces in it is not an error — it is a gameplay compilation, a
screencast, a slideshow. A detector that cannot load is a deployment problem
that should degrade rather than stop a channel publishing. So `track_video`
returns a `FaceTrackResult` with an empty track and a populated `fallback`
string in both cases, and the camera does what it already does well with an
empty track. `DecodeError` is the one thing that propagates, because a file the
decoder cannot open is not going to render either.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Any, Sequence

from ..gameplay.types import FaceSample, SpeakerTrack
from .config import FaceDetectionConfig, MIN_FACE_FRACTION, config_from_env
from .decode import VideoInfo, probe_video, sample_frames
from .detector import FaceDetector
from .tracking import FaceTracker, salience, smooth_activity
from .types import (
    DecodeError,
    DetectorUnavailable,
    FaceTrackResult,
    PersonSummary,
    PersonTrack,
    VisionError,
)

log = logging.getLogger("clipforge.vision")

__all__ = ["FaceTrackEngine", "track_video"]

#: A track covering less of the clip than this is reported but not trusted as
#: the primary subject. Below it the camera is mostly holding through gaps,
#: which looks identical to a static crop and should be labelled as one.
_THIN_COVERAGE = 0.25


class FaceTrackEngine:
    """Detects faces across a video and returns a camera-ready track.

    The detector is constructed lazily and reused across calls on one engine,
    because loading the ONNX graph costs more than detecting on a short clip.
    It is not thread-safe for the same reason `YuNetDetector` is not; give each
    worker its own engine.
    """

    def __init__(
        self,
        config: FaceDetectionConfig | None = None,
        detector: FaceDetector | None = None,
    ) -> None:
        self.config = config or config_from_env()
        self._detector = detector
        self._owns_detector = detector is None

    # -- detector ----------------------------------------------------------

    @property
    def detector(self) -> FaceDetector:
        if self._detector is None:
            from .yunet import YuNetDetector

            self._detector = YuNetDetector(self.config)
        return self._detector

    def availability(self):
        """Whether face detection can run at all. Cheap enough to call often."""
        try:
            return self.detector.availability()
        except VisionError as error:
            from .types import Availability

            return Availability(False, str(error))

    def close(self) -> None:
        if self._detector is not None and self._owns_detector:
            self._detector.close()
            self._detector = None

    def __enter__(self) -> "FaceTrackEngine":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- the work ----------------------------------------------------------

    def track_video(
        self,
        path: str,
        *,
        start_s: float = 0.0,
        duration_s: float = 0.0,
        max_frames: int = 0,
    ) -> FaceTrackResult:
        """Detect and track faces over a video, or part of one.

        `start_s`/`duration_s` scope the work to a clip's span inside a longer
        source. Sample times in the returned track are **relative to
        `start_s`**, because that is what the camera solver expects: it walks
        from zero to the clip's duration, and a track timed from the start of a
        two-hour podcast would miss its window entirely.
        """

        started = time.perf_counter()
        info = probe_video(path)                # raises DecodeError, on purpose

        available = self.availability()
        if not available.ready:
            return self._empty(
                info, start_s, duration_s, started,
                fallback=f"detector unavailable: {available.detail}",
            )

        try:
            frames, tracker = self._scan(
                path, info, start_s, duration_s, max_frames,
            )
        except DecodeError:
            raise
        except DetectorUnavailable as error:
            return self._empty(info, start_s, duration_s, started,
                               fallback=f"detector unavailable: {error}")
        except Exception as error:                          # noqa: BLE001
            # An unexpected detector failure degrades to a static crop rather
            # than failing the render. The clip is already downloaded and cut;
            # losing it over a framing signal is the wrong trade.
            log.exception("face detection failed on %s", path)
            return self._empty(
                info, start_s, duration_s, started,
                fallback=(
                    f"face detection failed ({type(error).__name__}: {error}) "
                    f"— framing fell back to a static crop"
                ),
            )

        believed = tracker.finish()
        sampled, with_face, simultaneous = tracker.stats
        interval = 1.0 / self.config.sample_fps if self.config.sample_fps else 0.0

        if not believed:
            reason = (
                "no faces detected — the source has none, or every face is "
                "below the detector's size floor "
                f"({MIN_FACE_FRACTION:.1%} of frame height)"
                if sampled else "no frames could be sampled from the video"
            )
            return self._empty(info, start_s, duration_s, started,
                               fallback=reason, frames_sampled=sampled)

        smooth_activity(believed)
        span = duration_s if duration_s > 0 else max(
            0.0, info.duration_s - start_s
        )
        samples = self._to_samples(believed, start_s)
        notes = self._notes(believed, span, sampled, with_face, simultaneous,
                            info)

        track = SpeakerTrack(
            samples=samples,
            source_width=info.width,
            source_height=info.height,
            detector_fps=self.config.sample_fps,
        )
        return FaceTrackResult(
            track=track,
            people=_relative(tracker.summaries(believed, interval), start_s),
            detector=self.detector.info,
            frames_sampled=sampled,
            frames_with_face=with_face,
            max_simultaneous=simultaneous,
            sample_fps=self.config.sample_fps,
            source_width=info.width,
            source_height=info.height,
            duration_s=span,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            notes=tuple(notes),
        )

    # -- internals ---------------------------------------------------------

    def _scan(
        self, path: str, info: VideoInfo, start_s: float, duration_s: float,
        max_frames: int,
    ) -> tuple[int, FaceTracker]:
        detector = self.detector
        tracker = FaceTracker(self.config)
        floor_px = MIN_FACE_FRACTION * info.height
        count = 0

        for frame in sample_frames(
            path,
            sample_fps=self.config.sample_fps,
            start_s=start_s,
            duration_s=duration_s,
            max_frames=max_frames,
        ):
            found = tuple(
                d for d in detector.detect(frame.image)
                if d.box.height >= floor_px
            )
            tracker.update(
                t=frame.t,
                detections=found,
                image=frame.image,
                frame_size=(info.width, info.height),
            )
            count += 1
        return count, tracker

    def _to_samples(
        self, tracks: Sequence[PersonTrack], start_s: float,
    ) -> tuple[FaceSample, ...]:
        """Flatten tracks into the camera's flat, time-ordered sample list.

        Salience is computed here, after smoothing, because it depends on the
        smoothed activity. It goes into `FaceSample.confidence` — which the
        camera documents as the tracker's channel for saying who is speaking,
        and which is therefore *not* the detector's score. `PersonSummary`
        keeps the detector score for anyone who needs the real thing.
        """

        samples: list[FaceSample] = []
        for track in tracks:
            for index, (t, detection) in enumerate(track.observations):
                shifted = t - start_s
                if shifted < -1e-6:
                    continue
                score = salience(
                    track.activity[index] if index < len(track.activity) else 0.0,
                    track.size_scores[index]
                    if index < len(track.size_scores) else 0.0,
                    track.centre_scores[index]
                    if index < len(track.centre_scores) else 0.0,
                )
                samples.append(FaceSample(
                    t=max(0.0, shifted),
                    box=detection.box,
                    confidence=score,
                    speaker_id=track.track_id,
                ))
        samples.sort(key=lambda s: (s.t, s.speaker_id))
        return tuple(samples)

    def _notes(
        self, tracks: Sequence[PersonTrack], span: float, sampled: int,
        with_face: int, simultaneous: int, info: VideoInfo,
    ) -> list[str]:
        notes: list[str] = []

        if info.fps_assumed:
            notes.append(
                f"{info.path.rsplit('/', 1)[-1]} reports no usable frame rate; "
                f"sample times assume 30fps and may drift against the audio"
            )

        rate = with_face / sampled if sampled else 0.0
        if sampled and rate < _THIN_COVERAGE:
            notes.append(
                f"a face was found in only {rate:.0%} of sampled frames — the "
                f"camera will hold still for most of the clip, which will look "
                f"like a static crop"
            )
        if simultaneous > 1:
            notes.append(
                f"up to {simultaneous} faces in frame at once; the camera will "
                f"cut between the {len(tracks)} tracked speakers on turn-taking"
            )
        if len(tracks) > 3:
            notes.append(
                f"{len(tracks)} distinct people tracked — for a clip this is "
                f"usually the tracker fragmenting one person rather than a "
                f"crowd, and the framing is worth an eye"
            )
        return notes

    def _empty(
        self, info: VideoInfo, start_s: float, duration_s: float,
        started: float, *, fallback: str, frames_sampled: int = 0,
    ) -> FaceTrackResult:
        """The no-track result — still carrying the real source geometry.

        That geometry is the point. An empty `SpeakerTrack()` would default to
        1920x1080 and produce a plan the renderer rejects against smaller
        media; this one describes the file that actually exists, so the static
        fallback crop is correct rather than merely present.
        """

        span = duration_s if duration_s > 0 else max(
            0.0, info.duration_s - start_s
        )
        return FaceTrackResult(
            track=SpeakerTrack(
                samples=(),
                source_width=info.width,
                source_height=info.height,
                detector_fps=self.config.sample_fps,
            ),
            detector=(
                self._detector.info if self._detector is not None else None
            ),
            frames_sampled=frames_sampled,
            sample_fps=self.config.sample_fps,
            source_width=info.width,
            source_height=info.height,
            duration_s=span,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            fallback=fallback,
        )


def _relative(
    people: tuple[PersonSummary, ...], start_s: float,
) -> tuple[PersonSummary, ...]:
    """Put the summaries on the same clock as the samples.

    The tracker works in the source file's timeline, because that is what the
    decoder hands it. Everything leaving this module is on the *clip's*
    timeline, starting at zero. Shipping `samples[0].t == 0.0` next to
    `people[0].first_t == 612.3` would be two clocks in one object, and the
    first person to plot them together would file a bug against the tracker.
    """

    if not start_s:
        return people
    return tuple(
        replace(
            person,
            first_t=max(0.0, person.first_t - start_s),
            last_t=max(0.0, person.last_t - start_s),
        )
        for person in people
    )


def track_video(path: str, **kwargs: Any) -> FaceTrackResult:
    """One-shot convenience: build an engine, use it, close it."""
    with FaceTrackEngine() as engine:
        return engine.track_video(path, **kwargs)
