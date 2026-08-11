"""The acquisition layer's front door.

    engine = AcquisitionEngine(db, "ten_acme", workspace="/var/lib/clipforge")
    engine.submit("https://youtu.be/dQw4w9WgXcQ")     # queued
    engine.submit("https://feeds.example.com/show")   # expands to episodes
    engine.run(limit=10)                              # a worker turn

`submit` resolves and queues; `run` drains. They are separate because
acquisition is slow and the caller submitting is usually a web request that
must not wait for a two-gigabyte podcast.

## Everything is resumable, because everything is a row

The queue is the `jobs` table from the persistence layer — leases, retries,
dead-lettering and all — and each job carries the acquisition id. Progress
lives in `acquisitions`, so a worker killed mid-download comes back, finds the
row in `downloading`, and the byte-range resume in `http.py` picks up from the
`.part` file. Nothing is held in this object that matters.

## The workspace

    <workspace>/<tenant>/<acquisition_id>/
        media.<ext>      the file
        media.<ext>.part while it is arriving
        thumb.jpg        the thumbnail

Per-acquisition directories rather than one flat pool: a failed acquisition is
deleted by removing one directory, and two sources with the same filename
cannot collide.

## Rights

Every source this layer creates is `UNVERIFIED`, which the channel gate
refuses by default. Acquisition establishes that material *exists*, not that
it may be republished — those are different questions and a layer that
answered both would turn a licensing decision into a technical one.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from ..factory.sources import Rights, RightsBasis, Source, SourceKind
from ..publish.types import utcnow
from ..store.records import AcquisitionRunRecord, JobRecord, SourceRecord
from .http import DownloadConfig, HttpDownloader
from .probe import MediaProber
from .resolve import resolve
from .rss import Feed, FeedItem, parse_feed
from .types import (
    Acquisition,
    AcquisitionError,
    Download,
    DownloadState,
    InputKind,
    MediaProbe,
    PermanentError,
    RetryableError,
    SourceRef,
    Thumbnail,
)
from .youtube import YouTubeAdapter, YouTubeConfig

__all__ = ["AcquisitionConfig", "AcquisitionEngine", "ACQUIRE_JOB"]

#: The job kind acquisition claims from the shared queue.
ACQUIRE_JOB = "discover_sources"

#: What a feed enclosure's MIME type maps to on disk when the URL has no
#: extension — which podcast CDNs arrange constantly.
_EXTENSIONS = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a", "audio/aac": ".aac", "audio/ogg": ".ogg",
    "audio/opus": ".opus", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
    "video/x-matroska": ".mkv",
}

_KIND_FOR_INPUT = {
    InputKind.YOUTUBE_VIDEO: SourceKind.LONGFORM_VIDEO,
    InputKind.YOUTUBE_CHANNEL: SourceKind.LONGFORM_VIDEO,
    InputKind.PODCAST_FEED: SourceKind.PODCAST,
    InputKind.MEDIA_URL: SourceKind.LONGFORM_VIDEO,
    InputKind.LOCAL_FILE: SourceKind.OWNED_UPLOAD,
}


@dataclass(slots=True)
class AcquisitionConfig:
    workspace: str = "/var/lib/clipforge/media"
    #: How many items a feed or channel expands to in one submission.
    expand_limit: int = 25
    #: Attempts before a queued acquisition is dead-lettered. Separate from
    #: the downloader's own per-attempt retries: this counts whole passes,
    #: including the ones that died with the worker.
    max_attempts: int = 5
    #: Base for the queue's retry backoff, in seconds.
    retry_base_s: int = 30
    lease_s: int = 900
    #: Copy an uploaded file into the workspace rather than referencing it in
    #: place. An operator who deletes their upload directory should not
    #: silently empty the library.
    copy_local_files: bool = True
    #: Refuse material shorter than this. A 4-second clip is not long-form
    #: material and produces nothing worth publishing.
    min_duration_s: float = 30.0


class AcquisitionEngine:
    """Resolves, queues, downloads, probes and persists."""

    def __init__(
        self,
        database: Any,
        tenant_id: str,
        *,
        config: AcquisitionConfig | None = None,
        downloader: HttpDownloader | None = None,
        prober: MediaProber | None = None,
        youtube: YouTubeAdapter | None = None,
        fetch_text: Callable[[str], bytes] | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if not tenant_id:
            raise ValueError("acquisition needs a tenant")
        self.database = database
        self.tenant_id = tenant_id
        self.config = config or AcquisitionConfig()
        self.downloader = downloader or HttpDownloader(DownloadConfig())
        self.prober = prober or MediaProber()
        self.youtube = youtube or YouTubeAdapter(YouTubeConfig())
        self._fetch_text = fetch_text or self._default_fetch_text
        self.clock = clock

    # -- submission --------------------------------------------------------

    def submit(self, value: str, *, channel_id: str = "") -> list[str]:
        """Resolve an input and queue what it names. Returns the job ids.

        A container — a channel or a feed — is expanded here rather than in
        the worker, so the operator who pasted it finds out immediately that
        it named forty episodes, and sees the count before the queue fills.
        """

        ref = resolve(value)
        refs = self.expand(ref) if ref.is_container else [ref]
        return [self._enqueue(item, channel_id) for item in refs]

    def expand(self, ref: SourceRef) -> list[SourceRef]:
        """A container reference into the item references it holds."""

        if ref.kind is InputKind.YOUTUBE_CHANNEL:
            return self.youtube.list_channel(ref, limit=self.config.expand_limit)
        if ref.kind is InputKind.PODCAST_FEED:
            return self._expand_feed(ref)
        return [ref]

    def _expand_feed(self, ref: SourceRef) -> list[SourceRef]:
        feed = self.read_feed(ref)
        refs: list[SourceRef] = []
        for item in feed.items[: self.config.expand_limit]:
            refs.append(SourceRef(
                kind=InputKind.MEDIA_URL,
                # The GUID, not the URL. Enclosure URLs change whenever a host
                # moves CDN or rewrites a tracking prefix, and deduplicating
                # on URL re-downloads an entire back catalogue when they do.
                key=item.guid,
                raw=ref.raw,
                url=item.media_url,
                hints={
                    "feed_url": ref.url,
                    "feed_title": feed.title,
                    "feed_author": feed.author or item.author,
                    "feed_image": item.image_url or feed.image_url,
                    "language": feed.language,
                    "categories": list(feed.categories),
                    "item": item.to_dict(),
                    "podcast": True,
                },
            ))
        return refs

    def read_feed(self, ref: SourceRef) -> Feed:
        payload = self._fetch_text(ref.url)
        feed = parse_feed(payload, limit=self.config.expand_limit)
        if not feed.items and ref.hints.get("guessed"):
            # The resolver guessed "feed" for an ambiguous URL. An empty parse
            # means the guess was wrong, and saying so beats reporting a show
            # with no episodes.
            raise PermanentError(
                f"{ref.url} parsed as XML but holds no episodes — it may not "
                f"be a podcast feed"
            )
        return feed

    def _enqueue(self, ref: SourceRef, channel_id: str) -> str:
        acquisition_id = f"acq_{uuid.uuid4().hex[:12]}"
        with self.database.unit_of_work(self.tenant_id) as uow:
            job = uow.jobs.enqueue(JobRecord(
                id=f"job_{uuid.uuid4().hex[:12]}",
                tenant_id=self.tenant_id,
                channel_id=channel_id or None,
                kind=ACQUIRE_JOB,
                state="queued",
                priority=100,
                run_after=self.clock(),
                max_attempts=self.config.max_attempts,
                payload={
                    "acquisition_id": acquisition_id,
                    "kind": ref.kind.value,
                    "key": ref.key,
                    "url": ref.url,
                    "raw": ref.raw,
                    "hints": ref.hints,
                },
                # The reference itself, so submitting the same video twice is
                # one job. Scoped per channel: two channels legitimately want
                # the same source, and a global key would give the second one
                # nothing.
                dedupe_key=f"acquire:{channel_id or '-'}:{ref.kind.value}:{ref.key}",
            ))
        return job.id

    # -- the worker turn ---------------------------------------------------

    def run(self, limit: int = 5, *, worker_id: str = "acquire-1") -> list[Acquisition]:
        """Claim queued acquisitions and carry them out.

        One transaction to claim, then the slow work outside any transaction,
        then one transaction to record the outcome. Holding a transaction open
        across a download would pin a connection for the length of a
        two-gigabyte transfer, and at fifty channels that is the pool.
        """

        now = self.clock()
        with self.database.unit_of_work(self.tenant_id) as uow:
            claimed = uow.jobs.claim(
                worker_id, now, lease_s=self.config.lease_s,
                kinds=(ACQUIRE_JOB,), limit=limit,
            )

        results: list[Acquisition] = []
        for job in claimed:
            results.append(self._run_job(job, worker_id))
        return results

    def _run_job(self, job: JobRecord, worker_id: str) -> Acquisition:
        payload = job.payload or {}
        ref = SourceRef(
            kind=InputKind(payload.get("kind", "media_url")),
            key=payload.get("key", ""),
            raw=payload.get("raw", ""),
            url=payload.get("url", ""),
            hints=payload.get("hints", {}) or {},
        )
        acquisition_id = payload.get("acquisition_id") or f"acq_{job.id}"

        try:
            acquisition = self.acquire(ref, acquisition_id=acquisition_id)
        except PermanentError as error:
            self._fail(job, str(error), retry=False)
            failed = Acquisition(acquisition_id, ref, error=str(error))
            self.record_run(failed, "failed", channel_id=job.channel_id)
            return failed
        except (RetryableError, AcquisitionError, OSError) as error:
            self._fail(job, str(error), retry=True)
            # `downloading`, not `failed`: the `.part` file is still there and
            # the next pass resumes from it. Recording this as failed would
            # invite a cleanup job to delete 890 MB of a 900 MB download.
            partial = Acquisition(acquisition_id, ref, error=str(error))
            self.record_run(partial, "downloading", channel_id=job.channel_id)
            return partial

        source = self._persist(acquisition, job.channel_id or "")
        acquisition.source_id = source.id
        self.record_run(acquisition, "ready", channel_id=job.channel_id)
        with self.database.unit_of_work(self.tenant_id) as uow:
            uow.jobs.succeed(
                job.id,
                {"source_id": source.id, "acquisition": acquisition.to_dict()},
                self.clock(),
            )
        return acquisition

    def _fail(self, job: JobRecord, message: str, *, retry: bool) -> None:
        now = self.clock()
        retry_at = None
        if retry:
            from datetime import timedelta

            # Exponential, from the attempt count the row already carries —
            # computed in the database rather than here, so two workers cannot
            # each read "attempt 3" and between them schedule a fourth twice.
            delay = self.config.retry_base_s * (2 ** min(job.attempts, 6))
            retry_at = now + timedelta(seconds=delay)
        with self.database.unit_of_work(self.tenant_id) as uow:
            uow.jobs.fail(job.id, message, retry_at, now)

    # -- doing the work ----------------------------------------------------

    def acquire(self, ref: SourceRef, *, acquisition_id: str = "") -> Acquisition:
        """Fetch, probe and thumbnail one reference. No queue involved.

        Usable directly — a synchronous "acquire this now" path for an
        operator watching a progress bar.
        """

        acquisition_id = acquisition_id or f"acq_{uuid.uuid4().hex[:12]}"
        directory = self.workspace_for(acquisition_id)
        os.makedirs(directory, exist_ok=True)

        if ref.kind is InputKind.YOUTUBE_VIDEO:
            acquisition = self._acquire_youtube(ref, acquisition_id, directory)
        elif ref.kind is InputKind.LOCAL_FILE:
            acquisition = self._acquire_local(ref, acquisition_id, directory)
        elif ref.kind is InputKind.MEDIA_URL:
            acquisition = self._acquire_url(ref, acquisition_id, directory)
        else:
            raise PermanentError(
                f"{ref.kind.value} is a container — expand it before acquiring"
            )

        path = acquisition.download.path if acquisition.download else ""
        acquisition.probe = self.prober.probe(path)
        self._check_usable(acquisition)
        acquisition.thumbnail = self._thumbnail(acquisition, directory)
        return acquisition

    def _check_usable(self, acquisition: Acquisition) -> None:
        probe = acquisition.probe
        if probe is None or probe.duration_s is None:
            raise PermanentError(
                f"{acquisition.ref.key}: downloaded, but its duration could "
                f"not be measured — it may not be media"
            )
        if not probe.has_audio:
            # Every downstream stage starts from a transcript. Silent footage
            # is not material this product can do anything with, and finding
            # that out here beats finding it out after a render.
            raise PermanentError(
                f"{acquisition.ref.key}: no audio track — nothing to transcribe"
            )
        if probe.duration_s < self.config.min_duration_s:
            raise PermanentError(
                f"{acquisition.ref.key}: {probe.duration_s:.1f}s is below the "
                f"{self.config.min_duration_s:.0f}s floor for long-form material"
            )

    def _acquire_youtube(
        self, ref: SourceRef, acquisition_id: str, directory: str
    ) -> Acquisition:
        acquisition = self.youtube.download(ref, directory)
        acquisition.acquisition_id = acquisition_id
        return acquisition

    def _acquire_local(
        self, ref: SourceRef, acquisition_id: str, directory: str
    ) -> Acquisition:
        """An uploaded file. No network, but everything else is the same."""

        origin = ref.key
        if not os.path.exists(origin):
            raise PermanentError(f"no such file: {origin}")

        suffix = os.path.splitext(origin)[1].lower() or ".mp4"
        destination = os.path.join(directory, f"media{suffix}")
        if self.config.copy_local_files:
            if os.path.abspath(origin) != os.path.abspath(destination):
                shutil.copy2(origin, destination)
        else:
            destination = origin

        from .http import sha256_file

        size = os.path.getsize(destination)
        return Acquisition(
            acquisition_id=acquisition_id,
            ref=ref,
            title=os.path.splitext(os.path.basename(origin))[0],
            external_id=ref.key,
            media_url=ref.url,
            download=Download(
                download_id=f"dl_{acquisition_id}",
                url=ref.url,
                path=destination,
                state=DownloadState.COMPLETE,
                bytes_done=size,
                bytes_total=size,
                checksum=sha256_file(destination),
                finished_at=self.clock(),
            ),
            raw_metadata={"origin_path": origin, "uploaded": True},
        )

    def _acquire_url(
        self, ref: SourceRef, acquisition_id: str, directory: str
    ) -> Acquisition:
        item = ref.hints.get("item") or {}
        suffix = (
            os.path.splitext(ref.url.split("?")[0])[1].lower()
            or _EXTENSIONS.get(item.get("media_type", ""), "")
            or ".mp4"
        )
        destination = os.path.join(directory, f"media{suffix}")

        download = Download(
            download_id=f"dl_{acquisition_id}",
            url=ref.url,
            path=destination,
        )
        self.downloader.fetch(download)

        published = item.get("published_at")
        return Acquisition(
            acquisition_id=acquisition_id,
            ref=ref,
            title=item.get("title") or os.path.basename(destination),
            creator=ref.hints.get("feed_author", ""),
            description=item.get("description", ""),
            published_at=_parse_iso(published),
            language=ref.hints.get("language", "en"),
            topics=tuple(ref.hints.get("categories", ()))[:20],
            external_id=ref.key,
            media_url=ref.url,
            download=download,
            raw_metadata={
                "feed_url": ref.hints.get("feed_url", ""),
                "feed_title": ref.hints.get("feed_title", ""),
                "feed_image": ref.hints.get("feed_image", ""),
                "item": item,
            },
        )

    def _thumbnail(self, acquisition: Acquisition, directory: str) -> Thumbnail | None:
        path = acquisition.download.path if acquisition.download else ""
        if not path:
            return None
        destination = os.path.join(directory, "thumb")

        # yt-dlp may already have written one next to the media, and a file on
        # disk beats decoding a frame.
        stem = os.path.splitext(path)[0]
        for suffix in (".jpg", ".png", ".webp"):
            candidate = f"{stem}{suffix}"
            if os.path.exists(candidate):
                return Thumbnail(path=candidate, origin="remote")

        try:
            return self.prober.thumbnail(path, destination, acquisition.probe)
        except OSError:
            # A missing thumbnail is a cosmetic loss. Failing the whole
            # acquisition over it would throw away the media too.
            return None

    # -- persistence -------------------------------------------------------

    def _persist(self, acquisition: Acquisition, channel_id: str) -> SourceRecord:
        """Write the acquired material into `sources`.

        Keyed on the fingerprint, so re-acquiring the same item updates the
        row rather than adding a second one. The rights basis is only ever
        written on insert: an operator who has since recorded a licence must
        not have it reset to `unverified` by a re-crawl.
        """

        probe = acquisition.probe
        fingerprint = _fingerprint(acquisition)

        with self.database.unit_of_work(self.tenant_id) as uow:
            existing = uow.sources.by_fingerprint(fingerprint)
            record = SourceRecord(
                id=existing.id if existing else f"src_{uuid.uuid4().hex[:12]}",
                tenant_id=self.tenant_id,
                title=acquisition.title or acquisition.ref.key,
                kind=_KIND_FOR_INPUT.get(
                    acquisition.ref.kind, SourceKind.LONGFORM_VIDEO
                ).value,
                url=acquisition.media_url or acquisition.ref.url,
                creator=acquisition.creator,
                language=acquisition.language,
                topics=list(acquisition.topics),
                duration_s=probe.duration_s if probe and probe.duration_s else 0.0,
                has_transcript=False,
                published_at=acquisition.published_at,
                fingerprint=fingerprint,
            )
            if existing is not None:
                # Rights are the operator's, not the crawler's.
                record.rights_basis = existing.rights_basis
                record.rights_reference = existing.rights_reference
                record.rights_attribution = existing.rights_attribution
                record.commercial_use = existing.commercial_use
                record.derivatives = existing.derivatives
                record.rights_verified_at = existing.rights_verified_at
                record.rights_expires_at = existing.rights_expires_at
                record.created_at = existing.created_at
            else:
                record.rights_basis = RightsBasis.UNVERIFIED.value
            saved = uow.sources.save(record)
        return saved

    def record_run(
        self,
        acquisition: Acquisition,
        state: str,
        *,
        channel_id: str | None = None,
    ) -> AcquisitionRunRecord:
        """Write the acquisition's own progress to `acquisition_runs`.

        Distinct from `_persist`, which writes the *material* to `sources`.
        This is the record of the work: what was asked for, how far it got,
        what it turned out to be, and what went wrong. Failed and in-flight
        acquisitions have a row here and none in the library, which is the
        point of keeping them apart — a half-downloaded file must not appear
        as something a channel can clip.

        Keyed on (kind, ref_key, channel_id), so a resumed acquisition updates
        its row rather than leaving a trail of abandoned ones.
        """

        ref = acquisition.ref
        download = acquisition.download
        probe = acquisition.probe
        thumbnail = acquisition.thumbnail
        channel = channel_id or None

        with self.database.unit_of_work(self.tenant_id) as uow:
            existing = uow.acquisitions.for_ref(ref.kind.value, ref.key, channel)
            record = AcquisitionRunRecord(
                id=existing.id if existing else acquisition.acquisition_id,
                tenant_id=self.tenant_id,
                source_id=acquisition.source_id or None,
                channel_id=channel,
                kind=ref.kind.value,
                state=state,
                ref_key=ref.key,
                ref_raw=ref.raw,
                url=ref.url,
                title=acquisition.title,
                creator=acquisition.creator,
                external_id=acquisition.external_id,
                published_at=acquisition.published_at,
                attempts=(existing.attempts + 1) if existing else 1,
                last_error=acquisition.error,
                finished_at=self.clock() if state in ("ready", "failed") else None,
                metadata=acquisition.raw_metadata or None,
            )
            if download is not None:
                record.media_path = download.path
                record.bytes_done = download.bytes_done
                record.bytes_total = download.bytes_total
                record.validator = download.validator
                record.content_type = download.content_type
                record.checksum = download.checksum
                record.resumable = download.resumable
            if probe is not None:
                record.duration_s = probe.duration_s
                record.width = probe.width
                record.height = probe.height
                record.has_audio = probe.has_audio
                record.has_video = probe.has_video
                record.prober = probe.prober
            if thumbnail is not None:
                record.thumbnail_path = thumbnail.path
                record.thumbnail_origin = thumbnail.origin
            if existing is not None:
                record.created_at = existing.created_at
                # A retry that got no further must not erase what the last
                # pass learned — the validator especially, since losing it
                # turns the next resume into a restart.
                record.validator = record.validator or existing.validator
                record.media_path = record.media_path or existing.media_path
                record.bytes_done = max(record.bytes_done, existing.bytes_done)
            return uow.acquisitions.save(record)

    def workspace_for(self, acquisition_id: str) -> str:
        return os.path.join(self.config.workspace, self.tenant_id, acquisition_id)

    # -- default transport -------------------------------------------------

    def _default_fetch_text(self, url: str) -> bytes:
        """Fetch a feed document. Small, so it is read whole rather than
        streamed — and capped, because a "feed" that is a 900 MB file is not
        a feed and must not be read into memory to find that out."""

        import urllib.error
        import urllib.request

        request = urllib.request.Request(url, method="GET")
        request.add_header("User-Agent", self.downloader.config.user_agent)
        request.add_header("Accept", "application/rss+xml, application/xml, text/xml")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read(16 << 20)
        except urllib.error.HTTPError as error:
            if error.code in (408, 425, 429) or error.code >= 500:
                raise RetryableError(f"{url}: HTTP {error.code}") from error
            raise PermanentError(f"{url}: HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise RetryableError(f"{url}: {error.reason}") from error


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fingerprint(acquisition: Acquisition) -> str:
    """Stable identity for a piece of material.

    Built from the *reference*, not the bytes. Two encodes of the same episode
    have different checksums and are the same episode; deduplicating on
    content hash would clip both.
    """

    import hashlib

    ref = acquisition.ref
    seed = f"{ref.kind.value}|{ref.key}"
    return hashlib.blake2b(seed.encode(), digest_size=12).hexdigest()


def _parse_iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
