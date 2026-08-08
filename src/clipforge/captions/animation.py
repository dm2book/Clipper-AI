"""Per-word animation keyframes.

Every animation is expressed as keyframes on four properties — scale, opacity,
vertical offset, colour — timed relative to the cue start. That keeps the
renderer dumb: it interpolates between keyframes and knows nothing about
animation styles, so a new style is data rather than render code.

Timings are tuned for 30fps output. The overshoot durations below are short on
purpose: at phone scale a 200ms pop reads as sluggish, and anything under about
60ms is invisible.
"""

from __future__ import annotations

from typing import Sequence

from .styles import CaptionStyle
from .types import Animation, CaptionWord, Keyframe

POP_RISE_MS = 90
POP_SETTLE_MS = 110
POP_OVERSHOOT = 1.18

BOUNCE_UP_MS = 90
BOUNCE_SETTLE_MS = 130
BOUNCE_HEIGHT_EM = 0.16

SLIDE_MS = 140
SLIDE_DISTANCE_EM = 0.35

FADE_IN_MS = 70


def _color_for(word: CaptionWord, style: CaptionStyle, active: bool) -> str | None:
    """Colour at a given moment, honouring speaker accent over style accent."""
    if active:
        return word.color or style.active_color
    return None


def keyframes_for(
    word: CaptionWord,
    cue_start_ms: int,
    cue_end_ms: int,
    style: CaptionStyle,
    is_last: bool = False,
) -> tuple[Keyframe, ...]:
    """Keyframes for one word, relative to its cue's start."""
    enter = max(0, word.start_ms - cue_start_ms)
    leave = max(enter, word.end_ms - cue_start_ms)
    cue_len = max(1, cue_end_ms - cue_start_ms)
    base = style.color
    active = _color_for(word, style, active=True)
    spoken = style.spoken_color or base

    animation = style.animation

    if animation is Animation.NONE:
        return (
            Keyframe(t_ms=0, color=base),
            Keyframe(t_ms=enter, color=active),
            Keyframe(t_ms=leave, color=spoken),
        )

    if animation is Animation.KARAOKE_FILL:
        # The whole line is visible from the start; only colour moves. Easiest
        # to read, because the viewer has the surrounding words for context.
        return (
            Keyframe(t_ms=0, opacity=1.0, color=base),
            Keyframe(t_ms=max(0, enter - 20), color=base),
            Keyframe(t_ms=enter, color=active),
            Keyframe(t_ms=leave, color=spoken),
            Keyframe(t_ms=cue_len, color=spoken),
        )

    if animation is Animation.POP:
        return (
            Keyframe(t_ms=max(0, enter - 40), scale=0.82, opacity=0.0, color=base),
            Keyframe(t_ms=enter, scale=1.0, opacity=1.0, color=active),
            Keyframe(t_ms=enter + POP_RISE_MS, scale=POP_OVERSHOOT, color=active),
            Keyframe(t_ms=enter + POP_RISE_MS + POP_SETTLE_MS, scale=1.0, color=active),
            Keyframe(t_ms=leave, scale=1.0, color=spoken),
        )

    if animation is Animation.BOUNCE:
        return (
            Keyframe(t_ms=max(0, enter - 40), opacity=0.0, offset_y=0.0, color=base),
            Keyframe(t_ms=enter, opacity=1.0, offset_y=0.0, color=active),
            Keyframe(t_ms=enter + BOUNCE_UP_MS, offset_y=-BOUNCE_HEIGHT_EM, color=active),
            Keyframe(
                t_ms=enter + BOUNCE_UP_MS + BOUNCE_SETTLE_MS,
                offset_y=0.0,
                color=active,
            ),
            Keyframe(t_ms=leave, offset_y=0.0, color=spoken),
        )

    if animation is Animation.SLIDE_UP:
        return (
            Keyframe(
                t_ms=max(0, enter - SLIDE_MS),
                opacity=0.0,
                offset_y=SLIDE_DISTANCE_EM,
                color=base,
            ),
            Keyframe(t_ms=enter, opacity=1.0, offset_y=0.0, color=active),
            Keyframe(t_ms=leave, opacity=1.0, offset_y=0.0, color=spoken),
        )

    if animation is Animation.TYPEWRITER:
        # Words appear and stay. No exit state — the whole cue clears at once,
        # which is what makes it read as typed rather than animated.
        return (
            Keyframe(t_ms=max(0, enter - 1), opacity=0.0, color=base),
            Keyframe(t_ms=enter, opacity=1.0, color=active),
            Keyframe(t_ms=min(cue_len, leave + FADE_IN_MS), opacity=1.0, color=spoken),
        )

    return (Keyframe(t_ms=0, color=base),)


def animate_cue(
    words: Sequence[CaptionWord],
    cue_start_ms: int,
    cue_end_ms: int,
    style: CaptionStyle,
) -> None:
    """Attach keyframes to every word in a cue, in place."""
    last_index = len(words) - 1
    for i, word in enumerate(words):
        if word.is_emoji:
            # Emoji do not karaoke — they punctuate. A single pop on arrival
            # reads as intentional; colour-cycling an emoji does not.
            enter = max(0, word.start_ms - cue_start_ms)
            word.keyframes = (
                Keyframe(t_ms=max(0, enter - 40), scale=0.6, opacity=0.0),
                Keyframe(t_ms=enter, scale=1.0, opacity=1.0),
                Keyframe(t_ms=enter + POP_RISE_MS, scale=1.22),
                Keyframe(t_ms=enter + POP_RISE_MS + POP_SETTLE_MS, scale=1.0),
            )
            continue

        word.keyframes = keyframes_for(
            word, cue_start_ms, cue_end_ms, style, is_last=(i == last_index)
        )
