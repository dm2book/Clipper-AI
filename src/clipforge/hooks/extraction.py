"""Pull fillable content out of a clip.

Templates are only as good as what goes in their slots. "The real reason it
failed" is a weak hook; "The real reason we lost $18 million" is a strong one,
and the difference is entirely whether extraction found the number.

Everything here is deterministic and offline. When a slot cannot be filled,
templates needing it are skipped rather than rendered with a placeholder —
shipping a hook that literally reads `{number}` is worse than shipping one
fewer hook.
"""

from __future__ import annotations

import re
from collections import Counter

from .types import ClipContext, Slots

# Money, percentages, and scaled counts, in priority order. Money first: a
# dollar figure is the single most clickable slot value there is, because it
# is unambiguous, comparable, and impossible to hand-wave.
# `\s*` rather than `\s?` between the figure and its scale word: ASR output
# has irregular spacing, and matching "$18" out of "$18   million" loses the
# only part of the figure that carries weight.
_MONEY = re.compile(
    r"[$€£]\s*\d[\d,.]*\s*(?:k|m|bn?|million|billion|thousand)?\b"
    r"|\b\d[\d,.]*\s*(?:million|billion|thousand|k)\s*(?:dollars|euros|pounds)?\b"
    r"|\b\d[\d,.]*\s*(?:dollars|euros|pounds|grand)\b",
    re.IGNORECASE,
)
_PERCENT = re.compile(r"\b\d[\d,.]*\s?(?:%|percent)\b", re.IGNORECASE)
_SPELLED_SCALE = re.compile(
    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred)"
    r"(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?\s+"
    r"(?:million|billion|thousand|hundred|percent)\b",
    re.IGNORECASE,
)
_PLAIN_COUNT = re.compile(r"\b\d{2,}\b")

_TIMEFRAME = re.compile(
    r"\b(?:in|after|within|over|for)\s+"
    r"(?:\d+|a|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"eighteen|twenty)\s+"
    r"(?:day|days|week|weeks|month|months|year|years|hour|hours|minute|minutes)\b",
    re.IGNORECASE,
)

# Strong past-tense outcomes. These make the best `outcome` slot because they
# already imply consequence, which is what a hook needs to promise.
_OUTCOME = re.compile(
    r"\b(?:lost|made|built|destroyed|quit|fired|failed|collapsed|doubled|"
    r"tripled|scaled|burned|wasted|saved|earned|raised|sold|bought|left|"
    r"ruined|survived|beat|won|crashed|exploded|grew|shrank|killed)\b",
    re.IGNORECASE,
)

_ENTITY = re.compile(r"\b(?:[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})?)\b")

_STOPWORDS = frozenset("""
a an the and or but if then than that this these those there here so as of to
in on at by for with from into about over under again further once is was are
were be been being am do does did doing have has had having i you he she it we
they them his her its our your their what which who whom when where why how all
any both each few more most other some such no nor not only own same too very
can will just don should now got get go going really actually basically like
one two really thing things way ways lot lots know think said say says
""".split())

# Words that make a poor topic even though they clear the stopword filter.
# Units and measure words are the important half: they are frequent, long
# enough to survive the length filter, and produce hooks like "Nobody talks
# about what happens after months", which is nonsense.
_WEAK_TOPICS = frozenset({
    # vague nouns
    "people", "everyone", "everybody", "nobody", "someone", "something",
    "anything", "everything", "nothing", "point", "thing", "stuff",
    "kind", "part", "place", "case", "reason", "problem",
    # Hook vocabulary. The templates already supply this framing, so picking
    # one as the topic produces "The mistake mistake that ruins everything"
    # and, once that is rejected, a set of hooks that all say the same word.
    "mistake", "mistakes", "decision", "moment", "story", "idea", "lesson",
    "question", "answer", "truth", "secret",
    # Speech filler, interjections and bare adverbs. Most of these are three
    # or four letters and were excluded by the old length floor; now that
    # three-letter nouns are admitted ("win", "job", "bug") they have to be
    # named explicitly.
    "yeah", "yes", "yep", "nope", "okay", "gonna", "wanna", "kinda", "sorta",
    "gotta", "dude", "bro", "man", "mate", "huh", "wow", "damn", "hey",
    "well", "sure", "maybe", "back", "down", "away", "off", "out", "much",
    "many", "even", "still", "let", "lets", "guy", "guys",
    # Bare adjectives. The `_MODIFIER_SUFFIXES` lookahead only catches
    # participles and derived forms; a plain adjective has no suffix to spot,
    # so "I do not have a strong view" hands the determiner bonus to "strong"
    # and produces "what I have never told anyone about a strong".
    "strong", "weak", "big", "bigger", "biggest", "small", "hard", "harder",
    "hardest", "easy", "real", "whole", "entire", "single", "main", "best",
    "worst", "better", "worse", "good", "bad", "great", "huge", "tiny",
    "quick", "slow", "simple", "weird", "crazy", "insane", "wild", "first",
    "last", "next", "right", "wrong", "clear", "full", "empty", "free",
    "new", "old", "young", "high", "low", "long", "short", "deep", "true",
    "false", "sad", "happy", "nice", "fine", "cool", "dumb", "smart",
    # time units
    "second", "seconds", "minute", "minutes", "hour", "hours", "day", "days",
    "week", "weeks", "month", "months", "year", "years", "time", "times",
    "today", "tomorrow", "yesterday", "morning", "night",
    # scale and currency units
    "hundred", "thousand", "million", "billion", "dollar", "dollars", "euro",
    "euros", "pound", "pounds", "percent", "grand",
    # spelled-out numbers
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "twenty", "thirty", "forty", "fifty", "ninety",
})

