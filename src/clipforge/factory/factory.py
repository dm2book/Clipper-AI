"""The channel factory — orchestration.

    create a channel from a niche
      → find sources that match its topics
      → run each through the pipeline, isolated
      → allocate the shared quota fairly across channels
      → place the results on the publishing calendar

The factory's own job is small. Everything that makes a clip is already built;
what this adds is the part that keeps seven of them running at once without one
taking the others down.

**Isolation is enforced, not hoped for.** `run_cycle` catches everything a
channel can throw, charges its own budget, trips its own breaker, and moves on.
A channel with a revoked token, an empty source library or a bad configuration
degrades to zero output and says why, and the other six do not notice.

**Blocked is not failed.** An item stopped by the rights gate or the quality
floor is the system working correctly, and must not count against a channel's
circuit breaker — otherwise one unlicensed source takes a healthy channel
offline. Only errors do that.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections.abc import MutableMapping
from typing import Any, Sequence

from ..captions.types import TimedWord
from ..publish import PublishingSystem, ScheduleError
from ..publish.types import Platform, utcnow
from .channel import Budget, Channel, ChannelState
from .niches import Niche, profile
from .pipeline import ITEM_COST_CENTS, Pipeline, PipelineConfig, Stage, WorkItem
from .scheduler import QuotaPlan, plan_quota
from .sources import (
    RegistrySourceFinder,
    Source,
    SourceFinder,
    expiring_soon,
    rights_summary,
)


@dataclass(slots=True)
class CycleReport:
    """What one channel did in one cycle."""

    channel_id: str
    ran: bool
    reason: str = ""
    items: list[WorkItem] = field(default_factory=list)
    scheduled: int = 0
    blocked: int = 0
    failed: int = 0
    spent_cents: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "ran": self.ran,
            "reason": self.reason,
            "considered": len(self.items),
            "scheduled": self.scheduled,
            "blocked": self.blocked,
            "failed": self.failed,
            "spent_cents": self.spent_cents,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(slots=True)
class FactoryConfig:
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    #: Sources examined per channel per cycle.
    sources_per_cycle: int = 5
    #: How far ahead the first post of a cycle is placed. Enough that a render
    #: has time to finish before its slot arrives.
    lead_time: timedelta = timedelta(hours=6)
    #: Spacing between a channel's own posts.
    spacing: timedelta = timedelta(hours=4)


class ChannelFactory:
    """Creates channels and runs them independently."""

    def __init__(
        self,
        publisher: PublishingSystem | None = None,
        finder: SourceFinder | None = None,
        config: FactoryConfig | None = None,
        channels: MutableMapping[str, Channel] | None = None,
    ) -> None:
        """The stores are injected; the defaults are the volatile ones.

        Passing nothing gives an entirely in-memory factory, which is what the
        tests and the demos use. Passing
        `clipforge.store.durable.DurableChannelBook` and
        `DurableSourceRegistry` gives the same factory backed by Postgres, with
        no other change to how it is driven.
        """

        self.config = config or FactoryConfig()
        self.publisher = publisher or PublishingSystem()
        self.finder = finder or RegistrySourceFinder()
        self.pipeline = Pipeline(self.config.pipeline)
        self.channels: MutableMapping[str, Channel] = (
            {} if channels is None else channels
        )

    # -- channels ---------------------------------------------------------------

    def create_channel(
        self,
        name: str,
        niche: Niche,
        accounts: dict[Platform, str] | None = None,
        topics: Sequence[str] = (),
        budget_cents: int = 20_000,
        timezone: str = "UTC",
        **overrides: Any,
    ) -> Channel:
        """Create a channel from a niche profile.

        The profile supplies every stage's configuration — signals, hook
        types, caption style, whether a gameplay bed helps, clip length,
        cadence. A channel is a niche plus accounts plus a budget.
        """
        channel = Channel(
            channel_id=f"ch_{uuid.uuid4().hex[:10]}",
            name=name,
            niche=niche,
            accounts=dict(accounts or {}),
            topics=tuple(topics) or (niche.value,),
            budget=Budget(monthly_cents=budget_cents),
            timezone=timezone,
            **overrides,
        )
        self.channels[channel.channel_id] = channel
        return channel

    def activate(self, channel_id: str) -> Channel:
        channel = self.channels[channel_id]
        if not channel.accounts:
            raise ValueError(
                f"{channel.name} has no publishing accounts connected"
            )
        channel.state = ChannelState.ACTIVE
        channel.health.reset_circuit()
        self._save(channel)
        return channel

    def pause(self, channel_id: str) -> Channel:
        channel = self.channels[channel_id]
        channel.state = ChannelState.PAUSED
        self._save(channel)
        return channel

    def _save(self, channel: Channel) -> None:
        """Write a channel's current state back to wherever channels live.

        A no-op against a plain dict, where the object fetched *is* the object
        stored. Against `DurableChannelBook` the fetch returns a copy, and
        without this every state change — activation, a tripped breaker, a
        month's spend — would be discarded when the call returned.
        """

        self.channels[channel.channel_id] = channel

    # -- running -----------------------------------------------------------------

    def run_channel(
        self,
        channel_id: str,
        transcripts: dict[str, Sequence[TimedWord]] | None = None,
        now: datetime | None = None,
        quota: QuotaPlan | None = None,
    ) -> CycleReport:
        """Run one cycle for one channel. Never raises.

        The write-back is a `finally` around the whole cycle rather than a call
        at each of the dozen places the run mutates the channel — spend, health
        counters, the breaker, the used-source set, the state. Those are spread
        over four early returns and a loop with three branches, and a
        write-back added at eleven of the twelve is a channel that quietly
        forgets it tripped its breaker.
        """

        channel = self.channels[channel_id]
        try:
            return self._run_channel(channel, transcripts, now, quota)
        finally:
            self._save(channel)

    def _run_channel(
        self,
        channel: Channel,
        transcripts: dict[str, Sequence[TimedWord]] | None = None,
        now: datetime | None = None,
        quota: QuotaPlan | None = None,
    ) -> CycleReport:
        now = now or utcnow()
        channel_id = channel.channel_id
        report = CycleReport(channel_id=channel_id, ran=False)

        runnable, reason = channel.runnable(now)
        if not runnable:
            report.reason = reason
            if channel.budget.exhausted:
                channel.state = ChannelState.BUDGET_EXHAUSTED
            elif channel.health.circuit_open(now):
                channel.state = ChannelState.CIRCUIT_OPEN
            return report

        report.ran = True
        transcripts = transcripts or {}
        budget_before = channel.budget.spent_cents

        try:
            sources = self.finder.find(
                channel.niche, channel.topics, self.config.sources_per_cycle
            )
        except Exception as error:                       # noqa: BLE001
            report.reason = f"source discovery failed: {error}"
            channel.health.record_failure(report.reason, now)
            return report

        if not sources:
            report.reason = "no sources matched this channel's topics"
            return report

        placed = 0
        target = self._target_posts(channel, quota)

        for source in sources:
            if placed >= target:
                break

            item = self.pipeline.run(
                channel, source,
                transcript_words=transcripts.get(source.source_id, ()),
                now=now,
            )
            report.items.append(item)
            channel.budget.charge(item.cost_cents)

            if item.stage is Stage.FAILED:
                report.failed += 1
                channel.health.record_failure(item.reason, now)
                continue
            if item.stage is Stage.BLOCKED:
                report.blocked += 1
                # Deliberately not a failure: the gate working as designed
                # must not take a healthy channel offline.
                channel.health.record_blocked(item.reason)
                continue

            scheduled = self._schedule(channel, item, now, placed, quota)
            if scheduled:
                channel.used_fingerprints.add(source.fingerprint)
                channel.health.record_success()
                report.scheduled += 1
                placed += 1
            else:
                report.blocked += 1
                channel.health.record_blocked(item.reason)

        report.spent_cents = channel.budget.spent_cents - budget_before
        if channel.budget.exhausted:
            channel.state = ChannelState.BUDGET_EXHAUSTED
        return report

    def run_cycle(
        self,
        transcripts: dict[str, Sequence[TimedWord]] | None = None,
        now: datetime | None = None,
    ) -> dict[str, CycleReport]:
        """Run every channel once, isolated from each other.

        The quota plan is computed once for the whole factory and handed to
        each channel, so allocation of the shared YouTube budget is decided
        before anyone starts rather than by whoever happens to run first.
        """
        now = now or utcnow()
        quota = plan_quota([
            c for c in self.channels.values()
            if c.state is ChannelState.ACTIVE
        ])

        reports: dict[str, CycleReport] = {}
        for channel_id in list(self.channels):
            try:
                reports[channel_id] = self.run_channel(
                    channel_id, transcripts, now, quota
                )
            except Exception as error:                   # noqa: BLE001
                # A channel must not be able to take the factory down. If one
                # gets here it is a bug in this file, but the other six still
                # need to run.
                channel = self.channels[channel_id]
                channel.health.record_failure(f"orchestrator: {error}")
                self._save(channel)
                reports[channel_id] = CycleReport(
                    channel_id=channel_id, ran=False,
                    reason=f"isolated failure: {type(error).__name__}: {error}",
                )
        return reports

    # -- internals ----------------------------------------------------------------

    def _target_posts(self, channel: Channel, quota: QuotaPlan | None) -> int:
        """How many clips this channel should place this cycle.

        Capped by the quota it was actually allocated, not by what its niche
        profile would like.
        """
        if quota is None:
            return channel.cadence_per_day
        granted = [
            quota.granted(channel.channel_id, platform)
            for platform in channel.platforms
        ]
        return max(granted) if granted else 0

    def _schedule(
        self, channel: Channel, item: WorkItem, now: datetime,
        index: int, quota: QuotaPlan | None,
    ) -> bool:
        """Place an item's posts on the publishing calendar."""
        run_at = now + self.config.lead_time + index * self.config.spacing
        placed = False
        problems: list[str] = []

        # `_specs` builds one spec per entry of `channel.platforms`, in order.
        for platform, spec in zip(channel.platforms, item.post_specs):
            if quota is not None and quota.granted(
                channel.channel_id, platform
            ) <= 0:
                problems.append(
                    f"{platform.value}: no quota allocated to this channel"
                )
                continue

            try:
                post = self.publisher.schedule(
                    channel.accounts[platform], spec, run_at
                )
            except ScheduleError as error:
                problems.append(f"{platform.value}: {error}")
                continue
            except KeyError as error:
                problems.append(f"{platform.value}: unknown account {error}")
                continue

            item.scheduled_post_ids.append(post.post_id)
            placed = True

        if not placed and problems:
            item.reason = "; ".join(problems)
        return placed

    # -- introspection ----------------------------------------------------------------

    def quota_plan(self) -> QuotaPlan:
        return plan_quota([
            c for c in self.channels.values()
            if c.state is ChannelState.ACTIVE
        ])

    def rights_report(self, now: datetime | None = None) -> dict[str, Any]:
        """The state of the source library's paperwork.

        Two numbers matter: how much material has no recorded basis at all,
        and which licences lapse inside the scheduling horizon. A factory
        booking a quarter ahead will publish under a licence that expires next
        month unless something checks.
        """
        sources: Sequence[Source] = getattr(self.finder, "all", ())
        expiring = expiring_soon(sources, within_days=90, now=now)
        summary = rights_summary(sources)

        return {
            "sources": len(sources),
            "by_basis": summary,
            "unverified": summary.get("unverified", 0),
            "channels_accepting_unverified": sorted(
                c.name for c in self.channels.values()
                if c.accepts_unverified()
            ),
            "expiring_within_90_days": [
                {"source_id": s.source_id, "days": d,
                 "expires": s.rights.expires_at.date().isoformat()}
                for s, d in expiring
            ],
        }

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        quota = self.quota_plan()

        return {
            "channels": len(self.channels),
            "active": sum(
                1 for c in self.channels.values()
                if c.state is ChannelState.ACTIVE
            ),
            "by_state": _count(
                c.state.value for c in self.channels.values()
            ),
            "by_niche": _count(
                c.niche.value for c in self.channels.values()
            ),
            "quota": quota.to_dict(),
            "rights": self.rights_report(now),
            "budget_cents": {
                "allocated": sum(
                    c.budget.monthly_cents for c in self.channels.values()
                ),
                "spent": sum(
                    c.budget.spent_cents for c in self.channels.values()
                ),
            },
            "publisher": self.publisher.status(),
        }

    def channel_table(self) -> list[dict[str, Any]]:
        return [c.to_dict() for c in self.channels.values()]


def _count(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))
