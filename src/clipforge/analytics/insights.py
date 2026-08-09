"""The five questions the product promises to answer.

Best posting times, best hooks, best topics, best clip lengths, best creators.
Each is a ranked comparison plus a straight answer about whether the ranking
survives contact with statistics — and for several of them, the honest answer
on a young account is "not yet, and here is how much more data it needs".

Two analyses deserve more than a ranking, and get one:

**Hooks are scored on the metric they are accountable for.** A hook's job ends
when the viewer decides to keep watching, so it is judged on view-through rate
and hook hold rather than on views. Ranking hooks by views credits the hook for
the clip's payoff and for whatever the algorithm did afterwards.

**Prediction is checked against outcome.** The hook engine has been emitting
`predicted_lift` since it was built, explicitly so this comparison could
happen. `calibration()` is what retires that prior or keeps it: if predicted
lift does not correlate with realised performance, the hand-tuned weights are
not carrying information and should be replaced rather than tuned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .attribution import (
    DIMENSIONS,
    METRICS,
    AnalyticsStore,
    PostRecord,
    dimension_value,
)
from .experiments import Validity, assess
from .metrics import PRIMARY_CHECKPOINT_H
from .stats import (
    Comparison,
    MIN_GROUP_N,
    compare,
    mean,
    samples_needed,
    stdev,
    trimmed_mean,
)


@dataclass(frozen=True, slots=True)
class Insight:
    """One answered question."""

    question: str
    comparison: Comparison
    validity: Validity
    metric: str
    #: What to do about it, or what is missing before anything can be done.
    recommendation: str = ""

    @property
    def actionable(self) -> bool:
        return self.comparison.conclusive

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "metric": self.metric,
            "actionable": self.actionable,
            "recommendation": self.recommendation,
            "validity": self.validity.to_dict(),
            "comparison": self.comparison.to_dict(),
        }


def _recommend(
    comparison: Comparison, validity: Validity, records: Sequence[PostRecord],
    metric: str, checkpoint_h: float,
) -> str:
    """Turn a comparison into advice, or into a sample-size requirement."""
    winner = comparison.winner
    if winner is not None:
        prefix = "" if validity.causal else "Associated with, not proven to cause: "
        return (
            f"{prefix}{winner.name} runs {winner.lift * 100:+.0f}% on "
            f"{METRICS.get(metric, metric)} across {winner.n} posts. "
            f"Weight toward it and keep measuring."
        )

    if not comparison.ranked:
        needed = MIN_GROUP_N - max(
            (n for _, n in comparison.excluded), default=0
        )
        return (
            f"No group has reached {MIN_GROUP_N} posts. About {max(1, needed)} "
            f"more in the leading group before this question can be asked."
        )

    values = [
        v for r in records
        if (v := r.value(metric, checkpoint_h)) is not None
    ]
    spread, baseline = stdev(values), trimmed_mean(values)
    if spread and baseline:
        for target in (0.20, 0.35, 0.50):
            required = samples_needed(target, spread, baseline)
            if required <= 200:
                return (
                    f"No difference this data can resolve. Detecting a "
                    f"{target * 100:.0f}% effect would need about {required} "
                    f"posts per group; the largest group has "
                    f"{comparison.ranked[0].n}."
                )
    return (
        "No difference this data can resolve, and the spread is wide enough "
        "that no realistic sample would settle it. Vary something else."
    )


def analyse(
    store: AnalyticsStore,
    records: Sequence[PostRecord],
    dimension: str,
    metric: str,
    question: str = "",
    checkpoint_h: float = PRIMARY_CHECKPOINT_H,
    seed: str = "clipforge",
) -> Insight:
    """Rank one dimension on one metric, honestly."""
    groups = store.group(records, dimension, metric, checkpoint_h)
    comparison = compare(
        DIMENSIONS.get(dimension, dimension),
        METRICS.get(metric, metric),
        groups, seed=seed,
    )
    validity = assess(records, dimension)
    return Insight(
        question=question or f"Best {DIMENSIONS.get(dimension, dimension)}?",
        comparison=comparison,
        validity=validity,
        metric=metric,
        recommendation=_recommend(
            comparison, validity, records, metric, checkpoint_h
        ),
    )


# --- the five --------------------------------------------------------------


def best_posting_times(
    store: AnalyticsStore, records: Sequence[PostRecord],
    checkpoint_h: float = PRIMARY_CHECKPOINT_H, seed: str = "clipforge",
) -> list[Insight]:
    """Posting time, at three granularities.

    Hour and weekday separately as well as the combined slot, because the
    combination has 168 buckets and a channel posting twice a day fills each
    one about once a month. Reporting only the combined slot guarantees an
    underpowered answer; reporting the margins first often finds a real effect
    the interaction cannot see.
    """
    return [
        analyse(store, records, "hour", "views",
                "What hour of day performs best?", checkpoint_h, seed),
        analyse(store, records, "weekday", "views",
                "What day of week performs best?", checkpoint_h, seed),
        analyse(store, records, "slot", "views",
                "What exact posting slot performs best?", checkpoint_h, seed),
    ]


def best_hooks(
    store: AnalyticsStore, records: Sequence[PostRecord],
    checkpoint_h: float = PRIMARY_CHECKPOINT_H, seed: str = "clipforge",
) -> list[Insight]:
    """Hook types, judged on what a hook is actually responsible for.

    View-through rate and hook hold, not views. A hook's job ends the moment
    the viewer decides to keep watching; crediting it for the clip's payoff
    and for the algorithm's subsequent decisions measures the wrong thing.
    """
    return [
        analyse(store, records, "hook_type", "view_through_rate",
                "Which hook type earns the click?", checkpoint_h, seed),
        analyse(store, records, "hook_type", "hook_hold",
                "Which hook type survives the first seconds?",
                checkpoint_h, seed),
    ]


def best_topics(
    store: AnalyticsStore, records: Sequence[PostRecord],
    checkpoint_h: float = PRIMARY_CHECKPOINT_H, seed: str = "clipforge",
) -> list[Insight]:
    """Topics, on reach and on whether they convert a viewer into a follower."""
    return [
        analyse(store, records, "topic", "views",
                "Which topics reach furthest?", checkpoint_h, seed),
        analyse(store, records, "topic", "follow_rate",
                "Which topics win subscribers?", checkpoint_h, seed),
    ]


def best_clip_lengths(
    store: AnalyticsStore, records: Sequence[PostRecord],
    checkpoint_h: float = PRIMARY_CHECKPOINT_H, seed: str = "clipforge",
) -> list[Insight]:
    """Clip length, on completion and on reach.

    Both, because they disagree in a way worth seeing: shorter clips almost
    always complete better and do not always travel further, and a channel
    optimising completion alone will shorten itself into irrelevance.
    """
    return [
        analyse(store, records, "duration_bucket", "completion",
                "Which clip length holds to the end?", checkpoint_h, seed),
        analyse(store, records, "duration_bucket", "views",
                "Which clip length reaches furthest?", checkpoint_h, seed),
    ]


def best_creators(
    store: AnalyticsStore, records: Sequence[PostRecord],
    checkpoint_h: float = PRIMARY_CHECKPOINT_H, seed: str = "clipforge",
) -> list[Insight]:
    """Which source material is worth licensing more of."""
    return [
        analyse(store, records, "creator", "views",
                "Whose source material performs best?", checkpoint_h, seed),
        analyse(store, records, "creator", "engagement_rate",
                "Whose source material engages best?", checkpoint_h, seed),
    ]


# --- prediction quality -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Calibration:
    """Whether a model's predictions match what happened.

    The reason `predicted_lift` and `predicted_virality` have been persisted
    with every decision since those engines were built. A prior that does not
    correlate with outcome is not a prior worth tuning; it is one worth
    replacing.
    """

    model: str
    weights_version: str
    n: int
    correlation: float
    #: Realised performance of the predicted-best tercile versus the worst.
    top_vs_bottom: float
    verdict: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "weights_version": self.weights_version,
            "n": self.n,
            "correlation": round(self.correlation, 4),
            "top_vs_bottom": round(self.top_vs_bottom, 4),
            "verdict": self.verdict,
        }


def _spearman(pairs: Sequence[tuple[float, float]]) -> float:
    """Rank correlation. Rank rather than linear because the prediction is an
    ordering — the engine only ever claimed relative lift, not absolute CTR."""
    if len(pairs) < 3:
        return 0.0

    def ranks(values: Sequence[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        index = 0
        while index < len(order):
            end = index
            while (end + 1 < len(order)
                   and values[order[end + 1]] == values[order[index]]):
                end += 1
            average = (index + end) / 2.0 + 1.0
            for position in range(index, end + 1):
                out[order[position]] = average
            index = end + 1
        return out

    xs, ys = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    n = len(pairs)
    mx, my = mean(xs), mean(ys)
    numerator = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs)
    dy = sum((y - my) ** 2 for y in ys)
    if dx <= 0 or dy <= 0:
        return 0.0
    return numerator / (dx * dy) ** 0.5


def calibration(
    records: Sequence[PostRecord],
    prediction: str = "predicted_lift",
    metric: str = "view_through_rate",
    checkpoint_h: float = PRIMARY_CHECKPOINT_H,
) -> Calibration:
    """Check a persisted prediction against the outcome it predicted."""
    pairs: list[tuple[float, float]] = []
    versions: set[str] = set()

    for record in records:
        predicted = getattr(record, prediction, 0.0)
        actual = record.value(metric, checkpoint_h)
        if not predicted or actual is None:
            continue
        pairs.append((float(predicted), float(actual)))
        versions.add(
            record.hook_weights_version if prediction == "predicted_lift"
            else record.viral_weights_version
        )

    model = "hook CTR estimator" if prediction == "predicted_lift" \
        else "viral ranker"
    version = ", ".join(sorted(v for v in versions if v)) or "unversioned"

    if len(pairs) < MIN_GROUP_N:
        return Calibration(
            model, version, len(pairs), 0.0, 0.0,
            f"not enough matured posts to evaluate ({len(pairs)}/{MIN_GROUP_N})",
        )

    correlation = _spearman(pairs)
    ordered = sorted(pairs, key=lambda p: p[0])
    third = max(1, len(ordered) // 3)
    bottom = trimmed_mean([p[1] for p in ordered[:third]])
    top = trimmed_mean([p[1] for p in ordered[-third:]])
    ratio = (top - bottom) / bottom if bottom else 0.0

    if correlation >= 0.30:
        verdict = (
            f"predictive: the top third the model picked outperforms the "
            f"bottom third by {ratio * 100:+.0f}%"
        )
    elif correlation <= -0.15:
        verdict = (
            "inverted: the model's preferred clips do worse. The weights are "
            "carrying real signal with the wrong sign — investigate before "
            "trusting any hook ranking."
        )
    else:
        verdict = (
            f"no better than chance (rho {correlation:+.2f}). The hand-tuned "
            f"weights are not carrying information; retrain on the feature "
            f"rows rather than tuning them further."
        )

    return Calibration(model, version, len(pairs), correlation, ratio, verdict)


# --- retention diagnosis --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetentionDiagnosis:
    """Where the audience leaves, aggregated across posts."""

    n: int
    median_hook_hold: float
    median_completion: float
    median_mid_drop: float
    median_mid_drop_share: float
    dominant_problem: str
    worst_posts: tuple[tuple[str, float], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "median_hook_hold": round(self.median_hook_hold, 4),
            "median_completion": round(self.median_completion, 4),
            "median_mid_drop": round(self.median_mid_drop, 4),
            "median_mid_drop_share": round(self.median_mid_drop_share, 4),
            "dominant_problem": self.dominant_problem,
            "worst_posts": [
                {"post_id": p, "hook_hold": round(v, 4)}
                for p, v in self.worst_posts
            ],
        }


def diagnose_retention(
    records: Sequence[PostRecord],
    checkpoint_h: float = PRIMARY_CHECKPOINT_H,
) -> RetentionDiagnosis:
    """Whether the audience is lost at the hook or at the payoff.

    The two need opposite fixes and produce the same average watch time, which
    is why the average is not reported here on its own.
    """
    from .stats import median

    holds: list[float] = []
    completions: list[float] = []
    drops: list[float] = []
    shares: list[float] = []
    worst: list[tuple[str, float]] = []

    for record in records:
        snapshot = record.metrics.at_age(checkpoint_h)
        if snapshot is None or not snapshot.retention.available:
            continue
        curve = snapshot.retention
        holds.append(curve.hook_hold)
        completions.append(curve.completion)
        drops.append(curve.mid_drop)
        shares.append(curve.mid_drop_share)
        worst.append((record.post_id, curve.hook_hold))

    if not holds:
        return RetentionDiagnosis(
            0, 0.0, 0.0, 0.0, 0.0,
            "no platform in this set reported a retention curve",
        )

    hook_hold = median(holds)
    completion = median(completions)
    drop = median(drops)
    drop_share = median(shares)

    if hook_hold < 0.55:
        problem = (
            f"hooks: half of all posts lose more than "
            f"{(1 - hook_hold) * 100:.0f}% of viewers before the content "
            f"starts. Hook rewriting is the highest-leverage change available."
        )
    elif drop_share > 0.45:
        problem = (
            f"payoff: hooks are working — {hook_hold * 100:.0f}% get past "
            f"them — but {drop_share * 100:.0f}% of those who do leave before "
            f"the end. Shorter clips or a faster payoff, not better hooks."
        )
    else:
        problem = (
            f"no dominant failure point; median completion "
            f"{completion * 100:.0f}%"
        )

    worst.sort(key=lambda pair: pair[1])
    return RetentionDiagnosis(
        len(holds), hook_hold, completion, drop, drop_share, problem,
        tuple(worst[:5]),
    )