# Determiners that mark a following head noun. A cheap syntactic cue that
# beats raw frequency: "the raise", "the culture" are what the clip is about,
# while the most *frequent* content word is usually a unit or a filler.
#
# Demonstratives are deliberately excluded. "That" is far more often a
# complementizer than a determiner in speech -- "the lesson is that headcount
# is not progress" -- and treating it as one yields the topic phrase "that
# headcount", which is both wrong and ungrammatical in most templates.
_DETERMINERS = frozenset({
    "the", "a", "an", "my", "our", "your", "his", "her", "its", "their",
})

# Suffixes that mark a token as more likely an adjective or participle than a
# head noun. Only a soft demotion now — the head-of-phrase decision is made
# structurally, by whether a content word follows. These stay because plenty
# of them are nominalisations ("the funding", "the hiring"): a real noun should
# beat them, but they should still be reachable when nothing else is.
_MODIFIER_SUFFIXES = (
    "ed", "ing", "ous", "ful", "ive", "able", "ible", "al", "ic", "ish",
)

# `-ly` is the one suffix worth excluding outright rather than demoting. An
# adverb is never the subject, and "Nobody talks about what happens after the
# absolutely" is the output when one is allowed to be.
_LY_NOUNS = frozenset({
    "family", "families", "supply", "supplies", "reply", "replies", "rally",
    "ally", "allies", "belly", "bully", "monopoly", "anomaly", "assembly",
    "folly", "jelly", "rivalry",
})

# The lowest score a token may have and still be called the clip's topic.
# One bare mention of a content word is not evidence of subjecthood; a
# determiner attachment (+2.5) or a second mention is. Below this the topic
# slot is left empty and the slotless fallback templates carry the set.
_TOPIC_FLOOR = 2.2

# Past-tense outcomes mapped to their base form. Templates that place the verb
# after "to" need an infinitive; the transcript only ever supplies past tense,
# and "I did not expect this to lost" is the result of ignoring that.
_OUTCOME_BASE: dict[str, str] = {
    "lost": "lose", "made": "make", "built": "build", "destroyed": "destroy",
    "quit": "quit", "fired": "fire", "failed": "fail", "collapsed": "collapse",
    "doubled": "double", "tripled": "triple", "scaled": "scale",
    "burned": "burn", "wasted": "waste", "saved": "save", "earned": "earn",
    "raised": "raise", "sold": "sell", "bought": "buy", "left": "leave",
    "ruined": "ruin", "survived": "survive", "beat": "beat", "won": "win",
    "crashed": "crash", "exploded": "explode", "grew": "grow",
    "shrank": "shrink", "killed": "kill",
}


def _clean(token: str) -> str:
    return token.strip(".,;:!?\"'()[]—–…").lower()


def extract_number(text: str) -> str:
    """The most clickable figure in the clip, or empty.

    Priority is money, then percentages, then spelled-out scaled numbers, then
    bare counts. A bare count is last because "18" alone means nothing while
    "$18 million" means everything.
    """
    for pattern in (_MONEY, _PERCENT, _SPELLED_SCALE):
        match = pattern.search(text)
        if match:
            return " ".join(match.group(0).split())
    match = _PLAIN_COUNT.search(text)
    return match.group(0) if match else ""


def extract_timeframe(text: str) -> str:
    match = _TIMEFRAME.search(text)
    if not match:
        return ""
    # Strip the leading preposition; templates supply their own.
    words = match.group(0).split()
    return " ".join(words[1:])


def extract_outcome(text: str) -> str:
    match = _OUTCOME.search(text)
    return match.group(0).lower() if match else ""


