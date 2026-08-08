"""Structural features — form rather than content.

The signal detectors answer "is anything interesting happening here?".
These answer "is this window shaped like a clip?" — which is a different
question, and the one that decides whether a genuinely good moment actually
performs once it is cut.

All features return 0..1 and are combined multiplicatively in `scoring.py`.
"""

from __future__ import annotations

import re

from .taxonomy import duration_fit
from .types import Candidate, Signal, Transcript, clamp, saturating_sum

# --- Hook: does the opening earn the next two seconds? -----------------------

_HOOK_PATTERNS: tuple[tuple[re.Pattern[str], float], ...] = tuple(
    (re.compile(p, re.IGNORECASE), w)
    for p, w in [
        (r"^\s*(?:why|how|what|when|who|where)\b", 0.70),   # question opener
        (r"\?", 0.45),                                       # question anywhere in the hook
        (r"\b\d[\d,.]*\s?(?:%|percent|k\b|million|billion)", 0.60),  # a number
        (r"\$\s?\d", 0.65),                                  # money up front
        (r"^\s*(?:the|my) (?:biggest|worst|best|hardest|craziest)\b", 0.75),
        (r"\bnobody\b|\beveryone\b|\bnever\b|\balways\b", 0.45),  # absolutes
        (r"^\s*I (?:got|lost|quit|fired|failed|almost)\b", 0.70),
        (r"^\s*(?:here'?s|this is) (?:the|why|how|what)\b", 0.60),
        (r"\bstop\b|\bdon'?t\b", 0.40),                      # imperative
    ]
)

# Openings that promise nothing. A clip starting on one of these has spent its
# most valuable second on throat-clearing.
_WEAK_OPENERS = re.compile(
    r"^\s*(?:so|and|but|well|yeah|right|okay|ok|um|uh|like|i mean|you know)\b[,\s]",
    re.IGNORECASE,
)


def hook_strength(candidate: Candidate, transcript: Transcript) -> float:
    """How hard the first moments grab. Scored on the opening ~12 words.

    Short-form viewers decide in under two seconds, so this reads only the
    opening, not the whole clip — a great line 20 seconds in cannot rescue a
    weak first sentence, and scoring the full text would hide that.
    """
    opening = " ".join(candidate.text.split()[:12])
    if not opening:
        return 0.0

    weights = [w for pattern, w in _HOOK_PATTERNS if pattern.search(opening)]
    score = saturating_sum(weights)

    if _WEAK_OPENERS.match(opening):
        score *= 0.55

    return clamp(score)


# --- Standalone: can a stranger follow this with no setup? -------------------

# Referring expressions with no antecedent inside the clip. "That's why it
# worked" is meaningless to someone who did not hear the preceding ten minutes.
_DANGLING_REFERENCE = re.compile(
    r"^\s*(?:that|this|it|they|he|she|those|these|such)\b"
    r"|^\s*(?:so|and|but|because|which|also)\b"
    r"|\bas (?:i|we) (?:said|mentioned|discussed)\b"
    r"|\blike i (?:said|mentioned)\b"
    r"|\bgoing back to\b"
    r"|\bthe (?:point|thing) i (?:was|were) making\b",
    re.IGNORECASE,
)

_PRONOUN = re.compile(r"\b(?:he|she|they|it|them|his|her|their|its)\b", re.IGNORECASE)
_PROPER_NOUN = re.compile(r"\b[A-Z][a-z]{2,}\b")


def standalone(candidate: Candidate, transcript: Transcript) -> float:
    """Comprehensibility without the surrounding source.

    Two heuristics: does the clip *open* on a dangling reference, and does the
    body lean on pronouns whose referent is never named inside the window.
    Both are proxies for the real question, which only the LLM tier can answer
    properly — which is why `LLM_BLEND` lets a semantic judgement override
    almost all of this.
    """
    text = candidate.text.strip()
    if not text:
        return 0.0

    score = 1.0

    opening = " ".join(text.split()[:8])
    if _DANGLING_REFERENCE.search(opening):
        score *= 0.45

    words = text.split()
    if words:
        pronouns = len(_PRONOUN.findall(text))
        proper_nouns = len(set(_PROPER_NOUN.findall(text)))
        pronoun_rate = pronouns / len(words)
        # Heavy pronoun use is fine when the clip also names people; it is a
        # problem only when nobody is ever identified.
        if pronoun_rate > 0.06 and proper_nouns == 0:
            score *= clamp(1.0 - (pronoun_rate - 0.06) * 6.0, 0.4, 1.0)

    return clamp(score)


