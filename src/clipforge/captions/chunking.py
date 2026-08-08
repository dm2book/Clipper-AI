"""Grouping timed words into cues and lines.

Two decisions happen here, and both are language-sensitive:

  cue boundaries   which words appear on screen together
  line breaks      how a cue's words split across its lines

The engine scores every candidate boundary rather than greedily filling to the
width limit. Greedy filling is what produces captions that break after "the",
strand a French `l'`, or leave a Spanish `¿` alone at the end of a line — all
of which are individually small and collectively make output look automated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from . import measure
from .languages import (
    NNBSP,
    LanguageRules,
    glues_to_next,
    is_medial,
    is_terminal,
    orphan_risk,
)
from .styles import CaptionStyle
from .types import CaptionLine, CaptionWord, TimedWord

# A silence longer than this is a natural cue boundary regardless of word count
# — the speaker stopped, so the caption should too.
PAUSE_BREAK_MS = 420

# Break scores. Higher is a better place to end a cue or line.
SCORE_TERMINAL = 100.0   # after . ! ?
SCORE_MEDIAL = 55.0      # after , ; :
SCORE_PAUSE = 45.0       # after a measurable silence
SCORE_NEUTRAL = 10.0
PENALTY_ORPHAN = -60.0   # ending on an article or preposition
PENALTY_GLUE = -1_000.0  # splitting l'ami or ¿qué — never allowed


@dataclass(frozen=True, slots=True)
class Boundary:
    """A candidate break after `index`."""

    index: int
    score: float


# Punctuation that attaches to the word *before* it.
_TRAILING_PUNCTUATION = set(".,;:!?…»)]\"")
# Punctuation that attaches to the word *after* it.
_LEADING_PUNCTUATION = set("¿¡«([")


def merge_punctuation(
    words: Sequence[TimedWord], rules: LanguageRules
) -> list[TimedWord]:
    """Fold standalone punctuation tokens into their neighbouring word.

    This is not hypothetical tidying. Correct French writes a space before
    `? ! ; :`, so a French transcript tokenises `perdu ?` as two words — and
    without this the `?` becomes its own caption cue, flashing on screen alone
    for a fifth of a second. Spanish `¿` and `¡` have the mirror problem at the
    start of a clause.

    The merged text keeps the language's spacing convention: a narrow
    no-break space before French punctuation, nothing at all elsewhere.
    """
    if not words:
        return []

    out: list[TimedWord] = []
    pending_lead: list[TimedWord] = []

    for word in words:
        stripped = word.text.strip()
        is_trailing = bool(stripped) and all(c in _TRAILING_PUNCTUATION for c in stripped)
        is_leading = bool(stripped) and all(c in _LEADING_PUNCTUATION for c in stripped)

        if is_leading:
            pending_lead.append(word)
            continue

        if is_trailing and out:
            previous = out[-1]
            separator = (
                NNBSP if stripped[0] in rules.space_before_punctuation else ""
            )
            out[-1] = TimedWord(
                text=previous.text + separator + stripped,
                start_ms=previous.start_ms,
                end_ms=max(previous.end_ms, word.end_ms),
                speaker=previous.speaker,
            )
            continue

        if pending_lead:
            prefix = "".join(w.text.strip() for w in pending_lead)
            word = TimedWord(
                text=prefix + word.text,
                start_ms=pending_lead[0].start_ms,
                end_ms=word.end_ms,
                speaker=word.speaker,
            )
            pending_lead = []

        out.append(word)

    # A trailing opening mark with nothing after it is malformed input; keep
    # the token rather than silently dropping transcript content.
    out.extend(pending_lead)
    return out


def break_score(
    words: Sequence[TimedWord], index: int, rules: LanguageRules
) -> float:
    """How good a break after `words[index]` would be."""
    word = words[index]

    if glues_to_next(word.text, rules) and index + 1 < len(words):
        return PENALTY_GLUE

    score = SCORE_NEUTRAL
    if is_terminal(word.text):
        score = SCORE_TERMINAL
    elif is_medial(word.text):
        score = SCORE_MEDIAL
    elif index + 1 < len(words):
        gap = words[index + 1].start_ms - word.end_ms
        if gap >= PAUSE_BREAK_MS:
            score = SCORE_PAUSE

    if orphan_risk(word.text, rules):
        score += PENALTY_ORPHAN

    # A speaker change is always a cue boundary; never merge two voices into
    # one caption, which would attribute words to the wrong person.
    if index + 1 < len(words) and words[index + 1].speaker != word.speaker:
        score += SCORE_TERMINAL

    return score


def split_into_cues(
    words: Sequence[TimedWord],
    rules: LanguageRules,
    style: CaptionStyle,
) -> list[list[TimedWord]]:
    """Group words into cue-sized runs.

    Fills up to the style's word limit, then backtracks to the best-scoring
    boundary inside the run. A hard boundary (speaker change, long pause,
    sentence end) closes the cue early.
    """
    words = merge_punctuation(words, rules)
    if not words:
        return []

    cues: list[list[TimedWord]] = []
    current: list[TimedWord] = []
    start_index = 0

    for i, word in enumerate(words):
        current.append(word)
        absolute = start_index + len(current) - 1

        at_end = i == len(words) - 1
        speaker_changes = (
            not at_end and words[i + 1].speaker != word.speaker
        )
        long_pause = (
            not at_end and words[i + 1].start_ms - word.end_ms >= PAUSE_BREAK_MS * 2
        )
        too_long = (
            current[-1].end_ms - current[0].start_ms >= style.max_cue_ms
        )
        full = len(current) >= style.max_words

        if at_end or speaker_changes or long_pause:
            cues.append(current)
            current = []
            start_index = i + 1
            continue

        if not (full or too_long):
            continue

        # The run is full. Look back for a better boundary than "right here",
        # but only a couple of words — backtracking further starves the next
        # cue and produces a stuttering rhythm.
        best = Boundary(absolute, break_score(words, absolute, rules))
        lookback = min(2, len(current) - 1)
        for offset in range(1, lookback + 1):
            candidate = absolute - offset
            score = break_score(words, candidate, rules)
            # Require a clear improvement; a marginally better break is not
            # worth making the cue shorter.
            if score > best.score + 20.0:
                best = Boundary(candidate, score)

        cut = best.index - start_index + 1
        cues.append(current[:cut])
        current = current[cut:]
        start_index = best.index + 1

    if current:
        cues.append(current)

    return [cue for cue in cues if cue]


def layout_lines(
    words: Sequence[CaptionWord],
    rules: LanguageRules,
    style: CaptionStyle,
    max_em: float,
) -> list[CaptionLine]:
    """Break a cue's words across lines that fit the available width.

    Same scoring as cue splitting, applied to width rather than word count.
    Overflow past the style's line limit is folded back into the last line and
    handled by shrinking the cue — dropping words is never acceptable.
    """
    if not words:
        return []

    lines: list[CaptionLine] = []
    current: list[CaptionWord] = []
    space = measure.char_width(" ")

    def width_of(items: list[CaptionWord]) -> float:
        return sum(w.width_em for w in items) + space * max(0, len(items) - 1)

    for word in words:
        tentative = current + [word]
        if not current or width_of(tentative) <= max_em:
            current = tentative
            continue

        # Overflow. Decide which trailing words have to travel to the next
        # line with the one that caused it.
        moved: list[CaptionWord] = []

        # Glued tokens can never be separated from what follows them: a French
        # `l'` or a Dutch `'s` alone at the end of a line is not a word. Walk
        # back through as many as are chained together.
        while current and glues_to_next(current[-1].text, rules):
            moved.insert(0, current.pop())

        # An article or preposition left dangling at a line end reads as a
        # mistake even though it is legal, so move it too.
        if len(current) > 1 and orphan_risk(current[-1].text, rules):
            moved.insert(0, current.pop())

        if not current:
            # Everything in the line was glued to the overflowing word, so
            # there is no legal break. Keep them together and let the caller
            # shrink the cue — breaking here would corrupt the text.
            current = moved + [word]
            continue

        lines.append(CaptionLine(words=current))
        current = moved + [word]

    if current:
        lines.append(CaptionLine(words=current))

    if len(lines) > style.max_lines:
        # Too many lines for the style. Merge the tail into the last allowed
        # line; the caller shrinks the font so it still fits.
        head = lines[: style.max_lines - 1]
        tail_words = [w for line in lines[style.max_lines - 1 :] for w in line.words]
        head.append(CaptionLine(words=tail_words))
        lines = head

    return lines


def balance_lines(lines: list[CaptionLine]) -> list[CaptionLine]:
    """Even out a two-line cue so the first line is not far longer.

    A caption reading "THIS IS A REALLY LONG FIRST LINE / AND" looks broken.
    Only moves a word when it genuinely improves the balance and the boundary
    is legal, so this can never override the language rules above.
    """
    if len(lines) != 2:
        return lines

    first, second = lines
    if len(first.words) < 2:
        return lines

    current_delta = abs(first.width_em - second.width_em)
    candidate_first = first.words[:-1]
    candidate_second = [first.words[-1]] + second.words

    space = measure.char_width(" ")

    def width(items: list[CaptionWord]) -> float:
        return sum(w.width_em for w in items) + space * max(0, len(items) - 1)

    new_delta = abs(width(candidate_first) - width(candidate_second))
    if new_delta < current_delta:
        return [CaptionLine(candidate_first), CaptionLine(candidate_second)]
    return lines
