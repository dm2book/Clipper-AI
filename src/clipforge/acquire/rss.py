"""Podcast feeds, parsed with the standard library.

RSS 2.0 with the iTunes extensions, plus Atom, which a few shows still use.
No `feedparser` dependency: what is needed here is a dozen fields out of an
XML document, and `xml.etree` reads them in about a hundred lines.

## The parts that are not obvious

**The enclosure is the episode.** `<enclosure url=... type=... length=...>` is
the audio file; everything else in the item is description. A feed item with
no enclosure is a blog post the show cross-posted, and treating it as an
episode produces a source with no media.

**Durations are three formats.** `<itunes:duration>` is "3600", "60:00" or
"01:00:00" depending on the host, and all three appear in the wild. It is also
frequently wrong — hosts fill it in by hand — so it is a hint, superseded by
the real measurement once the file is downloaded.

**Artwork cascades.** An item may carry its own `itunes:image`; if not, the
channel's applies. Falling back is what gives every episode of a show a
thumbnail rather than only the handful with per-episode art.

**GUIDs are the identity.** `<guid>` is what makes the same episode
recognisable when its URL changes — and enclosure URLs change constantly,
because hosts move CDNs and rewrite tracking prefixes. Deduplicating on URL
means re-downloading the entire back catalogue after a host migration.

## XML from strangers

`xml.etree.ElementTree` does not expand external entities, which is what makes
the billion-laughs and external-entity attacks work against other parsers. It
is the safe choice here and the reason this does not reach for `lxml`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree

from .types import PermanentError

__all__ = ["FeedItem", "Feed", "parse_feed", "parse_duration"]

_ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
_ATOM = "http://www.w3.org/2005/Atom"
_CONTENT = "http://purl.org/rss/1.0/modules/content/"
_MEDIA = "http://search.yahoo.com/mrss/"

#: Audio and video MIME prefixes an enclosure must carry to be an episode.
_MEDIA_TYPES = ("audio/", "video/")

_DURATION_HHMMSS = re.compile(r"^\d{1,3}(:\d{1,2}){1,2}$")


@dataclass(frozen=True, slots=True)
class FeedItem:
    """One episode."""

    guid: str
    title: str
    media_url: str
    #: From `<enclosure type>`. Worth keeping: it decides the file extension
    #: when the URL has none, which podcast CDNs frequently arrange.
    media_type: str = ""
    #: The feed's claim about the byte length. A hint — hosts get it wrong,
    #: and Content-Length at download time is the number that counts.
    media_bytes: int = 0
    description: str = ""
    published_at: datetime | None = None
    #: The feed's claim about duration, in seconds. Also a hint.
    duration_s: float | None = None
    image_url: str = ""
    author: str = ""
    episode: int | None = None
    season: int | None = None
    explicit: bool = False
    keywords: tuple[str, ...] = ()
    link: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "guid": self.guid,
            "title": self.title,
            "media_url": self.media_url,
            "media_type": self.media_type,
            "published_at": (
                self.published_at.isoformat() if self.published_at else None
            ),
            "duration_s": self.duration_s,
            "episode": self.episode,
            "season": self.season,
        }


@dataclass(frozen=True, slots=True)
class Feed:
    """A show, and the episodes currently in its feed."""

    title: str = ""
    author: str = ""
    description: str = ""
    link: str = ""
    language: str = "en"
    image_url: str = ""
    categories: tuple[str, ...] = ()
    explicit: bool = False
    items: tuple[FeedItem, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "author": self.author,
            "language": self.language,
            "categories": list(self.categories),
            "items": len(self.items),
        }


def parse_duration(value: str | None) -> float | None:
    """"3600", "60:00" and "01:00:00" are all an hour. So is "1:00:00.500"."""

    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if _DURATION_HHMMSS.match(text.split(".")[0]):
        total = 0.0
        for part in text.split(":"):
            try:
                total = total * 60 + float(part)
            except ValueError:
                return None
        return total
    try:
        seconds = float(text)
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def parse_feed(payload: bytes | str, *, limit: int = 0) -> Feed:
    """Parse a feed document. Raises `PermanentError` on anything unparseable.

    Permanent, not retryable: a document that is not XML this minute will not
    be XML in eight minutes either, and retrying it is a queue burning its
    afternoon on a host that is serving an HTML error page.
    """

    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise PermanentError(f"not parseable as XML: {error}") from error

    if root.tag == f"{{{_ATOM}}}feed":
        return _atom(root, limit)
    channel = root.find("channel")
    if channel is None:
        raise PermanentError(
            f"XML with no <channel> and no Atom <feed> — root is {root.tag!r}"
        )
    return _rss(channel, limit)


# ---------------------------------------------------------------------------
# RSS 2.0
# ---------------------------------------------------------------------------


def _rss(channel: ElementTree.Element, limit: int) -> Feed:
    image = (
        _attr(channel, f"{{{_ITUNES}}}image", "href")
        or _text(channel, "image/url")
    )
    categories = tuple(
        value for element in channel.findall(f"{{{_ITUNES}}}category")
        if (value := element.get("text", "").strip())
    ) or tuple(
        value for element in channel.findall("category")
        if (value := (element.text or "").strip())
    )

    items = []
    for element in channel.findall("item"):
        if item := _rss_item(element, image):
            items.append(item)
            if limit and len(items) >= limit:
                break

    return Feed(
        title=_text(channel, "title"),
        author=(
            _text(channel, f"{{{_ITUNES}}}author")
            or _text(channel, "managingEditor")
        ),
        description=(
            _text(channel, "description")
            or _text(channel, f"{{{_ITUNES}}}summary")
        ),
        link=_text(channel, "link"),
        language=(_text(channel, "language") or "en").split("-")[0].lower(),
        image_url=image,
        categories=categories,
        explicit=_yes(_text(channel, f"{{{_ITUNES}}}explicit")),
        items=tuple(items),
    )


def _rss_item(
    element: ElementTree.Element, channel_image: str
) -> FeedItem | None:
    enclosure = element.find("enclosure")
    url = (enclosure.get("url", "").strip() if enclosure is not None else "")
    media_type = (enclosure.get("type", "").strip() if enclosure is not None else "")

    if not url:
        # `<media:content>` is the other common way to attach a file — some
        # video podcasts use it instead of an enclosure.
        content = element.find(f"{{{_MEDIA}}}content")
        if content is not None:
            url = content.get("url", "").strip()
            media_type = content.get("type", "").strip()

    if not url:
        return None  # a cross-posted article, not an episode
    if media_type and not media_type.startswith(_MEDIA_TYPES):
        return None

    guid = _text(element, "guid") or url
    title = _text(element, "title") or "(untitled episode)"

    return FeedItem(
        guid=guid,
        title=title,
        media_url=url,
        media_type=media_type,
        media_bytes=_int(enclosure.get("length") if enclosure is not None else None),
        description=(
            _text(element, f"{{{_ITUNES}}}summary")
            or _text(element, "description")
            or _text(element, f"{{{_CONTENT}}}encoded")
        ),
        published_at=_date(_text(element, "pubDate")),
        duration_s=parse_duration(_text(element, f"{{{_ITUNES}}}duration")),
        image_url=_attr(element, f"{{{_ITUNES}}}image", "href") or channel_image,
        author=_text(element, f"{{{_ITUNES}}}author") or _text(element, "author"),
        episode=_int_or_none(_text(element, f"{{{_ITUNES}}}episode")),
        season=_int_or_none(_text(element, f"{{{_ITUNES}}}season")),
        explicit=_yes(_text(element, f"{{{_ITUNES}}}explicit")),
        keywords=tuple(
            k.strip() for k in _text(element, f"{{{_ITUNES}}}keywords").split(",")
            if k.strip()
        ),
        link=_text(element, "link"),
    )


# ---------------------------------------------------------------------------
# Atom
# ---------------------------------------------------------------------------


def _atom(root: ElementTree.Element, limit: int) -> Feed:
    items = []
    for entry in root.findall(f"{{{_ATOM}}}entry"):
        link = next(
            (
                element for element in entry.findall(f"{{{_ATOM}}}link")
                if element.get("rel") == "enclosure"
                and (element.get("type") or "").startswith(_MEDIA_TYPES)
            ),
            None,
        )
        if link is None or not link.get("href"):
            continue
        items.append(FeedItem(
            guid=_text(entry, f"{{{_ATOM}}}id") or link.get("href", ""),
            title=_text(entry, f"{{{_ATOM}}}title") or "(untitled episode)",
            media_url=link.get("href", ""),
            media_type=link.get("type", ""),
            media_bytes=_int(link.get("length")),
            description=(
                _text(entry, f"{{{_ATOM}}}summary")
                or _text(entry, f"{{{_ATOM}}}content")
            ),
            published_at=_iso(
                _text(entry, f"{{{_ATOM}}}published")
                or _text(entry, f"{{{_ATOM}}}updated")
            ),
        ))
        if limit and len(items) >= limit:
            break

    return Feed(
        title=_text(root, f"{{{_ATOM}}}title"),
        author=_text(root, f"{{{_ATOM}}}author/{{{_ATOM}}}name"),
        description=_text(root, f"{{{_ATOM}}}subtitle"),
        link=_atom_link(root),
        items=tuple(items),
    )


def _atom_link(root: ElementTree.Element) -> str:
    for element in root.findall(f"{{{_ATOM}}}link"):
        if element.get("rel") in (None, "alternate"):
            return element.get("href", "")
    return ""


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------


def _text(element: ElementTree.Element, path: str) -> str:
    found = element.find(path)
    return (found.text or "").strip() if found is not None else ""


def _attr(element: ElementTree.Element, path: str, name: str) -> str:
    found = element.find(path)
    return (found.get(name, "") or "").strip() if found is not None else ""


def _int(value: str | None) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _int_or_none(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _yes(value: str) -> bool:
    return value.strip().lower() in ("yes", "true", "explicit")


def _date(value: str) -> datetime | None:
    """RFC 2822, as RSS requires — and a few things that are nearly it."""

    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return _iso(value)
    if parsed is None:
        return None
    # A feed that omits the zone means UTC here rather than local time: local
    # is the server's timezone, which is not a fact about the episode.
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
