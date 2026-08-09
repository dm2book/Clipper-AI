"""Core types for hook generation and CTR estimation.

A hook is the on-screen text that has to stop a scroll in under a second. It
is not the clip's title, not a description, and not a sentence — it is a
promise, and its only job is to make the next second feel mandatory.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Sequence


class HookType(str, enum.Enum):
    """The psychological lever a hook pulls.

    Stable wire identifiers — persisted with every hook so that, once real
    click data exists, per-type performance can be measured per creator and
    per audience. Which lever works is not universal: a finance channel and a
    comedy channel have opposite type rankings.
    """

    CURIOSITY = "curiosity"          # opens a loop the viewer needs closed
    CONTROVERSY = "controversy"      # stakes out a contestable position
    AUTHORITY = "authority"          # earns the claim with credentials or cost
    FEAR = "fear"                    # loss aversion — what you stand to lose
    SURPRISE = "surprise"            # a fact that violates expectation
    NUMBER = "number"                # concrete specificity as the draw
    QUESTION = "question"            # direct address demanding an answer
    TRANSFORMATION = "transformation"  # before/after delta
    NEGATIVITY = "negativity"        # mistakes, warnings, what not to do
    SOCIAL_PROOF = "social_proof"    # everyone/nobody framing


#: The five the product leads with. The other five exist because clips whose
#: content does not suit these produce weak hooks when forced into them.
CORE_TYPES: tuple[HookType, ...] = (
    HookType.CURIOSITY,
    HookType.CONTROVERSY,
    HookType.AUTHORITY,
    HookType.FEAR,
    HookType.SURPRISE,
)


@dataclass(frozen=True, slots=True)
class ClipContext:
    """What the generator knows about the clip it is writing hooks for.

    Everything except `text` is optional. Richer context produces better
    hooks — a clip with an extracted dollar figure can use specificity
    templates that a bare transcript cannot — but the generator degrades
    rather than failing when context is thin.
    """

    text: str
    signals: tuple[str, ...] = ()      # e.g. ("secret", "money") from the viral engine
    speaker: str = ""
    duration_s: float = 0.0
    language: str = "en"
    topic_hint: str = ""               # caller-supplied subject, overrides extraction

    @property
    def words(self) -> list[str]:
        return self.text.split()


@dataclass(frozen=True, slots=True)
class Slots:
    """Content pulled out of a clip for templates to fill.

    A template declares which slots it needs; templates whose slots are
    missing are skipped rather than rendered with a placeholder. Shipping a
    hook that literally reads "{number}" is worse than shipping one fewer
    hook.
    """

    topic: str = ""          # bare noun, attributive: "The {topic} mistake"
    topic_phrase: str = ""   # with determiner, nominal: "after {topic_phrase}"
    number: str = ""
    outcome: str = ""        # past tense, as spoken: "lost"
    outcome_base: str = ""   # infinitive, for "expected it to {outcome_base}"
    timeframe: str = ""
    entity: str = ""
    quote: str = ""

    def has(self, name: str) -> bool:
        return bool(getattr(self, name, ""))

    def as_dict(self) -> dict[str, str]:
        return {
            "topic": self.topic,
            "topic_phrase": self.topic_phrase,
            "number": self.number,
            "outcome": self.outcome,
            "outcome_base": self.outcome_base,
            "timeframe": self.timeframe,
            "entity": self.entity,
            "quote": self.quote,
        }


@dataclass(frozen=True, slots=True)
class CtrEstimate:
    """An estimated click-through rate, and how much to trust it.

    Read `lift` as the model's actual output and `ctr` as a convenience
    projection onto the caller's baseline. The engine estimates *relative*
    performance between hooks for the same clip; it has no way to know a
    channel's absolute CTR until that channel's own numbers are fed back in.

    `confidence` is `prior` for every hook this engine produces today. It
    becomes meaningful only once `HookSet.feature_rows()` has been joined to
    real impression data and the weights retrained — the field exists so that
    consumers are forced to notice the difference.
    """

    lift: float           # multiplier against the baseline, e.g. 1.35
    ctr: float            # baseline * lift, as a percentage
    baseline: float       # the baseline this was projected onto
    confidence: str = "prior"

    @property
    def percent(self) -> str:
        return f"{self.ctr:.1f}%"

    def to_dict(self) -> dict[str, Any]:
        return {
            "lift": round(self.lift, 4),
            "ctr_percent": round(self.ctr, 2),
            "baseline_percent": round(self.baseline, 2),
            "confidence": self.confidence,
        }


@dataclass(slots=True)
class Hook:
    """One generated hook, scored and explained."""

    text: str
    hook_type: HookType
    estimate: CtrEstimate
    features: dict[str, float] = field(default_factory=dict)
    penalties: tuple[str, ...] = ()
    source: str = "template"   # "template" | "llm"
    template_id: str = ""

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    @property
    def char_count(self) -> int:
        return len(self.text)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "text": self.text,
            "type": self.hook_type.value,
            "words": self.word_count,
            "chars": self.char_count,
            "estimate": self.estimate.to_dict(),
            "features": {k: round(v, 4) for k, v in sorted(self.features.items())},
            "source": self.source,
        }
        if self.penalties:
            out["penalties"] = list(self.penalties)
        if self.template_id:
            out["template_id"] = self.template_id
        return out


@dataclass(slots=True)
class HookSet:
    """The ranked hooks for one clip."""

    hooks: list[Hook]
    slots: Slots
    weights_version: str
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def best(self) -> Hook | None:
        return self.hooks[0] if self.hooks else None

    def by_type(self) -> dict[HookType, list[Hook]]:
        grouped: dict[HookType, list[Hook]] = {}
        for hook in self.hooks:
            grouped.setdefault(hook.hook_type, []).append(hook)
        return grouped

    def feature_rows(self) -> list[dict[str, Any]]:
        """Flat rows for the training table.

        This is the whole reason the feature vector is persisted rather than
        just the score. Once these are joined to real impressions and clicks,
        the hand-tuned weights in `scoring.py` get replaced by a fitted model —
        and the rows have to include hooks that were *not* chosen, which is why
        the full ranked set is returned rather than only the winner.
        """
        return [
            {
                "text": hook.text,
                "type": hook.hook_type.value,
                "source": hook.source,
                "template_id": hook.template_id,
                "predicted_lift": hook.estimate.lift,
                "weights_version": self.weights_version,
                **{f"f_{k}": v for k, v in hook.features.items()},
            }
            for hook in self.hooks
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "weights_version": self.weights_version,
            "slots": self.slots.as_dict(),
            "stats": self.stats,
            "hooks": [h.to_dict() for h in self.hooks],
        }


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))
