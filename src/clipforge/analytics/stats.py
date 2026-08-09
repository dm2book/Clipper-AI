"""Honest comparison of small samples.

An analytics engine's failure mode is not being wrong. It is being *confidently*
wrong at a volume no human can audit: rank seven posting hours by mean views,
bold the top one, and a creator reorganises their week around three posts'
worth of noise.

Four things stop that, and all four are necessary.

**A minimum sample.** Below `MIN_GROUP_N` a group is not ranked at all. It is
listed as "not enough data", which is a true statement, where a rank would be a
false one.

**A significance test that makes no distributional assumptions.** A permutation
test asks the only question that matters — how often would label-shuffled data
produce a gap this large? — and needs no normality, no equal variances, and no
t-tables. With the sample sizes a real channel produces, assuming normality is
not a safe simplification.

**Multiple-comparison correction.** Testing 24 posting hours at p<0.05 yields
roughly one "significant" hour by chance alone even when every hour is
identical, and the ranking guarantees that hour ends up on top. Benjamini-
Hochberg controls the false discovery rate across the whole family of tests, so
the answer to "which hour is best" can correctly be *none of them*.

**Minimum detectable effect.** "No significant difference" is a useless finding
on its own; a creator cannot act on it. Reporting the smallest effect the data
*could* have detected turns it into something actionable — "this cannot
distinguish anything below a 45% difference; forty more posts would get you to
20%".

Everything is deterministic: resampling is seeded, so the same data produces
the same conclusions and a report can be reproduced.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable, Sequence

#: Below this a group is not ranked. Eight is already generous for the variance
#: short-form metrics carry; it is a floor, not a target.
MIN_GROUP_N = 8

#: False discovery rate for a family of comparisons.
#:
#: 0.05 rather than the 0.10 that is conventional in exploratory analysis.
#: These are not exploratory findings — a report presents its conclusions as a
#: single list of recommendations, and a creator acts on them. At 0.10, with a
#: report typically making only two or three claims across a dozen families,
#: one of them being noise is routine. Tightening costs some real effects near
#: the detection boundary, which is the better error to make: `detectable_effect`
#: still reports those as "cannot resolve, here is what it would take".
DEFAULT_FDR = 0.05

#: Smallest relative difference worth reporting, regardless of significance.
#:
#: The mirror image of the noise problem, and just as damaging. With a tight
#: enough spread a 3% difference is statistically undeniable and operationally
#: meaningless — nobody reschedules a channel over 3%. A report that headlines
#: it has amplified noise just as surely as one that headlines a fluke, and has
#: also taught its reader that the findings section is not worth reading.
#:
#: A finding must clear both bars: distinguishable from chance, *and* large
#: enough that acting on it could matter.
MIN_MATERIAL_EFFECT = 0.10

DEFAULT_ITERATIONS = 2000
DEFAULT_CONFIDENCE = 0.90

#: z(0.975) + z(0.80) — the constant in the two-sample minimum-detectable-effect
#: formula at 5% significance and 80% power.
_MDE_Z = 2.80


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    average = mean(values)
    variance = sum((v - average) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def trimmed_mean(values: Sequence[float], trim: float = 0.1) -> float:
    """Mean with the tails removed.

    The default statistic for view counts. View distributions are extremely
    heavy-tailed — one clip in fifty carries more views than the other
    forty-nine combined — so a plain mean of ten posts is largely a report on
    whether one of them went viral.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    cut = int(len(ordered) * trim)
    kept = ordered[cut: len(ordered) - cut] or ordered
    return mean(kept)


def bootstrap_ci(
    values: Sequence[float],
    statistic: Callable[[Sequence[float]], float] = mean,
    confidence: float = DEFAULT_CONFIDENCE,
    iterations: int = DEFAULT_ITERATIONS,
    seed: str = "clipforge",
) -> tuple[float, float]:
    """Percentile confidence interval by resampling.

    Distribution-free and works for any statistic, including the trimmed mean
    and the median, for which no closed-form interval is available.
    """
    if len(values) < 2:
        point = statistic(values) if values else 0.0
        return point, point

    rng = random.Random(f"{seed}|{len(values)}|{sum(values):.6f}")
    size = len(values)
    draws = sorted(
        statistic([values[rng.randrange(size)] for _ in range(size)])
        for _ in range(iterations)
    )

    tail = (1.0 - confidence) / 2.0
    low = draws[max(0, int(tail * iterations))]
    high = draws[min(iterations - 1, int((1.0 - tail) * iterations))]
    return low, high


