"""The ten signal detectors — tier one of the cascade.

These are cheap, deterministic, and run over every utterance in the source.
Their job is *recall*: locate everything that might be a moment, so the
expensive LLM tier only ever sees a few hundred candidates instead of a
few thousand windows.

They are lexical and structural, so they know nothing about meaning. A
detector firing means "this region is worth looking at", not "this is a good
clip" — precision comes from `scoring.py` and the LLM tier. Deliberately
tuned to over-fire rather than miss.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterator, Pattern, Sequence

from .types import Signal, SignalHit, Transcript, Utterance, clamp, saturating_sum

# A weighted pattern: matching contributes `weight` toward the signal's strength.
WeightedPattern = tuple[Pattern[str], float]


def _compile(pairs: Sequence[tuple[str, float]]) -> tuple[WeightedPattern, ...]:
    return tuple((re.compile(p, re.IGNORECASE), w) for p, w in pairs)


@dataclass(frozen=True, slots=True)
class DetectorContext:
    """Everything a detector may look at beyond the utterance itself."""

    transcript: Transcript
    index: int

    @property
    def utterance(self) -> Utterance:
        return self.transcript.utterances[self.index]

    @property
    def previous(self) -> Utterance | None:
        return self.transcript.utterances[self.index - 1] if self.index > 0 else None

    @property
    def following(self) -> Utterance | None:
        nxt = self.index + 1
        us = self.transcript.utterances
        return us[nxt] if nxt < len(us) else None

    def speaker_changed(self) -> bool:
        prev = self.previous
        return prev is not None and prev.speaker != self.utterance.speaker

    def turn_density(self, window: int = 4) -> float:
        """Fraction of nearby utterance boundaries that are speaker changes.

        High density means a rapid back-and-forth — the structural fingerprint
        of an argument, and what separates one from a monologue that happens
        to contain angry words.
        """
        us = self.transcript.utterances
        lo = max(0, self.index - window)
        hi = min(len(us) - 1, self.index + window)
        if hi <= lo:
            return 0.0
        changes = sum(
            1 for i in range(lo + 1, hi + 1) if us[i].speaker != us[i - 1].speaker
        )
        return changes / (hi - lo)


def _match_strength(
    text: str, patterns: tuple[WeightedPattern, ...]
) -> tuple[float, list[str]]:
    """Saturating combination of every matching pattern, plus the evidence."""
    weights: list[float] = []
    evidence: list[str] = []
    for pattern, weight in patterns:
        found = pattern.search(text)
        if found:
            weights.append(weight)
            evidence.append(found.group(0).strip())
    return saturating_sum(weights), evidence


# --- Pattern banks -----------------------------------------------------------

CONTROVERSY_PATTERNS = _compile([
    (r"\bunpopular opinion\b", 0.85),
    (r"\bcontroversial\b", 0.70),
    (r"\b(?:people|everyone|they)(?:'re| are) (?:not going to|gonna hate|won't like)\b", 0.80),
    (r"\bhot take\b", 0.75),
    (r"\bI(?:'m| am) going to get (?:cancelled|hate|destroyed)\b", 0.85),
    (r"\bnobody wants to (?:hear|admit|say)\b", 0.70),
    (r"\bthe truth is (?:nobody|no one|most people)\b", 0.60),
    (r"\b(?:everyone|everybody) (?:is wrong|gets this wrong|believes)\b", 0.65),
    (r"\bthat(?:'s| is) (?:completely |totally |just )?(?:wrong|false|nonsense|rubbish)\b", 0.55),
    (r"\bI (?:completely |strongly )?disagree\b", 0.55),
    (r"\boverrated\b|\boverhyped\b", 0.50),
    (r"\bscam\b|\bfraud\b|\blying to you\b", 0.60),
])

EMOTION_PATTERNS = _compile([
    (r"\b(?:oh my god|oh my goodness|holy|what the)\b", 0.70),
    (r"\bI (?:was |am )?(?:so |absolutely |completely )?(?:devastated|terrified|furious|heartbroken|stunned|shocked)\b", 0.85),
    (r"\bI (?:started |just )?(?:crying|cried|broke down)\b", 0.85),
    (r"\b(?:incredible|unbelievable|insane|crazy|wild|mind-?blowing)\b", 0.55),
    (r"\b(?:never|worst|best|hardest|scariest) (?:thing|day|moment|time) (?:of|in) my life\b", 0.80),
    (r"\bI(?:'ll| will) never forget\b", 0.70),
    (r"!{2,}", 0.45),
    (r"\b(?:absolutely|literally|genuinely|honestly) \w+", 0.30),
    (r"\b(?:terrifying|humiliating|devastating|exhilarating)\b", 0.70),
])

MONEY_PATTERNS = _compile([
    (r"\$\s?\d[\d,.]*\s?(?:k|m|b|million|billion|thousand)?\b", 0.80),
    (r"\b\d[\d,.]*\s?(?:dollars|euros|pounds|grand)\b", 0.75),
    (r"\b(?:revenue|profit|margin|valuation|funding|salary|paycheck|payroll)\b", 0.55),
    (r"\b(?:raised|made|lost|burned|spent) (?:over |about |roughly )?\$?\d", 0.80),
    (r"\b(?:bankrupt|broke|debt|mortgage|investors?|equity|IPO|acquisition)\b", 0.50),
    (r"\b\d+\s?(?:x|times) (?:return|revenue|growth)\b", 0.65),
    (r"\bpaid (?:me|him|her|them|us) \$?\d", 0.70),
    (r"\b(?:price|cost|charge|fee) (?:is|was|of) \$?\d", 0.55),
])

FUNNY_PATTERNS = _compile([
    (r"\[laughs?\]|\[laughter\]|\[chuckles?\]", 0.90),
    (r"\bhaha+\b|\blmao\b|\blol\b", 0.70),
    (r"\bI(?:'m| am) (?:dead|dying|crying)\b", 0.60),
    (r"\bthat(?:'s| is) (?:so |absolutely |genuinely )?(?:hilarious|funny|ridiculous)\b", 0.70),
    (r"\bcracking up\b|\bcouldn't stop laughing\b", 0.75),
    (r"\bjoke\b|\bjoking\b|\bkidding\b", 0.40),
    (r"\bworst part (?:is|was)\b", 0.35),
])

ARGUMENT_PATTERNS = _compile([
    # The intensifier slot matters: "no, that's just wrong" is at least as
    # common as the bare form, and omitting it silently loses the match.
    (r"\bno,? (?:that|you|it)(?:'s| is| are)? (?:just |completely |totally |simply |flat(?:-| )out )?(?:not|wrong|nonsense)\b", 0.85),
    (r"\b(?:let me|can I) finish\b", 0.90),
    (r"\bhold on\b|\bwait,? no\b|\bexcuse me\b", 0.60),
    (r"\byou(?:'re| are) (?:not listening|missing the point|twisting)\b", 0.85),
    (r"\bthat(?:'s| is) not what I said\b", 0.85),
    (r"\bwith (?:all due )?respect\b", 0.55),
    (r"\byou just (?:said|contradicted)\b", 0.70),
    (r"\bare you (?:seriously|actually) (?:saying|suggesting)\b", 0.75),
    (r"\b(?:absolutely|completely) not\b", 0.55),
])

DEBATE_PATTERNS = _compile([
    (r"\bon the other hand\b", 0.65),
    (r"\bthe counter(?:argument|point)\b", 0.80),
    (r"\bI(?:'d| would) push back on (?:that|this)\b", 0.80),
    (r"\bdevil(?:'s)? advocate\b", 0.75),
    (r"\bthe (?:steelman|strongest) (?:case|argument)\b", 0.80),
    (r"\bwhere I (?:disagree|differ)\b", 0.70),
    (r"\bfair(?: enough)?,? but\b", 0.60),
    (r"\bI (?:take|see) your point,? (?:but|however)\b", 0.70),
    (r"\bboth (?:things|can be) true\b", 0.55),
    (r"\bthe evidence (?:says|suggests|shows)\b", 0.45),
])

FAILURE_PATTERNS = _compile([
    (r"\bbiggest mistake\b", 0.90),
    (r"\bI (?:completely |totally )?(?:failed|screwed up|messed up|blew it)\b", 0.85),
    (r"\bwe (?:went|almost went) (?:bankrupt|under|bust)\b", 0.90),
    (r"\bI lost (?:everything|it all|my|the)\b", 0.85),
    (r"\b(?:got|was) fired\b|\blaid off\b", 0.75),
    (r"\bit (?:completely |totally )?(?:fell apart|collapsed|imploded)\b", 0.80),
    (r"\bworst decision\b", 0.85),
    (r"\bshould(?:'ve| have) never\b", 0.65),
    (r"\bwasted (?:\d+ )?(?:years?|months?)\b", 0.70),
    (r"\bdidn(?:'t| not) work\b", 0.40),
])

SUCCESS_PATTERNS = _compile([
    (r"\bwe (?:hit|reached|crossed) (?:\$|\d)", 0.85),
    (r"\bscaled (?:it |us |the \w+ )?to\b", 0.80),
    (r"\bgrew (?:from|to) \d", 0.80),
    (r"\bbest decision (?:I|we) ever made\b", 0.80),
    (r"\bit (?:completely )?(?:worked|took off|exploded)\b", 0.70),
    (r"\b(?:doubled|tripled|10x(?:ed)?)\b", 0.75),
    (r"\bfirst (?:million|customer|hire|sale)\b", 0.70),
    (r"\bproudest\b|\bturning point\b", 0.60),
])

SECRET_PATTERNS = _compile([
    (r"\bnobody (?:talks about|tells you|mentions)\b", 0.90),
    (r"\bI(?:'ve| have) never (?:told|said|shared) (?:this|anyone)\b", 0.95),
    (r"\bwhat (?:they|people) don(?:'t| not) (?:tell|want) you\b", 0.90),
    (r"\bbehind the scenes\b", 0.60),
    (r"\boff the record\b", 0.85),
    (r"\bthe (?:real|dirty little) secret\b", 0.85),
    (r"\bhere(?:'s| is) what (?:really|actually) happened\b", 0.80),
    (r"\bmost people (?:don't|never) (?:realise|realize|know)\b", 0.75),
    (r"\bI(?:'m| am) probably not supposed to say\b", 0.90),
    (r"\bthe part (?:they|nobody) (?:leaves? out|skips?)\b", 0.80),
])

LESSON_PATTERNS = _compile([
    (r"\bthe lesson (?:here )?(?:is|was)\b", 0.90),
    (r"\bif I could go back\b", 0.85),
    (r"\bwhat I(?:'d| would) tell (?:my younger self|anyone|you)\b", 0.85),
    (r"\bthe (?:one )?thing (?:I|you) (?:learned|should)\b", 0.75),
    (r"\bmy advice (?:is|would be)\b", 0.85),
    (r"\bnever (?:do|make|take) (?:this|that|the)\b", 0.60),
    (r"\balways (?:start|do|ask|check)\b", 0.55),
    (r"\bthe rule (?:is|I follow)\b", 0.70),
    (r"\bhere(?:'s| is) (?:how|what) (?:you|to)\b", 0.55),
    (r"\btakeaway\b", 0.70),
])


# --- Detectors ---------------------------------------------------------------

Detector = Callable[[DetectorContext], Iterator[SignalHit]]


def _lexical_detector(
    signal: Signal, patterns: tuple[WeightedPattern, ...], floor: float = 0.25
) -> Detector:
    """Build a detector that fires purely on the utterance's own text."""

    def detect(ctx: DetectorContext) -> Iterator[SignalHit]:
        strength, evidence = _match_strength(ctx.utterance.text, patterns)
        if strength >= floor:
            yield SignalHit(
                signal=signal,
                strength=strength,
                utterance_index=ctx.index,
                evidence="; ".join(evidence[:3]),
            )

    return detect


