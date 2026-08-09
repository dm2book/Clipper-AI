"""Core types for gameplay-background composition.

The output of this engine is a **render plan**, not pixels: a deterministic,
hashable description of every crop, panel, scale and timing decision, plus the
ffmpeg filtergraph that executes it. That matches the rest of the system — the
architecture's edit decision list is cached on exactly this kind of spec — and
it keeps the engine testable without a video toolchain in the test suite.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Sequence

#: The only output format this engine emits. Both numbers are load-bearing:
#: 1080x1920 is the native canvas for all three destinations, and 60fps is
#: what makes the gameplay panel read as smooth rather than as a slideshow.
OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920
OUTPUT_FPS = 60


class Game(str, enum.Enum):
    """The gameplay sources the engine knows how to compose."""

    SUBWAY_SURFERS = "subway_surfers"
    MINECRAFT_PARKOUR = "minecraft_parkour"
    GTA_DRIVING = "gta_driving"
    ROCKET_LEAGUE = "rocket_league"
    SATISFYING = "satisfying"


class LayoutStyle(str, enum.Enum):
    """How the speaker and the gameplay share the 1080x1920 canvas."""

    SPLIT = "split"                    # speaker above, gameplay below
    SPEAKER_DOMINANT = "speaker_dominant"   # 72/28 — for dense talking
    GAMEPLAY_DOMINANT = "gameplay_dominant"  # 40/60 — for thin talking
    INSET = "inset"                    # full-bleed gameplay, speaker as a PIP
    SPEAKER_ONLY = "speaker_only"      # no gameplay at all


class Motion(str, enum.Enum):
    """What the virtual camera did at a keyframe."""

    HOLD = "hold"    # deadband absorbed the movement; camera is still
    PAN = "pan"      # smooth follow
    CUT = "cut"      # instantaneous jump, e.g. to a different speaker


@dataclass(frozen=True, slots=True)
class Box:
    """An axis-aligned rectangle in source-frame pixels."""

    x: float
    y: float
    width: float
    height: float

    @property
    def cx(self) -> float:
        return self.x + self.width / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.height / 2.0

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    def to_dict(self) -> dict[str, float]:
        return {
            "x": round(self.x, 2),
            "y": round(self.y, 2),
            "width": round(self.width, 2),
            "height": round(self.height, 2),
        }


@dataclass(frozen=True, slots=True)
class FaceSample:
    """One detection from an upstream face or speaker tracker.

    `speaker_id` is what makes multi-person clips work: two people in frame
    produce two tracks, and the camera cuts between them on turn-taking rather
    than framing a wide two-shot in which neither face is legible at phone
    size.

    `confidence` is used for gap handling, not for weighting. A detector that
    loses the face for four frames should not make the camera drift to centre
    and back.
    """

    t: float                # seconds from clip start
    box: Box
    confidence: float = 1.0
    speaker_id: str = "main"


@dataclass(frozen=True, slots=True)
class SpeakerTrack:
    """Face detections over the life of a clip, from an upstream detector.

    The engine does no computer vision. It takes a track and solves the part
    that actually decides whether the output looks professional: turning noisy,
    gappy, low-rate detections into a camera path that a human would accept.
    """

    samples: tuple[FaceSample, ...] = ()
    source_width: int = 1920
    source_height: int = 1080
    detector_fps: float = 10.0

    def __post_init__(self) -> None:
        if self.samples:
            ordered = tuple(sorted(self.samples, key=lambda s: s.t))
            object.__setattr__(self, "samples", ordered)

    @property
    def is_empty(self) -> bool:
        return not self.samples

    @property
    def speaker_ids(self) -> tuple[str, ...]:
        seen: list[str] = []
        for sample in self.samples:
            if sample.speaker_id not in seen:
                seen.append(sample.speaker_id)
        return tuple(seen)


@dataclass(frozen=True, slots=True)
class GameplayAsset:
    """A gameplay recording available to composite behind a speaker.

    `loop_points` matter more than they look. A gameplay bed shorter than the
    clip has to repeat, and an arbitrary cut back to zero is the single most
    obvious tell that a video was machine-assembled. The engine cannot find
    visually continuous loop points without decoding frames, so it takes them
    as metadata and is explicit in the plan when it had none to work with.
    """

    asset_id: str
    game: Game
    duration_s: float
    width: int = 1920
    height: int = 1080
    fps: float = 60.0
    path: str = ""
    #: Timestamps where a cut back to `loop_points[0]` is visually continuous.
    loop_points: tuple[float, ...] = ()
    #: Seconds at the head to skip — menus, fades, an intro card.
    lead_in_s: float = 0.0
    has_audio: bool = True

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0

    @property
    def is_vertical(self) -> bool:
        return self.height > self.width


@dataclass(frozen=True, slots=True)
class CropKeyframe:
    """The speaker crop window at one instant.

    Width and height are constant for the whole clip by design — see
    `camera.plan_crop_size`. Only `x` and `y` vary, which is both what good
    camera work looks like and what ffmpeg can actually retarget at runtime.
    """

    t: float
    x: int
    y: int
    motion: Motion = Motion.PAN

    def to_dict(self) -> dict[str, Any]:
        return {"t": round(self.t, 4), "x": self.x, "y": self.y,
                "motion": self.motion.value}


@dataclass(frozen=True, slots=True)
class CameraPath:
    """The full speaker camera solution for a clip."""

    width: int
    height: int
    keyframes: tuple[CropKeyframe, ...]
    #: Where the camera cut rather than panned, in seconds.
    cuts: tuple[float, ...] = ()
    #: Fraction of the clip the camera spent perfectly still. High is good:
    #: a camera that is always moving is a camera nobody asked for.
    hold_ratio: float = 0.0
    tracking: str = "tracked"   # "tracked" | "static" | "interpolated"
    notes: tuple[str, ...] = ()

    def at(self, t: float) -> CropKeyframe:
        """The keyframe in effect at time `t`."""
        current = self.keyframes[0]
        for keyframe in self.keyframes:
            if keyframe.t > t:
                break
            current = keyframe
        return current

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "tracking": self.tracking,
            "cuts": [round(c, 3) for c in self.cuts],
            "hold_ratio": round(self.hold_ratio, 3),
            "keyframes": [k.to_dict() for k in self.keyframes],
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class Panel:
    """One destination rectangle on the 1080x1920 canvas."""

    name: str                 # "speaker" | "gameplay"
    x: int
    y: int
    width: int
    height: int
    #: Source rectangle feeding this panel. For the speaker panel only the
    #: size is fixed — the position comes from the camera path, per frame.
    source_x: int = 0
    source_y: int = 0
    source_width: int = 0
    source_height: int = 0
    corner_radius: int = 0
    z: int = 0
    #: "cover" crops to fill the panel; "fit" scales the whole frame in and
    #: fills the remainder, for footage that cannot survive a crop.
    scale_mode: str = "cover"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name, "x": self.x, "y": self.y,
            "width": self.width, "height": self.height, "z": self.z,
            "scale_mode": self.scale_mode,
        }
        if self.source_width:
            out["source"] = {
                "x": self.source_x, "y": self.source_y,
                "width": self.source_width, "height": self.source_height,
            }
        if self.corner_radius:
            out["corner_radius"] = self.corner_radius
        return out


@dataclass(frozen=True, slots=True)
class LoopSegment:
    """One span of gameplay laid onto the timeline."""

    out_start: float     # position on the output timeline
    out_end: float
    in_start: float      # position within the gameplay asset
    seam: str = "none"   # "none" | "clean" | "visible"

    @property
    def duration(self) -> float:
        return self.out_end - self.out_start

    def to_dict(self) -> dict[str, Any]:
        return {
            "out_start": round(self.out_start, 3),
            "out_end": round(self.out_end, 3),
            "in_start": round(self.in_start, 3),
            "seam": self.seam,
        }


@dataclass(frozen=True, slots=True)
class GameplayTiming:
    """How the gameplay bed is laid against the clip's duration."""

    segments: tuple[LoopSegment, ...]
    asset_id: str
    fps_conform: str = "duplicate"   # "native" | "duplicate" | "interpolate"
    audio: str = "muted"
    notes: tuple[str, ...] = ()

    @property
    def loops(self) -> int:
        return max(0, len(self.segments) - 1)

    @property
    def visible_seams(self) -> int:
        return sum(1 for s in self.segments if s.seam == "visible")

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "fps_conform": self.fps_conform,
            "audio": self.audio,
            "loops": self.loops,
            "visible_seams": self.visible_seams,
            "segments": [s.to_dict() for s in self.segments],
            "notes": list(self.notes),
        }


