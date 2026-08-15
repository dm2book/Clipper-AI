"""What a face detector produces, before the camera gets hold of it.

These types are deliberately richer than `gameplay.FaceSample`, which is what
this module eventually emits. A detector knows things the camera has no use
for — five facial landmarks, the raw detector score, which sampled frame a box
came from — and throwing them away at the detector boundary means the tracker
cannot use them either. The narrowing happens once, in `engine.py`, at the
point where a `PersonTrack` becomes a `SpeakerTrack`.

`Box` is imported from `gameplay.types` rather than redefined. It is the same
rectangle with the same `to_dict`, and two of them drift.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from ..gameplay.types import Box

__all__ = [
    "Box",
    "Landmarks",
    "Detection",
    "TrackState",
    "PersonTrack",
    "PersonSummary",
    "FaceTrackResult",
    "DetectorInfo",
    "Availability",
    "VisionError",
    "DetectorUnavailable",
    "DecodeError",
    "iou",
]


class VisionError(RuntimeError):
    """Base for everything this module raises."""


class DetectorUnavailable(VisionError):
    """The detector cannot run: no model file, no OpenCV, no readable weights.

    Separate from `DecodeError` because the two have different repairs and
    different blast radii. A missing model breaks every clip until someone
    installs it; an unreadable file breaks one.
    """


class DecodeError(VisionError):
    """The video could not be opened or produced no frames."""


@dataclass(frozen=True, slots=True)
class Landmarks:
    """The five points YuNet returns, in source-frame pixels.

    Kept because the mouth corners are the only evidence this module has about
    *who is speaking* — see `tracking.activity`. Eyes are kept because they
    give a real eyeline, which is better than the `EYES_IN_BOX` constant the
    camera falls back to when it has only a box.
    """

    right_eye: tuple[float, float]
    left_eye: tuple[float, float]
    nose: tuple[float, float]
    right_mouth: tuple[float, float]
    left_mouth: tuple[float, float]

    @property
    def eye_distance(self) -> float:
        dx = self.left_eye[0] - self.right_eye[0]
        dy = self.left_eye[1] - self.right_eye[1]
        return (dx * dx + dy * dy) ** 0.5

    @property
    def eye_centre(self) -> tuple[float, float]:
        return (
            (self.right_eye[0] + self.left_eye[0]) / 2.0,
            (self.right_eye[1] + self.left_eye[1]) / 2.0,
        )

    @property
    def mouth_centre(self) -> tuple[float, float]:
        return (
            (self.right_mouth[0] + self.left_mouth[0]) / 2.0,
            (self.right_mouth[1] + self.left_mouth[1]) / 2.0,
        )

    def to_dict(self) -> dict[str, list[float]]:
        return {
            "right_eye": [round(v, 1) for v in self.right_eye],
            "left_eye": [round(v, 1) for v in self.left_eye],
            "nose": [round(v, 1) for v in self.nose],
            "right_mouth": [round(v, 1) for v in self.right_mouth],
            "left_mouth": [round(v, 1) for v in self.left_mouth],
        }


@dataclass(frozen=True, slots=True)
class Detection:
    """One face found in one frame.

    `confidence` here is the detector's own score and nothing else. The
    salience score the camera reads is computed later and lives on the emitted
    `FaceSample`; conflating the two at this level would mean a quiet person
    looked like a bad detection.
    """

    box: Box
    confidence: float
    landmarks: Landmarks | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "box": self.box.to_dict(),
            "confidence": round(self.confidence, 4),
        }
        if self.landmarks is not None:
            out["landmarks"] = self.landmarks.to_dict()
        return out


class TrackState(str, enum.Enum):
    """Where a person's track is in its life.

    `TENTATIVE` exists to absorb one-frame false positives. A detector firing
    once on a doorknob should not create a speaker the camera might cut to, so
    a track emits nothing until it has been seen enough times to be believed —
    and then emits its buffered history, so a real face loses no samples for
    having been provisional at first.
    """

    TENTATIVE = "tentative"
    CONFIRMED = "confirmed"
    LOST = "lost"
    ENDED = "ended"


@dataclass(slots=True)
class PersonTrack:
    """One person followed across sampled frames.

    Mutable, unlike almost everything else here, because it is built by
    accumulation inside the tracker and frozen into a `PersonSummary` on the
    way out.
    """

    track_id: str
    state: TrackState = TrackState.TENTATIVE
    #: (t, Detection) in time order, including frames before confirmation.
    observations: list[tuple[float, Detection]] = field(default_factory=list)
    hits: int = 0
    misses: int = 0
    first_t: float = 0.0
    last_t: float = 0.0
    #: Per-observation activity in [0, 1], parallel to `observations`.
    activity: list[float] = field(default_factory=list)
    #: This face's area as a fraction of the largest face in the same frame,
    #: and how near the frame's centre it sat. Both are captured at ingest
    #: because they depend on the *other* faces in that frame, which nothing
    #: downstream still has. Salience is blended from these plus a smoothed
    #: activity, after the whole video has been seen.
    size_scores: list[float] = field(default_factory=list)
    centre_scores: list[float] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return max(0.0, self.last_t - self.first_t)

    @property
    def latest(self) -> Detection | None:
        return self.observations[-1][1] if self.observations else None


@dataclass(frozen=True, slots=True)
class PersonSummary:
    """What became of one track, for the report rather than the camera."""

    speaker_id: str
    first_t: float
    last_t: float
    samples: int
    mean_confidence: float
    mean_activity: float
    #: Longest run of sampled frames the track survived without a detection —
    #: an occlusion, a head turn, or the detector simply missing.
    longest_gap_s: float = 0.0

    @property
    def duration_s(self) -> float:
        return max(0.0, self.last_t - self.first_t)

    def to_dict(self) -> dict[str, Any]:
        return {
            "speaker_id": self.speaker_id,
            "first_t": round(self.first_t, 3),
            "last_t": round(self.last_t, 3),
            "duration_s": round(self.duration_s, 3),
            "samples": self.samples,
            "mean_confidence": round(self.mean_confidence, 4),
            "mean_activity": round(self.mean_activity, 4),
            "longest_gap_s": round(self.longest_gap_s, 3),
        }


@dataclass(frozen=True, slots=True)
class DetectorInfo:
    """Which detector ran, and on what."""

    name: str
    version: str = ""
    model: str = ""
    #: What the detector actually executed on — never what was requested. A
    #: run that asked for CUDA and silently got CPU is a run whose timings
    #: mean something different, and the difference has to be visible.
    device: str = "cpu"
    #: Set when a device was asked for and could not be used, with the reason.
    device_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "version": self.version, "model": self.model,
            "device": self.device, "device_note": self.device_note,
        }


@dataclass(frozen=True, slots=True)
class Availability:
    """Whether the detector can run, and if not, what is missing."""

    ready: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ready": self.ready, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class FaceTrackResult:
    """The engine's output: a `SpeakerTrack` plus everything about how it went.

    The track alone is not enough to act on. "No samples" can mean the video
    has no faces in it, that the detector could not load, or that every
    detection fell below threshold — three situations with three different
    repairs, and a bare empty track tells them apart not at all. `fallback`
    carries which one, and it is the empty string when nothing went wrong.
    """

    #: `gameplay.SpeakerTrack`. Typed loosely to keep the import one-way.
    track: Any
    people: tuple[PersonSummary, ...] = ()
    detector: DetectorInfo | None = None
    frames_sampled: int = 0
    frames_with_face: int = 0
    max_simultaneous: int = 0
    sample_fps: float = 0.0
    source_width: int = 0
    source_height: int = 0
    duration_s: float = 0.0
    elapsed_ms: int = 0
    #: Why the track is empty or degraded. Empty string means it is neither.
    fallback: str = ""
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.fallback and bool(getattr(self.track, "samples", ()))

    @property
    def detection_rate(self) -> float:
        if not self.frames_sampled:
            return 0.0
        return self.frames_with_face / self.frames_sampled

    @property
    def speaker_ids(self) -> tuple[str, ...]:
        return tuple(p.speaker_id for p in self.people)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": {"width": self.source_width, "height": self.source_height,
                       "duration_s": round(self.duration_s, 3)},
            "detector": self.detector.to_dict() if self.detector else None,
            "sample_fps": round(self.sample_fps, 3),
            "frames_sampled": self.frames_sampled,
            "frames_with_face": self.frames_with_face,
            "detection_rate": round(self.detection_rate, 4),
            "max_simultaneous": self.max_simultaneous,
            "people": [p.to_dict() for p in self.people],
            "samples": len(getattr(self.track, "samples", ())),
            "fallback": self.fallback,
            "notes": list(self.notes),
        }


def iou(a: Box, b: Box) -> float:
    """Intersection over union of two boxes.

    Zero for boxes that do not overlap, which is most pairs in a two-person
    frame and is exactly why the tracker gates on centroid distance as well:
    at ten samples a second a head can move further than its own width, and an
    IoU-only matcher drops the track every time somebody leans.
    """

    left = max(a.x, b.x)
    top = max(a.y, b.y)
    right = min(a.x + a.width, b.x + b.width)
    bottom = min(a.y + a.height, b.y + b.height)
    if right <= left or bottom <= top:
        return 0.0
    overlap = (right - left) * (bottom - top)
    union = a.area + b.area - overlap
    return overlap / union if union > 0 else 0.0