def permutation_p(
    group: Sequence[float],
    rest: Sequence[float],
    statistic: Callable[[Sequence[float]], float] = mean,
    iterations: int = DEFAULT_ITERATIONS,
    seed: str = "clipforge",
) -> float:
    """Two-sided p-value for the difference between two samples.

    Shuffles the group labels and counts how often chance reproduces a gap at
    least as large. Assumes nothing about the shape of the distribution, which
    matters because view counts are nowhere near normal.
    """
    if len(group) < 2 or len(rest) < 2:
        return 1.0

    observed = abs(statistic(group) - statistic(rest))
    pool = list(group) + list(rest)
    split = len(group)
    rng = random.Random(f"{seed}|perm|{len(pool)}|{sum(pool):.6f}")

    extreme = 0
    for _ in range(iterations):
        rng.shuffle(pool)
        shuffled = abs(statistic(pool[:split]) - statistic(pool[split:]))
        if shuffled >= observed:
            extreme += 1

    # +1 in both terms: a permutation test can never honestly report p = 0,
    # and reporting one implies more certainty than resampling can supply.
    return (extreme + 1) / (iterations + 1)


def benjamini_hochberg(
    pvalues: Sequence[float], fdr: float = DEFAULT_FDR
) -> list[bool]:
    """Which of a family of tests survive false-discovery-rate control.

    Less brutal than Bonferroni, which at twenty-four comparisons discards
    almost every real effect a channel-sized dataset can show, while still
    preventing the ranking from manufacturing a winner out of noise.
    """
    if not pvalues:
        return []

    indexed = sorted(enumerate(pvalues), key=lambda pair: pair[1])
    count = len(pvalues)
    threshold_rank = -1

    for rank, (_, value) in enumerate(indexed, start=1):
        if value <= fdr * rank / count:
            threshold_rank = rank

    significant = [False] * count
    if threshold_rank > 0:
        for _, (index, _) in enumerate(indexed[:threshold_rank]):
            significant[index] = True
    return significant


def minimum_detectable_effect(
    group_n: int, rest_n: int, spread: float, baseline: float
) -> float:
    """Smallest relative difference this much data could have detected.

    The number that turns "no significant difference" from a dead end into a
    decision. Without it a creator cannot tell whether a comparison found
    nothing because there is nothing there, or because eleven posts cannot see
    anything smaller than a doubling.
    """
    if group_n < 2 or rest_n < 2 or baseline <= 0 or spread <= 0:
        return float("inf")
    harmonic = 1.0 / group_n + 1.0 / rest_n
    return _MDE_Z * spread * math.sqrt(harmonic) / baseline


def samples_needed(target_lift: float, spread: float, baseline: float) -> int:
    """Posts per group needed to detect `target_lift`, at 80% power.

    Pairs with the above: having said the data cannot see a 20% difference,
    this says how many more posts would.
    """
    if target_lift <= 0 or baseline <= 0 or spread <= 0:
        return 0
    effect = target_lift * baseline
    return max(2, math.ceil(2.0 * (_MDE_Z * spread / effect) ** 2))


@dataclass(frozen=True, slots=True)
class GroupResult:
    """One group's standing within a comparison."""

    name: str
    n: int
    value: float
    ci: tuple[float, float] = (0.0, 0.0)
    #: Relative to every *other* group pooled, not to the overall mean — which
    #: would include this group and dilute its own effect.
    lift: float = 0.0
    p_value: float = 1.0
    significant: bool = False
    enough_data: bool = False
    detectable_effect: float = float("inf")

    @property
    def material(self) -> bool:
        """Whether the effect is big enough to be worth acting on."""
        return abs(self.lift) >= MIN_MATERIAL_EFFECT

    @property
    def verdict(self) -> str:
        if not self.enough_data:
            return f"not enough data ({self.n}/{MIN_GROUP_N})"
        if self.significant and not self.material:
            return (
                f"a real but tiny difference ({self.lift * 100:+.1f}%) — "
                f"below the {MIN_MATERIAL_EFFECT * 100:.0f}% worth acting on"
            )
        if self.significant:
            direction = "above" if self.lift > 0 else "below"
            return f"{abs(self.lift) * 100:.0f}% {direction} the rest"
        if math.isinf(self.detectable_effect):
            return "indistinguishable from the rest"
        return (
            f"indistinguishable — this data cannot detect anything below "
            f"{self.detectable_effect * 100:.0f}%"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "n": self.n,
            "value": round(self.value, 5),
            "ci": [round(self.ci[0], 5), round(self.ci[1], 5)],
            "lift": round(self.lift, 4),
            "p_value": round(self.p_value, 4),
            "significant": self.significant,
            "material": self.material,
            "enough_data": self.enough_data,
            "detectable_effect": (
                None if math.isinf(self.detectable_effect)
                else round(self.detectable_effect, 4)
            ),
            "verdict": self.verdict,
        }