def extract_entity(text: str) -> str:
    """A proper noun, skipping sentence-initial capitals that are not names."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    for sentence in sentences:
        tokens = sentence.split()
        for i, token in enumerate(tokens):
            if i == 0:
                continue  # sentence-initial capital proves nothing
            match = _ENTITY.fullmatch(token.strip(".,;:!?\"'()"))
            if match and _clean(token) not in _STOPWORDS:
                return match.group(0)
    return ""


def extract_topic(text: str, hint: str = "") -> tuple[str, str]:
    """The clip's subject, as `(bare_noun, noun_phrase)`.

    Both forms are returned because templates need different ones: "The
    {topic} mistake" wants the bare noun, while "what happens after
    {topic_phrase}" wants the determiner. Filling one from the other produces
    either "The the raise mistake" or "after raise".

    Selection is determiner-aware rather than purely frequency-based. Raw
    frequency picks whatever unit or filler word repeats most, which is how an
    earlier version decided a clip about a funding round was about "months".
    A noun introduced by "the" or "my" is far more likely to be the subject.
    """
    if hint:
        bare = hint.strip()
        return bare, bare

    raw = text.split()
    tokens = [_clean(t) for t in raw]
    scores: dict[str, float] = {}
    phrases: dict[str, str] = {}

    def is_content(index: int) -> bool:
        """Whether the token at `index` could be a head noun.

        The floor is three characters, not four. Four excluded the exact nouns
        short-form content is most often about -- "win", "job", "bug", "app",
        "ban", "tax" -- and the cost of admitting them is a longer
        `_WEAK_TOPICS` list, which is the cheaper side of that trade.
        """
        if not 0 <= index < len(tokens):
            return False
        token = tokens[index]
        if token.endswith("ly") and token not in _LY_NOUNS:
            return False
        return (
            len(token) >= 3
            and token.isalpha()
            and token not in _STOPWORDS
            and token not in _WEAK_TOPICS
        )

    for i, token in enumerate(tokens):
        if not is_content(i):
            continue

        previous = tokens[i - 1] if i > 0 else ""

        # A token immediately after a number is a unit ("fourteen million",
        # "seven months"), never the subject.
        if previous.isdigit() or previous in _WEAK_TOPICS:
            continue

        score = 1.0
        if previous in _DETERMINERS:
            # The determiner attaches to the whole noun phrase, so when it is
            # followed by two content words the head is the later one:
            # "a guaranteed win" is about the win, "a straight line" about the
            # line. Gating this on a suffix list only catches participles and
            # derived forms — a plain adjective has no suffix to spot, and
            # every one missed becomes a hook reading "about a straight".
            if not is_content(i + 1):
                score += 2.5
                phrases.setdefault(token, f"{previous} {token}")
        elif tokens[i - 2 : i - 1] and tokens[i - 2] in _DETERMINERS:
            # Head noun of a "determiner adjective noun" phrase.
            score += 2.5
            phrases.setdefault(token, f"{tokens[i - 2]} {token}")

        # Earlier mentions are more likely to be the subject.
        score += max(0.0, 1.0 - i / max(1, len(tokens))) * 0.5
        score += len(token) * 0.04

        if token.endswith(_MODIFIER_SUFFIXES):
            score *= 0.55

        scores[token] = scores.get(token, 0.0) + score

    if not scores:
        return "", ""

    best, best_score = max(scores.items(), key=lambda kv: (kv[1], len(kv[0])))

    # A single bare mention is not evidence of subjecthood. Returning nothing
    # is better than returning a word the clip is not about: the slotless
    # fallback templates still fill the set, and a generic hook beats twenty
    # hooks confidently naming the wrong thing.
    if best_score < _TOPIC_FLOOR:
        return "", ""

    # Default to a definite article when no determiner was observed. Templates
    # use the phrase nominally ("what happens after ..."), where a bare noun
    # reads as a dropped word.
    return best, phrases.get(best, f"the {best}")


def extract_quote(text: str, max_words: int = 9) -> str:
    """The most quotable short sentence, for hooks that lead with a line.

    Prefers sentences that are short, declarative, and contain a strong verb or
    a figure — the properties that make a line survive being lifted out of
    context and put on screen alone.
    """
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    scored: list[tuple[float, str]] = []

    for sentence in sentences:
        words = sentence.split()
        if not 3 <= len(words) <= max_words:
            continue
        score = 0.0
        if _OUTCOME.search(sentence):
            score += 2.0
        if _MONEY.search(sentence) or _PERCENT.search(sentence):
            score += 2.5
        if sentence.rstrip().endswith("?"):
            score += 0.5

        # The content bar is a gate, not a term. Without it the brevity bonus
        # alone qualifies any short sentence, and "It was a Tuesday" gets put
        # on screen as the clip's most quotable line.
        if score <= 0:
            continue

        # Shorter is better once that bar is met.
        score += max(0.0, (max_words - len(words)) * 0.15)
        scored.append((score, sentence.rstrip(".")))

    if not scored:
        return ""
    return max(scored, key=lambda pair: pair[0])[1]


def extract(context: ClipContext) -> Slots:
    """Every slot the templates might need."""
    text = context.text
    topic, topic_phrase = extract_topic(text, context.topic_hint)
    outcome = extract_outcome(text)
    return Slots(
        topic=topic,
        topic_phrase=topic_phrase,
        number=extract_number(text),
        outcome=outcome,
        outcome_base=_OUTCOME_BASE.get(outcome, ""),
        timeframe=extract_timeframe(text),
        entity=extract_entity(text),
        quote=extract_quote(text),
    )