@dataclass(slots=True)
class GameplayPlan:
    """Everything the renderer needs to produce one 1080x1920 60fps clip."""

    width: int
    height: int
    fps: int
    duration_s: float
    style: LayoutStyle
    panels: tuple[Panel, ...]
    camera: CameraPath
    timing: GameplayTiming | None
    caption_zone: tuple[int, int, int, int] = (0, 0, 0, 0)
    game: Game | None = None
    warnings: tuple[str, ...] = ()
    stats: dict[str, Any] = field(default_factory=dict)

    #: How long the solve took. Deliberately **not** part of `to_dict()`: the
    #: serialised plan is the render layer's cache key, and a wall-clock
    #: measurement inside it means two identical plans hash differently and
    #: the cache never hits. Measurement belongs in logs, not in the spec.
    elapsed_ms: int = 0

    def panel(self, name: str) -> Panel | None:
        return next((p for p in self.panels if p.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": {"width": self.width, "height": self.height,
                       "fps": self.fps, "duration_s": round(self.duration_s, 3)},
            "style": self.style.value,
            "game": self.game.value if self.game else None,
            "panels": [p.to_dict() for p in self.panels],
            "camera": self.camera.to_dict(),
            "timing": self.timing.to_dict() if self.timing else None,
            "caption_zone": {
                "x": self.caption_zone[0], "y": self.caption_zone[1],
                "width": self.caption_zone[2], "height": self.caption_zone[3],
            },
            "warnings": list(self.warnings),
            "stats": self.stats,
        }


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def even(value: float) -> int:
    """Round to an even integer.

    Not fussiness: yuv420p subsamples chroma 2x2, so odd crop or scale
    dimensions either fail outright in ffmpeg or shift the chroma plane by
    half a pixel, which shows up as a coloured fringe along one edge.
    """
    return int(round(value / 2.0)) * 2