def detect_argument(ctx: DetectorContext) -> Iterator[SignalHit]:
    """Argument = adversarial language *plus* structural back-and-forth.

    Lexical evidence alone is not enough: a speaker quoting someone else's
    outburst reads identical to a real one at the token level. Requiring rapid
    speaker alternation is what separates the two.
    """
    lexical, evidence = _match_strength(ctx.utterance.text, ARGUMENT_PATTERNS)
    if lexical < 0.25:
        return
    density = ctx.turn_density()
    # Structure can amplify by up to 1.4x, or damp to 0.6x when a supposed
    # argument is happening inside a monologue.
    structural = 0.6 + 0.8 * density
    strength = clamp(lexical * structural)
    if strength >= 0.25:
        yield SignalHit(
            signal=Signal.ARGUMENT,
            strength=strength,
            utterance_index=ctx.index,
            evidence="; ".join(evidence[:3]) + f" (turn density {density:.2f})",
        )


def detect_debate(ctx: DetectorContext) -> Iterator[SignalHit]:
    """Debate = reasoned disagreement, requiring at least two speakers present.

    Damped when the argument detector also fires hard on the same utterance:
    heat and structured reasoning are different products, and a moment that is
    clearly a row should not also be sold as a measured debate.
    """
    lexical, evidence = _match_strength(ctx.utterance.text, DEBATE_PATTERNS)
    if lexical < 0.25:
        return
    if len(ctx.transcript.speakers) < 2:
        lexical *= 0.5
    heat, _ = _match_strength(ctx.utterance.text, ARGUMENT_PATTERNS)
    strength = clamp(lexical * (1.0 - 0.5 * heat))
    if strength >= 0.2:
        yield SignalHit(
            signal=Signal.DEBATE,
            strength=strength,
            utterance_index=ctx.index,
            evidence="; ".join(evidence[:3]),
        )


