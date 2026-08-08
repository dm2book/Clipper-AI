"""The caption generation engine — orchestration.

    timed words
      → detect / accept language
      → split into cues (language-aware boundaries)
      → apply case, typography, emoji
      → measure and lay out lines, shrinking where a compound overflows
      → assign speaker colours
      → generate animation keyframes
      → CaptionTrack

Word-level input is required. Given sentence-level subtitles there is nothing
to karaoke, nowhere precise to place an emoji, and no way to know which word is
being spoken — so the engine raises rather than faking the timing, which would
produce highlighting that drifts visibly out of sync.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Sequence

from . import animation as animation_mod
from . import chunking, emoji as emoji_mod, measure, styles
from .languages import detect, rules_for
from .styles import CaptionStyle
from .types import (
    Box,
    DEFAULT_BOX,
    CaptionCue,
    CaptionTrack,
    CaptionWord,
    Language,
    TimedWord,
    coerce_words,
)


@dataclass(slots=True)
class CaptionConfig:
    """Tuning surface for one caption run."""

    style: CaptionStyle = styles.DEFAULT_STYLE
    language: Language | None = None   # None = auto-detect
    box: Box = DEFAULT_BOX

    # Emoji are capped at one per cue and spaced out; back-to-back emoji is
    # the clearest tell that captions were machine-generated.
    emoji_min_gap_cues: int = 2

    # Below this the shrink-to-fit gives up and the cue is flagged rather than
    # rendered illegibly small.
    min_font_scale: float = 0.62

    # Pad cue timings so a caption is not on screen for exactly the duration of
    # its speech, which reads as clipped.
    lead_in_ms: int = 60
    lead_out_ms: int = 140


class CaptionEngine:
    """Turns word-level ASR output into a styled, animated caption track."""

    def __init__(self, config: CaptionConfig | None = None) -> None:
        self.config = config or CaptionConfig()

    def generate(self, words: Sequence[Any]) -> CaptionTrack:
        started = time.perf_counter()
        cfg = self.config
        style = cfg.style

        timed = coerce_words(words)
        if not timed:
            return CaptionTrack(
                language=cfg.language or Language.ENGLISH,
                style_name=style.name,
                box=cfg.box,
                cues=[],
                stats={"reason": "no words supplied"},
            )

        self._validate(timed)

        language = cfg.language or detect([w.text for w in timed])
        rules = rules_for(language)

        speakers: list[str] = []
        for word in timed:
            if word.speaker not in speakers:
                speakers.append(word.speaker)
        speaker_colors = styles.assign_speaker_colors(speakers, style)

        max_em = measure.max_em_for(cfg.box.width, style.font_size_px)

        groups = chunking.split_into_cues(timed, rules, style)
        cues: list[CaptionCue] = []
        emoji_added = 0
        last_emoji_cue = -99
        shrunk_count = 0

        for index, group in enumerate(groups, start=1):
            caption_words = [
                CaptionWord(
                    text=styles.apply_case(w.text, style.case, language),
                    start_ms=w.start_ms,
                    end_ms=w.end_ms,
                    speaker=w.speaker,
                    color=speaker_colors.get(w.speaker),
                )
                for w in group
            ]

            if (
                style.emoji_enabled
                and index - last_emoji_cue > cfg.emoji_min_gap_cues
            ):
                if self._add_emoji(caption_words, language, style):
                    emoji_added += 1
                    last_emoji_cue = index

            for word in caption_words:
                word.width_em = measure.text_width(word.text)

            lines = chunking.layout_lines(caption_words, rules, style, max_em)
            lines = chunking.balance_lines(lines)

            font_scale, shrunk = self._fit(lines, max_em, rules.heavy_compounding, cfg)
            if shrunk:
                shrunk_count += 1

            start_ms = max(0, group[0].start_ms - cfg.lead_in_ms)
            end_ms = group[-1].end_ms + cfg.lead_out_ms
            if end_ms - start_ms < style.min_cue_ms:
                end_ms = start_ms + style.min_cue_ms

            cue = CaptionCue(
                index=index,
                start_ms=start_ms,
                end_ms=end_ms,
                lines=lines,
                speaker=group[0].speaker,
                font_scale=font_scale,
                shrunk=shrunk,
            )
            animation_mod.animate_cue(cue.words, cue.start_ms, cue.end_ms, style)
            cues.append(cue)

        self._resolve_overlaps(cues)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return CaptionTrack(
            language=language,
            style_name=style.name,
            box=cfg.box,
            cues=cues,
            speaker_colors=speaker_colors,
            stats={
                "words": len(timed),
                "cues": len(cues),
                "speakers": len(speakers),
                "emoji_added": emoji_added,
                "cues_shrunk": shrunk_count,
                "language_detected": cfg.language is None,
                "max_em": round(max_em, 3),
                "elapsed_ms": elapsed_ms,
            },
        )

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _validate(words: list[TimedWord]) -> None:
        """Reject input that cannot produce word-level captions."""
        zero_length = sum(1 for w in words if w.duration_ms <= 0)
        if zero_length > len(words) * 0.5:
            raise ValueError(
                "more than half the words have zero duration — this looks like "
                "sentence-level subtitles rather than word-level ASR output. "
                "Word timing is required for karaoke highlighting."
            )

    def _add_emoji(
        self, words: list[CaptionWord], language: Language, style: CaptionStyle
    ) -> bool:
        """Insert at most one emoji into a cue. Returns whether it did."""
        if any(w.is_emoji for w in words):
            return False
        if any(emoji_mod.contains_emoji(w.text) for w in words):
            return False

        suggestion = emoji_mod.suggest(
            [w.text for w in words], language, threshold=style.emoji_threshold
        )
        if suggestion is None:
            return False

        token_index, glyph, _ = suggestion
        anchor = words[token_index]
        words.insert(
            token_index + 1,
            CaptionWord(
                text=glyph,
                # Ride the anchor word's timing so the emoji lands with the
                # word that earned it rather than drifting to the cue end.
                start_ms=anchor.start_ms,
                end_ms=anchor.end_ms,
                speaker=anchor.speaker,
                is_emoji=True,
            ),
        )
        return True

    def _fit(
        self,
        lines: list,
        max_em: float,
        heavy_compounding: bool,
        cfg: CaptionConfig,
    ) -> tuple[float, bool]:
        """Font scale for a cue, shrinking when a line overflows.

        Compounding languages get a lower floor: a German cue containing one
        long compound is normal, and refusing to shrink it far enough would
        clip the word rather than merely making it small.
        """
        if not lines:
            return 1.0, False

        widest = max(line.width_em for line in lines)
        if widest <= max_em:
            return 1.0, False

        floor = cfg.min_font_scale * (0.9 if heavy_compounding else 1.0)
        scale = max(floor, max_em / widest)
        return round(scale, 4), True

    @staticmethod
    def _resolve_overlaps(cues: list[CaptionCue]) -> None:
        """Stop a cue's lead-out from colliding with the next cue's lead-in.

        Two captions on screen at once is the most visible failure this engine
        can produce, and padding makes it easy to cause.
        """
        for current, following in zip(cues, cues[1:]):
            if current.end_ms > following.start_ms:
                current.end_ms = max(current.start_ms + 1, following.start_ms - 20)


def generate(
    words: Sequence[Any],
    style: str | CaptionStyle = "punch",
    language: Language | str | None = None,
    box: Box = DEFAULT_BOX,
) -> CaptionTrack:
    """Convenience wrapper for the common case."""
    resolved_style = styles.get(style) if isinstance(style, str) else style
    resolved_language = Language(language) if isinstance(language, str) else language
    return CaptionEngine(
        CaptionConfig(style=resolved_style, language=resolved_language, box=box)
    ).generate(words)
