"""The content calendar: what is booked, what collides, what will not fit.

A calendar is not a list of scheduled posts with a month view drawn round it.
Its job is to answer the questions that stop a customer shipping a bad
schedule *before* it runs:

- Does this day already exceed what the platform will accept?
- Are two posts to the same account close enough together to cannibalise each
  other?
- Is a bulk import of 200 clips going to fit in the next 30 days at all, or is
  it silently going to run until March?

The daily caps are hard platform limits, and the third question is the one that
matters most for bulk uploads. YouTube's six-a-day is per API *project*, so a
200-clip import is a 34-day job at minimum no matter how it is spread — and
telling someone that when they press the button is worth more than any amount
of retry logic afterwards.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

from .limits import limits_for
from .types import (
    Platform,
    PostState,
    ScheduledPost,
    TERMINAL_STATES,
    UTC,
    ensure_utc,
)

#: Two posts to the same account closer together than this are flagged. Not a
#: platform rule — a distribution one. Back-to-back posts split their own
#: audience, and the second reliably underperforms.
MIN_SPACING_S = 90 * 60

#: Posts counted against a platform's rolling daily cap.
COUNTED_STATES = frozenset({
    PostState.SCHEDULED, PostState.CLAIMED, PostState.UPLOADING,
    PostState.PROCESSING, PostState.RETRYING, PostState.PUBLISHED,
    # A draft in the creator's inbox has already spent an upload against the
    # platform's daily allowance, live or not.
    PostState.AWAITING_CREATOR,
})


@dataclass(frozen=True, slots=True)
class Conflict:
    kind: str            # "spacing" | "daily_cap" | "project_quota"
    when: datetime
    account_id: str
    platform: Platform
    detail: str
    post_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "when": self.when.isoformat(),
            "account_id": self.account_id,
            "platform": self.platform.value,
            "detail": self.detail,
            "post_ids": list(self.post_ids),
        }


@dataclass(frozen=True, slots=True)
class DaySlot:
    """One day's occupancy for one account."""

    day: date
    account_id: str
    platform: Platform
    count: int
    cap: int

    @property
    def full(self) -> bool:
        return self.count >= self.cap

    @property
    def remaining(self) -> int:
        return max(0, self.cap - self.count)


