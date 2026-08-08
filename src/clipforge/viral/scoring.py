"""Score synthesis: signals + structure → the five output scores.

The pipeline is deliberately linear and inspectable. Every score can be traced
back to the signals and features that produced it, which matters for two
reasons: the product surfaces "why was this clip chosen", and the eventual
learned ranker needs the same feature vector the heuristic ranker used.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from .taxonomy import AFFINITY, MODIFIERS, VIRALITY_MIX
from .types import (
    Candidate,
    LlmVerdict,
    Scores,
    Signal,
    SignalHit,
    clamp,
    saturating_sum,
)

BEHAVIOURS = ("retention", "engagement", "comment", "share")


def aggregate_signals(hits: Sequence[SignalHit]) -> dict[Signal, float]:
    """Collapse per-utterance hits into one strength per signal.

    Saturating rather than additive: five weak mentions of money across a
    45-second window is a clip that touches on money, not a clip five times
    more financial than one strong mention.
    """
    grouped: dict[Signal, list[float]] = {}
    for hit in hits:
        grouped.setdefault(hit.signal, []).append(hit.strength)
    return {signal: saturating_sum(values) for signal, values in grouped.items()}


def behaviour_scores(signals: Mapping[Signal, float]) -> dict[str, float]:
    """Project signal strengths through the affinity matrix.

    Normalised by the strongest single contribution rather than the sum, so a
    clip carrying one very strong signal is not penalised against a clip
    carrying four weak ones.
    """
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
        # Strongest contribution sets the floor; the rest add with diminishing
        # returns. Keeps a multi-signal clip ahead of a single-signal one
        # without letting signal count dominate signal quality.
        strongest = max(contributions)
        rest = saturating_sum(c * 0.35 for c in contributions if c != strongest)
        raw[behaviour] = clamp(strongest + (1.0 - strongest) * rest)

    return raw


def apply_modifiers(
    raw: Mapping[str, float], features: Mapping[str, float]
) -> dict[str, float]:
    """Damp each behaviour score by the structural features that gate it.

    A modifier with weight w scales the score by `(1 - w) + w * feature`, so a
    feature of 1.0 is a no-op and a feature of 0.0 removes exactly w of the
    score. Nothing is ever amplified above the content-derived value —
    structure can only cost, never create.
    """
    adjusted: dict[str, float] = {}
    for behaviour, score in raw.items():
        multiplier = 1.0
        for modifier in MODIFIERS.get(behaviour, ()):
            value = clamp(features.get(modifier.feature, 0.0))
            multiplier *= (1.0 - modifier.weight) + modifier.weight * value
        adjusted[behaviour] = clamp(score * multiplier)
    return adjusted


def virality(behaviours: Mapping[str, float]) -> float:
    """Composite score, weighted toward what actually drives distribution."""
    return clamp(
        sum(behaviours.get(name, 0.0) * weight for name, weight in VIRALITY_MIX.items())
    )


def blend_llm(
    features: Mapping[str, float], verdict: LlmVerdict | None, blend: Mapping[str, float]
) -> dict[str, float]:
    """Merge the LLM's semantic judgement into the heuristic feature vector.

    Per-feature blend weights live in `taxonomy.LLM_BLEND`. Where the LLM is
    dramatically better than keyword matching (standalone comprehensibility) it
    almost fully overrides; where the heuristic is already decent (payoff) it
    only nudges.
    """
    merged = dict(features)
    if verdict is None:
        # Quotability has no heuristic equivalent, so it stays neutral rather
        # than zero — a zero here would silently penalise every unjudged clip.
        merged.setdefault("quotability", 0.5)
        return merged

    for name in ("hook_strength", "standalone", "payoff", "quotability"):
        llm_value = clamp(getattr(verdict, name))
        weight = blend.get(name, 0.0)
        heuristic = clamp(merged.get(name, 0.5))
        merged[name] = heuristic * (1.0 - weight) + llm_value * weight

    return merged


def to_percent(value: float) -> int:
    return int(round(clamp(value) * 100))


def score_candidate(
    candidate: Candidate,
    features: Mapping[str, float],
    verdict: LlmVerdict | None = None,
    blend: Mapping[str, float] | None = None,
    extra_signals: Iterable[Signal] = (),
) -> tuple[Scores, dict[Signal, float], dict[str, float]]:
    """Score one candidate end to end.

    Returns the five scores, the aggregated signal strengths, and the final
    (post-LLM-blend) feature vector — all three are persisted, because the
    feedback loop needs to know what the ranker believed at decision time.
    """
    from .taxonomy import LLM_BLEND

    signals = aggregate_signals(candidate.hits)

    # Signals the LLM spotted that no detector caught. Entered at moderate
    # strength: the model is reliable about presence and unreliable about
    # intensity, so we trust the "what" and discount the "how much".
    for signal in extra_signals:
        signals.setdefault(signal, 0.55)

    merged_features = blend_llm(features, verdict, blend or LLM_BLEND)

    raw = behaviour_scores(signals)
    adjusted = apply_modifiers(raw, merged_features)

    # Quotability lifts share directly — a line people repeat is a line people
    # forward. It has no other behavioural pathway, so it is applied here
    # rather than in the modifier table.
    quotability = clamp(merged_features.get("quotability", 0.5))
    adjusted["share"] = clamp(adjusted["share"] * (0.8 + 0.4 * quotability))

    scores = Scores(
        virality=to_percent(virality(adjusted)),
        engagement=to_percent(adjusted["engagement"]),
        retention=to_percent(adjusted["retention"]),
        comment=to_percent(adjusted["comment"]),
        share=to_percent(adjusted["share"]),
    )
    return scores, signals, merged_features
