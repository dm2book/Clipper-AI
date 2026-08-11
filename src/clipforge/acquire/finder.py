"""A `SourceFinder` that acquires, rather than waiting to be fed.

`RegistrySourceFinder` serves material an operator entered by hand. That is a
real workflow and it stays — a signed licence, a source entered, the factory
working from the registry is what a rights-cleared pipeline actually looks
like. What it cannot do is *get* anything.

`AcquiringSourceFinder` closes that gap. It is pointed at a set of inputs —
YouTube channels, podcast feeds, individual videos, an uploads directory — and
keeps the library topped up from them, then answers the factory's `find` from
what has actually landed.

## Acquired material is not publishable material

Everything acquisition creates is `UNVERIFIED`, and the channel gate refuses
that by default. So a channel wired to this finder acquires steadily and
publishes nothing until somebody records a licence against each source. That
is the intended behaviour and not a bug to route around: the alternative is a
system that quietly republishes other people's work because a URL was pasted
into a text box.

The one configuration where it flows end to end without a human in the loop is
`OWNED_UPLOAD` — a customer's own footage, which `mark_owned` records as such
because the customer supplying it *is* the rights holder.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from ..factory.niches import Niche
from ..factory.sources import RightsBasis, Source, SourceKind
from ..publish.types import utcnow
from ..store.durable import DurableSourceRegistry
from .engine import AcquisitionEngine
from .resolve import resolve
from .types import AcquisitionError, InputKind, UnsupportedInput

__all__ = ["AcquiringSourceFinder", "WatchedInput"]


@dataclass(slots=True)
class WatchedInput:
    """Something to keep pulling material from."""

    value: str
    #: Which channel the acquired material is for. Empty means the tenant's
    #: shared library — the sensible default for a feed several channels clip.
    channel_id: str = ""
    #: Topic labels stamped onto everything from this input, so a feed of
    #: business interviews reaches the business channel's `find`.
    topics: tuple[str, ...] = ()
    #: Recorded when the material is the customer's own. The one basis
    #: acquisition may set without a human, because the customer supplying
    #: their own footage is the rights holder.
    owned: bool = False
    last_swept_at: datetime | None = None


class AcquiringSourceFinder(DurableSourceRegistry):
    """Reads the library like `DurableSourceRegistry`; fills it as well.

    Inherits `find` — the scoring is a product decision and there is no reason
    for two copies of it — and adds `sweep`, which submits the watched inputs
    and drains the queue.

    `find` never blocks on acquisition. A cycle that had to wait for a
    two-gigabyte podcast before deciding what to clip would be a cycle that
    times out; `sweep` is a separate call, made by a scheduler.
    """

    def __init__(
        self,
        database: Any,
        tenant_id: str,
        engine: AcquisitionEngine,
        *,
        watching: Sequence[WatchedInput] = (),
    ) -> None:
        super().__init__(database, tenant_id)
        self.engine = engine
        self.watching: list[WatchedInput] = list(watching)

    # -- what to watch -----------------------------------------------------

    def watch(self, value: str, **kwargs: Any) -> WatchedInput:
        """Add an input. Validated now, so a typo is reported to the person
        who made it rather than surfacing in a worker log at 3am."""

        resolve(value)  # raises UnsupportedInput on nonsense
        entry = WatchedInput(value=value, **kwargs)
        self.watching.append(entry)
        return entry

    def unwatch(self, value: str) -> bool:
        before = len(self.watching)
        self.watching = [w for w in self.watching if w.value != value]
        return len(self.watching) < before

    # -- filling the library -----------------------------------------------

    def sweep(self, *, drain: int = 25, now: datetime | None = None) -> dict[str, Any]:
        """Submit every watched input, then work the queue.

        Returns a summary rather than raising: one dead feed must not stop the
        other six from being swept, in the same way one failing channel does
        not stop a factory cycle.
        """

        now = now or utcnow()
        submitted = 0
        problems: list[str] = []

        for entry in self.watching:
            try:
                jobs = self.engine.submit(entry.value, channel_id=entry.channel_id)
            except (AcquisitionError, OSError) as error:
                problems.append(f"{entry.value}: {error}")
                continue
            submitted += len(jobs)
            entry.last_swept_at = now

        acquired = self.engine.run(limit=drain) if drain else []
        failed = [a for a in acquired if a.error]

        for acquisition in acquired:
            if acquisition.error or not acquisition.source_id:
                continue
            entry = self._entry_for(acquisition.ref.raw)
            if entry is None:
                continue
            if entry.topics:
                self._stamp_topics(acquisition.source_id, entry.topics)
            if entry.owned:
                self.mark_owned(acquisition.source_id)

        return {
            "submitted": submitted,
            "acquired": len(acquired) - len(failed),
            "failed": [a.error for a in failed],
            "problems": problems,
        }

    def _entry_for(self, raw: str) -> WatchedInput | None:
        return next((w for w in self.watching if w.value == raw), None)

    def _stamp_topics(self, source_id: str, topics: Sequence[str]) -> None:
        """Union, not replace. A feed's topics are added to whatever the
        platform already reported rather than overwriting it."""

        with self._uow() as uow:
            record = uow.sources.get(source_id)
            if record is None:
                return
            merged = list(dict.fromkeys([*record.topics, *topics]))
            if merged != record.topics:
                record.topics = merged
                uow.sources.save(record)

    def mark_owned(self, source_id: str) -> bool:
        """Record that this material is the customer's own.

        The only rights basis this layer sets without a person, and only for
        material the customer supplied. Everything acquired from somewhere
        else stays `unverified` until someone records a licence against it.
        """

        with self._uow() as uow:
            record = uow.sources.get(source_id)
            if record is None:
                return False
            if record.rights_basis != RightsBasis.UNVERIFIED.value:
                return False  # already decided; not this layer's to change
            record.rights_basis = RightsBasis.OWNED.value
            record.rights_reference = "customer upload"
            record.rights_verified_at = utcnow()
            uow.sources.save(record)
        return True

    # -- reading -----------------------------------------------------------

    def find(
        self, niche: Niche, topics: Sequence[str], limit: int = 20
    ) -> list[Source]:
        """Material this channel could clip, cleared or not.

        The rights gate lives in the pipeline, not here: a finder that hid
        unverified sources would make "nothing to clip" and "forty things
        waiting on a licence" look identical to an operator.
        """

        return super().find(niche, topics, limit)

    def clearable(self) -> tuple[Source, ...]:
        """Acquired material waiting on a rights decision.

        The operator's work queue, and the reason a channel wired to this
        finder can look busy and publish nothing.
        """

        return tuple(
            source for source in self.all
            if source.rights.basis is RightsBasis.UNVERIFIED
        )

    def ingest_directory(
        self, directory: str, *, channel_id: str = "", owned: bool = True
    ) -> list[str]:
        """Submit every media file in a directory. The bulk-upload path."""

        if not os.path.isdir(directory):
            raise UnsupportedInput(f"not a directory: {directory}")
        jobs: list[str] = []
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            try:
                ref = resolve(path)
            except UnsupportedInput:
                continue  # a stray README in an uploads folder is not an error
            if ref.kind is not InputKind.LOCAL_FILE:
                continue
            self.watching.append(
                WatchedInput(value=path, channel_id=channel_id, owned=owned)
            )
            jobs.extend(self.engine.submit(path, channel_id=channel_id))
        return jobs
