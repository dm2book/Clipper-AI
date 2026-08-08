"""Core data types for the viral detection engine.

Everything here is a plain dataclass with no behaviour beyond derived
properties, so the whole pipeline stays trivially serialisable and testable.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence


class Signal(str, enum.Enum):
    """The ten moment categories the engine detects.

    Values are stable wire identifiers — they are persisted on `moments.features`
    and used as ClickHouse column keys, so do not rename them.
    """

    CONTROVERSY = "controversy"
    EMOTIONAL_SPIKE = "emotional_spike"
    MONEY = "money"
    FUNNY = "funny"
    ARGUMENT = "argument"
    DEBATE = "debate"
    FAILURE = "failure"
    SUCCESS = "success"
    SECRET = "secret"
    LESSON = "lesson"


@dataclass(frozen=True, slots=True)
class Word:
    """A single word with its ASR timing."""

    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class Utterance:
    """One speaker turn (or sentence within a turn).

    The engine treats utterances as atoms: clip boundaries always land on
    utterance edges, which is what keeps cuts off mid-word.
    """

    index: int
    start_ms: int
    end_ms: int
    speaker: str
    text: str
    words: tuple[Word, ...] = ()

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True, slots=True)
class Transcript:
    """A full transcript, already diarised and segmented into utterances."""

    source_id: str
    utterances: tuple[Utterance, ...]
    language: str = "en"

    @property
    def duration_ms(self) -> int:
        return self.utterances[-1].end_ms if self.utterances else 0

    @property
    def speakers(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for u in self.utterances:
            seen.setdefault(u.speaker, None)
        return tuple(seen)

    def text_between(self, first: int, last: int) -> str:
        """Joined text for the inclusive utterance index range."""
        return " ".join(u.text for u in self.utterances[first : last + 1])


@dataclass(frozen=True, slots=True)
class SignalHit:
    """One detector firing on one utterance.

    `strength` is 0..1 within the detector's own scale — detectors are
    calibrated independently, and cross-signal weighting happens in scoring.
    """

    signal: Signal
    strength: float
    utterance_index: int
    evidence: str

    def __post_init__(self) -> None:
        if not 0.0 <= self.strength <= 1.0:
            raise ValueError(f"strength must be in [0,1], got {self.strength}")


@dataclass(frozen=True, slots=True)
class Candidate:
    """A contiguous utterance span being considered as a clip."""

    first_utterance: int
    last_utterance: int
    start_ms: int
    end_ms: int
    text: str
    hits: tuple[SignalHit, ...] = ()

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def duration_s(self) -> float:
        return self.duration_ms / 1000.0

    @property
    def span(self) -> tuple[int, int]:
        return (self.first_utterance, self.last_utterance)

    def overlap_ratio(self, other: "Candidate") -> float:
        """Intersection-over-union on the time axis. Used for NMS."""
        lo = max(self.start_ms, other.start_ms)
        hi = min(self.end_ms, other.end_ms)
        intersection = max(0, hi - lo)
        union = self.duration_ms + other.duration_ms - intersection
        return intersection / union if union > 0 else 0.0


@dataclass(frozen=True, slots=True)
class Scores:
    """The five output scores, each 0-100.

    `virality` is a weighted composite of the other four, not an independent
    judgement — see `taxonomy.VIRALITY_MIX` for the weighting and its rationale.
    """

    virality: int
    engagement: int
    retention: int
    comment: int
    share: int

    def as_dict(self) -> dict[str, int]:
        return {
            "virality": self.virality,
            "engagement": self.engagement,
            "retention": self.retention,
            "comment": self.comment,
            "share": self.share,
        }


@dataclass(frozen=True, slots=True)
class LlmVerdict:
    """Semantic judgement for one candidate, from the LLM tier."""

    candidate_span: tuple[int, int]
    hook_strength: float
    standalone: float
    payoff: float
    quotability: float
    title: str
    rationale: str
    signals: tuple[Signal, ...] = ()

    @property
    def mean(self) -> float:
        return (self.hook_strength + self.standalone + self.payoff + self.quotability) / 4.0


@dataclass(slots=True)
class Moment:
    """A scored candidate — the engine's unit of output before selection."""

    candidate: Candidate
    scores: Scores
    features: dict[str, float]
    signals: dict[Signal, float]
    title: str = ""
    rationale: str = ""
    judged_by_llm: bool = False

    @property
    def start_ms(self) -> int:
        return self.candidate.start_ms

    @property
    def end_ms(self) -> int:
        return self.candidate.end_ms

    def to_dict(self) -> dict[str, Any]:
        """Flat, JSON-safe projection — this is what lands in `moments`."""
        return {
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "duration_s": round(self.candidate.duration_s, 2),
            "title": self.title,
            "rationale": self.rationale,
            "scores": self.scores.as_dict(),
            "signals": {s.value: round(v, 4) for s, v in sorted(
                self.signals.items(), key=lambda kv: -kv[1]
            ) if v > 0},
            "features": {k: round(v, 4) for k, v in sorted(self.features.items())},
            "judged_by_llm": self.judged_by_llm,
            "text": self.candidate.text,
        }


@dataclass(slots=True)
class DetectionResult:
    """Everything the engine produces for one source.

    `top` is the deliverable; `ranked` is the full ordered set, kept because
    the feedback loop trains on candidates that were *not* selected as well as
    those that were.
    """

    source_id: str
    top: list[Moment]
    ranked: list[Moment]
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "stats": self.stats,
            "clips": [m.to_dict() for m in self.top],
        }


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def saturating_sum(values: Iterable[float]) -> float:
    """Combine independent 0..1 evidence without ever exceeding 1.

    Probabilistic OR: three weak hits of 0.3 read as 0.66, not 0.9, so a pile
    of marginal keyword matches can never outrank one strong signal.
    """
    remaining = 1.0
    for v in values:
        remaining *= 1.0 - clamp(v)
    return 1.0 - remaining


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0