@dataclass(frozen=True, slots=True)
class Comparison:
    """A ranked set of groups, with a straight answer about whether it means anything."""

    dimension: str
    metric: str
    groups: tuple[GroupResult, ...] = ()
    #: Groups that never entered the ranking, and why.
    excluded: tuple[tuple[str, int], ...] = ()
    total_n: int = 0
    fdr: float = DEFAULT_FDR

    @property
    def ranked(self) -> tuple[GroupResult, ...]:
        return tuple(sorted(
            (g for g in self.groups if g.enough_data),
            key=lambda g: -g.value,
        ))

    @property
    def winner(self) -> GroupResult | None:
        """The best group — **only** if it is distinguishable from the others.

        Returning None is the important behaviour. A ranking always has a top
        row, and presenting that row as a finding is exactly how noise becomes
        a strategy.
        """
        for group in self.ranked:
            if group.significant and group.material and group.lift > 0:
                return group
        return None

    @property
    def conclusive(self) -> bool:
        return self.winner is not None

    def summary(self) -> str:
        if not self.ranked:
            return (
                f"{self.dimension}: no group has {MIN_GROUP_N} posts yet "
                f"({self.total_n} in total)"
            )
        winner = self.winner
        if winner is None:
            best = self.ranked[0]
            return (
                f"{self.dimension}: no clear winner. Top is {best.name} at "
                f"{best.value:.3g}, but the difference is inside the noise — "
                f"{best.verdict}."
            )
        return (
            f"{self.dimension}: {winner.name} leads on {self.metric}, "
            f"{winner.lift * 100:+.0f}% versus the rest "
            f"(n={winner.n}, p={winner.p_value:.3f})"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "dimension": self.dimension,
            "metric": self.metric,
            "conclusive": self.conclusive,
            "winner": self.winner.name if self.winner else None,
            "total_n": self.total_n,
            "fdr": self.fdr,
            "summary": self.summary(),
            "groups": [g.to_dict() for g in self.ranked],
            "excluded": [
                {"name": name, "n": n} for name, n in self.excluded
            ],
        }


def compare(
    dimension: str,
    metric: str,
    groups: dict[str, Sequence[float]],
    statistic: Callable[[Sequence[float]], float] = trimmed_mean,
    fdr: float = DEFAULT_FDR,
    min_n: int = MIN_GROUP_N,
    seed: str = "clipforge",
    iterations: int = DEFAULT_ITERATIONS,
) -> Comparison:
    """Rank groups on a metric, and say whether the ranking means anything.

    Each qualifying group is tested against every other group pooled, and the
    whole family of tests is then corrected together.
    """
    eligible = {k: list(v) for k, v in groups.items() if len(v) >= min_n}
    excluded = tuple(sorted(
        ((k, len(v)) for k, v in groups.items() if len(v) < min_n),
        key=lambda pair: -pair[1],
    ))
    total = sum(len(v) for v in groups.values())

    if not eligible:
        return Comparison(dimension, metric, (), excluded, total, fdr)

    results: list[GroupResult] = []
    pvalues: list[float] = []

    for name, values in sorted(eligible.items()):
        rest = [
            value
            for other, others in eligible.items() if other != name
            for value in others
        ]
        point = statistic(values)
        rest_point = statistic(rest) if rest else point

        p_value = (
            permutation_p(values, rest, statistic, iterations, f"{seed}|{name}")
            if rest else 1.0
        )
        pvalues.append(p_value)

        results.append(GroupResult(
            name=name,
            n=len(values),
            value=point,
            ci=bootstrap_ci(values, statistic, seed=f"{seed}|{name}",
                            iterations=iterations),
            lift=(point - rest_point) / rest_point if rest_point else 0.0,
            p_value=p_value,
            enough_data=True,
            detectable_effect=minimum_detectable_effect(
                len(values), len(rest), stdev(values + rest),
                rest_point if rest_point else 1.0,
            ),
        ))

    flags = benjamini_hochberg(pvalues, fdr)
    corrected = tuple(
        GroupResult(
            name=r.name, n=r.n, value=r.value, ci=r.ci, lift=r.lift,
            p_value=r.p_value, significant=flag, enough_data=True,
            detectable_effect=r.detectable_effect,
        )
        for r, flag in zip(results, flags)
    )

    return Comparison(dimension, metric, corrected, excluded, total, fdr)
