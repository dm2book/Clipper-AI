"""Signal extraction from chat, events, and (optionally) the transcript.

Chat is bursty and its baseline varies by two orders of magnitude between a
200-viewer stream and a 50,000-viewer one, so everything here is measured
*relative to the stream's own baseline* rather than against absolute rates. A
tripling of chat velocity is the same signal on both streams; "40 messages per
second" is not.
"""

from __future__ import annotations

import statistics
from typing import Sequence

from . import emotes
from .types import (
    ChatMessage,
    EventKind,
    Platform,
    SignalSample,
    Spike,
    StreamEvent,
    StreamSession,
    StreamSignal,
    clamp,
    saturating_sum,
)

# Chat is bucketed at one second. Finer resolution just amplifies noise;
# coarser loses the onset, which is the number the whole clipper depends on.
BUCKET_MS = 1_000

# Trailing window for the rolling baseline. Long enough to survive a hype
# moment without the baseline chasing it, short enough to track a stream that
# genuinely gains viewers over four hours.
BASELINE_WINDOW_S = 180

# A bucket is "spiking" once it exceeds this multiple of baseline.
SPIKE_ENTER = 2.2
# ...and the spike continues until it falls back below this one. Hysteresis
# stops a single quiet second from splitting one reaction into two spikes.
SPIKE_EXIT = 1.4

MIN_BASELINE_RATE = 0.5  # messages/sec floor, so a dead chat cannot divide by ~0


