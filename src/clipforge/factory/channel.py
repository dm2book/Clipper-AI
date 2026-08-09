"""A channel: its identity, its accounts, its budget, and its health.

"Run channels independently" is the requirement, and independence has to be
built rather than assumed. Three mechanisms carry it:

**A budget per channel.** Without one, a channel whose sources produce
expensive work quietly consumes the whole account's spend and six other
channels stop. The budget is checked *before* an item starts, not as it runs,
because a pipeline that dies at the render stage has already paid for
transcription and detection and has nothing to show for either.

**A circuit breaker per channel.** A channel failing every item — a revoked
token, a dead source, a bad configuration — should stop trying and say so.
Retrying it forever burns budget and buries the real signal in noise.

**No shared mutable state.** Channels hold their own queues and counters. The
one thing they genuinely cannot have to themselves is the YouTube API quota,
which is per project rather than per channel — and `scheduler.py` exists
because that single exception breaks the independence claim in a way no amount
of code structure can fix.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from ..publish.types import Platform, UTC, utcnow
from .niches import Niche, NicheProfile, profile
from .sources import DEFAULT_ACCEPTED_RIGHTS, RightsBasis


class ChannelState(str, enum.Enum):
    DRAFT = "draft"              # created, not yet running
    ACTIVE = "active"
    PAUSED = "paused"            # by a human
    BUDGET_EXHAUSTED = "budget_exhausted"
    CIRCUIT_OPEN = "circuit_open"   # failing repeatedly; stopped itself


#: Consecutive item failures before a channel stops trying.
FAILURE_THRESHOLD = 5
#: How long a tripped breaker stays open before one probe item is allowed.
CIRCUIT_COOLDOWN = timedelta(hours=6)


@dataclass(slots=True)
class Budget:
    """Spend control, in cents, per calendar month.

    Costs are estimates supplied by the pipeline rather than measured — the
    point is not accounting precision, it is refusing to start work that
    cannot be finished.
    """

    monthly_cents: int = 20_000
    spent_cents: int = 0
    period: str = ""

    def __post_init__(self) -> None:
        if not self.period:
            self.period = utcnow().strftime("%Y-%m")

    def roll(self, now: datetime | None = None) -> None:
        current = (now or utcnow()).strftime("%Y-%m")
        if current != self.period:
            self.period = current
            self.spent_cents = 0

    @property
    def remaining_cents(self) -> int:
        return max(0, self.monthly_cents - self.spent_cents)

    @property
    def exhausted(self) -> bool:
        return self.spent_cents >= self.monthly_cents

    def can_afford(self, cents: int) -> bool:
        return self.spent_cents + cents <= self.monthly_cents

    def charge(self, cents: int) -> None:
        self.spent_cents += cents

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "monthly_cents": self.monthly_cents,
            "spent_cents": self.spent_cents,
            "remaining_cents": self.remaining_cents,
            "used_pct": round(
                100.0 * self.spent_cents / self.monthly_cents, 1
            ) if self.monthly_cents else 0.0,
        }


@dataclass(slots=True)
class ChannelHealth:
    """Failure tracking and the breaker built on it."""

    consecutive_failures: int = 0
    total_items: int = 0
    total_published: int = 0
    total_blocked: int = 0
    total_failed: int = 0
    opened_at: datetime | None = None
    last_error: str = ""

    @property
    def success_rate(self) -> float:
        return self.total_published / self.total_items if self.total_items else 0.0

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.total_items += 1
        self.total_published += 1

    def record_blocked(self, reason: str) -> None:
        # A rights block is not a failure of the channel — it is the gate
        # working. It must not trip the breaker, or a single unlicensed source
        # would take a healthy channel offline.
        self.total_items += 1
        self.total_blocked += 1
        self.last_error = reason

    def record_failure(self, reason: str, now: datetime | None = None) -> None:
        # Takes the clock rather than reading it. Everything else in this
        # system is given `now`, and a breaker that reads the wall clock
        # directly cannot be tested or replayed against a recorded timeline.
        self.consecutive_failures += 1
        self.total_items += 1
        self.total_failed += 1
        self.last_error = reason
        if self.consecutive_failures >= FAILURE_THRESHOLD and not self.opened_at:
            self.opened_at = now or utcnow()

    def circuit_open(self, now: datetime | None = None) -> bool:
        if self.opened_at is None:
            return False
        now = now or utcnow()
        if now - self.opened_at >= CIRCUIT_COOLDOWN:
            # Half-open: let one item through to test the water.
            return False
        return True

    def reset_circuit(self) -> None:
        self.opened_at = None
        self.consecutive_failures = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "consecutive_failures": self.consecutive_failures,
            "items": self.total_items,
            "published": self.total_published,
            "blocked": self.total_blocked,
            "failed": self.total_failed,
            "success_rate": round(self.success_rate, 3),
            "circuit_opened_at": (
                self.opened_at.isoformat() if self.opened_at else None
            ),
            "last_error": self.last_error,
        }


@dataclass(slots=True)
class Channel:
    """One autonomous channel."""

    channel_id: str
    name: str
    niche: Niche
    org_id: str = "org1"

    #: Publishing accounts, by platform. A channel with no account for a
    #: platform simply does not post there.
    accounts: dict[Platform, str] = field(default_factory=dict)

    #: Topic filters handed to the source finder.
    topics: tuple[str, ...] = ()

    #: Which rights bases this channel will publish. Excluding `UNVERIFIED`
    #: is the default and opting in is a named, visible decision.
    accepted_rights: frozenset[RightsBasis] = DEFAULT_ACCEPTED_RIGHTS

    #: Whether this channel is monetised, which decides whether CC-NC and
    #: similar non-commercial licences are usable at all.
    monetised: bool = True

    timezone: str = "UTC"
    state: ChannelState = ChannelState.DRAFT
    budget: Budget = field(default_factory=Budget)
    health: ChannelHealth = field(default_factory=ChannelHealth)

    #: Overrides on the niche profile. Left empty, the profile decides.
    cadence_override: int = 0
    quality_floor_override: float = 0.0

    #: Fingerprints of sources already used, so the same material is not
    #: republished. Persisted with the channel in a real deployment.
    used_fingerprints: set[str] = field(default_factory=set)

    created_at: datetime = field(default_factory=utcnow)

    @property
    def profile(self) -> NicheProfile:
        return profile(self.niche)

    @property
    def cadence_per_day(self) -> int:
        return self.cadence_override or self.profile.cadence_per_day

    @property
    def quality_floor(self) -> float:
        return self.quality_floor_override or self.profile.quality_floor

    @property
    def platforms(self) -> tuple[Platform, ...]:
        """Platforms this channel can actually post to.

        The intersection of what the niche targets and what has an account
        connected — not the niche's wishlist.
        """
        return tuple(p for p in self.profile.platforms if p in self.accounts)

    def runnable(self, now: datetime | None = None) -> tuple[bool, str]:
        """Whether this channel should do work right now, and why not."""
        now = now or utcnow()
        self.budget.roll(now)

        if self.state is ChannelState.PAUSED:
            return False, "paused by an operator"
        if self.state is ChannelState.DRAFT:
            return False, "not activated"
        if self.health.circuit_open(now):
            reopens = self.health.opened_at + CIRCUIT_COOLDOWN
            return False, (
                f"circuit open after {self.health.consecutive_failures} "
                f"consecutive failures ({self.health.last_error}); retries "
                f"at {reopens:%Y-%m-%d %H:%M}"
            )
        if self.budget.exhausted:
            return False, (
                f"monthly budget of ${self.budget.monthly_cents / 100:.0f} "
                f"exhausted for {self.budget.period}"
            )
        if not self.accounts:
            return False, "no publishing accounts connected"
        return True, ""

    def accepts_unverified(self) -> bool:
        return RightsBasis.UNVERIFIED in self.accepted_rights

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "name": self.name,
            "niche": self.niche.value,
            "state": self.state.value,
            "platforms": [p.value for p in self.platforms],
            "cadence_per_day": self.cadence_per_day,
            "quality_floor": self.quality_floor,
            "topics": list(self.topics),
            "monetised": self.monetised,
            "accepted_rights": sorted(r.value for r in self.accepted_rights),
            "accepts_unverified": self.accepts_unverified(),
            "budget": self.budget.to_dict(),
            "health": self.health.to_dict(),
            "sources_used": len(self.used_fingerprints),
        }
