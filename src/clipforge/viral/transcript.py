"""Transcript loading and normalisation.

The engine accepts three input shapes and normalises all of them to
`Transcript`. Word-level timings are preserved when supplied — caption timing
and precise cut points depend on them downstream — but the detector stack only
needs utterances, so a segment-level transcript is fully supported.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

from .types import Transcript, Utterance, Word

# Sentence boundaries: terminal punctuation followed by whitespace. Kept
# deliberately simple — over-clever splitting mangles "$1.5M" and "e.g.".
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")

_MAX_UTTERANCE_MS = 20_000


def from_utterances(
    source_id: str,
    rows: Iterable[dict[str, Any]],
    language: str = "en",
) -> Transcript:
    """Build a transcript from utterance dicts.

    Each row needs `start_ms`, `end_ms`, `text`, and optionally `speaker` and
    `words`. Rows are sorted by start time and re-indexed, so callers do not
    need to pre-sort.
    """
    parsed: list[Utterance] = []
    ordered = sorted(rows, key=lambda r: int(r["start_ms"]))
    for i, row in enumerate(ordered):
        words = tuple(
            Word(text=w["text"], start_ms=int(w["start_ms"]), end_ms=int(w["end_ms"]))
            for w in row.get("words", ())
        )
        parsed.append(
            Utterance(
                index=i,
                start_ms=int(row["start_ms"]),
                end_ms=int(row["end_ms"]),
                speaker=str(row.get("speaker", "SPEAKER_00")),
                text=" ".join(str(row["text"]).split()),
                words=words,
            )
        )
    return Transcript(source_id=source_id, utterances=tuple(parsed), language=language)


def from_words(
    source_id: str,
    words: Sequence[dict[str, Any]],
    language: str = "en",
    max_gap_ms: int = 700,
) -> Transcript:
    """Build a transcript from a flat word stream (Whisper `word` granularity).

    Utterances break on speaker change, on a silence gap wider than
    `max_gap_ms`, on sentence-terminal punctuation, and on a hard duration cap
    so a monologue without punctuation cannot become one giant atom.
    """
    if not words:
        return Transcript(source_id=source_id, utterances=(), language=language)

    rows: list[dict[str, Any]] = []
    bucket: list[dict[str, Any]] = []

    def flush() -> None:
        if not bucket:
            return
        rows.append(
            {
                "start_ms": bucket[0]["start_ms"],
                "end_ms": bucket[-1]["end_ms"],
                "speaker": bucket[0].get("speaker", "SPEAKER_00"),
                "text": " ".join(w["text"] for w in bucket),
                "words": list(bucket),
            }
        )
        bucket.clear()

    for word in words:
        if bucket:
            prev = bucket[-1]
            speaker_changed = word.get("speaker") != prev.get("speaker")
            gap = int(word["start_ms"]) - int(prev["end_ms"])
            too_long = int(word["end_ms"]) - int(bucket[0]["start_ms"]) > _MAX_UTTERANCE_MS
            sentence_ended = prev["text"].rstrip().endswith((".", "!", "?"))
            if speaker_changed or gap > max_gap_ms or too_long or sentence_ended:
                flush()
        bucket.append(dict(word))
    flush()

    return from_utterances(source_id, rows, language=language)


def load_json(path: str | Path, source_id: str | None = None) -> Transcript:
    """Load a transcript from a JSON file.

    Accepts either `{"source_id": ..., "utterances": [...]}` or
    `{"source_id": ..., "words": [...]}`; a bare list is treated as utterances.
    """
    p = Path(path)
    data = json.loads(p.read_text())
    if isinstance(data, list):
        return from_utterances(source_id or p.stem, data)

    sid = source_id or data.get("source_id") or p.stem
    language = data.get("language", "en")
    if "utterances" in data:
        return from_utterances(sid, data["utterances"], language=language)
    if "words" in data:
        return from_words(sid, data["words"], language=language)
    raise ValueError(
        f"{p} has neither 'utterances' nor 'words'; cannot build a transcript"
    )


def split_sentences(text: str) -> list[str]:
    """Split a block of text into sentences. Used when an utterance is long."""
    parts = [s.strip() for s in _SENTENCE_END.split(text)]
    return [s for s in parts if s]
