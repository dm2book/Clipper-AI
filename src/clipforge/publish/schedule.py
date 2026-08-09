"""Recurring schedules, computed in the creator's own timezone.

"Every weekday at 5pm" is a statement about local wall-clock time, and the
only correct way to expand it months ahead is to generate local times in an
IANA zone and convert each one to UTC individually. Storing the rule as a UTC
cron looks equivalent and is wrong twice a year: the entire schedule silently
shifts by an hour at each DST transition, and nobody notices until a customer
asks why their 5pm posts are going out at 4pm.

Two transitions need handling explicitly, and neither is hypothetical:

**Spring forward.** 02:30 does not exist on the day the clocks jump. Python
will happily construct that datetime and give it the pre-transition offset,
which silently resolves to 03:30 — so a 02:30 daily schedule posts an hour
late on that one day. `NonexistentTime` says what to do about it.

**Fall back.** 01:30 happens twice. A naive expansion emits both, and the same
post goes out twice an hour apart. The default takes the first occurrence only.
"""

from __future__ import annotations

import calendar as _calendar
import enum
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterator, Sequence
from zoneinfo import ZoneInfo

from .types import UTC, ensure_utc, utcnow


class Frequency(str, enum.Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class NonexistentTime(str, enum.Enum):
    """What to do when a local time does not exist on a given day."""

    SKIP = "skip"        # no post that day
    SHIFT = "shift"      # take the moment the clocks land on
    NEXT_HOUR = "next_hour"


class AmbiguousTime(str, enum.Enum):
    """What to do when a local time happens twice."""

    FIRST = "first"      # pre-transition — the default, and posts once
    SECOND = "second"    # post-transition
    BOTH = "both"        # deliberately post twice; almost never wanted


#: Monday is 0, matching `datetime.weekday()`.
WEEKDAYS = (0, 1, 2, 3, 4)
WEEKEND = (5, 6)
EVERY_DAY = (0, 1, 2, 3, 4, 5, 6)


def is_nonexistent(local: datetime, zone: ZoneInfo) -> bool:
    """Whether this local wall-clock time is skipped by a DST jump.

    Detected by round-tripping through UTC: a time inside the gap comes back
    as a different wall-clock time.
    """
    naive = local.replace(tzinfo=None)
    attached = naive.replace(tzinfo=zone)
    return attached.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != naive


def is_ambiguous(local: datetime, zone: ZoneInfo) -> bool:
    """Whether this local wall-clock time occurs twice."""
    naive = local.replace(tzinfo=None)
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    return first.utcoffset() != second.utcoffset()


def resolve_local(
    naive: datetime,
    zone: ZoneInfo,
    nonexistent: NonexistentTime = NonexistentTime.SHIFT,
    ambiguous: AmbiguousTime = AmbiguousTime.FIRST,
) -> list[datetime]:
    """Turn a naive local time into zero, one or two UTC instants."""
    attached = naive.replace(tzinfo=zone)

    if is_nonexistent(attached, zone):
        if nonexistent is NonexistentTime.SKIP:
            return []
        if nonexistent is NonexistentTime.NEXT_HOUR:
            bumped = (naive + timedelta(hours=1)).replace(tzinfo=zone)
            return [bumped.astimezone(UTC)]
        # SHIFT: whatever instant the clocks actually land on.
        return [attached.astimezone(UTC)]

    if is_ambiguous(attached, zone):
        first = naive.replace(tzinfo=zone, fold=0).astimezone(UTC)
        second = naive.replace(tzinfo=zone, fold=1).astimezone(UTC)
        if ambiguous is AmbiguousTime.FIRST:
            return [first]
        if ambiguous is AmbiguousTime.SECOND:
            return [second]
        return [first, second]

    return [attached.astimezone(UTC)]


@dataclass(frozen=True, slots=True)
class Recurrence:
    """A repeating posting slot, expressed in local time.

    `times` holds local wall-clock times, and `timezone` is the IANA zone they
    are meant in. Neither can be dropped: 17:00 alone is not a schedule, and
    17:00 UTC is not what anyone asked for.
    """

    frequency: Frequency
    times: tuple[time, ...]
    timezone: str = "UTC"
    #: WEEKLY: which weekdays. MONTHLY: ignored.
    weekdays: tuple[int, ...] = EVERY_DAY
    #: MONTHLY: days of the month. -1 means the last day.
    month_days: tuple[int, ...] = (1,)
    #: Every N periods. 2 with WEEKLY is fortnightly.
    interval: int = 1
    starts_on: date | None = None
    ends_on: date | None = None
    max_occurrences: int = 0
    nonexistent: NonexistentTime = NonexistentTime.SHIFT
    ambiguous: AmbiguousTime = AmbiguousTime.FIRST
    series_id: str = ""

    def __post_init__(self) -> None:
        if not self.times:
            raise ValueError("a recurrence needs at least one time of day")
        if self.interval < 1:
            raise ValueError("interval must be at least 1")
        ZoneInfo(self.timezone)   # fail loudly on a bad zone, at build time
        if self.frequency is Frequency.WEEKLY and not self.weekdays:
            raise ValueError("a weekly recurrence needs at least one weekday")

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def _candidate_dates(self, start: date, end: date) -> Iterator[date]:
        anchor = self.starts_on or start

        if self.frequency is Frequency.DAILY:
            day = max(start, anchor)
            while day <= end:
                if ((day - anchor).days % self.interval) == 0:
                    yield day
                day += timedelta(days=1)
            return

        if self.frequency is Frequency.WEEKLY:
            # Interval counts weeks from the anchor's own week.
            anchor_week = anchor - timedelta(days=anchor.weekday())
            day = max(start, anchor)
            while day <= end:
                if day.weekday() in self.weekdays:
                    week = day - timedelta(days=day.weekday())
                    if (((week - anchor_week).days // 7) % self.interval) == 0:
                        yield day
                day += timedelta(days=1)
            return

        # MONTHLY. Walk months rather than days so "the 31st" behaves.
        year, month = anchor.year, anchor.month
        while True:
            first = date(year, month, 1)
            if first > end:
                return
            months_since = (year - anchor.year) * 12 + (month - anchor.month)
            if months_since >= 0 and months_since % self.interval == 0:
                last_day = _calendar.monthrange(year, month)[1]
                for requested in sorted(self.month_days):
                    day_number = last_day if requested == -1 else requested
                    # A 31st that does not exist this month is skipped, not
                    # rolled into the 1st of the next one.
                    if 1 <= day_number <= last_day:
                        day = date(year, month, day_number)
                        if start <= day <= end:
                            yield day
            month += 1
            if month > 12:
                month, year = 1, year + 1

    def occurrences(
        self, start: datetime, end: datetime
    ) -> list[datetime]:
        """Every UTC instant this rule fires in `[start, end]`.

        Both bounds are *instants*, inclusive — not dates. Passing midnight as
        `end` therefore excludes everything later that day, which is rarely
        what a caller reading "1 May to 7 May" has in mind. Pass the end of the
        final day when a whole-day range is meant.
        """
        start = ensure_utc(start)
        end = ensure_utc(end)
        zone = self.zone

        # Widen by a day at each edge: a local time near midnight can fall on
        # the far side of the boundary once converted.
        first_day = (start.astimezone(zone) - timedelta(days=1)).date()
        last_day = (end.astimezone(zone) + timedelta(days=1)).date()
        if self.ends_on:
            last_day = min(last_day, self.ends_on)

        results: list[datetime] = []
        for day in self._candidate_dates(first_day, last_day):
            if self.starts_on and day < self.starts_on:
                continue
            if self.ends_on and day > self.ends_on:
                break
            for slot in self.times:
                naive = datetime.combine(day, slot)
                for moment in resolve_local(
                    naive, zone, self.nonexistent, self.ambiguous
                ):
                    if start <= moment <= end:
                        results.append(moment)

        results.sort()
        if self.max_occurrences:
            results = results[: self.max_occurrences]
        return results

    def next_after(self, moment: datetime, horizon_days: int = 400) -> datetime | None:
        """The first firing strictly after `moment`, or None."""
        moment = ensure_utc(moment)
        window = self.occurrences(
            moment + timedelta(seconds=1),
            moment + timedelta(days=horizon_days),
        )
        return window[0] if window else None

    def describe(self) -> str:
        slots = ", ".join(t.strftime("%H:%M") for t in self.times)
        every = "" if self.interval == 1 else f"every {self.interval} "

        if self.frequency is Frequency.DAILY:
            unit = "days" if self.interval > 1 else "day"
            body = f"{every or 'every '}{unit}"
        elif self.frequency is Frequency.WEEKLY:
            names = [_calendar.day_abbr[d] for d in sorted(self.weekdays)]
            unit = "weeks on " if self.interval > 1 else ""
            body = f"{every or 'every '}{unit}{', '.join(names)}"
        else:
            # -1 means the last day of the month, so it sorts *after* the
            # numbered days rather than before them.
            ordered = sorted(self.month_days, key=lambda d: (d == -1, d))
            days = ["last" if d == -1 else _ordinal(d) for d in ordered]
            unit = "months" if self.interval > 1 else "month"
            body = f"{every or 'every '}{unit} on the {', '.join(days)}"

        return f"{body} at {slots} ({self.timezone})"

    def to_dict(self) -> dict[str, object]:
        return {
            "series_id": self.series_id,
            "frequency": self.frequency.value,
            "times": [t.strftime("%H:%M") for t in self.times],
            "timezone": self.timezone,
            "weekdays": list(self.weekdays),
            "month_days": list(self.month_days),
            "interval": self.interval,
            "starts_on": self.starts_on.isoformat() if self.starts_on else None,
            "ends_on": self.ends_on.isoformat() if self.ends_on else None,
            "nonexistent": self.nonexistent.value,
            "ambiguous": self.ambiguous.value,
            "description": self.describe(),
        }


def _ordinal(number: int) -> str:
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def daily(hour: int, minute: int = 0, tz: str = "UTC", **kwargs) -> Recurrence:
    return Recurrence(Frequency.DAILY, (time(hour, minute),), tz, **kwargs)


def weekdays_at(hour: int, minute: int = 0, tz: str = "UTC", **kwargs) -> Recurrence:
    return Recurrence(
        Frequency.WEEKLY, (time(hour, minute),), tz, weekdays=WEEKDAYS, **kwargs
    )


def weekly_on(
    days: Sequence[int], hour: int, minute: int = 0, tz: str = "UTC", **kwargs
) -> Recurrence:
    return Recurrence(
        Frequency.WEEKLY, (time(hour, minute),), tz,
        weekdays=tuple(days), **kwargs
    )


def monthly_on(
    days: Sequence[int], hour: int, minute: int = 0, tz: str = "UTC", **kwargs
) -> Recurrence:
    return Recurrence(
        Frequency.MONTHLY, (time(hour, minute),), tz,
        month_days=tuple(days), **kwargs
    )


@dataclass(frozen=True, slots=True)
class DstReport:
    """DST transitions a schedule will cross, and what it will do about them."""

    skipped: tuple[datetime, ...] = ()
    shifted: tuple[datetime, ...] = ()
    doubled: tuple[datetime, ...] = ()

    @property
    def clean(self) -> bool:
        return not (self.skipped or self.shifted or self.doubled)

    def to_dict(self) -> dict[str, object]:
        return {
            "skipped": [d.isoformat() for d in self.skipped],
            "shifted": [d.isoformat() for d in self.shifted],
            "doubled": [d.isoformat() for d in self.doubled],
            "clean": self.clean,
        }


def dst_report(
    rule: Recurrence, start: datetime, end: datetime
) -> DstReport:
    """Which occurrences in a window are affected by a clock change.

    Worth surfacing before a customer commits to a quarter of scheduling. A
    daily 02:30 slot in a DST-observing zone will do something surprising
    exactly once in spring and once in autumn, and it is far better for them
    to hear that now than to file a bug in March.
    """
    zone = rule.zone
    start = ensure_utc(start)
    end = ensure_utc(end)

    skipped: list[datetime] = []
    shifted: list[datetime] = []
    doubled: list[datetime] = []

    first_day = start.astimezone(zone).date()
    last_day = end.astimezone(zone).date()

    for day in rule._candidate_dates(first_day, last_day):
        for slot in rule.times:
            naive = datetime.combine(day, slot)
            attached = naive.replace(tzinfo=zone)
            if is_nonexistent(attached, zone):
                target = skipped if rule.nonexistent is NonexistentTime.SKIP \
                    else shifted
                target.append(naive.replace(tzinfo=zone))
            elif is_ambiguous(attached, zone):
                if rule.ambiguous is AmbiguousTime.BOTH:
                    doubled.append(naive.replace(tzinfo=zone))

    return DstReport(tuple(skipped), tuple(shifted), tuple(doubled))