# --- Payoff: does the tension resolve inside the window? ---------------------

_PAYOFF_MARKERS = re.compile(
    r"\b(?:so|which is why|and that'?s (?:why|how|when)|turns out|in the end"
    r"|the result|eventually|finally|the lesson|what happened was"
    r"|and (?:then|now)|the point is)\b",
    re.IGNORECASE,
)


def payoff(candidate: Candidate, transcript: Transcript) -> float:
    """Whether the clip lands rather than trailing off.

    Measured in the closing third: a resolution marker, or a signal hit late in
    the window, both indicate the moment completes rather than being cut short.
    """
    utterances = transcript.utterances[
        candidate.first_utterance : candidate.last_utterance + 1
    ]
    if not utterances:
        return 0.0

    cutoff = candidate.start_ms + int(candidate.duration_ms * 0.66)
    tail = [u for u in utterances if u.end_ms > cutoff] or utterances[-1:]
    tail_text = " ".join(u.text for u in tail)

    evidence: list[float] = []
    if _PAYOFF_MARKERS.search(tail_text):
        evidence.append(0.65)

    tail_indices = {u.index for u in tail}
    late_hits = [h.strength for h in candidate.hits if h.utterance_index in tail_indices]
    if late_hits:
        evidence.append(clamp(max(late_hits) * 0.8))

    # A clip that ends on a question is unresolved, which reads as truncation.
    if tail_text.rstrip().endswith("?"):
        evidence.append(0.2)

    return clamp(saturating_sum(evidence))


# --- Audience question: the cheapest comment lever there is ------------------

_AUDIENCE_QUESTION = re.compile(
    r"\bwhat (?:do|would) you (?:think|do)\b"
    r"|\bam i (?:wrong|crazy|the only)\b"
    r"|\btell me i'?m wrong\b"
    r"|\bwho else\b"
    r"|\bchange my mind\b"
    r"|\byour thoughts\b"
    r"|\bagree\?|\bright\?",
    re.IGNORECASE,
)


def audience_question(candidate: Candidate, transcript: Transcript) -> float:
    """Direct address to the viewer, which reliably converts into comments."""
    if _AUDIENCE_QUESTION.search(candidate.text):
        return 1.0
    # A rhetorical question still invites a reply, just less reliably.
    return 0.4 if "?" in candidate.text else 0.0


# --- Speaker dynamics --------------------------------------------------------


def speaker_balance(candidate: Candidate, transcript: Transcript) -> float:
    """1.0 for a clean single voice or a balanced exchange; lower when choppy.

    Rapid alternation is great for arguments and terrible for everything else,
    so this feeds engagement rather than retention.
    """
    utterances = transcript.utterances[
        candidate.first_utterance : candidate.last_utterance + 1
    ]
    if len(utterances) <= 1:
        return 1.0

    changes = sum(
        1 for a, b in zip(utterances, utterances[1:]) if a.speaker != b.speaker
    )
    per_10s = changes / max(candidate.duration_s / 10.0, 0.1)
    if per_10s <= 3.0:
        return 1.0
    return clamp(1.0 - (per_10s - 3.0) * 0.15, 0.3, 1.0)


def extract(candidate: Candidate, transcript: Transcript) -> dict[str, float]:
    """The full structural feature vector for one candidate.

    Persisted verbatim on the moment so the learned ranker can train on exactly
    the inputs the heuristic ranker saw.
    """
    return {
        "hook_strength": hook_strength(candidate, transcript),
        "standalone": standalone(candidate, transcript),
        "payoff": payoff(candidate, transcript),
        "duration_fit": duration_fit(candidate.duration_s),
        "audience_question": audience_question(candidate, transcript),
        "speaker_balance": speaker_balance(candidate, transcript),
        "duration_s": candidate.duration_s,
        "signal_count": float(len({h.signal for h in candidate.hits})),
    }
