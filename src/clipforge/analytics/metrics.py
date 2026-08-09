"""Platform metrics, and the three things that make them comparable.

Raw counts are almost never the number you want, and comparing them directly
is the most common way an analytics dashboard produces confident nonsense.

**Age.** A post published two hours ago has fewer views than one published two
weeks ago, and that says nothing about either. Every comparison here is made at
a *matched age* — views at 24 hours against views at 24 hours — which means
metrics have to be stored as a series of snapshots rather than as a scalar that
gets overwritten. A single mutable `views` column makes matched-age comparison
impossible after the fact, and there is no way to reconstruct it later.

**Platform.** Ten thousand views on TikTok and ten thousand on YouTube Shorts
are not the same event. Every figure is normalised against a per-platform
baseline before it is compared to anything.

**Audience size.** A channel that doubled its subscribers will show doubled
views on identical content. Comparing this month to last without accounting for
that measures growth, not the content — so rates per follower matter alongside
absolute counts.

### Retention is the metric, and the curve matters more than the average

Views are downstream of distribution and distribution is downstream of
retention, so retention is the only one of the six that is close to a cause
rather than an effect. But the *average* watch percentage collapses the useful
part: a clip where 40% of viewers leave in the first two seconds and the rest
finish, and a clip where everyone watches 60% and drifts off, produce the same
average and need opposite fixes. The first has a hook problem; the second has a
payoff problem. `RetentionCurve` keeps them apart.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Sequence

from ..publish.types import Platform, UTC, ensure_utc, utcnow

#: Ages at which posts are compared. A post is only included in a comparison
#: once it has reached the age being compared at — otherwise a fresh post drags
#: every average down and looks like a content problem.
CHECKPOINTS_H: tuple[float, ...] = (1.0, 6.0, 24.0, 72.0, 168.0, 720.0)

#: The default comparison point. Long enough that the initial push has
#: resolved, short enough that a week of posts can be compared this week.
PRIMARY_CHECKPOINT_H = 24.0

#: Fraction of the clip that counts as "the hook". Viewers who leave inside
#: this window never saw the content at all.
HOOK_WINDOW_PCT = 0.10


@dataclass(frozen=True, slots=True)
class RetentionCurve:
    """Share of viewers still watching, sampled across the clip.

    Points are `(position_pct, viewers_pct)` with position in 0..1. Platforms
    report this at wildly different granularities — YouTube gives a fine curve,
    TikTok and Instagram give little more than an average — so the curve is
    optional everywhere and its absence is visible rather than imputed.
    """

    points: tuple[tuple[float, float], ...] = ()

    def __post_init__(self) -> None:
        if self.points:
            object.__setattr__(
                self, "points", tuple(sorted(self.points, key=lambda p: p[0]))
            )

    @property
    def available(self) -> bool:
        return len(self.points) >= 2

    def at(self, position: float) -> float:
        """Viewers remaining at a position, linearly interpolated."""
        if not self.points:
            return 0.0
        positions = [p[0] for p in self.points]
        index = bisect.bisect_left(positions, position)

        if index == 0:
            return self.points[0][1]
        if index >= len(self.points):
            return self.points[-1][1]

        (x0, y0), (x1, y1) = self.points[index - 1], self.points[index]
        if x1 == x0:
            return y1
        return y0 + (y1 - y0) * (position - x0) / (x1 - x0)

    @property
    def hook_hold(self) -> float:
        """Share still watching past the hook window.

        The number a hook is actually accountable for. Everything after this
        point is the clip's problem, not the hook's.
        """
        return self.at(HOOK_WINDOW_PCT)

    @property
    def completion(self) -> float:
        return self.at(1.0)

    @property
    def mid_drop(self) -> float:
        """Viewers lost between the hook window and the end, in absolute terms."""
        if not self.available:
            return 0.0
        return max(0.0, self.hook_hold - self.completion)

    @property
    def mid_drop_share(self) -> float:
        """Share of those who got past the hook who then left before the end.

        The number the payoff is actually accountable for. Measuring the drop
        against *all* viewers understates it by exactly the fraction the hook
        already lost: a clip holding 63% past the hook and finishing 32% looks
        like a mild 31-point decline, when in truth half of everyone who gave
        the clip a chance abandoned it. Same denominator error as reporting a
        checkout conversion against total site traffic.
        """
        if not self.available or self.hook_hold <= 0:
            return 0.0
        return max(0.0, (self.hook_hold - self.completion) / self.hook_hold)

    @property
    def diagnosis(self) -> str:
        """Which end of the clip is losing people."""
        if not self.available:
            return "no curve reported"
        if self.hook_hold < 0.55:
            return "hook: most viewers leave before the content starts"
        if self.mid_drop_share > 0.45:
            return "payoff: viewers stay for the hook and leave before the end"
        if self.completion > 0.55:
            return "healthy: majority reach the end"
        return "gradual decay: no single failure point"

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "hook_hold": round(self.hook_hold, 4),
            "mid_drop": round(self.mid_drop, 4),
            "mid_drop_share": round(self.mid_drop_share, 4),
            "completion": round(self.completion, 4),
            "diagnosis": self.diagnosis,
            "points": [[round(x, 3), round(y, 4)] for x, y in self.points],
        }


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One reading of a post's counters at a known age.

    Immutable and append-only. Overwriting a single row of current values makes
    every matched-age comparison impossible afterwards, and that history cannot
    be reconstructed once it is gone.
    """

    taken_at: datetime
    age_hours: float

    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0
    #: Subscribers or followers gained that are attributable to this post.
    follows: int = 0
    watch_time_s: float = 0.0
    avg_watch_pct: float = 0.0
    retention: RetentionCurve = field(default_factory=RetentionCurve)
    #: Views the platform attributes to being recommended rather than followed.
    impressions: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "taken_at", ensure_utc(self.taken_at))

    # -- derived rates ---------------------------------------------------------

    @property
    def engagement_rate(self) -> float:
        if not self.views:
            return 0.0
        return (self.likes + self.comments + self.shares + self.saves) / self.views

    @property
    def like_rate(self) -> float:
        return self.likes / self.views if self.views else 0.0

    @property
    def comment_rate(self) -> float:
        return self.comments / self.views if self.views else 0.0

    @property
    def share_rate(self) -> float:
        """The rate that drives distribution.

        A share puts the clip in front of an audience the algorithm did not
        choose, which is why it moves reach far more than a like does.
        """
        return self.shares / self.views if self.views else 0.0

    @property
    def follow_rate(self) -> float:
        return self.follows / self.views if self.views else 0.0

    @property
    def view_through_rate(self) -> float:
        """Views per impression — the closest thing to a real CTR.

        This is the number the hook engine's `predicted_lift` was estimating,
        and the only field here that can retire that prior.
        """
        return self.views / self.impressions if self.impressions else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "taken_at": self.taken_at.isoformat(),
            "age_hours": round(self.age_hours, 2),
            "views": self.views,
            "likes": self.likes,
            "comments": self.comments,
            "shares": self.shares,
            "saves": self.saves,
            "follows": self.follows,
            "impressions": self.impressions,
            "avg_watch_pct": round(self.avg_watch_pct, 4),
            "engagement_rate": round(self.engagement_rate, 5),
            "share_rate": round(self.share_rate, 5),
            "view_through_rate": round(self.view_through_rate, 5),
            "retention": self.retention.to_dict(),
        }


