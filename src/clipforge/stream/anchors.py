"""Anchor detection and clip window placement.

The central problem in stream clipping: **chat reacts after the fact.**

Between something happening on screen and a message appearing in chat there is
broadcast latency, then human reaction time, then typing time. On Twitch that
totals four or five seconds; on YouTube Live, where DVR latency is higher, it
can be eight. A clipper that centres its window on the chat spike therefore
starts several seconds *after* the moment and captures only the aftermath —
the clip opens on people reacting to something the viewer never saw.

So the pipeline is:

    chat spike onset  −  reaction lag  =  the moment on screen
    the moment        −  lead-in       =  where the clip starts

Everything else here follows from getting that subtraction right.
"""

from __future__ import annotations

from typing import Sequence

from .signals import BUCKET_MS, aggregate_window
from .types import (
    Anchor,
    Platform,
    SignalSample,
    Spike,
    StreamSession,
    StreamSignal,
    clamp,
    saturating_sum,
)

# Median observed lag from on-screen event to chat reaction, per platform.
# Broadcast latency dominates and differs by platform delivery stack; the
# remainder is human reaction plus typing, which is roughly constant.
REACTION_LAG_MS: dict[Platform, int] = {
    Platform.TWITCH: 4_500,       # LL-HLS ~3s + ~1.5s react/type
    Platform.KICK: 4_000,         # slightly lower latency in practice
    Platform.YOUTUBE_LIVE: 6_500, # higher default DVR latency
}

# How far into the clip the moment should land, by clip length. Shorter clips
# cannot afford much setup; longer ones need it or they open on dead air.
LEAD_FRACTION: dict[int, float] = {15: 0.30, 30: 0.33, 45: 0.36, 60: 0.38}

# Absolute ceiling on setup regardless of clip length — past this the opening
# is just waiting, and short-form viewers do not wait.
MAX_LEAD_MS = 22_000

# Anchors closer together than this are the same moment seen twice. Chat's
# reaction to a single event decays over 10-15 seconds and the spike detector's
# hysteresis routinely splits that tail into two or three bursts, so the merge
# window has to be wider than the reaction itself or one play becomes three
# near-identical clips.
ANCHOR_MERGE_MS = 16_000

# How wide a window around the anchor is read for signal classification.
# Asymmetric: chat evidence arrives after the moment, so the window leans
# forward from the anchor rather than centring on it.
EVIDENCE_BEFORE_MS = 3_000
EVIDENCE_AFTER_MS = 15_000


def reaction_lag(platform: Platform, override_ms: int | None = None) -> int:
    return override_ms if override_ms is not None else REACTION_LAG_MS[platform]


def _chat_in_window(session: StreamSession, start_ms: int, end_ms: int) -> int:
    """How many chat messages fall in a window.

    The denominator for share-based signal strength — see
    `signals.aggregate_window`.
    """
    return sum(1 for m in session.chat if start_ms <= m.offset_ms < end_ms)


def _spike_intensity(spike: Spike) -> float:
    """Normalise a spike's magnitude to 0..1.

    Saturating: chat going 20x above baseline is not twice as interesting as
    10x, it is the same "chat exploded" event with more people in the room.
    """
    magnitude = spike.magnitude
    if magnitude <= 1.0:
        return 0.0
    return clamp(1.0 - 1.0 / (1.0 + (magnitude - 1.0) / 4.0))


def from_spikes(
    session: StreamSession,
    samples: Sequence[SignalSample],
    spikes: Sequence[Spike],
    lag_ms: int | None = None,
) -> list[Anchor]:
    """Convert chat spikes into lag-corrected anchors."""
    lag = reaction_lag(session.platform, lag_ms)
    anchors: list[Anchor] = []

    for spike in spikes:
        moment_ms = max(0, spike.onset_ms - lag)
        lo = moment_ms - EVIDENCE_BEFORE_MS
        hi = moment_ms + EVIDENCE_AFTER_MS
        signals = aggregate_window(
            samples, lo, hi, _chat_in_window(session, lo, hi)
        )
        if not signals:
            # Chat surged but said nothing classifiable. Still a moment — raw
            # velocity is signal on its own — recorded as an unattributed
            # reaction rather than discarded.
            signals = {StreamSignal.REACTION: _spike_intensity(spike) * 0.7}

        intensity = clamp(
            saturating_sum([_spike_intensity(spike), max(signals.values()) * 0.8])
        )
        anchors.append(
            Anchor(
                offset_ms=moment_ms,
                signals=signals,
                intensity=intensity,
                spike=spike,
                evidence=(
                    f"chat {spike.magnitude:.1f}x baseline "
                    f"(onset {spike.onset_ms // 1000}s, lag-corrected −{lag // 1000}s)",
                ),
            )
        )

    return anchors


