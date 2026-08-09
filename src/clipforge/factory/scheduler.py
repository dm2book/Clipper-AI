"""Allocating a shared quota across channels that are supposed to be independent.

Six of the seven ways a channel can be independent are a matter of code
structure — separate queues, separate budgets, separate breakers. The seventh
is not.

**YouTube's upload quota is per API project, not per channel.** Ten thousand
units a day at sixteen hundred per upload is six uploads a day *in total*,
shared by every channel in the factory and every customer on the plan.
Connecting more channels does not raise it; it divides it. Seven channels each
wanting two YouTube posts a day is fourteen against six, and no amount of
process isolation changes the arithmetic.

That leaves two options and this module implements the honest one. It could
let channels race and have the losers fail at post time with a quota error —
which is what happens by default, and which looks like a flaky product. Or it
can allocate the scarce resource up front, tell each channel what it actually
gets, and report the shortfall as a number someone can act on. The number is
the point: "your factory is over-subscribed by 8 YouTube posts a day, raise
the quota or drop two channels" is a decision, where a scattering of
`quotaExceeded` errors is a mystery.

Allocation is **max-min fair**: everyone gets an equal share, and a channel
wanting less than its share releases the remainder to the others rather than
letting it go unused. A channel asking for one post a day is not penalised for
sharing a factory with a channel asking for four.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from ..publish.limits import limits_for
from ..publish.types import Platform
from .channel import Channel


@dataclass(frozen=True, slots=True)
class Demand:
    channel_id: str
    platform: Platform
    wanted: int


@dataclass(frozen=True, slots=True)
class Allocation:
    channel_id: str
    platform: Platform
    wanted: int
    granted: int

    @property
    def shortfall(self) -> int:
        return max(0, self.wanted - self.granted)

    @property
    def satisfied(self) -> bool:
        return self.granted >= self.wanted

    def to_dict(self) -> dict[str, object]:
        return {
            "channel_id": self.channel_id,
            "platform": self.platform.value,
            "wanted": self.wanted,
            "granted": self.granted,
            "shortfall": self.shortfall,
        }


def max_min_fair(wants: dict[str, int], capacity: int) -> dict[str, int]:
    """Split `capacity` across `wants` so nobody is starved by a greedy peer.

    Equal shares, with anything a channel does not want flowing back to the
    channels that do. Plain proportional division would give a channel asking
    for one post a day less than one post a day simply because it shares a
    factory with a hungrier channel.
    """
    if capacity <= 0 or not wants:
        return {key: 0 for key in wants}

    granted = {key: 0 for key in wants}
    remaining = capacity
    unsatisfied = {key for key, value in wants.items() if value > 0}

    while remaining > 0 and unsatisfied:
        share = remaining // len(unsatisfied)
        if share == 0:
            # Fewer units left than claimants. Hand them out one each, in a
            # stable order so the same channel is not always the loser.
            for key in sorted(unsatisfied):
                if remaining == 0:
                    break
                granted[key] += 1
                remaining -= 1
            break

        for key in sorted(unsatisfied):
            take = min(share, wants[key] - granted[key])
            granted[key] += take
            remaining -= take

        unsatisfied = {
            key for key in unsatisfied if granted[key] < wants[key]
        }

    return granted


@dataclass(slots=True)
class QuotaPlan:
    """Who gets to post what, and what the factory is short of."""

    allocations: tuple[Allocation, ...] = ()
    #: Platform → (wanted, capacity) where wanted exceeds capacity.
    oversubscribed: dict[str, tuple[int, int]] = field(default_factory=dict)

    def granted(self, channel_id: str, platform: Platform) -> int:
        for allocation in self.allocations:
            if (allocation.channel_id == channel_id
                    and allocation.platform is platform):
                return allocation.granted
        return 0

    @property
    def total_shortfall(self) -> int:
        return sum(a.shortfall for a in self.allocations)

    @property
    def healthy(self) -> bool:
        return not self.oversubscribed

    def warnings(self) -> list[str]:
        out: list[str] = []
        for platform, (wanted, capacity) in sorted(self.oversubscribed.items()):
            entry = limits_for(Platform(platform))
            out.append(
                f"{platform}: the factory wants {wanted} posts a day against "
                f"a {capacity}/day {entry.rate.quota_scope}-scoped cap. "
                f"{wanted - capacity} will not run. Raise the quota, cut "
                f"cadence, or run fewer channels — adding accounts does not "
                f"help when the scope is the project."
            )
        return out

    def to_dict(self) -> dict[str, object]:
        return {
            "healthy": self.healthy,
            "total_shortfall": self.total_shortfall,
            "oversubscribed": {
                k: {"wanted": v[0], "capacity": v[1]}
                for k, v in sorted(self.oversubscribed.items())
            },
            "allocations": [a.to_dict() for a in self.allocations],
            "warnings": self.warnings(),
        }


def plan_quota(channels: Sequence[Channel]) -> QuotaPlan:
    """Allocate each platform's daily capacity across the channels.

    Project-scoped platforms contend across the whole factory.
    Account-scoped ones contend only where channels share an account, which
    is the usual reason two channels interfere when nobody expects them to.
    """
    allocations: list[Allocation] = []
    oversubscribed: dict[str, tuple[int, int]] = {}

    platforms = {
        platform
        for channel in channels
        for platform in channel.platforms
    }

    for platform in sorted(platforms, key=lambda p: p.value):
        entry = limits_for(platform)
        capacity = entry.rate.posts_per_day
        wants = {
            channel.channel_id: channel.cadence_per_day
            for channel in channels
            if platform in channel.platforms
        }
        total_wanted = sum(wants.values())

        if entry.rate.quota_scope == "project":
            # One pool for the entire factory.
            granted = max_min_fair(wants, capacity)
            if total_wanted > capacity:
                oversubscribed[platform.value] = (total_wanted, capacity)
        else:
            # Per account. Channels only contend where they share one.
            by_account: dict[str, dict[str, int]] = {}
            for channel in channels:
                if platform not in channel.platforms:
                    continue
                account = channel.accounts[platform]
                by_account.setdefault(account, {})[channel.channel_id] = (
                    channel.cadence_per_day
                )

            granted = {}
            for account, account_wants in by_account.items():
                granted.update(max_min_fair(account_wants, capacity))
                account_total = sum(account_wants.values())
                if account_total > capacity:
                    oversubscribed[platform.value] = (
                        max(account_total,
                            oversubscribed.get(platform.value, (0, 0))[0]),
                        capacity,
                    )

        for channel_id, wanted in sorted(wants.items()):
            allocations.append(Allocation(
                channel_id=channel_id, platform=platform,
                wanted=wanted, granted=granted.get(channel_id, 0),
            ))

    return QuotaPlan(tuple(allocations), oversubscribed)


def daily_capacity(channels: Sequence[Channel]) -> dict[str, int]:
    """Total posts a day the factory can actually place, per platform."""
    plan = plan_quota(channels)
    totals: dict[str, int] = {}
    for allocation in plan.allocations:
        totals[allocation.platform.value] = (
            totals.get(allocation.platform.value, 0) + allocation.granted
        )
    return dict(sorted(totals.items()))