@dataclass(slots=True)
class PostMetrics:
    """Every reading taken for one published post."""

    post_id: str
    platform: Platform
    published_at: datetime
    snapshots: list[Snapshot] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.published_at = ensure_utc(self.published_at)
        self.snapshots.sort(key=lambda s: s.age_hours)

    def record(self, snapshot: Snapshot) -> None:
        self.snapshots.append(snapshot)
        self.snapshots.sort(key=lambda s: s.age_hours)

    @property
    def latest(self) -> Snapshot | None:
        return self.snapshots[-1] if self.snapshots else None

    @property
    def age_hours(self) -> float:
        return self.snapshots[-1].age_hours if self.snapshots else 0.0

    def at_age(self, age_hours: float, tolerance: float = 0.25) -> Snapshot | None:
        """The reading closest to `age_hours`, or None if the post is too young.

        Returns None rather than the latest reading when the post has not
        reached that age. Substituting a younger reading is the single most
        common way a dashboard reports that "recent posts are underperforming"
        when what it has measured is that recent posts are recent.
        """
        if not self.snapshots:
            return None

        window = age_hours * tolerance
        candidates = [
            s for s in self.snapshots if abs(s.age_hours - age_hours) <= window
        ]
        if candidates:
            return min(candidates, key=lambda s: abs(s.age_hours - age_hours))

        if self.snapshots[-1].age_hours < age_hours:
            return None   # too young to compare at this checkpoint

        # Old enough, but no reading near the checkpoint: take the closest
        # earlier one rather than inventing an interpolation.
        earlier = [s for s in self.snapshots if s.age_hours <= age_hours]
        return earlier[-1] if earlier else None

    def mature_at(self, age_hours: float) -> bool:
        return bool(self.snapshots) and self.snapshots[-1].age_hours >= age_hours

    def velocity(self, early_h: float = 1.0, late_h: float = 24.0) -> float:
        """Share of a day's views collected in the first hour.

        A proxy for whether the platform pushed the clip. A high ratio means
        the initial audience responded and the algorithm amplified; a low one
        means it found its views slowly, which is a different kind of success
        and often a more durable one.
        """
        early, late = self.at_age(early_h), self.at_age(late_h)
        if not early or not late or not late.views:
            return 0.0
        return early.views / late.views

    def to_dict(self) -> dict[str, Any]:
        return {
            "post_id": self.post_id,
            "platform": self.platform.value,
            "published_at": self.published_at.isoformat(),
            "snapshots": [s.to_dict() for s in self.snapshots],
        }


