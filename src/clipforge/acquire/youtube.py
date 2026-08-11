"""YouTube, through yt-dlp.

## What this does and does not decide

Downloading a video is a technical act; republishing it is a licensing one.
This adapter does the first and takes no position on the second: everything it
produces carries `RightsBasis.UNVERIFIED`, which the channel gate refuses by
default. Making a source publishable is a decision a person records against a
licence, and routing it through here would launder that decision into a
technical one.

Worth saying plainly once: **YouTube's Terms of Service prohibit downloading
content except through features YouTube provides.** Material a customer owns,
material published under a Creative Commons licence, and material the creator
has given written permission for are the defensible cases; a general crawler
over other people's uploads is not, whatever this code is capable of. The
rights model exists because that distinction has to be recorded per source,
and this adapter deliberately cannot short-circuit it.

## Why yt-dlp rather than the Data API

The Data API returns metadata and no media. It is the right tool for
discovering what a channel has published — and `list_channel` uses the flat
playlist path for exactly that — but there is no API that returns the file.

## Format selection

Progressive MP4 first, then a merged best-video-plus-best-audio. The
preference is not about quality: a single progressive file needs no
remuxing, so acquisition stays a download rather than a download plus an
ffmpeg pass, and the pipeline gets a file with a `moov` the box reader can
measure. When only adaptive formats exist, yt-dlp merges them, which does
require ffmpeg — checked up front so the failure names the missing binary
rather than surfacing as a mysterious empty file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from .probe import find_ffmpeg
from .types import (
    Acquisition,
    Download,
    DownloadState,
    InputKind,
    PermanentError,
    RetryableError,
    SourceRef,
)

__all__ = ["YouTubeConfig", "YouTubeAdapter", "metadata_from_info", "yt_dlp_available"]

#: yt-dlp error fragments that mean "do not try again". Matched on the message
#: because yt-dlp raises one exception type for everything.
_PERMANENT_SIGNS = (
    "video unavailable",
    "private video",
    "removed by the uploader",
    "account associated with this video has been terminated",
    "this video is not available",
    "members-only",
    "is not a valid url",
    "unsupported url",
    "video has been removed",
    "age-restricted",
    "sign in to confirm your age",
    "copyright",
)


def yt_dlp_available() -> bool:
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass(slots=True)
class YouTubeConfig:
    #: Progressive MP4 first — see the module docstring on why.
    format: str = "best[ext=mp4][acodec!=none][vcodec!=none]/bv*+ba/best"
    #: How many items `list_channel` walks. A channel with 4,000 uploads is
    #: not a thing to enumerate by accident.
    channel_limit: int = 50
    #: Newest first. A channel is browsed for recent material, and the tail is
    #: reachable by raising the limit deliberately.
    newest_first: bool = True
    socket_timeout_s: int = 30
    retries: int = 3
    #: Passed through to yt-dlp. A cookie file is how an operator downloads
    #: their *own* unlisted or members-only uploads, which is a legitimate
    #: case the public path cannot serve.
    cookiefile: str = ""
    proxy: str = ""
    #: Written to the download directory alongside the media.
    write_thumbnail: bool = True
    quiet: bool = True
    extra_opts: dict[str, Any] = field(default_factory=dict)


class YouTubeAdapter:
    """Metadata and media for one video, and listings for one channel.

    yt-dlp is imported lazily so the rest of acquisition — feeds, uploads,
    plain media URLs — works in an install that does not have it.
    """

    def __init__(
        self,
        config: YouTubeConfig | None = None,
        *,
        ydl_factory: Callable[[dict], Any] | None = None,
    ) -> None:
        self.config = config or YouTubeConfig()
        # Injected in tests so the adapter's own logic — option assembly,
        # metadata mapping, error classification — is testable without a
        # network round trip to YouTube.
        self._ydl_factory = ydl_factory

    # -- options -----------------------------------------------------------

    def _options(self, *, download: bool, outtmpl: str = "") -> dict[str, Any]:
        options: dict[str, Any] = {
            "format": self.config.format,
            "quiet": self.config.quiet,
            "no_warnings": self.config.quiet,
            "noprogress": True,
            "socket_timeout": self.config.socket_timeout_s,
            "retries": self.config.retries,
            # yt-dlp's own resumption. Complementary to `http.HttpDownloader`,
            # which handles the plain-URL path; here yt-dlp owns the transfer
            # because only it can reassemble adaptive formats.
            "continuedl": True,
            "noplaylist": True,
            # A playlist error must not abort the whole channel walk — one
            # deleted video should not cost the other forty-nine.
            "ignoreerrors": "only_download",
        }
        if download:
            options["outtmpl"] = outtmpl
            options["writethumbnail"] = self.config.write_thumbnail
        else:
            options["skip_download"] = True
        if self.config.cookiefile:
            options["cookiefile"] = self.config.cookiefile
        if self.config.proxy:
            options["proxy"] = self.config.proxy
        options.update(self.config.extra_opts)
        return options

    def _ydl(self, options: dict[str, Any]):
        if self._ydl_factory is not None:
            return self._ydl_factory(options)
        try:
            from yt_dlp import YoutubeDL
        except ImportError as error:  # pragma: no cover - depends on install
            raise PermanentError(
                "yt-dlp is not installed — `pip install yt-dlp` to acquire "
                "from YouTube"
            ) from error
        return YoutubeDL(options)

    # -- metadata ----------------------------------------------------------

    def describe(self, ref: SourceRef) -> Acquisition:
        """Everything about the video except the bytes."""

        if ref.kind is not InputKind.YOUTUBE_VIDEO:
            raise PermanentError(f"not a YouTube video reference: {ref.kind}")

        info = self._extract(ref.url, download=False)
        acquisition = metadata_from_info(info, ref)
        return acquisition

    def list_channel(self, ref: SourceRef, limit: int = 0) -> list[SourceRef]:
        """The channel's uploads, as video references.

        A flat listing: metadata for each video is fetched later, per video,
        because a full extraction of fifty videos is fifty round trips before
        anything has been decided about any of them.
        """

        if ref.kind is not InputKind.YOUTUBE_CHANNEL:
            raise PermanentError(f"not a YouTube channel reference: {ref.kind}")

        limit = limit or self.config.channel_limit
        options = self._options(download=False)
        options.update({
            "extract_flat": "in_playlist",
            "playlistend": limit,
            "noplaylist": False,
        })
        # `/videos` rather than the channel root: the root is a curated home
        # page whose first "playlist" is often a shelf of someone else's
        # content, and walking that acquires videos from another channel.
        url = ref.url.rstrip("/")
        if not url.endswith("/videos"):
            url = f"{url}/videos"

        info = self._extract(url, download=False, url_override=url, options=options)
        entries = info.get("entries") or []
        refs: list[SourceRef] = []
        for entry in entries:
            if not entry:
                continue  # `ignoreerrors` leaves a None where a video failed
            video_id = entry.get("id", "")
            if not video_id:
                continue
            refs.append(SourceRef(
                kind=InputKind.YOUTUBE_VIDEO,
                key=video_id,
                raw=ref.raw,
                url=f"https://www.youtube.com/watch?v={video_id}",
                hints={
                    "channel_id": info.get("channel_id") or ref.key,
                    "channel_title": info.get("title", ""),
                    "from_channel": True,
                },
            ))
            if len(refs) >= limit:
                break
        return refs

    # -- download ----------------------------------------------------------

    def download(self, ref: SourceRef, directory: str) -> Acquisition:
        """Fetch the video and report what landed."""

        if ref.kind is not InputKind.YOUTUBE_VIDEO:
            raise PermanentError(f"not a YouTube video reference: {ref.kind}")

        os.makedirs(directory, exist_ok=True)
        template = os.path.join(directory, f"{ref.key}.%(ext)s")
        options = self._options(download=True, outtmpl=template)

        if _needs_merge(self.config.format) and not find_ffmpeg():
            # Named up front. Without ffmpeg yt-dlp writes the video and audio
            # streams as separate files and merges nothing, and the failure
            # surfaces later as "the file has no audio" with nothing pointing
            # back to a missing binary.
            raise PermanentError(
                "this format selection may need ffmpeg to merge adaptive "
                "streams, and no ffmpeg was found"
            )

        info = self._extract(ref.url, download=True, options=options)
        acquisition = metadata_from_info(info, ref)

        path = _downloaded_path(info, directory, ref.key)
        if not path:
            raise RetryableError(
                f"{ref.key}: yt-dlp reported success but no file was found in "
                f"{directory}"
            )
        size = os.path.getsize(path)
        acquisition.download = Download(
            download_id=f"dl_{ref.key}",
            url=ref.url,
            path=path,
            state=DownloadState.COMPLETE,
            bytes_done=size,
            bytes_total=size,
            content_type=f"video/{os.path.splitext(path)[1].lstrip('.')}",
            finished_at=datetime.now(UTC),
        )
        return acquisition

    # -- plumbing ----------------------------------------------------------

    def _extract(
        self,
        url: str,
        *,
        download: bool,
        url_override: str = "",
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        options = options if options is not None else self._options(download=download)
        target = url_override or url
        try:
            with self._ydl(options) as ydl:
                info = ydl.extract_info(target, download=download)
        except Exception as error:  # noqa: BLE001 - yt-dlp raises many types
            raise _classify(error, target) from error
        if info is None:
            raise PermanentError(f"{target}: yt-dlp returned nothing")
        if hasattr(info, "get") and options.get("skip_download") is not True:
            info = getattr(info, "get", dict)("__post_extract", None) or info
        return info


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def metadata_from_info(info: dict[str, Any], ref: SourceRef) -> Acquisition:
    """yt-dlp's `info_dict` into an `Acquisition`.

    Kept as a plain function so it can be tested against recorded `info_dict`
    payloads without a network call. The mapping is where field-name drift
    between yt-dlp releases bites, and it is worth being able to pin it.
    """

    duration = info.get("duration")
    published = _upload_date(info)

    return Acquisition(
        acquisition_id=f"acq_{ref.key}",
        ref=ref,
        title=info.get("title") or "",
        creator=(
            info.get("uploader")
            or info.get("channel")
            or info.get("uploader_id")
            or ""
        ),
        description=info.get("description") or "",
        published_at=published,
        language=(info.get("language") or "en").split("-")[0].lower(),
        topics=tuple(t for t in (info.get("tags") or []) if isinstance(t, str))[:20],
        external_id=info.get("id") or ref.key,
        media_url=info.get("webpage_url") or ref.url,
        raw_metadata={
            # Deliberately a subset. The full info_dict is megabytes of format
            # descriptors, and storing it per video is a jsonb column nobody
            # reads and everybody pays for.
            "duration": duration,
            "view_count": info.get("view_count"),
            "like_count": info.get("like_count"),
            "channel_id": info.get("channel_id"),
            "channel_url": info.get("channel_url"),
            "thumbnail": info.get("thumbnail"),
            "categories": info.get("categories"),
            "live_status": info.get("live_status"),
            "availability": info.get("availability"),
            # The one licence signal YouTube exposes. Not trusted as a rights
            # basis on its own — it is a hint for the person recording one.
            "license": info.get("license"),
            "age_limit": info.get("age_limit"),
            "width": info.get("width"),
            "height": info.get("height"),
            "fps": info.get("fps"),
            "ext": info.get("ext"),
        },
    )


def _upload_date(info: dict[str, Any]) -> datetime | None:
    if timestamp := info.get("timestamp"):
        try:
            return datetime.fromtimestamp(int(timestamp), UTC)
        except (TypeError, ValueError, OSError):
            pass
    raw = info.get("upload_date") or ""
    if len(raw) == 8 and raw.isdigit():
        try:
            return datetime(int(raw[:4]), int(raw[4:6]), int(raw[6:]), tzinfo=UTC)
        except ValueError:
            return None
    return None


def _downloaded_path(info: dict[str, Any], directory: str, key: str) -> str:
    """Where the file actually landed.

    yt-dlp reports it in three different places depending on version and
    whether a merge happened, so all three are checked before falling back to
    looking in the directory.
    """

    downloads = info.get("requested_downloads") or []
    for entry in downloads:
        for field_name in ("filepath", "_filename", "filename"):
            candidate = entry.get(field_name)
            if candidate and os.path.exists(candidate):
                return candidate
    for field_name in ("filepath", "_filename", "filename"):
        candidate = info.get(field_name)
        if candidate and os.path.exists(candidate):
            return candidate

    # Last resort: the biggest file whose name starts with the video id, and
    # which is not the thumbnail or the metadata sidecar.
    matches = [
        os.path.join(directory, name)
        for name in os.listdir(directory)
        if name.startswith(key)
        and not name.endswith((".jpg", ".png", ".webp", ".json", ".part"))
    ]
    return max(matches, key=os.path.getsize) if matches else ""


def _needs_merge(format_spec: str) -> bool:
    """True when the selection can resolve to separate video and audio."""

    return "+" in format_spec


def _classify(error: Exception, target: str) -> Exception:
    message = str(error).lower()
    if any(sign in message for sign in _PERMANENT_SIGNS):
        return PermanentError(f"{target}: {error}")
    return RetryableError(f"{target}: {error}")