def detect_emotional_spike(ctx: DetectorContext) -> Iterator[SignalHit]:
    """Emotional amplitude, with a bonus for shouted (all-caps) text.

    ASR rarely emits caps, but human-corrected transcripts and stream chat
    overlays do, and it is a strong enough cue to be worth reading.
    """
    strength, evidence = _match_strength(ctx.utterance.text, EMOTION_PATTERNS)
    words = ctx.utterance.text.split()
    shouted = [w for w in words if len(w) > 3 and w.isupper()]
    if shouted:
        strength = saturating_sum([strength, min(0.6, 0.2 * len(shouted))])
        evidence.append(f"shouted: {' '.join(shouted[:3])}")
    if strength >= 0.25:
        yield SignalHit(
            signal=Signal.EMOTIONAL_SPIKE,
            strength=strength,
            utterance_index=ctx.index,
            evidence="; ".join(evidence[:3]),
        )


DETECTORS: tuple[Detector, ...] = (
    _lexical_detector(Signal.CONTROVERSY, CONTROVERSY_PATTERNS),
    detect_emotional_spike,
    _lexical_detector(Signal.MONEY, MONEY_PATTERNS),
    _lexical_detector(Signal.FUNNY, FUNNY_PATTERNS),
    detect_argument,
    detect_debate,
    _lexical_detector(Signal.FAILURE, FAILURE_PATTERNS),
    _lexical_detector(Signal.SUCCESS, SUCCESS_PATTERNS),
    _lexical_detector(Signal.SECRET, SECRET_PATTERNS),
    _lexical_detector(Signal.LESSON, LESSON_PATTERNS),
)


def detect_all(transcript: Transcript) -> list[SignalHit]:
    """Run every detector over every utterance. O(utterances × patterns)."""
    hits: list[SignalHit] = []
    for i in range(len(transcript.utterances)):
        ctx = DetectorContext(transcript=transcript, index=i)
        for detector in DETECTORS:
            hits.extend(detector(ctx))
    return hits


def hits_by_utterance(hits: Sequence[SignalHit]) -> dict[int, list[SignalHit]]:
    grouped: dict[int, list[SignalHit]] = {}
    for hit in hits:
        grouped.setdefault(hit.utterance_index, []).append(hit)
    return grouped
