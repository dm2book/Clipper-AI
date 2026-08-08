"""Candidate window generation.

Windows are always aligned to utterance boundaries, so a clip can never start
or end mid-sentence. Generation is anchored on detector hits rather than
sweeping the whole timeline uniformly: a two-hour source has thousands of
possible windows but only a few dozen interesting regions, and scoring every
window would waste the budget the LLM tier needs.

We deliberately over-generate here. Non-maximum suppression in `ranking.py`
collapses the overlap afterwards, and it is far cheaper to discard a redundant
candidate than to miss a moment that was never proposed.
"""

from __future__ import annotations

from typing import Sequence

from .taxonomy import IDEAL_DURATION_S, MAX_DURATION_S, MIN_DURATION_S
from .types import Candidate, SignalHit, Transcript

# Target lengths to try around each anchor, in seconds. Weighted toward the
# performance sweet spot but wide enough to catch a long story that needs room.
TARGET_DURATIONS_S: tuple[float, ...] = (15.0, 22.0, 30.0, 45.0, 60.0)

# How many utterances before the anchor to consider as an opening. A moment's
# setup usually lives one or two turns before the line that triggered the hit.
LOOKBACK = 3


def _window_from(
    transcript: Transcript, first: int, target_s: float
) -> Candidate | None:
    """Grow a window forward from `first` until it reaches `target_s`.

    Returns None when the resulting window falls outside the absolute duration
    bounds — better no candidate than an unusable one.
    """
    utterances = transcript.utterances
    if first >= len(utterances):
        return None

    start_ms = utterances[first].start_ms
    last = first
    for i in range(first, len(utterances)):
        last = i
        if (utterances[i].end_ms - start_ms) / 1000.0 >= target_s:
            break

    end_ms = utterances[last].end_ms
    duration_s = (end_ms - start_ms) / 1000.0
    if duration_s < MIN_DURATION_S or duration_s > MAX_DURATION_S:
        return None

    return Candidate(
        first_utterance=first,
        last_utterance=last,
        start_ms=start_ms,
        end_ms=end_ms,
        text=transcript.text_between(first, last),
    )


def _anchors(hits: Sequence[SignalHit], transcript: Transcript) -> list[int]:
    """Utterance indices worth building windows around, strongest first."""
    weight: dict[int, float] = {}
    for hit in hits:
        weight[hit.utterance_index] = weight.get(hit.utterance_index, 0.0) + hit.strength
    ordered = sorted(weight.items(), key=lambda kv: (-kv[1], kv[0]))
    return [idx for idx, _ in ordered]


def generate(
    transcript: Transcript,
    hits: Sequence[SignalHit],
    max_candidates: int = 240,
) -> list[Candidate]:
    """Produce candidate windows for a transcript.

    Each anchor gets windows at several target durations and several starting
    offsets, because the right opening line is frequently a turn or two before
    the line that triggered the detector.
    """
    utterances = transcript.utterances
    if not utterances:
        return []

    hits_by_index: dict[int, list[SignalHit]] = {}
    for hit in hits:
        hits_by_index.setdefault(hit.utterance_index, []).append(hit)

    seen: set[tuple[int, int]] = set()
    candidates: list[Candidate] = []

    for anchor in _anchors(hits, transcript):
        for back in range(LOOKBACK + 1):
            first = anchor - back
            if first < 0:
                continue
            for target in TARGET_DURATIONS_S:
                window = _window_from(transcript, first, target)
                if window is None or window.span in seen:
                    continue
                seen.add(window.span)
                # Attach every hit that falls inside the window, not just the
                # anchor's — a window covering three signals should be scored
                # on all three.
                inside = tuple(
                    hit
                    for idx in range(window.first_utterance, window.last_utterance + 1)
                    for hit in hits_by_index.get(idx, ())
                )
                candidates.append(
                    Candidate(
                        first_utterance=window.first_utterance,
                        last_utterance=window.last_utterance,
                        start_ms=window.start_ms,
                        end_ms=window.end_ms,
                        text=window.text,
                        hits=inside,
                    )
                )
                if len(candidates) >= max_candidates:
                    return candidates

    return candidates


def fallback_windows(transcript: Transcript, stride_s: float = 20.0) -> list[Candidate]:
    """Uniform windows for transcripts where no detector fired.

    A source can be genuinely interesting and still trip zero keyword patterns
    — a calm technical explainer, or any language the pattern banks do not
    cover. Returning nothing there would be a silent failure, so we hand the
    LLM tier an evenly-spaced sample of the source instead.
    """
    utterances = transcript.utterances
    if not utterances:
        return []

    target = sum(IDEAL_DURATION_S) / 2.0
    candidates: list[Candidate] = []
    seen: set[tuple[int, int]] = set()
    cursor_ms = utterances[0].start_ms

    while cursor_ms < transcript.duration_ms:
        first = next(
            (u.index for u in utterances if u.start_ms >= cursor_ms),
            None,
        )
        if first is None:
            break
        window = _window_from(transcript, first, target)
        if window is not None and window.span not in seen:
            seen.add(window.span)
            candidates.append(window)
        cursor_ms += int(stride_s * 1000)

    return candidates
