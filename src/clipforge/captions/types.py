"""Core types for caption generation.

A caption track is a list of cues; a cue is one or two lines; a line is a list
of words with their own timings. Word-level timing is the whole point — it is
what makes karaoke highlighting, per-word animation, and precise emoji
placement possible, and it is why the engine refuses to work from
sentence-level subtitles.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Sequence


class Language(str, enum.Enum):
    ENGLISH = "en"
    DUTCH = "nl"
    GERMAN = "de"
    FRENCH = "fr"
    SPANISH = "es"


class Animation(str, enum.Enum):
    """How words arrive on screen."""

    NONE = "none"
    POP = "pop"                # scale overshoot on activation — the TikTok default
    KARAOKE_FILL = "karaoke"   # colour sweep, no motion
    BOUNCE = "bounce"          # vertical kick
    TYPEWRITER = "typewriter"  # words appear one by one and stay
    SLIDE_UP = "slide_up"      # rise into place with a fade


class CaseTransform(str, enum.Enum):
    NONE = "none"
    UPPER = "upper"
    LOWER = "lower"
    TITLE = "title"


@dataclass(frozen=True, slots=True)
class TimedWord:
    """One word with ASR timing. The engine's required input."""

    text: str
    start_ms: int
    end_ms: int
    speaker: str = "SPEAKER_00"

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


@dataclass(frozen=True, slots=True)
class Keyframe:
    """One animation state at a point in time, relative to the cue start."""

    t_ms: int
    scale: float = 1.0
    opacity: float = 1.0
    offset_y: float = 0.0   # in em, relative to the baseline
    color: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "t_ms": self.t_ms,
            "scale": round(self.scale, 4),
            "opacity": round(self.opacity, 4),
            "offset_y": round(self.offset_y, 4),
        }
        if self.color:
            out["color"] = self.color
        return out


@dataclass(slots=True)
class CaptionWord:
    """A word as it will be drawn."""

    text: str
    start_ms: int
    end_ms: int
    speaker: str = "SPEAKER_00"
    is_emoji: bool = False
    emphasis: bool = False
    color: str | None = None
    keyframes: tuple[Keyframe, ...] = ()
    width_em: float = 0.0

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "text": self.text,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "width_em": round(self.width_em, 4),
        }
        if self.speaker != "SPEAKER_00":
            out["speaker"] = self.speaker
        if self.is_emoji:
            out["is_emoji"] = True
        if self.emphasis:
            out["emphasis"] = True
        if self.color:
            out["color"] = self.color
        if self.keyframes:
            out["keyframes"] = [k.to_dict() for k in self.keyframes]
        return out


@dataclass(slots=True)
class CaptionLine:
    words: list[CaptionWord]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def width_em(self) -> float:
        # Words plus the spaces between them.
        return sum(w.width_em for w in self.words) + 0.28 * max(0, len(self.words) - 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "width_em": round(self.width_em, 4),
            "words": [w.to_dict() for w in self.words],
        }


@dataclass(slots=True)
class CaptionCue:
    """One on-screen caption: up to a couple of lines, shown as a unit."""

    index: int
    start_ms: int
    end_ms: int
    lines: list[CaptionLine]
    speaker: str = "SPEAKER_00"
    font_scale: float = 1.0
    shrunk: bool = False  # True when a long word forced the cue smaller

    @property
    def words(self) -> list[CaptionWord]:
        return [w for line in self.lines for w in line.words]

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "index": self.index,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "speaker": self.speaker,
            "lines": [line.to_dict() for line in self.lines],
        }
        if self.font_scale != 1.0:
            out["font_scale"] = round(self.font_scale, 4)
        if self.shrunk:
            out["shrunk_to_fit"] = True
        return out


@dataclass(frozen=True, slots=True)
class Box:
    """The rectangle captions must stay inside, in output pixels.

    Comes from the layout planner — `clipforge.stream.layout` produces exactly
    this from a destination's safe zones, so captions never land under the
    TikTok right rail or the Reels bottom chrome.
    """

    x: int
    y: int
    width: int
    height: int

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


DEFAULT_BOX = Box(x=64, y=1180, width=952, height=340)


@dataclass(slots=True)
class CaptionTrack:
    """The complete deliverable for one clip."""

    language: Language
    style_name: str
    box: Box
    cues: list[CaptionCue]
    speaker_colors: dict[str, str] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> int:
        return self.cues[-1].end_ms if self.cues else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language.value,
            "style": self.style_name,
            "box": self.box.to_dict(),
            "speaker_colors": self.speaker_colors,
            "stats": self.stats,
            "cues": [c.to_dict() for c in self.cues],
        }


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def coerce_words(raw: Sequence[Any]) -> list[TimedWord]:
    """Accept `TimedWord`, the viral engine's `Word`, or plain dicts.

    The transcript pipeline and the caption engine were written separately;
    normalising here means neither has to know about the other's types.
    """
    out: list[TimedWord] = []
    for item in raw:
        if isinstance(item, TimedWord):
            out.append(item)
        elif isinstance(item, dict):
            out.append(
                TimedWord(
                    text=str(item["text"]),
                    start_ms=int(item["start_ms"]),
                    end_ms=int(item["end_ms"]),
                    speaker=str(item.get("speaker", "SPEAKER_00")),
                )
            )
        else:
            out.append(
                TimedWord(
                    text=str(getattr(item, "text")),
                    start_ms=int(getattr(item, "start_ms")),
                    end_ms=int(getattr(item, "end_ms")),
                    speaker=str(getattr(item, "speaker", "SPEAKER_00")),
                )
            )
    return sorted(out, key=lambda w: w.start_ms)