def from_events(
    session: StreamSession, samples: Sequence[SignalSample]
) -> list[Anchor]:
    """Anchors from platform events, which need no lag correction.

    A donation's timestamp is the donation, not a reaction to it — the event
    stream is ground truth in a way chat never is. The interesting content is
    usually the streamer reading it out, which happens *after*, so these
    anchors sit slightly forward of the raw event.
    """
    anchors: list[Anchor] = []
    for sample in samples:
        if sample.origin != "event" or sample.signal is not StreamSignal.DONATION:
            continue
        if sample.strength < 0.5:
            continue  # routine subs are not clips

        # Streamers acknowledge donations a beat later; centre on the read.
        moment_ms = sample.offset_ms + 2_000
        lo = moment_ms - EVIDENCE_BEFORE_MS
        hi = moment_ms + EVIDENCE_AFTER_MS
        signals = aggregate_window(
            samples, lo, hi, _chat_in_window(session, lo, hi)
        )
        signals.setdefault(StreamSignal.DONATION, sample.strength)
        anchors.append(
            Anchor(
                offset_ms=moment_ms,
                signals=signals,
                intensity=clamp(sample.strength),
                evidence=(sample.evidence,),
            )
        )
    return anchors


def merge(anchors: Sequence[Anchor], window_ms: int = ANCHOR_MERGE_MS) -> list[Anchor]:
    """Collapse anchors that describe the same moment.

    Keeps the strongest anchor's timing but unions the evidence and signals, so
    a donation that triggers a chat explosion becomes one anchor carrying both
    rather than two competing ones.
    """
    if not anchors:
        return []

    ordered = sorted(anchors, key=lambda a: a.offset_ms)
    merged: list[Anchor] = []
    group: list[Anchor] = [ordered[0]]

    def flush(items: list[Anchor]) -> Anchor:
        best = max(items, key=lambda a: a.intensity)
        signals: dict[StreamSignal, float] = {}
        for anchor in items:
            for signal, strength in anchor.signals.items():
                signals[signal] = max(signals.get(signal, 0.0), strength)
        evidence = tuple(dict.fromkeys(e for a in items for e in a.evidence))
        return Anchor(
            offset_ms=best.offset_ms,
            signals=signals,
            intensity=max(a.intensity for a in items),
            spike=best.spike,
            evidence=evidence,
        )

    for anchor in ordered[1:]:
        if anchor.offset_ms - group[-1].offset_ms <= window_ms:
            group.append(anchor)
        else:
            merged.append(flush(group))
            group = [anchor]
    merged.append(flush(group))

    return merged


def detect(
    session: StreamSession,
    samples: Sequence[SignalSample],
    spikes: Sequence[Spike],
    lag_ms: int | None = None,
) -> list[Anchor]:
    """All anchors for a session, merged and ranked by intensity."""
    anchors = [
        *from_spikes(session, samples, spikes, lag_ms),
        *from_events(session, samples),
    ]
    return sorted(merge(anchors), key=lambda a: -a.intensity)


def window_for(
    anchor: Anchor, duration_s: int, session: StreamSession
) -> tuple[int, int]:
    """Place a clip of `duration_s` around an anchor.

    The moment lands `LEAD_FRACTION` of the way in, capped by `MAX_LEAD_MS`,
    then the window is shifted (never shortened) to stay inside the stream. A
    clip is always exactly its nominal length — the schedulers and render
    templates downstream assume that, and silently returning a 47-second
    "45-second clip" would break both.
    """
    duration_ms = duration_s * 1000
    if duration_ms > session.duration_ms:
        return 0, session.duration_ms

    lead = min(int(duration_ms * LEAD_FRACTION.get(duration_s, 0.33)), MAX_LEAD_MS)
    start = anchor.offset_ms - lead

    # Clamp into range by sliding, preserving exact duration.
    start = max(0, min(start, session.duration_ms - duration_ms))
    return start, start + duration_ms


def variants(
    anchor: Anchor, session: StreamSession, durations_s: Sequence[int]
) -> list[tuple[int, int, int]]:
    """`(duration_s, start_ms, end_ms)` for every requested length."""
    return [
        (duration, *window_for(anchor, duration, session)) for duration in durations_s
    ]


def bucket_index(offset_ms: int) -> int:
    """Bucket index for an offset, matching `signals.bucket_chat`."""
    return offset_ms // BUCKET_MS