def bucket_chat(chat: Sequence[ChatMessage], duration_ms: int) -> list[int]:
    """Message counts per one-second bucket across the whole stream."""
    buckets = [0] * (max(1, duration_ms // BUCKET_MS) + 1)
    for message in chat:
        index = message.offset_ms // BUCKET_MS
        if 0 <= index < len(buckets):
            buckets[index] += 1
    return buckets


def rolling_baseline(counts: Sequence[int], window_s: int = BASELINE_WINDOW_S) -> list[float]:
    """Median message rate over the trailing window, per bucket.

    Median rather than mean: chat is heavy-tailed, and a single 500-message
    second would drag a mean baseline up far enough to hide the next real
    spike. The median is unmoved by it.
    """
    baseline: list[float] = []
    for i in range(len(counts)):
        lo = max(0, i - window_s)
        window = counts[lo : i + 1] or [0]
        baseline.append(max(MIN_BASELINE_RATE, statistics.median(window)))
    return baseline


def find_spikes(counts: Sequence[int], baseline: Sequence[float]) -> list[Spike]:
    """Locate chat bursts, recording where each one *started*.

    The onset matters far more than the peak. Chat lags the stream, so the peak
    is already several seconds after the thing that caused it; a clipper that
    centres on the peak reliably cuts off the moment it was trying to capture.
    """
    spikes: list[Spike] = []
    in_spike = False
    onset = 0
    peak_index = 0
    peak_rate = 0.0

    for i, count in enumerate(counts):
        ratio = count / baseline[i]
        if not in_spike:
            if ratio >= SPIKE_ENTER:
                in_spike = True
                onset = i
                peak_index = i
                peak_rate = float(count)
        else:
            if count > peak_rate:
                peak_rate = float(count)
                peak_index = i
            if ratio < SPIKE_EXIT:
                spikes.append(
                    Spike(
                        onset_ms=onset * BUCKET_MS,
                        peak_ms=peak_index * BUCKET_MS,
                        end_ms=i * BUCKET_MS,
                        peak_rate=peak_rate,
                        baseline_rate=baseline[onset],
                    )
                )
                in_spike = False

    if in_spike:
        spikes.append(
            Spike(
                onset_ms=onset * BUCKET_MS,
                peak_ms=peak_index * BUCKET_MS,
                end_ms=len(counts) * BUCKET_MS,
                peak_rate=peak_rate,
                baseline_rate=baseline[onset],
            )
        )

    return spikes


def chat_signals(
    chat: Sequence[ChatMessage], platform: Platform
) -> list[SignalSample]:
    """Classify every chat message and emit one sample per signal found."""
    samples: list[SignalSample] = []
    for message in chat:
        found = emotes.classify_message(message.text, message.emotes, platform)
        for signal, strength in found.items():
            if strength <= 0.0:
                continue
            samples.append(
                SignalSample(
                    offset_ms=message.offset_ms,
                    signal=signal,
                    strength=strength,
                    origin="chat",
                    evidence=message.text[:60],
                )
            )
    return samples


def event_signals(events: Sequence[StreamEvent]) -> list[SignalSample]:
    """Turn platform events into donation signals, scaled within the stream.

    Amounts are normalised against this stream's own median donation rather
    than an absolute dollar scale: a $20 tip is a major moment on a small
    channel and unremarkable on a large one, and the clipper should behave the
    same way on both.
    """
    monetary = [e for e in events if e.is_monetary and e.amount > 0]
    reference = statistics.median([e.amount for e in monetary]) if monetary else 1.0
    reference = max(reference, 1.0)

    samples: list[SignalSample] = []
    for event in events:
        if event.kind is EventKind.FOLLOW:
            continue  # too frequent and too small to be a clip anchor

        if event.is_monetary and event.amount > 0:
            # Log ratio so a 100x donation is a strong signal, not a 100x one.
            import math

            ratio = event.amount / reference
            strength = clamp(0.35 + 0.32 * math.log10(max(ratio, 0.1) * 10))
            label = f"{event.kind.value} {event.amount:.2f} {event.currency}"
        elif event.kind is EventKind.RAID:
            strength = 0.6
            label = f"raid from {event.author}"
        else:
            strength = 0.4
            label = event.kind.value

        samples.append(
            SignalSample(
                offset_ms=event.offset_ms,
                signal=StreamSignal.DONATION,
                strength=strength,
                origin="event",
                evidence=f"{event.author}: {label}",
            )
        )

        # A donation with a message is often read aloud, which produces a
        # genuine on-stream reaction worth clipping alongside the donation.
        if event.message.strip():
            samples.append(
                SignalSample(
                    offset_ms=event.offset_ms,
                    signal=StreamSignal.REACTION,
                    strength=clamp(strength * 0.6),
                    origin="event",
                    evidence=f"read aloud: {event.message[:50]}",
                )
            )

    return samples


# Transcript categories from the viral engine that map onto stream categories.
# Not every viral signal has a stream equivalent, and that is fine — money and
# lessons are podcast concerns, not stream ones.
_TRANSCRIPT_MAP = {
    "funny": StreamSignal.FUNNY,
    "emotional_spike": StreamSignal.EMOTIONAL,
    "argument": StreamSignal.ARGUMENT,
    "controversy": StreamSignal.ARGUMENT,
    "failure": StreamSignal.FAIL,
    "success": StreamSignal.WIN,
    "debate": StreamSignal.ARGUMENT,
}


def transcript_signals(transcript: object) -> list[SignalSample]:
    """Reuse the viral engine's transcript detectors on stream audio.

    Optional: a stream with no transcript is fully supported, and chat alone
    carries most of the signal. Where a transcript exists it disambiguates —
    chat spamming OMEGALUL tells you something happened, and the streamer
    shouting tells you what.
    """
    if transcript is None:
        return []

    try:
        from clipforge.viral.detectors import detect_all
    except ImportError:  # pragma: no cover - viral package always ships together
        return []

    samples: list[SignalSample] = []
    utterances = getattr(transcript, "utterances", ())
    for hit in detect_all(transcript):
        mapped = _TRANSCRIPT_MAP.get(hit.signal.value)
        if mapped is None:
            continue
        utterance = utterances[hit.utterance_index]
        samples.append(
            SignalSample(
                offset_ms=utterance.start_ms,
                signal=mapped,
                # Transcript evidence is discounted against chat: ASR is noisy,
                # and one streamer's word carries less than a thousand viewers
                # reacting at once.
                strength=clamp(hit.strength * 0.75),
                origin="transcript",
                evidence=utterance.text[:60],
            )
        )
    return samples


def collect(session: StreamSession) -> list[SignalSample]:
    """Every signal sample for a session, from all three timelines."""
    return [
        *chat_signals(session.chat, session.platform),
        *event_signals(session.events),
        *transcript_signals(session.transcript),
    ]


# What share of chat carrying a signal counts as fully that signal. A burst
# where a quarter of messages are laughter is a funny moment; the rest of the
# chat is always filler, greetings, and people asking what game this is.
SATURATION_COVERAGE = 0.22


def aggregate_window(
    samples: Sequence[SignalSample],
    start_ms: int,
    end_ms: int,
    chat_in_window: int = 0,
) -> dict[StreamSignal, float]:
    """Combined signal strengths over a time window.

    Chat evidence is scored by **share of the conversation**, not by absolute
    count. Saturating over raw hits does not work at chat scale: a 500-message
    burst contains a few of everything, so every signal pins at 1.0 and the
    engine can no longer tell a rage moment from a donation. Measuring the
    fraction of chat carrying each signal restores that discrimination and is
    also viewer-count independent, which is the same reason the spike detector
    works on ratios rather than rates.

    Event and transcript evidence is sparse and authoritative, so it keeps the
    saturating combination — one $250 donation is a donation.
    """
    chat_hits: dict[StreamSignal, list[float]] = {}
    other_hits: dict[StreamSignal, list[float]] = {}

    for sample in samples:
        if not (start_ms <= sample.offset_ms < end_ms):
            continue
        bucket = chat_hits if sample.origin == "chat" else other_hits
        bucket.setdefault(sample.signal, []).append(sample.strength)

    combined: dict[StreamSignal, float] = {}

    denominator = max(chat_in_window, sum(len(v) for v in chat_hits.values()), 1)
    for signal, values in chat_hits.items():
        coverage = len(values) / denominator
        intensity = sum(values) / len(values)
        combined[signal] = clamp(
            intensity * min(1.0, coverage / SATURATION_COVERAGE)
        )

    for signal, values in other_hits.items():
        combined[signal] = clamp(
            saturating_sum([combined.get(signal, 0.0), saturating_sum(values)])
        )

    return combined
