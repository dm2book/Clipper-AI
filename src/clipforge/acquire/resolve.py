"""What did the operator just paste?

One function, `resolve`, turning a string into a `SourceRef`. It looks trivial
and is not: the same YouTube video has at least six URL forms, and a system
that treats them as different videos downloads and publishes the same clip
several times to the same channel.

Normalisation is therefore the point, not URL parsing. `key` is what
deduplication runs on, so every form of the same thing has to produce the same
key — and two genuinely different things must never collide into one.
"""

from __future__ import annotations

import os
import re
from urllib.parse import parse_qs, urlparse, urlunparse

from .types import InputKind, SourceRef, UnsupportedInput

__all__ = ["resolve", "is_youtube_host", "normalise_url", "YOUTUBE_ID"]

#: A YouTube video id: exactly 11 characters of base64url. Anchored, because
#: an unanchored match happily finds eleven characters in the middle of an
#: unrelated string and invents a video that does not exist.
YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

#: Channel ids are `UC` plus 22 more. Handles are `@name`.
_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_HANDLE = re.compile(r"^@[A-Za-z0-9._-]{3,30}$")

_YOUTUBE_HOSTS = frozenset({
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "www.youtu.be", "youtube-nocookie.com",
    "www.youtube-nocookie.com",
})

#: Query parameters that never identify the resource. Stripped so the same
#: video shared from a phone, from a playlist and from an ad campaign all
#: normalise to one key.
_TRACKING = frozenset({
    "si", "feature", "app", "utm_source", "utm_medium", "utm_campaign",
    "utm_term", "utm_content", "gclid", "fbclid", "ref", "ref_src",
    "pp", "ab_channel", "themeRefresh",
})

#: Extensions that mean "this is the media itself", not a page about it.
_MEDIA_SUFFIXES = frozenset({
    ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi",
    ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav",
})

_FEED_SUFFIXES = frozenset({".rss", ".xml", ".atom"})

#: Path fragments that mark a feed even without a telling extension —
#: Megaphone, Libsyn, Simplecast and Anchor all use one of these.
_FEED_MARKERS = ("/feed", "/rss", "/podcast", "feeds.", "/episodes.xml")


def is_youtube_host(host: str) -> bool:
    return host.lower().lstrip(".") in _YOUTUBE_HOSTS


def normalise_url(url: str) -> str:
    """Strip tracking parameters and the fragment; keep everything meaningful.

    Deliberately conservative. An unrecognised parameter is kept, because a
    podcast host that puts the episode id in `?e=` would otherwise have every
    episode normalise to the same key — all of them the feed's front page.
    """

    parsed = urlparse(url)
    kept = [
        (key, value)
        for key, value in parse_qs(parsed.query, keep_blank_values=True).items()
        for value in value
        if key not in _TRACKING
    ]
    query = "&".join(f"{k}={v}" for k, v in sorted(kept))
    return urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path.rstrip("/") or "/",
        parsed.params,
        query,
        "",  # fragment never identifies a resource
    ))


def _youtube(parsed, raw: str) -> SourceRef:
    host = parsed.netloc.lower()
    path = parsed.path.strip("/")
    query = parse_qs(parsed.query)
    hints: dict[str, object] = {}

    # A playlist id rides along on plenty of video links. Noted, not acted on:
    # the user pasted a video, and quietly acquiring the other 200 items in
    # the playlist is not what they asked for.
    if "list" in query:
        hints["playlist_id"] = query["list"][0]
    for name in ("t", "start"):
        if name in query:
            hints["start_hint"] = query[name][0]
            break

    # youtu.be/<id>
    if host.endswith("youtu.be"):
        candidate = path.split("/")[0]
        if YOUTUBE_ID.match(candidate):
            return SourceRef(InputKind.YOUTUBE_VIDEO, candidate, raw,
                             f"https://www.youtube.com/watch?v={candidate}", hints)
        raise UnsupportedInput(f"youtu.be link with no video id: {raw!r}")

    # /watch?v=<id>
    if path == "watch" and "v" in query:
        candidate = query["v"][0]
        if YOUTUBE_ID.match(candidate):
            return SourceRef(InputKind.YOUTUBE_VIDEO, candidate, raw,
                             f"https://www.youtube.com/watch?v={candidate}", hints)
        raise UnsupportedInput(f"not a video id: {candidate!r}")

    # /shorts/<id>, /live/<id>, /embed/<id>, /v/<id>
    head, _, tail = path.partition("/")
    if head in ("shorts", "live", "embed", "v"):
        candidate = tail.split("/")[0]
        if YOUTUBE_ID.match(candidate):
            hints["surface"] = head
            return SourceRef(InputKind.YOUTUBE_VIDEO, candidate, raw,
                             f"https://www.youtube.com/watch?v={candidate}", hints)
        raise UnsupportedInput(f"not a video id: {candidate!r}")

    # Channels. `/channel/UC...` is canonical; `@handle`, `/c/name` and
    # `/user/name` are not resolvable to an id without asking YouTube, so the
    # handle is carried as the key and the adapter resolves it.
    if head == "channel" and _CHANNEL_ID.match(tail.split("/")[0]):
        channel_id = tail.split("/")[0]
        return SourceRef(InputKind.YOUTUBE_CHANNEL, channel_id, raw,
                         f"https://www.youtube.com/channel/{channel_id}", hints)

    first = path.split("/")[0]
    if _HANDLE.match(first):
        return SourceRef(InputKind.YOUTUBE_CHANNEL, first, raw,
                         f"https://www.youtube.com/{first}", hints)
    if head in ("c", "user") and tail:
        name = tail.split("/")[0]
        hints["legacy_path"] = head
        return SourceRef(InputKind.YOUTUBE_CHANNEL, f"{head}/{name}", raw,
                         f"https://www.youtube.com/{head}/{name}", hints)

    raise UnsupportedInput(f"YouTube URL that names nothing acquirable: {raw!r}")


