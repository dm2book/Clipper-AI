"""Exploration: the thing that makes "best hook" answerable at all.

The factory publishes the hook the model ranked first. Every outcome ever
observed for a hook type is therefore an outcome for a hook the model already
liked, and an analysis of that data cannot distinguish "authority hooks work"
from "the model likes authority hooks". It will confirm whatever the prior was,
which is worse than having no analysis, because it comes with a number attached.

The only escape is to sometimes publish something the model did not pick.

**The cost is real and should be stated.** Exploring means knowingly shipping a
hook the model rates lower — at 15%, roughly one clip in seven is deliberately
not the best guess. In exchange, the other six become interpretable. Without
paying it, the hand-tuned weights in `hooks/scoring.py` can never be retired,
because there will never be data capable of contradicting them.

**Exploration is capped, not uniform.** Sampling from the whole ranked set
would occasionally publish the twentieth-best hook, which is not a measurement
worth making at the price of a slot. Candidates come from the top
`EXPLORE_DEPTH`, where the model's own confidence gap is small enough that it
might genuinely be wrong.

This is deliberately *not* a bandit. A bandit optimises reward while learning,
which sounds strictly better and quietly reintroduces the same confounding:
allocation depends on observed performance, so the sample stops being random
and the causal question becomes unanswerable again. Fixed-rate randomisation is
less efficient and gives an answer that can be trusted.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Sequence

#: Share of publishes that deliberately use a non-top variant.
DEFAULT_EXPLORE_RATE = 0.15

#: How far down the ranking an exploration may reach.
EXPLORE_DEPTH = 5

#: Explored posts needed before a comparison is treated as causal rather than
#: descriptive. Below this the analysis still runs — it is what the account
#: experienced — but it is labelled as confounded.
MIN_EXPLORED = 12


@dataclass(frozen=True, slots=True)
class Assignment:
    """Which variant to publish, and whether it was an exploration."""

    index: int
    explored: bool
    rate: float = DEFAULT_EXPLORE_RATE
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "explored": self.explored,
            "rate": self.rate,
            "reason": self.reason,
        }


def _unit(seed: str) -> float:
    """Deterministic float in [0, 1) from a string.

    Deterministic rather than random so the same post always makes the same
    choice. A retry, a replay or a re-run must not re-roll the assignment —
    that would let a post be counted as explored in one pass and not in
    another, which corrupts exactly the data exploration exists to protect.
    """
    digest = hashlib.blake2b(seed.encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / float(1 << 64)


@dataclass(slots=True)
class ExplorationPolicy:
    """Decides when to publish something other than the model's pick."""

    rate: float = DEFAULT_EXPLORE_RATE
    depth: int = EXPLORE_DEPTH
    enabled: bool = True

    def assign(self, variants: int, key: str) -> Assignment:
        """Pick a variant index for a post identified by `key`."""
        if variants <= 0:
            raise ValueError("no variants to choose from")

        if not self.enabled or variants == 1:
            return Assignment(
                0, False, self.rate,
                "exploration disabled" if not self.enabled else "only one variant",
            )

        if _unit(f"{key}|explore") >= self.rate:
            return Assignment(0, False, self.rate, "model's own pick")

        # Uniform over ranks 1..depth-1. Rank 0 is excluded: choosing it would
        # be indistinguishable from not exploring, and would inflate the
        # explored count without producing any unconfounded contrast.
        reachable = min(self.depth, variants)
        if reachable < 2:
            return Assignment(0, False, self.rate, "not enough variants to explore")

        offset = int(_unit(f"{key}|rank") * (reachable - 1)) + 1
        return Assignment(
            offset, True, self.rate,
            f"exploring rank {offset} of top {reachable}",
        )

    def to_dict(self) -> dict[str, Any]:
        return {"rate": self.rate, "depth": self.depth, "enabled": self.enabled}


@dataclass(frozen=True, slots=True)
class Validity:
    """Whether a comparison supports a causal claim, and if not, why not."""

    explored: int
    total: int
    causal: bool
    caveat: str = ""

    @property
    def explore_rate(self) -> float:
        return self.explored / self.total if self.total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "explored": self.explored,
            "total": self.total,
            "explore_rate": round(self.explore_rate, 3),
            "causal": self.causal,
            "caveat": self.caveat,
        }


def assess(records: Sequence[Any], dimension: str) -> Validity:
    """Whether a comparison on `dimension` can support a causal claim.

    Only the dimensions the system *chose* are confounded by its own ranking.
    Posting time, clip length and source creator are properties of the
    material and the schedule rather than of a model's preference, so a
    comparison across them is confounded by the usual observational problems
    but not by the selection loop — and saying so precisely matters more than
    a blanket disclaimer nobody reads.
    """
    total = len(records)
    explored = sum(1 for r in records if getattr(r, "explored", False))

    model_chosen = dimension in ("hook_type", "hook")
    if not model_chosen:
        return Validity(
            explored, total, causal=False,
            caveat=(
                "observational: not randomised, so differences may reflect "
                "what was posted when rather than the dimension itself"
            ),
        )

    if explored >= MIN_EXPLORED:
        return Validity(explored, total, causal=True)

    return Validity(
        explored, total, causal=False,
        caveat=(
            f"confounded: only {explored} of {total} posts used a hook the "
            f"model did not rank first, so this largely measures the model's "
            f"own preferences. {MIN_EXPLORED} explored posts are needed before "
            f"this can be read as evidence about hooks themselves."
        ),
    )
