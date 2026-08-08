"""Platform adapters — normalise Twitch, Kick, and YouTube Live into one shape.

Each platform exports chat and events differently enough that the rest of the
engine should never see the difference:

  Twitch        VOD comments carry `content_offset_seconds` (already relative
                to stream start) and structured emote fragments.
  Kick          Messages carry wall-clock ISO timestamps and inline emote
                markup `[emote:12345:KEKW]`, so offsets must be derived from a
                supplied stream start and emotes parsed out of the body.
  YouTube Live  Chat replay carries `videoOffsetTimeMsec`; Super Chats arrive
                as a distinct snippet type with the amount in micros.

Every adapter is tolerant of missing optional fields, because chat exports are
routinely partial — a dropped `emotes` array should cost a little precision,
not abort the ingest of a four-hour stream.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable, Sequence

from .types import ChatMessage, EventKind, Platform, StreamEvent, StreamSession, VideoRegion

# Kick renders emotes inline in the message body.
_KICK_EMOTE = re.compile(r"\[emote:(\d+):([A-Za-z0-9_]+)\]")


def _iso_to_ms(value: str) -> int:
    """Parse an ISO-8601 timestamp to epoch milliseconds."""
    text = value.replace("Z", "+00:00")
    return int(datetime.fromisoformat(text).timestamp() * 1000)


# --- Twitch ------------------------------------------------------------------


def twitch_chat(comments: Iterable[dict[str, Any]]) -> list[ChatMessage]:
    """Normalise Twitch VOD comments.

    Offsets are already relative to stream start, which is why Twitch is the
    easiest of the three to ingest.
    """
    out: list[ChatMessage] = []
    for row in comments:
        message = row.get("message", {})
        badges = {b.get("_id") or b.get("set_id") for b in message.get("user_badges", ())}
        emotes = tuple(
            fragment["emoticon"].get("emoticon_set_id") or fragment.get("text", "")
            for fragment in message.get("fragments", ())
            if fragment.get("emoticon")
        )
        # Prefer the fragment text: it is the emote *name*, which is what the
        # taxonomy keys on. Set ids are useless for classification.
        named = tuple(
            fragment.get("text", "")
            for fragment in message.get("fragments", ())
            if fragment.get("emoticon") and fragment.get("text")
        )
        out.append(
            ChatMessage(
                offset_ms=int(float(row.get("content_offset_seconds", 0)) * 1000),
                author=row.get("commenter", {}).get("display_name", "unknown"),
                text=message.get("body", ""),
                emotes=named or emotes,
                is_moderator="moderator" in badges,
                is_subscriber="subscriber" in badges,
            )
        )
    return sorted(out, key=lambda m: m.offset_ms)


def twitch_events(rows: Iterable[dict[str, Any]]) -> list[StreamEvent]:
    """Normalise Twitch bits, subs, gifts and raids.

    Bits are converted to dollars at the standard 100:1 rate so the scorer can
    compare a cheer against a Kick tip and a YouTube Super Chat on one scale.
    """
    kinds = {
        "cheer": EventKind.DONATION,
        "bits": EventKind.DONATION,
        "subscription": EventKind.SUBSCRIPTION,
        "resub": EventKind.SUBSCRIPTION,
        "subgift": EventKind.GIFT,
        "raid": EventKind.RAID,
        "follow": EventKind.FOLLOW,
    }
    out: list[StreamEvent] = []
    for row in rows:
        kind = kinds.get(str(row.get("type", "")).lower())
        if kind is None:
            continue
        bits = float(row.get("bits", 0) or 0)
        amount = bits / 100.0 if bits else float(row.get("amount", 0) or 0)
        out.append(
            StreamEvent(
                offset_ms=int(float(row.get("offset_seconds", 0)) * 1000),
                kind=kind,
                author=row.get("user_name", "unknown"),
                amount=amount,
                message=row.get("message", "") or "",
                tier=str(row.get("tier", "")),
            )
        )
    return sorted(out, key=lambda e: e.offset_ms)


# --- Kick --------------------------------------------------------------------


def kick_chat(
    messages: Iterable[dict[str, Any]], stream_started_at: str | int
) -> list[ChatMessage]:
    """Normalise Kick chatroom messages.

    Kick timestamps are wall-clock, so the caller must supply the stream start.
    Emotes are inline markup and are both extracted and stripped, so the
    residual text is what a human actually typed.
    """
    origin = (
        _iso_to_ms(stream_started_at)
        if isinstance(stream_started_at, str)
        else int(stream_started_at)
    )
    out: list[ChatMessage] = []
    for row in messages:
        content = row.get("content", "")
        emotes = tuple(name for _, name in _KICK_EMOTE.findall(content))
        stripped = _KICK_EMOTE.sub(" ", content).strip()

        created = row.get("created_at")
        if isinstance(created, str):
            offset = _iso_to_ms(created) - origin
        else:
            offset = int(created or 0) - origin

        sender = row.get("sender", {})
        identity = sender.get("identity", {}) or {}
        badges = {b.get("type") for b in identity.get("badges", ())}
        out.append(
            ChatMessage(
                offset_ms=max(0, offset),
                author=sender.get("username", "unknown"),
                text=stripped,
                emotes=emotes,
                is_moderator="moderator" in badges,
                is_subscriber="subscriber" in badges,
            )
        )
    return sorted(out, key=lambda m: m.offset_ms)


def kick_events(
    rows: Iterable[dict[str, Any]], stream_started_at: str | int
) -> list[StreamEvent]:
    """Normalise Kick tips, subscriptions and gifted subs."""
    origin = (
        _iso_to_ms(stream_started_at)
        if isinstance(stream_started_at, str)
        else int(stream_started_at)
    )
    kinds = {
        "tip": EventKind.DONATION,
        "donation": EventKind.DONATION,
        "subscription": EventKind.SUBSCRIPTION,
        "gifted_subscriptions": EventKind.GIFT,
        "host": EventKind.RAID,
        "follow": EventKind.FOLLOW,
    }
    out: list[StreamEvent] = []
    for row in rows:
        kind = kinds.get(str(row.get("type", "")).lower())
        if kind is None:
            continue
        created = row.get("created_at")
        offset = (
            _iso_to_ms(created) - origin if isinstance(created, str) else int(created or 0) - origin
        )
        out.append(
            StreamEvent(
                offset_ms=max(0, offset),
                kind=kind,
                author=row.get("username", "unknown"),
                amount=float(row.get("amount", 0) or 0),
                currency=str(row.get("currency", "USD")),
                message=row.get("message", "") or "",
            )
        )
    return sorted(out, key=lambda e: e.offset_ms)


# --- YouTube Live ------------------------------------------------------------


def youtube_chat(items: Iterable[dict[str, Any]]) -> list[ChatMessage]:
    """Normalise a YouTube Live chat replay.

    YouTube has no named-emote vocabulary comparable to Twitch's, so the
    taxonomy leans on emoji for this platform — which is what YouTube chat
    mostly is anyway.
    """
    out: list[ChatMessage] = []
    for item in items:
        snippet = item.get("snippet", {})
        author = item.get("authorDetails", {})
        text = (
            snippet.get("displayMessage")
            or snippet.get("textMessageDetails", {}).get("messageText", "")
            or snippet.get("superChatDetails", {}).get("userComment", "")
        )
        offset = snippet.get("videoOffsetTimeMsec")
        out.append(
            ChatMessage(
                offset_ms=int(offset) if offset is not None else 0,
                author=author.get("displayName", "unknown"),
                text=text,
                is_moderator=bool(author.get("isChatModerator")),
                is_subscriber=bool(author.get("isChatSponsor")),
            )
        )
    return sorted(out, key=lambda m: m.offset_ms)


def youtube_events(items: Iterable[dict[str, Any]]) -> list[StreamEvent]:
    """Normalise Super Chats, Super Stickers and memberships."""
    out: list[StreamEvent] = []
    for item in items:
        snippet = item.get("snippet", {})
        kind_name = snippet.get("type", "")
        author = item.get("authorDetails", {}).get("displayName", "unknown")
        offset = int(snippet.get("videoOffsetTimeMsec") or 0)

        if kind_name in ("superChatEvent", "superStickerEvent"):
            details = (
                snippet.get("superChatDetails")
                or snippet.get("superStickerDetails")
                or {}
            )
            out.append(
                StreamEvent(
                    offset_ms=offset,
                    kind=EventKind.DONATION,
                    author=author,
                    amount=float(details.get("amountMicros", 0)) / 1_000_000,
                    currency=details.get("currency", "USD"),
                    message=details.get("userComment", "") or "",
                )
            )
        elif kind_name in ("newSponsorEvent", "membershipGiftingEvent"):
            is_gift = kind_name == "membershipGiftingEvent"
            out.append(
                StreamEvent(
                    offset_ms=offset,
                    kind=EventKind.GIFT if is_gift else EventKind.SUBSCRIPTION,
                    author=author,
                    tier=snippet.get("newSponsorDetails", {}).get("memberLevelName", ""),
                )
            )
    return sorted(out, key=lambda e: e.offset_ms)


# --- Session assembly --------------------------------------------------------

_CHAT_ADAPTERS = {
    Platform.TWITCH: twitch_chat,
    Platform.KICK: kick_chat,
    Platform.YOUTUBE_LIVE: youtube_chat,
}

_EVENT_ADAPTERS = {
    Platform.TWITCH: twitch_events,
    Platform.KICK: kick_events,
    Platform.YOUTUBE_LIVE: youtube_events,
}


def build_session(
    session_id: str,
    platform: Platform,
    duration_ms: int,
    raw_chat: Sequence[dict[str, Any]] = (),
    raw_events: Sequence[dict[str, Any]] = (),
    regions: Sequence[VideoRegion] = (),
    source_width: int = 1920,
    source_height: int = 1080,
    transcript: Any = None,
    stream_started_at: str | int | None = None,
) -> StreamSession:
    """Normalise raw platform exports into a `StreamSession`.

    `stream_started_at` is required for Kick, whose timestamps are wall-clock.
    """
    if platform is Platform.KICK and stream_started_at is None:
        raise ValueError(
            "Kick chat timestamps are wall-clock; stream_started_at is required "
            "to derive offsets relative to stream start"
        )

    chat_fn = _CHAT_ADAPTERS[platform]
    event_fn = _EVENT_ADAPTERS[platform]

    if platform is Platform.KICK:
        chat = chat_fn(raw_chat, stream_started_at)
        events = event_fn(raw_events, stream_started_at)
    else:
        chat = chat_fn(raw_chat)
        events = event_fn(raw_events)

    return StreamSession(
        session_id=session_id,
        platform=platform,
        duration_ms=duration_ms,
        chat=tuple(chat),
        events=tuple(events),
        regions=tuple(regions),
        source_width=source_width,
        source_height=source_height,
        transcript=transcript,
    )
