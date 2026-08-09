"""The weekly report.

A report nobody reads is worse than no report, because it costs the same to
produce and provides cover for not looking. Three rules shape this one:

**Lead with what changed, not with what is.** Totals are a dashboard's job. A
weekly report exists to say what is different from last week and whether the
difference is real.

**Say "we cannot tell" out loud, and say what it would take.** Most weeks on
most channels, the honest answer to four of the five questions is that there is
not enough data yet. Burying that under a ranked table is how a creator ends up
reorganising their schedule around three posts of noise.

**Never report a week-on-week change without asking whether it is noise.** Two
weeks of a small channel differ by 30% on nothing at all. Every delta here
carries a significance test, and an insignificant one is printed as flat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Sequence

from ..publish.types import Platform, ensure_utc
from .attribution import AnalyticsStore, PostRecord
from .insights import (
    Calibration,
    Insight,
    RetentionDiagnosis,
    best_clip_lengths,
    best_creators,
    best_hooks,
    best_posting_times,
    best_topics,
    calibration,
    diagnose_retention,
)
from .metrics import Baselines, PRIMARY_CHECKPOINT_H
from .stats import MIN_MATERIAL_EFFECT, permutation_p, trimmed_mean


@dataclass(frozen=True, slots=True)
class Delta:
    """A week-on-week change, with a verdict on whether it is real."""

    metric: str
    current: float
    previous: float
    n_current: int
    n_previous: int
    p_value: float = 1.0
    significant: bool = False

    @property
    def change(self) -> float:
        if not self.previous:
            return 0.0
        return (self.current - self.previous) / self.previous

    @property
    def arrow(self) -> str:
        if not self.significant:
            return "="
        return "↑" if self.change > 0 else "↓"

    def describe(self) -> str:
        if not self.significant:
            reason = (
                "too small to matter" if abs(self.change) < MIN_MATERIAL_EFFECT
                else "inside the noise"
            )
            return (
                f"{self.metric}: {self.current:.4g} "
                f"(was {self.previous:.4g} — {reason})"
            )
        return (
            f"{self.metric}: {self.current:.4g} "
            f"{self.change * 100:+.0f}% on last week (p={self.p_value:.3f})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "current": round(self.current, 5),
            "previous": round(self.previous, 5),
            "change": round(self.change, 4),
            "n_current": self.n_current,
            "n_previous": self.n_previous,
            "p_value": round(self.p_value, 4),
            "significant": self.significant,
            "description": self.describe(),
        }


@dataclass(slots=True)
class WeeklyReport:
    """One week, for one scope."""

    week_start: datetime
    week_end: datetime
    scope: str = "all channels"

    posts: int = 0
    mature_posts: int = 0
    totals: dict[str, int] = field(default_factory=dict)
    deltas: list[Delta] = field(default_factory=list)
    insights: list[Insight] = field(default_factory=list)
    retention: RetentionDiagnosis | None = None
    calibrations: list[Calibration] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> list[Insight]:
        return [i for i in self.insights if i.actionable]

    @property
    def waiting(self) -> list[Insight]:
        return [i for i in self.insights if not i.actionable]

    def to_dict(self) -> dict[str, Any]:
        return {
            "week": {
                "start": self.week_start.isoformat(),
                "end": self.week_end.isoformat(),
                "scope": self.scope,
            },
            "posts": self.posts,
            "mature_posts": self.mature_posts,
            "totals": self.totals,
            "deltas": [d.to_dict() for d in self.deltas],
            "actionable": [i.to_dict() for i in self.actionable],
            "waiting": [i.to_dict() for i in self.waiting],
            "retention": self.retention.to_dict() if self.retention else None,
            "calibrations": [c.to_dict() for c in self.calibrations],
            "notes": list(self.notes),
        }

    def render(self, width: int = 74) -> str:
        """Plain text, for email or a terminal."""
        line = "=" * width
        out: list[str] = [
            line,
            f"  WEEKLY REPORT — {self.scope}",
            f"  {self.week_start:%d %b} to {self.week_end:%d %b %Y}",
            line,
            "",
            f"  {self.posts} posts published, {self.mature_posts} old enough "
            f"to measure",
        ]

        if self.totals:
            out.append("")
            for name, value in self.totals.items():
                out.append(f"    {name:<16} {value:>12,}")

        if self.deltas:
            out += ["", "  CHANGE ON LAST WEEK", ""]
            for delta in self.deltas:
                out.append(f"    {delta.arrow} {delta.describe()}")

        if self.retention and self.retention.n:
            out += [
                "", "  WHERE THE AUDIENCE LEAVES", "",
                f"    past the hook   "
                f"{self.retention.median_hook_hold * 100:.0f}%",
                f"    of those, lost  "
                f"{self.retention.median_mid_drop_share * 100:.0f}%",
                f"    reach the end   "
                f"{self.retention.median_completion * 100:.0f}%",
                "", f"    {self.retention.dominant_problem}",
            ]

        actionable = self.actionable
        out += ["", f"  FINDINGS ({len(actionable)} actionable)", ""]
        if actionable:
            for insight in actionable:
                out.append(f"    ✓ {insight.question}")
                out.append(f"      {insight.recommendation}")
                if not insight.validity.causal and insight.validity.caveat:
                    out.append(f"      ⚠ {insight.validity.caveat}")
        else:
            out.append("    Nothing this week clears the significance bar.")

        waiting = self.waiting
        if waiting:
            out += ["", f"  NOT YET ANSWERABLE ({len(waiting)})", ""]
            for insight in waiting:
                out.append(f"    · {insight.question}")
                out.append(f"      {insight.recommendation}")

        if self.calibrations:
            out += ["", "  ARE THE MODELS ANY GOOD?", ""]
            for entry in self.calibrations:
                out.append(f"    {entry.model} ({entry.weights_version})")
                out.append(f"      n={entry.n}  {entry.verdict}")

        if self.notes:
            out += ["", "  NOTES", ""]
            out += [f"    {note}" for note in self.notes]

        out += ["", line, ""]
        return "\n".join(out)


def _totals(records: Sequence[PostRecord]) -> dict[str, int]:
    fields = ("views", "likes", "comments", "shares", "follows")
    totals = {name: 0 for name in fields}
    for record in records:
        latest = record.metrics.latest
        if latest is None:
            continue
        for name in fields:
            totals[name] += getattr(latest, name, 0)
    totals["subscribers"] = totals.pop("follows")
    return totals


def _delta(
    metric: str, current: Sequence[PostRecord], previous: Sequence[PostRecord],
    checkpoint_h: float, seed: str,
) -> Delta:
    now = [v for r in current if (v := r.value(metric, checkpoint_h)) is not None]
    before = [v for r in previous if (v := r.value(metric, checkpoint_h)) is not None]

    if len(now) < 3 or len(before) < 3:
        return Delta(
            metric, trimmed_mean(now), trimmed_mean(before),
            len(now), len(before), 1.0, False,
        )

    current, previous = trimmed_mean(now), trimmed_mean(before)
    p_value = permutation_p(now, before, trimmed_mean, seed=f"{seed}|{metric}")

    # Both bars. A 3% shift on a tight distribution is statistically
    # undeniable and operationally irrelevant; reporting it as a change
    # teaches the reader to skip this section.
    change = (current - previous) / previous if previous else 0.0
    flagged = p_value < 0.05 and abs(change) >= MIN_MATERIAL_EFFECT

    return Delta(metric, current, previous, len(now), len(before),
                 p_value, flagged)


def build_weekly(
    store: AnalyticsStore,
    week_end: datetime,
    scope: str = "all channels",
    channel_id: str = "",
    platform: Platform | None = None,
    checkpoint_h: float = PRIMARY_CHECKPOINT_H,
    lookback_weeks: int = 8,
    seed: str = "clipforge",
    baselines: Baselines | None = None,
) -> WeeklyReport:
    """Build one week's report.

    The week's *totals* come from the week. The *findings* deliberately do not:
    they are computed over `lookback_weeks`, because no channel publishes
    enough in seven days to answer any of the five questions, and recomputing
    them weekly on a week of data would produce a different confident winner
    every Monday.
    """
    week_end = ensure_utc(week_end)
    week_start = week_end - timedelta(days=7)
    previous_start = week_start - timedelta(days=7)

    current = store.select(
        since=week_start, until=week_end, channel_id=channel_id,
        platform=platform, require_mature=False,
    )
    mature = [r for r in current if r.mature(checkpoint_h)]
    previous = store.select(
        since=previous_start, until=week_start, channel_id=channel_id,
        platform=platform, checkpoint_h=checkpoint_h,
    )

    window = store.select(
        since=week_end - timedelta(weeks=lookback_weeks), until=week_end,
        channel_id=channel_id, platform=platform, checkpoint_h=checkpoint_h,
    )

    report = WeeklyReport(
        week_start=week_start, week_end=week_end, scope=scope,
        posts=len(current), mature_posts=len(mature),
        totals=_totals(current),
    )

    for metric in ("views", "engagement_rate", "share_rate", "avg_watch_pct"):
        report.deltas.append(
            _delta(metric, mature, previous, checkpoint_h, seed)
        )

    report.insights = [
        *best_posting_times(store, window, checkpoint_h, seed),
        *best_hooks(store, window, checkpoint_h, seed),
        *best_topics(store, window, checkpoint_h, seed),
        *best_clip_lengths(store, window, checkpoint_h, seed),
        *best_creators(store, window, checkpoint_h, seed),
    ]
    report.retention = diagnose_retention(window, checkpoint_h)
    report.calibrations = [
        calibration(window, "predicted_lift", "view_through_rate", checkpoint_h),
        calibration(window, "predicted_virality", "views", checkpoint_h),
    ]

    immature = len(current) - len(mature)
    if immature:
        report.notes.append(
            f"{immature} of this week's {len(current)} posts are younger than "
            f"{checkpoint_h:.0f}h and are excluded from every comparison — "
            f"including them would report their age as underperformance."
        )
    report.notes.append(
        f"Findings are computed over {lookback_weeks} weeks "
        f"({len(window)} posts), not over this week alone. A week is not "
        f"enough data to answer any of these questions, and recomputing them "
        f"weekly on a week would produce a different winner every Monday."
    )
    if baselines is not None:
        unobserved = [
            p.value for p in Platform if not baselines.is_observed(p)
        ]
        if unobserved:
            report.notes.append(
                f"Baselines for {', '.join(unobserved)} are still the built-in "
                f"placeholders — cross-platform indices are indicative only "
                f"until this account has {baselines.min_posts} posts on each."
            )

    return report
