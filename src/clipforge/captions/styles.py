"""Premium caption style presets.

Each preset is a complete look: typeface, size, case, colours, stroke, shadow,
grouping, and animation. They are declarative — the renderer consumes them, and
because they are hashable the render cache keys on them.

Sizes are in output pixels against a 1080×1920 frame. `max_words` is the real
differentiator between styles: one-word-at-a-time reads as high-energy and
burns screen time, while four-word groups read as editorial.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .types import Animation, CaseTransform, Language

# Per-speaker accent colours, assigned in first-appearance order. Chosen to
# stay distinguishable against a burned-in white body with a black stroke, and
# to remain separable for the most common colour-vision deficiencies —
# red/green pairs are avoided in the first three slots, which is as far as most
# clips get.
SPEAKER_PALETTE: tuple[str, ...] = (
    "#FFE14D",  # amber
    "#4DD2FF",  # cyan
    "#FF7BD5",  # pink
    "#8CFF66",  # lime
    "#FFA24D",  # orange
    "#C9A3FF",  # violet
)


@dataclass(frozen=True, slots=True)
class CaptionStyle:
    """A complete caption look."""

    name: str
    font_family: str
    font_size_px: int
    font_weight: int = 800
    case: CaseTransform = CaseTransform.UPPER
    animation: Animation = Animation.POP

    # Colours
    color: str = "#FFFFFF"
    active_color: str = "#FFE14D"      # the word currently being spoken
    spoken_color: str | None = None    # words already said, when karaoke-filling
    stroke_color: str = "#000000"
    stroke_width_px: int = 10
    shadow_color: str | None = "#00000080"
    shadow_offset_px: int = 6
    background: str | None = None      # optional pill behind the text

    # Grouping
    max_words: int = 4
    max_lines: int = 2
    min_cue_ms: int = 500
    max_cue_ms: int = 2_600

    # Behaviour
    highlight_speaker: bool = True
    emoji_enabled: bool = True
    emoji_threshold: float = 0.55
    line_height: float = 1.16

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "font_family": self.font_family,
            "font_size_px": self.font_size_px,
            "font_weight": self.font_weight,
            "case": self.case.value,
            "animation": self.animation.value,
            "color": self.color,
            "active_color": self.active_color,
            "spoken_color": self.spoken_color,
            "stroke_color": self.stroke_color,
            "stroke_width_px": self.stroke_width_px,
            "shadow_color": self.shadow_color,
            "shadow_offset_px": self.shadow_offset_px,
            "background": self.background,
            "max_words": self.max_words,
            "max_lines": self.max_lines,
            "line_height": self.line_height,
        }


# --- Presets -----------------------------------------------------------------

#: One or two words at a time, huge, with a hard yellow highlight. The
#: highest-energy look and the one most associated with short-form business
#: content. Burns a lot of screen time per word, which is the point — it forces
#: the viewer's eye to track.
PUNCH = CaptionStyle(
    name="punch",
    font_family="Montserrat ExtraBold",
    font_size_px=96,
    font_weight=800,
    case=CaseTransform.UPPER,
    animation=Animation.POP,
    color="#FFFFFF",
    active_color="#FFE14D",
    stroke_color="#000000",
    stroke_width_px=12,
    max_words=2,
    max_lines=1,
    max_cue_ms=1_600,
)

#: Classic karaoke: a full line stays up while the active word fills with
#: colour. Easiest to read and the safest default for dense speech, because the
#: viewer sees the words either side of the one being spoken.
KARAOKE = CaptionStyle(
    name="karaoke",
    font_family="Inter SemiBold",
    font_size_px=68,
    font_weight=600,
    case=CaseTransform.NONE,
    animation=Animation.KARAOKE_FILL,
    color="#FFFFFF",
    active_color="#4DD2FF",
    spoken_color="#B9C6D2",
    stroke_color="#0A0A0A",
    stroke_width_px=8,
    max_words=6,
    max_lines=2,
    max_cue_ms=3_000,
)

#: Bold and bouncy without being shouty. A good general-purpose premium look.
BOUNCE = CaptionStyle(
    name="bounce",
    font_family="Poppins Bold",
    font_size_px=80,
    font_weight=700,
    case=CaseTransform.UPPER,
    animation=Animation.BOUNCE,
    color="#FFFFFF",
    active_color="#8CFF66",
    stroke_color="#111111",
    stroke_width_px=10,
    max_words=3,
    max_lines=2,
)

#: Understated: lowercase, tight, no stroke, soft shadow. Suits interviews and
#: anything where shouting undercuts the content.
MINIMAL = CaptionStyle(
    name="minimal",
    font_family="Inter Medium",
    font_size_px=58,
    font_weight=500,
    case=CaseTransform.LOWER,
    animation=Animation.SLIDE_UP,
    color="#F5F5F5",
    active_color="#FFFFFF",
    spoken_color="#9AA4AE",
    stroke_color="#000000",
    stroke_width_px=0,
    shadow_color="#000000A0",
    shadow_offset_px=4,
    max_words=6,
    max_lines=2,
    emoji_enabled=False,
    highlight_speaker=False,
)

#: Typewriter reveal on a dark pill. Reads as documentary rather than hype.
TYPEWRITER = CaptionStyle(
    name="typewriter",
    font_family="JetBrains Mono Bold",
    font_size_px=54,
    font_weight=700,
    case=CaseTransform.NONE,
    animation=Animation.TYPEWRITER,
    color="#FFFFFF",
    active_color="#FFE14D",
    stroke_color="#000000",
    stroke_width_px=0,
    background="#000000B3",
    max_words=7,
    max_lines=2,
    emoji_enabled=False,
)

PRESETS: dict[str, CaptionStyle] = {
    style.name: style
    for style in (PUNCH, KARAOKE, BOUNCE, MINIMAL, TYPEWRITER)
}

DEFAULT_STYLE = PUNCH


def get(name: str) -> CaptionStyle:
    try:
        return PRESETS[name]
    except KeyError:
        raise ValueError(
            f"unknown caption style {name!r}; available: {', '.join(sorted(PRESETS))}"
        ) from None


def assign_speaker_colors(speakers: list[str], style: CaptionStyle) -> dict[str, str]:
    """Map speakers to accent colours in first-appearance order.

    Stable within a clip, which is what matters: a viewer learns "yellow is the
    host" within a couple of cues, and shuffling colours between cues destroys
    that instantly.
    """
    if not style.highlight_speaker:
        return {}
    return {
        speaker: SPEAKER_PALETTE[i % len(SPEAKER_PALETTE)]
        for i, speaker in enumerate(speakers)
    }


def apply_case(
    text: str, case: CaseTransform, language: Language | None = None
) -> str:
    """Apply the style's case transform.

    German is exempt from lowercasing: noun capitalisation is grammatical
    there, not stylistic. `der hund` reads as an error to a German viewer in a
    way `the dog` does not to an English one, so the `minimal` style falls back
    to the original casing rather than corrupting the text. Uppercase and title
    case are safe in all five languages.
    """
    if case is CaseTransform.UPPER:
        return text.upper()
    if case is CaseTransform.LOWER:
        if language is Language.GERMAN:
            return text
        return text.lower()
    if case is CaseTransform.TITLE:
        return text.title()
    return text