class ContentCalendar:
    """A view over scheduled posts, in the creator's timezone."""

    def __init__(self, posts: Iterable[ScheduledPost] = (), tz: str = "UTC") -> None:
        self._posts: dict[str, ScheduledPost] = {}
        self.timezone = tz
        for post in posts:
            self.add(post)

    # -- membership -----------------------------------------------------------

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def add(self, post: ScheduledPost) -> None:
        self._posts[post.post_id] = post

    def remove(self, post_id: str) -> None:
        self._posts.pop(post_id, None)

    def get(self, post_id: str) -> ScheduledPost | None:
        return self._posts.get(post_id)

    @property
    def posts(self) -> tuple[ScheduledPost, ...]:
        return tuple(sorted(self._posts.values(), key=lambda p: p.run_at))

    def __len__(self) -> int:
        return len(self._posts)

    # -- queries --------------------------------------------------------------

    def between(
        self, start: datetime, end: datetime,
        account_id: str = "", platform: Platform | None = None,
    ) -> tuple[ScheduledPost, ...]:
        start, end = ensure_utc(start), ensure_utc(end)
        return tuple(
            post for post in self.posts
            if start <= post.run_at <= end
            and (not account_id or post.account_id == account_id)
            and (platform is None or post.platform is platform)
        )

    def due(self, now: datetime) -> tuple[ScheduledPost, ...]:
        """Posts whose moment has arrived and which are ready to run."""
        now = ensure_utc(now)
        ready: list[ScheduledPost] = []
        for post in self.posts:
            if post.state not in (PostState.SCHEDULED, PostState.RETRYING):
                continue
            when = post.next_attempt_at or post.run_at
            if when <= now:
                ready.append(post)
        return tuple(ready)

    def local_day(self, post: ScheduledPost) -> date:
        return post.run_at.astimezone(self.zone).date()

    # -- occupancy ------------------------------------------------------------

    def occupancy(
        self, start: date, end: date
    ) -> dict[tuple[date, str], DaySlot]:
        """Per-day, per-account counts against each platform's cap."""
        counts: dict[tuple[date, str], int] = defaultdict(int)
        platforms: dict[str, Platform] = {}

        for post in self.posts:
            if post.state not in COUNTED_STATES:
                continue
            day = self.local_day(post)
            if not (start <= day <= end):
                continue
            counts[(day, post.account_id)] += 1
            platforms[post.account_id] = post.platform

        return {
            key: DaySlot(
                day=key[0], account_id=key[1], platform=platforms[key[1]],
                count=value, cap=limits_for(platforms[key[1]]).rate.posts_per_day,
            )
            for key, value in counts.items()
        }

    def conflicts(self, start: date | None = None,
                  end: date | None = None) -> list[Conflict]:
        """Everything wrong with the current schedule."""
        posts = [p for p in self.posts if p.state not in TERMINAL_STATES
                 or p.state is PostState.PUBLISHED]
        if start:
            posts = [p for p in posts if self.local_day(p) >= start]
        if end:
            posts = [p for p in posts if self.local_day(p) <= end]

        problems: list[Conflict] = []

        # Spacing, per account.
        by_account: dict[str, list[ScheduledPost]] = defaultdict(list)
        for post in posts:
            by_account[post.account_id].append(post)

        for account_id, group in by_account.items():
            group.sort(key=lambda p: p.run_at)
            for first, second in zip(group, group[1:]):
                gap = (second.run_at - first.run_at).total_seconds()
                if gap < MIN_SPACING_S:
                    problems.append(Conflict(
                        kind="spacing",
                        when=second.run_at,
                        account_id=account_id,
                        platform=second.platform,
                        detail=(
                            f"{int(gap / 60)} minutes after the previous post "
                            f"— under the {MIN_SPACING_S // 60}-minute spacing "
                            f"floor, and the second will split its own audience"
                        ),
                        post_ids=(first.post_id, second.post_id),
                    ))

        # Daily caps, per account per local day.
        daily: dict[tuple[date, str], list[ScheduledPost]] = defaultdict(list)
        for post in posts:
            daily[(self.local_day(post), post.account_id)].append(post)

        for (day, account_id), group in sorted(daily.items()):
            cap = limits_for(group[0].platform).rate.posts_per_day
            if len(group) > cap:
                problems.append(Conflict(
                    kind="daily_cap",
                    when=group[0].run_at,
                    account_id=account_id,
                    platform=group[0].platform,
                    detail=(
                        f"{len(group)} posts on {day} but "
                        f"{group[0].platform.value} accepts {cap} a day — "
                        f"{len(group) - cap} will be rejected"
                    ),
                    post_ids=tuple(p.post_id for p in group),
                ))

        # Project-scoped quota: YouTube's cap is shared across every channel.
        youtube_days: dict[date, list[ScheduledPost]] = defaultdict(list)
        for post in posts:
            if post.platform is Platform.YOUTUBE:
                youtube_days[self.local_day(post)].append(post)

        project_cap = limits_for(Platform.YOUTUBE).rate.posts_per_day
        for day, group in sorted(youtube_days.items()):
            if len(group) > project_cap:
                accounts = {p.account_id for p in group}
                problems.append(Conflict(
                    kind="project_quota",
                    when=group[0].run_at,
                    account_id=",".join(sorted(accounts)),
                    platform=Platform.YOUTUBE,
                    detail=(
                        f"{len(group)} YouTube uploads on {day} across "
                        f"{len(accounts)} channel(s), but the daily quota is "
                        f"{project_cap} for the whole API project — adding "
                        f"channels does not raise it"
                    ),
                    post_ids=tuple(p.post_id for p in group),
                ))

        problems.sort(key=lambda c: (c.when, c.kind))
        return problems

    # -- capacity planning -----------------------------------------------------

    def next_free_slot(
        self,
        account_id: str,
        platform: Platform,
        earliest: datetime,
        preferred_times: Sequence[datetime] = (),
        spacing_s: int = MIN_SPACING_S,
        horizon_days: int = 400,
    ) -> datetime | None:
        """The first moment this account can take another post.

        Respects the daily cap and the spacing floor. Returns None when the
        horizon fills up, which is the honest answer for a bulk import that
        does not fit.
        """
        earliest = ensure_utc(earliest)
        cap = limits_for(platform).rate.posts_per_day
        existing = sorted(
            (p for p in self.posts
             if p.account_id == account_id and p.state in COUNTED_STATES),
            key=lambda p: p.run_at,
        )

        candidates = [ensure_utc(t) for t in preferred_times if ensure_utc(t) >= earliest]
        if not candidates:
            candidates = [earliest]

        limit = earliest + timedelta(days=horizon_days)
        for candidate in candidates:
            cursor = candidate
            while cursor <= limit:
                day = cursor.astimezone(self.zone).date()
                same_day = [p for p in existing
                            if p.run_at.astimezone(self.zone).date() == day]
                if len(same_day) >= cap:
                    # Day is full; jump to the start of the next one.
                    next_day = datetime.combine(
                        day + timedelta(days=1), cursor.astimezone(self.zone).timetz()
                    )
                    cursor = next_day.astimezone(UTC)
                    continue

                too_close = any(
                    abs((p.run_at - cursor).total_seconds()) < spacing_s
                    for p in existing
                )
                if too_close:
                    cursor += timedelta(seconds=spacing_s)
                    continue

                return cursor
        return None

    def capacity_forecast(
        self, platform: Platform, count: int, start: datetime,
        accounts: Sequence[str] = (),
    ) -> dict[str, object]:
        """How long `count` posts will take to drain, and why.

        The answer a bulk-upload button should show before it is pressed.
        """
        entry = limits_for(platform)
        cap = entry.rate.posts_per_day
        account_count = max(1, len(accounts))

        if entry.rate.quota_scope == "project":
            per_day = cap
            explanation = (
                f"{cap} a day for the whole API project, shared across "
                f"{account_count} account(s) — connecting more does not help"
            )
        else:
            per_day = cap * account_count
            explanation = (
                f"{cap} a day per account across {account_count} account(s)"
            )

        days = -(-count // per_day) if per_day else 0
        finish = ensure_utc(start) + timedelta(days=max(0, days - 1))

        return {
            "platform": platform.value,
            "requested": count,
            "per_day": per_day,
            "quota_scope": entry.rate.quota_scope,
            "days_required": days,
            "finishes_on": finish.date().isoformat(),
            "explanation": explanation,
        }

    # -- presentation -----------------------------------------------------------

    def month_view(self, year: int, month: int) -> dict[str, object]:
        """A month of the calendar, grouped by local day."""
        import calendar as _calendar

        last = _calendar.monthrange(year, month)[1]
        first_day, last_day = date(year, month, 1), date(year, month, last)

        grid: dict[str, list[dict[str, object]]] = {}
        for post in self.posts:
            day = self.local_day(post)
            if not (first_day <= day <= last_day):
                continue
            grid.setdefault(day.isoformat(), []).append({
                "post_id": post.post_id,
                "time": post.run_at.astimezone(self.zone).strftime("%H:%M"),
                "platform": post.platform.value,
                "account_id": post.account_id,
                "state": post.state.value,
                "title": post.spec.title or post.spec.asset.asset_id,
            })

        for entries in grid.values():
            entries.sort(key=lambda e: str(e["time"]))

        occupancy = self.occupancy(first_day, last_day)
        return {
            "year": year,
            "month": month,
            "timezone": self.timezone,
            "days": grid,
            "total": sum(len(v) for v in grid.values()),
            "full_days": sorted({
                slot.day.isoformat() for slot in occupancy.values() if slot.full
            }),
        }

    def summary(self) -> dict[str, object]:
        by_state: dict[str, int] = defaultdict(int)
        by_platform: dict[str, int] = defaultdict(int)
        for post in self.posts:
            by_state[post.state.value] += 1
            by_platform[post.platform.value] += 1

        upcoming = [p for p in self.posts if p.state is PostState.SCHEDULED]
        return {
            "total": len(self._posts),
            "by_state": dict(sorted(by_state.items())),
            "by_platform": dict(sorted(by_platform.items())),
            "first_scheduled": (
                upcoming[0].run_at.isoformat() if upcoming else None
            ),
            "last_scheduled": (
                upcoming[-1].run_at.isoformat() if upcoming else None
            ),
            "conflicts": len(self.conflicts()),
        }
