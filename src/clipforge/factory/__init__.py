"""ClipForge AI — the channel factory.

Create channels; the factory finds content, clips it, writes hooks, builds
captions, composes the frame and schedules the upload — each channel running
independently of the others.

    from clipforge.factory import ChannelFactory, Niche

    factory = ChannelFactory(publisher=publishing_system, finder=registry)
    cars = factory.create_channel("Redline", Niche.CARS, accounts={...})
    factory.activate(cars.channel_id)
    reports = factory.run_cycle()

**A niche is a configuration, not a label.** `niches.py` sets every stage
coherently per niche, and the settings genuinely conflict: Cars, Luxury and
Gaming get no gameplay bed because their footage is already the visual, while
Business and AI get the lowest-salience bed available because dense speech
punishes a busy one. History gets the longest clips and the slowest cadence.

**Rights are a state gate, not a disclaimer.** Republishing third-party video
is infringement unless something makes it lawful, and a factory does it
thousands of times unattended. Sources carry a basis; the default for anything
without one is `UNVERIFIED`, which publishes nowhere; and accepting unverified
material is a named decision that shows up in `rights_report()`.

**Independence is enforced where it can be, and reported where it cannot.**
Channels hold their own budgets, breakers and queues, and `run_cycle` isolates
every failure. The exception is YouTube's quota, which is per API project — six
uploads a day for the whole factory. `scheduler.py` allocates it max-min fairly
and states the shortfall as a number, rather than letting channels race and
fail at post time.
"""

from .channel import (
    Budget,
    CIRCUIT_COOLDOWN,
    Channel,
    ChannelHealth,
    ChannelState,
    FAILURE_THRESHOLD,
)
from .factory import ChannelFactory, CycleReport, FactoryConfig
from .niches import (
    Niche,
    NicheProfile,
    PROFILES,
    SourceKind,
    all_niches,
    domain_affinity,
    hook_preference,
    profile,
    target_duration,
    uses_stream_clipper,
)
from .pipeline import (
    ITEM_COST_CENTS,
    NullTranscriber,
    Pipeline,
    PipelineConfig,
    STAGE_COST_CENTS,
    STAGE_ORDER,
    Stage,
    Transcriber,
    WorkItem,
)
from .scheduler import (
    Allocation,
    Demand,
    QuotaPlan,
    daily_capacity,
    max_min_fair,
    plan_quota,
)
from .sources import (
    Clearance,
    DEFAULT_ACCEPTED_RIGHTS,
    RegistrySourceFinder,
    Rights,
    RightsBasis,
    Source,
    SourceFinder,
    clear,
    expiring_soon,
    rights_summary,
)

__all__ = [
    "Allocation",
    "Budget",
    "CIRCUIT_COOLDOWN",
    "Channel",
    "ChannelFactory",
    "ChannelHealth",
    "ChannelState",
    "Clearance",
    "CycleReport",
    "DEFAULT_ACCEPTED_RIGHTS",
    "Demand",
    "FACTORY_NICHES",
    "FAILURE_THRESHOLD",
    "FactoryConfig",
    "ITEM_COST_CENTS",
    "Niche",
    "NicheProfile",
    "NullTranscriber",
    "PROFILES",
    "Pipeline",
    "PipelineConfig",
    "QuotaPlan",
    "RegistrySourceFinder",
    "Rights",
    "RightsBasis",
    "STAGE_COST_CENTS",
    "STAGE_ORDER",
    "Source",
    "SourceFinder",
    "SourceKind",
    "Stage",
    "Transcriber",
    "WorkItem",
    "all_niches",
    "clear",
    "daily_capacity",
    "domain_affinity",
    "expiring_soon",
    "hook_preference",
    "max_min_fair",
    "plan_quota",
    "profile",
    "rights_summary",
    "target_duration",
    "uses_stream_clipper",
]

#: The seven the product ships with.
FACTORY_NICHES: tuple[Niche, ...] = all_niches()
