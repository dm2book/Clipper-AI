"""Shared test helpers.

Inserts `src/` on the path so the suite runs with no install step
(`python -m unittest discover tests`), which keeps CI and a fresh clone
identical.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from clipforge.viral.types import Transcript, Utterance  # noqa: E402

DEMO_TRANSCRIPT = ROOT / "demo" / "sample_transcript.json"


def transcript(*lines: tuple[str, str], seconds_each: float = 5.0) -> Transcript:
    """Build a transcript from (speaker, text) pairs with uniform timing."""
    step = int(seconds_each * 1000)
    utterances = tuple(
        Utterance(
            index=i,
            start_ms=i * step,
            end_ms=(i + 1) * step - 100,
            speaker=speaker,
            text=text,
        )
        for i, (speaker, text) in enumerate(lines)
    )
    return Transcript(source_id="test", utterances=utterances)


def solo(text: str, seconds: float = 5.0) -> Transcript:
    """A one-utterance transcript — the minimal case for a lexical detector."""
    return transcript(("A", text), seconds_each=seconds)
