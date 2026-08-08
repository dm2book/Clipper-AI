"""Signal taxonomy and the weights that turn signals into scores.

This module is the engine's opinion about what makes short-form content
perform. Everything here is versioned (`WEIGHTS_VERSION`) and persisted
alongside each moment, so when the ranker is later retrained on real platform
performance we can tell which weight set produced which outcome.

Nothing in this file does I/O or computation — it is a declarative table that
`scoring.py` consumes.
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import Signal

WEIGHTS_VERSION = "heuristic-v1"


@dataclass(frozen=True, slots=True)
class Affinity:
    """How strongly one signal predicts each viewer behaviour.

    Values are 0..1 and are *not* probabilities — they are relative weights
    within a column, calibrated so that the strongest driver of each behaviour
    sits near 0.9.
    """

    retention: float
    engagement: float
    comment: float
    share: float


# --- The affinity matrix -----------------------------------------------------
#
# Read this table by column, not by row. Each column answers one question:
#
#   retention  — will they watch to the end?  Driven by open loops and tension.
#   engagement — will they react at all?      Driven by emotional amplitude.
#   comment    — will they type something?    Driven by disagreement and stance.
#   share      — will they send it to someone? Driven by utility and humour.
#
# The comment column is the one people usually get wrong: comments come from
# *disagreement*, not from quality. A brilliant, uncontroversial explanation
# gets saved and shared; it does not get argued about.

AFFINITY: dict[Signal, Affinity] = {
    # Someone stakes out a position others will contest. The single strongest
    # comment driver there is — people type to disagree far more than to agree.
    Signal.CONTROVERSY: Affinity(retention=0.35, engagement=0.70, comment=0.95, share=0.45),
    # Raw emotional amplitude — shouting, laughing, breaking down, going quiet.
    # The strongest engagement driver, and a solid retention one: amplitude in
    # the first seconds is what stops a scroll.
    Signal.EMOTIONAL_SPIKE: Affinity(retention=0.75, engagement=0.90, comment=0.40, share=0.55),
    # Concrete figures. Shares well because a number is forwardable on its own
    # ("he made $40k in a week") in a way a nuanced argument is not.
    Signal.MONEY: Affinity(retention=0.60, engagement=0.55, comment=0.45, share=0.80),
    # Humour is the highest-share category in short form, full stop. People
    # send jokes to specific friends; that is a share, not a like.
    Signal.FUNNY: Affinity(retention=0.70, engagement=0.80, comment=0.35, share=0.90),
    # Heat between speakers. Retains (conflict is a natural open loop) and
    # draws comments as viewers pick a side.
    Signal.ARGUMENT: Affinity(retention=0.80, engagement=0.75, comment=0.90, share=0.50),
    # Reasoned disagreement rather than heat. Comments well, but retains less
    # than an argument — a measured exchange has no rising tension.
    Signal.DEBATE: Affinity(retention=0.55, engagement=0.50, comment=0.80, share=0.45),
    # Admitted failure. Retains strongly: an admission of loss opens a loop the
    # viewer wants closed. Underrated by most systems.
    Signal.FAILURE: Affinity(retention=0.75, engagement=0.70, comment=0.55, share=0.60),
    # Achievement. Weaker than failure across the board — success is less
    # surprising and less relatable, and reads as bragging when unearned.
    Signal.SUCCESS: Affinity(retention=0.55, engagement=0.55, comment=0.35, share=0.70),
    # Insider information framed as revelation. The best retention driver we
    # have — "nobody talks about this" is a curiosity gap in one phrase — and
    # near-top for shares, because passing on a secret is social currency.
    Signal.SECRET: Affinity(retention=0.85, engagement=0.75, comment=0.40, share=0.85),
    # Generalisable advice. Shares extremely well (saving and forwarding is the
    # whole point) but rarely provokes comment: nobody argues with good advice.
    Signal.LESSON: Affinity(retention=0.50, engagement=0.45, comment=0.30, share=0.90),
}


# --- Virality composition ----------------------------------------------------
#
# Virality is deliberately NOT the mean of the other four. Short-form ranking
# systems weight watch-through most heavily, then amplification (shares), then
# lightweight engagement, then comments. A clip that everyone argues about but
# nobody finishes does not travel; a clip everyone finishes and forwards does.

VIRALITY_MIX: dict[str, float] = {
    "retention": 0.40,
    "share": 0.25,
    "engagement": 0.20,
    "comment": 0.15,
}


# --- Structural modifiers ----------------------------------------------------
#
# Applied multiplicatively after the signal matrix. These encode form rather
# than content: the same words in a badly-cut window perform worse.

@dataclass(frozen=True, slots=True)
class Modifier:
    """A structural feature's influence on one score.

    `weight` is how much of the score is placed at the feature's mercy. At
    weight 0.5, a feature value of 0 halves the score and a value of 1 leaves
    it untouched — the multiplier is `(1 - weight) + weight * value`.
    """

    feature: str
    weight: float


MODIFIERS: dict[str, tuple[Modifier, ...]] = {
    # A viewer who does not understand the opening cannot be retained by it,
    # and a clip that runs past the platform sweet spot bleeds watch-through.
    "retention": (
        Modifier("hook_strength", 0.45),
        Modifier("standalone", 0.40),
        Modifier("duration_fit", 0.35),
        Modifier("payoff", 0.25),
    ),
    "engagement": (
        Modifier("hook_strength", 0.25),
        Modifier("duration_fit", 0.25),
    ),
    # An explicit question to the audience is the highest-leverage comment
    # lever available and costs nothing to detect.
    "comment": (
        Modifier("audience_question", 0.30),
        Modifier("standalone", 0.25),
        Modifier("duration_fit", 0.15),
    ),
    # Nobody forwards a clip the recipient will not understand.
    "share": (
        Modifier("standalone", 0.45),
        Modifier("duration_fit", 0.30),
        Modifier("payoff", 0.20),
    ),
}
#
# `duration_fit` appears in all four rows deliberately. Signal strength
# aggregates across a window, so a long window accumulates more evidence and
# would otherwise always outrank a tighter cut of the same moment. Length is a
# platform constraint that costs performance on every axis, not just
# watch-through, so it has to push back everywhere.


# --- Duration model ----------------------------------------------------------
#
# Per-platform caps live in the render layer; this is the *performance* curve,
# which is much narrower than what the platforms technically allow.

IDEAL_DURATION_S = (21.0, 34.0)
MIN_DURATION_S = 8.0
MAX_DURATION_S = 75.0


def duration_fit(duration_s: float) -> float:
    """Score 0..1 for how well a clip length matches short-form performance.

    Flat 1.0 across the sweet spot, tapering either side, hard zero outside the
    absolute bounds. Deliberately asymmetric: too short is worse than too long,
    because a clip under ~8s reads as an incomplete thought and gets skipped
    before the algorithm ever measures it.
    """
    lo, hi = IDEAL_DURATION_S
    if duration_s < MIN_DURATION_S or duration_s > MAX_DURATION_S:
        return 0.0
    if lo <= duration_s <= hi:
        return 1.0
    if duration_s < lo:
        # Steeper taper below the sweet spot.
        return max(0.0, (duration_s - MIN_DURATION_S) / (lo - MIN_DURATION_S)) ** 1.4
    return max(0.0, (MAX_DURATION_S - duration_s) / (MAX_DURATION_S - hi)) ** 0.8


# --- LLM blending ------------------------------------------------------------
#
# How much the semantic judgement displaces the heuristic one when the LLM tier
# runs. Heuristics are good at *locating* moments and bad at judging whether
# they stand alone; the LLM is the reverse. The blend reflects that.

LLM_BLEND = {
    "hook_strength": 0.75,   # LLM is far better at this than keyword matching
    "standalone": 0.85,      # heuristics can barely approximate comprehension
    "payoff": 0.60,
    "quotability": 1.00,     # no heuristic equivalent — LLM-only feature
}