#: Order-of-magnitude reference points, used to make platforms comparable.
#: Placeholders, and replaced by the account's own history the moment there is
#: enough of it — `Baselines.observed` does exactly that.
DEFAULT_BASELINES: dict[Platform, dict[str, float]] = {
    Platform.TIKTOK: {
        "views_24h": 4000.0, "engagement_rate": 0.075,
        "share_rate": 0.006, "avg_watch_pct": 0.45,
    },
    Platform.YOUTUBE: {
        "views_24h": 1500.0, "engagement_rate": 0.055,
        "share_rate": 0.004, "avg_watch_pct": 0.55,
    },
    Platform.INSTAGRAM: {
        "views_24h": 2500.0, "engagement_rate": 0.045,
        "share_rate": 0.008, "avg_watch_pct": 0.40,
    },
}


@dataclass(slots=True)
class Baselines:
    """Per-platform reference points for normalisation.

    Starts from the defaults above and replaces each figure with the account's
    own median as soon as enough posts exist. The account's own history is a
    far better baseline than any industry figure, and a median rather than a
    mean because one clip that went viral should not redefine "normal".
    """

    values: dict[Platform, dict[str, float]] = field(
        default_factory=lambda: {k: dict(v) for k, v in DEFAULT_BASELINES.items()}
    )
    observed_from: dict[Platform, int] = field(default_factory=dict)

    #: Posts needed before an account's own history replaces the default.
    min_posts: int = 12

    def get(self, platform: Platform, metric: str) -> float:
        return self.values.get(platform, {}).get(metric, 1.0)

    def is_observed(self, platform: Platform) -> bool:
        return self.observed_from.get(platform, 0) >= self.min_posts

    def learn(
        self, metrics: Sequence[PostMetrics],
        checkpoint_h: float = PRIMARY_CHECKPOINT_H,
    ) -> None:
        """Replace defaults with the account's own medians, where possible."""
        by_platform: dict[Platform, list[Snapshot]] = {}
        for record in metrics:
            snapshot = record.at_age(checkpoint_h)
            if snapshot is not None:
                by_platform.setdefault(record.platform, []).append(snapshot)

        for platform, snapshots in by_platform.items():
            if len(snapshots) < self.min_posts:
                continue
            self.values.setdefault(platform, {})
            self.values[platform]["views_24h"] = _median(
                [float(s.views) for s in snapshots]
            )
            self.values[platform]["engagement_rate"] = _median(
                [s.engagement_rate for s in snapshots]
            )
            self.values[platform]["share_rate"] = _median(
                [s.share_rate for s in snapshots]
            )
            self.values[platform]["avg_watch_pct"] = _median(
                [s.avg_watch_pct for s in snapshots]
            )
            self.observed_from[platform] = len(snapshots)

    def index(self, platform: Platform, metric: str, value: float) -> float:
        """A metric as a multiple of its platform baseline.

        100 is baseline. This is what makes a TikTok post and a Shorts post
        comparable at all, and every cross-platform figure in the reports is
        expressed this way rather than as a raw count.
        """
        reference = self.get(platform, metric)
        if reference <= 0:
            return 100.0
        return 100.0 * value / reference

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_posts": self.min_posts,
            "platforms": {
                platform.value: {
                    "observed": self.is_observed(platform),
                    "from_posts": self.observed_from.get(platform, 0),
                    "values": {k: round(v, 5) for k, v in sorted(values.items())},
                }
                for platform, values in sorted(
                    self.values.items(), key=lambda kv: kv[0].value
                )
            },
        }


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0
