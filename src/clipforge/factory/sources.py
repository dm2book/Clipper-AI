"""Content discovery, and the rights gate every item has to pass.

### Why this file leads with rights rather than discovery

"Finds content" is the one stage of the factory with no technical difficulty
and a large legal one. Clipping and reuploading someone else's video is
copyright infringement unless something makes it lawful, and a system that runs
seven channels unattended will do it thousands of times before anyone looks. At
that volume the exposure is not a takedown — it is a pattern of commercial
infringement across a portfolio, which is the kind of thing that ends a
company rather than a video.

So rights are a **state gate**, not a disclaimer. A source carries the basis on
which it may be used; an item cannot leave `CLEARED` without one the channel
accepts; and the default basis for anything discovered rather than supplied is
`UNVERIFIED`, which publishes nowhere. Turning that off is a deliberate act
with a name (`Channel.accepted_rights`) that appears in the factory's own
status output.

There are entirely legitimate ways to fill a channel, and the model supports
all of them: your own footage, a licensed stock library, public-domain
material, explicit creator permission, and Creative Commons — with the two
conditions CC actually imposes, since `BY` requires attribution in the
description and `NC` forbids exactly the monetised use a channel factory is
built for.

### Discovery itself

`SourceFinder` is a protocol. The offline implementation returns a registry the
operator curated by hand, which is also the shape a rights-cleared pipeline
takes in practice: someone signs a licence, the source goes in the registry,
and the factory works from that. A crawler is a different product with a
different risk profile and is deliberately not here.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol, Sequence

from ..publish.types import UTC, utcnow
from .niches import Niche, SourceKind


class RightsBasis(str, enum.Enum):
    """The reason this material may lawfully be republished."""

    OWNED = "owned"                        # first-party footage
    LICENSED = "licensed"                  # a signed licence, on file
    CREATOR_PERMISSION = "creator_permission"
    PUBLIC_DOMAIN = "public_domain"
    CREATIVE_COMMONS = "creative_commons"  # conditions apply — see below
    STOCK = "stock"                        # licensed stock library
    UNVERIFIED = "unverified"              # the default. Publishes nowhere.


#: What a channel accepts unless it says otherwise. `UNVERIFIED` is absent
#: deliberately: opting in to it is a decision someone has to make by name.
DEFAULT_ACCEPTED_RIGHTS: frozenset[RightsBasis] = frozenset({
    RightsBasis.OWNED,
    RightsBasis.LICENSED,
    RightsBasis.CREATOR_PERMISSION,
    RightsBasis.PUBLIC_DOMAIN,
    RightsBasis.STOCK,
})


@dataclass(frozen=True, slots=True)
class Rights:
    """The rights posture of one source.

    `verified_at` and `expires_at` matter as much as the basis. A licence that
    lapsed in March is not a licence, and a factory scheduling three months
    ahead will happily publish under one unless something checks.
    """

    basis: RightsBasis = RightsBasis.UNVERIFIED
    #: Contract, licence id, or a link to the written permission.
    reference: str = ""
    #: Required in the description for CC-BY and most stock licences.
    attribution: str = ""
    #: False for CC-NC and for stock licences that exclude monetised use.
    commercial_use: bool = True
    #: Whether the licence permits cutting the work up, which is the entire
    #: operation here. Some stock licences allow use but forbid derivatives.
    derivatives: bool = True
    verified_at: datetime | None = None
    expires_at: datetime | None = None

    def active(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return self.expires_at is None or now < self.expires_at

    def to_dict(self) -> dict[str, object]:
        return {
            "basis": self.basis.value,
            "reference": self.reference,
            "attribution": self.attribution,
            "commercial_use": self.commercial_use,
            "derivatives": self.derivatives,
            "verified_at": (
                self.verified_at.isoformat() if self.verified_at else None
            ),
            "expires_at": (
                self.expires_at.isoformat() if self.expires_at else None
            ),
        }


@dataclass(frozen=True, slots=True)
class Source:
    """One piece of raw material a channel could clip."""

    source_id: str
    title: str
    kind: SourceKind
    rights: Rights = field(default_factory=Rights)
    url: str = ""
    creator: str = ""
    duration_s: float = 0.0
    published_at: datetime | None = None
    language: str = "en"
    #: Free-form topic labels, matched against a channel's interests.
    topics: tuple[str, ...] = ()
    #: Set when a transcript already exists; otherwise one must be produced
    #: before the viral engine can see it.
    has_transcript: bool = False

    @property
    def fingerprint(self) -> str:
        """Stable identity, so the same source is not clipped twice.

        Derived from the source identifier rather than the file, because the
        same upload reappears under different URLs across a creator's own
        re-posts and a factory will otherwise republish it.
        """
        return hashlib.blake2b(
            f"{self.creator}|{self.source_id}".encode(), digest_size=12
        ).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "title": self.title,
            "kind": self.kind.value,
            "creator": self.creator,
            "duration_s": self.duration_s,
            "topics": list(self.topics),
            "rights": self.rights.to_dict(),
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class Clearance:
    """Whether a source may be used by a given channel, and on what terms."""

    cleared: bool
    basis: RightsBasis
    reason: str = ""
    required_attribution: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "cleared": self.cleared,
            "basis": self.basis.value,
            "reason": self.reason,
            "required_attribution": self.required_attribution,
        }


def clear(
    source: Source,
    accepted: frozenset[RightsBasis],
    monetised: bool = True,
    now: datetime | None = None,
) -> Clearance:
    """Decide whether this source may be published by this channel.

    Every branch that returns `cleared=False` is a case that would otherwise
    become a takedown, a strike, or a licence breach — checked here rather than
    discovered after a hundred videos are live.
    """
    now = now or utcnow()
    rights = source.rights

    if rights.basis is RightsBasis.UNVERIFIED:
        return Clearance(
            False, rights.basis,
            "no rights basis recorded — republishing third-party material "
            "without one is infringement, and at factory volume it is a "
            "pattern of it",
        )

    if rights.basis not in accepted:
        return Clearance(
            False, rights.basis,
            f"channel does not accept {rights.basis.value} material",
        )

    if not rights.active(now):
        return Clearance(
            False, rights.basis,
            f"licence expired on {rights.expires_at:%Y-%m-%d} — a schedule "
            f"reaching past that date would publish without one",
        )

    if not rights.derivatives:
        return Clearance(
            False, rights.basis,
            "licence permits use but forbids derivative works, and clipping "
            "is a derivative work",
        )

    if monetised and not rights.commercial_use:
        return Clearance(
            False, rights.basis,
            "licence excludes commercial use (CC-NC or equivalent) and this "
            "channel is monetised",
        )

    if rights.basis in (RightsBasis.CREATIVE_COMMONS, RightsBasis.STOCK):
        if not rights.attribution:
            return Clearance(
                False, rights.basis,
                f"{rights.basis.value} requires attribution and none is "
                f"recorded — the licence is void without it",
            )

    return Clearance(
        True, rights.basis,
        required_attribution=rights.attribution,
    )


class SourceFinder(Protocol):
    """Supplies candidate material for a niche."""

    def find(
        self, niche: Niche, topics: Sequence[str], limit: int
    ) -> list[Source]: ...


class RegistrySourceFinder:
    """Serves sources an operator has registered and cleared by hand.

    The default, and not a placeholder for something better. A rights-cleared
    pipeline genuinely looks like this: a licence is signed, the source is
    entered, the factory works from the registry. An automated crawler is a
    different product with a different risk profile.
    """

    def __init__(self, sources: Sequence[Source] = ()) -> None:
        self._sources: dict[str, Source] = {s.source_id: s for s in sources}

    def register(self, source: Source) -> None:
        self._sources[source.source_id] = source

    def remove(self, source_id: str) -> None:
        self._sources.pop(source_id, None)

    @property
    def all(self) -> tuple[Source, ...]:
        return tuple(self._sources.values())

    def find(
        self, niche: Niche, topics: Sequence[str], limit: int = 20
    ) -> list[Source]:
        from .niches import profile as niche_profile

        wanted_kinds = set(niche_profile(niche).source_kinds)
        wanted_topics = {t.lower() for t in topics}

        scored: list[tuple[float, Source]] = []
        for source in self._sources.values():
            if source.kind not in wanted_kinds:
                continue

            overlap = len(wanted_topics & {t.lower() for t in source.topics})
            if wanted_topics and not overlap:
                continue

            score = float(overlap)
            # Recency, mildly. Short-form audiences do not reward archive
            # material the way a long-form audience does.
            if source.published_at:
                age_days = (utcnow() - source.published_at).days
                score += max(0.0, 1.0 - age_days / 365.0)
            # Prefer material that already has a transcript: producing one is
            # the most expensive stage in the pipeline.
            if source.has_transcript:
                score += 0.5

            scored.append((score, source))

        scored.sort(key=lambda pair: (-pair[0], pair[1].source_id))
        return [source for _, source in scored[:limit]]


def rights_summary(sources: Sequence[Source]) -> dict[str, int]:
    """How a library breaks down by basis. The number to watch is unverified."""
    counts: dict[str, int] = {}
    for source in sources:
        counts[source.rights.basis.value] = (
            counts.get(source.rights.basis.value, 0) + 1
        )
    return dict(sorted(counts.items()))


def expiring_soon(
    sources: Sequence[Source], within_days: int = 60,
    now: datetime | None = None,
) -> list[tuple[Source, int]]:
    """Licences lapsing inside the scheduling horizon.

    A factory that schedules a quarter ahead will publish under a licence that
    expires next month unless something notices first.
    """
    now = now or utcnow()
    deadline = now + timedelta(days=within_days)

    out: list[tuple[Source, int]] = []
    for source in sources:
        expires = source.rights.expires_at
        if expires and now < expires <= deadline:
            out.append((source, (expires - now).days))
    out.sort(key=lambda pair: pair[1])
    return out