def resolve(value: str) -> SourceRef:
    """Turn what the operator typed into a normalised reference.

    Order matters. A local path is checked before anything URL-shaped, so a
    Windows path or a filename containing a colon is not mistaken for a scheme.
    """

    raw = (value or "").strip()
    if not raw:
        raise UnsupportedInput("empty input")

    # -- a file on disk ----------------------------------------------------
    if raw.startswith("file://"):
        path = urlparse(raw).path
        return _local(path, raw)
    if os.path.sep in raw or raw.startswith("."):
        expanded = os.path.abspath(os.path.expanduser(raw))
        if os.path.exists(expanded):
            return _local(expanded, raw)

    parsed = urlparse(raw)

    # A bare id is a convenience worth having: operators paste them constantly.
    if not parsed.scheme:
        if YOUTUBE_ID.match(raw):
            return SourceRef(InputKind.YOUTUBE_VIDEO, raw, raw,
                             f"https://www.youtube.com/watch?v={raw}")
        if _CHANNEL_ID.match(raw) or _HANDLE.match(raw):
            suffix = raw if _HANDLE.match(raw) else f"channel/{raw}"
            return SourceRef(InputKind.YOUTUBE_CHANNEL, raw, raw,
                             f"https://www.youtube.com/{suffix}")
        expanded = os.path.abspath(os.path.expanduser(raw))
        if os.path.exists(expanded):
            return _local(expanded, raw)
        raise UnsupportedInput(f"not a URL, an id, or a path that exists: {raw!r}")

    if parsed.scheme not in ("http", "https"):
        raise UnsupportedInput(f"unsupported scheme {parsed.scheme!r}")

    if is_youtube_host(parsed.netloc):
        return _youtube(parsed, raw)

    url = normalise_url(raw)
    suffix = os.path.splitext(urlparse(url).path)[1].lower()

    if suffix in _MEDIA_SUFFIXES:
        return SourceRef(InputKind.MEDIA_URL, url, raw, url)

    lowered = url.lower()
    if suffix in _FEED_SUFFIXES or any(m in lowered for m in _FEED_MARKERS):
        return SourceRef(InputKind.PODCAST_FEED, url, raw, url)

    # Ambiguous http(s) URL with nothing telling in it. Treated as a feed
    # rather than refused: podcast hosts serve feeds from paths with no
    # extension all the time, and the parser reports a clear error if the body
    # turns out not to be XML. Guessing "media" instead would download a web
    # page and hand the renderer an HTML file named `.mp4`.
    return SourceRef(InputKind.PODCAST_FEED, url, raw, url,
                     {"guessed": True})


def _local(path: str, raw: str) -> SourceRef:
    absolute = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(absolute):
        raise UnsupportedInput(f"no such file: {absolute}")
    if not os.path.isfile(absolute):
        raise UnsupportedInput(f"not a file: {absolute}")
    suffix = os.path.splitext(absolute)[1].lower()
    if suffix and suffix not in _MEDIA_SUFFIXES:
        raise UnsupportedInput(
            f"{suffix} is not a media extension this layer accepts"
        )
    return SourceRef(InputKind.LOCAL_FILE, absolute, raw, f"file://{absolute}")
