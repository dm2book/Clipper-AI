"""Scoring for stream clips.

Different economics from long-form. A podcast clip travels on utility and
shareability; a stream clip travels on spectacle and the crowd reacting to it.
The score set reflects that:

  hype       — is this loud, fast, and immediately exciting?
  retention  — will someone watch to the end?
  clarity    — does it make sense without knowing the stream, the game, or the
               running joke? The most common failure mode in stream clipping.
  virality   — weighted composite of the three.

Weights are versioned and persisted with every clip, same contract as the
viral engine, so a learned ranker can later train against real outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .types import Anchor, Scores, StreamSignal, clamp, saturating_sum

WEIGHTS_VERSION = "stream-heuristic-v1"


@dataclass(frozen=True, slots=True)
class Affinity:
    hype: float
    retention: float
    clarity: float


# Read by column. `clarity` is the interesting one: a reaction clip is exciting
# and almost meaningless out of context, while a donation is dull and perfectly
# self-explanatory. Most systems conflate the two and ship confusing clips.
AFFINITY: dict[StreamSignal, Affinity] = {
    # Rage is the most reliably clippable stream content there is: loud,
    # immediately legible, and it needs no knowledge of the game.
    StreamSignal.RAGE: Affinity(hype=0.90, retention=0.80, clarity=0.75),
    # Laughter travels, and a genuine laugh reads without setup.
    StreamSignal.FUNNY: Affinity(hype=0.85, retention=0.85, clarity=0.80),
    # Wins are peak hype but need slightly more context — the viewer has to
    # understand what was at stake.
    StreamSignal.WIN: Affinity(hype=0.90, retention=0.75, clarity=0.70),
    # Fails retain better than wins. Schadenfreude is a stronger hook than
    # admiration, and a fail is legible even when the game is not.
    StreamSignal.FAIL: Affinity(hype=0.75, retention=0.80, clarity=0.75),
    # A pure reaction shot is exciting and frequently incomprehensible: the
    # viewer sees someone react to something off-screen.
    StreamSignal.REACTION: Affinity(hype=0.70, retention=0.70, clarity=0.50),
    # Donations are clear and low-energy. They clip well only when the amount
    # or the message carries the moment.
    StreamSignal.DONATION: Affinity(hype=0.45, retention=0.50, clarity=0.85),
    # Arguments hold attention longer than anything else on this list, but
    # drop the viewer into the middle of a disagreement they have no stake in.
    StreamSignal.ARGUMENT: Affinity(hype=0.65, retention=0.85, clarity=0.55),
    # Emotional moments retain strongly and are the least hype-driven category.
    StreamSignal.EMOTIONAL: Affinity(hype=0.55, retention=0.80, clarity=0.70),
}

VIRALITY_MIX: dict[str, float] = {"hype": 0.40, "retention": 0.35, "clarity": 0.25}

BEHAVIOURS = ("hype", "retention", "clarity")


# --- Duration preference -----------------------------------------------------
#
# All four lengths are always produced; this decides which one is the *best*
# cut of a given moment, which is what the product surfaces. A headshot needs
# fifteen seconds. An argument needs a minute, and cutting it to fifteen
# produces a clip where two people are inexplicably annoyed.

_FAST_SIGNALS = {
    StreamSignal.WIN,
    StreamSignal.FAIL,
    StreamSignal.FUNNY,
    StreamSignal.REACTION,
}
_SLOW_SIGNALS = {
    StreamSignal.ARGUMENT,
    StreamSignal.EMOTIONAL,
    StreamSignal.DONATION,
}

_FAST_CURVE = {15: 1.00, 30: 0.95, 45: 0.75, 60: 0.60}
_SLOW_CURVE = {15: 0.55, 30: 0.85, 45: 1.00, 60: 0.95}
_NEUTRAL_CURVE = {15: 0.85, 30: 1.00, 45: 0.90, 60: 0.75}


def duration_preference(dominant: StreamSignal | None, duration_s: int) -> float:
    """How well a clip length suits the kind of moment it contains."""
    if dominant in _FAST_SIGNALS:
        curve = _FAST_CURVE
    elif dominant in _SLOW_SIGNALS:
        curve = _SLOW_CURVE
    else:
        curve = _NEUTRAL_CURVE
    return curve.get(duration_s, 0.7)


def behaviour_scores(signals: Mapping[StreamSignal, float]) -> dict[str, float]:
    """Project signal strengths through the affinity matrix."""
    raw = {behaviour: 0.0 for behaviour in BEHAVIOURS}
    if not signals:
        return raw

    for behaviour in BEHAVIOURS:
        contributions = [
            strength * getattr(AFFINITY[signal], behaviour)
            for signal, strength in signals.items()
            if signal in AFFINITY
        ]
        if not contributions:
            continue
        strongest = max(contributions)
        rest = saturating_sum([c * 0.3 for c in contributions if c != strongest])
        raw[behaviour] = clamp(strongest + (1.0 - strongest) * rest)

    return raw


def clip_features(
    anchor: Anchor,
    duration_s: int,
    start_ms: int,
    end_ms: int,
    session_duration_ms: int,
) -> dict[str, float]:
    """Structural features for one clip variant."""
    span = max(1, end_ms - start_ms)
    anchor_position = (anchor.offset_ms - start_ms) / span

    # The moment should sit in the first half — early enough that the viewer
    # reaches it, late enough that there is context. Penalise both extremes.
    if 0.2 <= anchor_position <= 0.5:
        placement = 1.0
    elif anchor_position < 0.2:
        placement = clamp(anchor_position / 0.2, 0.3, 1.0)
    else:
        placement = clamp(1.0 - (anchor_position - 0.5) * 1.6, 0.2, 1.0)

    chat_multiple = anchor.spike.magnitude if anchor.spike else 0.0

    return {
        "anchor_position": anchor_position,
        "placement": placement,
        "duration_preference": duration_preference(anchor.dominant, duration_s),
        "chat_multiple": chat_multiple,
        "intensity": anchor.intensity,
        "signal_count": float(len([v for v in anchor.signals.values() if v > 0])),
        "truncated_head": 1.0 if start_ms <= 0 else 0.0,
        "truncated_tail": 1.0 if end_ms >= session_duration_ms else 0.0,
    }


def score(
    anchor: Anchor,
    duration_s: int,
    features: Mapping[str, float],
) -> Scores:
    """Score one clip variant."""
    raw = behaviour_scores(anchor.signals)

    placement = clamp(features.get("placement", 1.0))
    preference = clamp(features.get("duration_preference", 1.0))

    # Chat magnitude is blended into hype rather than added to it. Adding
    # saturated the top of the range — most strong moments clamped to 100 and
    # the ranking lost all resolution exactly where it matters most.
    chat_multiple = features.get("chat_multiple", 0.0)
    chat_lift = clamp((chat_multiple - 1.0) / 12.0) if chat_multiple > 1.0 else 0.0
    hype = clamp((0.72 * raw["hype"] + 0.28 * chat_lift) * (0.82 + 0.18 * preference))

    # A moment placed badly is a moment the viewer never reaches.
    retention = clamp(
        raw["retention"] * (0.55 + 0.45 * placement) * (0.7 + 0.3 * preference)
    )

    # Clarity degrades when the window is cut off by the stream boundary: the
    # setup or the payoff is physically missing from the source.
    truncation = 1.0 - 0.25 * (
        features.get("truncated_head", 0.0) + features.get("truncated_tail", 0.0)
    )
    clarity = clamp(raw["clarity"] * clamp(truncation, 0.5, 1.0))

    behaviours = {"hype": hype, "retention": retention, "clarity": clarity}
    virality = clamp(
        sum(behaviours[name] * weight for name, weight in VIRALITY_MIX.items())
    )

    return Scores(
        virality=int(round(virality * 100)),
        hype=int(round(hype * 100)),
        retention=int(round(retention * 100)),
        clarity=int(round(clarity * 100)),
    )


def title_for(anchor: Anchor, evidence: Sequence[str] = ()) -> str:
    """A serviceable title from the signal mix.

    Placeholder until the LLM tier titles stream clips: descriptive rather than
    clickbait, and honest about what the clip contains.
    """
    dominant = anchor.dominant
    if dominant is None:
        return "Stream moment"

    labels = {
        StreamSignal.RAGE: "Streamer loses it",
        StreamSignal.FUNNY: "Chat can't breathe",
        StreamSignal.WIN: "Insane play",
        StreamSignal.FAIL: "It all goes wrong",
        StreamSignal.REACTION: "Wait, what just happened",
        StreamSignal.DONATION: "Big donation lands",
        StreamSignal.ARGUMENT: "This turns into a fight",
        StreamSignal.EMOTIONAL: "A real moment",
    }
    base = labels[dominant]

    secondary = sorted(
        (s for s in anchor.signals if s is not dominant),
        key=lambda s: -anchor.signals[s],
    )
    if not secondary:
        return base

    runner_up = secondary[0]
    strength = anchor.signals[runner_up]

    # A secondary signal only belongs in the title when it is genuinely
    # co-equal, and when it is not a known artifact of the dominant one.
    # OMEGALUL scores both funny and fail; Sadge scores both fail and
    # emotional. Naming the artifact produces titles like "A real moment —
    # disaster", which describe the taxonomy rather than the clip.
    redundant = {
        frozenset({StreamSignal.FUNNY, StreamSignal.FAIL}),
        frozenset({StreamSignal.EMOTIONAL, StreamSignal.FAIL}),
        frozenset({StreamSignal.WIN, StreamSignal.REACTION}),
        frozenset({StreamSignal.RAGE, StreamSignal.FUNNY}),
    }
    if frozenset({dominant, runner_up}) in redundant:
        return base
    if strength < 0.6 or strength < anchor.signals[dominant] * 0.8:
        return base

    extra = {
        StreamSignal.RAGE: "rage",
        StreamSignal.FUNNY: "chat in tears",
        StreamSignal.WIN: "clutch",
        StreamSignal.FAIL: "disaster",
        StreamSignal.REACTION: "pure shock",
        StreamSignal.DONATION: "mid-donation",
        StreamSignal.ARGUMENT: "argument",
        StreamSignal.EMOTIONAL: "emotional",
    }[runner_up]
    return f"{base} — {extra}"
