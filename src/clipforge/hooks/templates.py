"""The template bank.

Each template declares the slots it needs. A template whose slots cannot be
filled is skipped, which is why the bank is deliberately over-provisioned:
roughly a hundred templates so that even a clip yielding only a topic still
produces twenty distinct hooks.

Templates are English. They are **not** translated for the other four
languages the caption engine supports, and should not be: hook phrasing is
idiomatic and culturally specific, and a literal translation of "Nobody talks
about this" lands flat in German and reads as an accusation in Dutch. The bank
is keyed by language so adding one is data rather than code, but each needs
native authoring against that market's own short-form conventions.
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import HookType


@dataclass(frozen=True, slots=True)
class Template:
    """One hook pattern.

    `weight` is a prior on the pattern's strength, used to break ties between
    hooks that score identically on features. It encodes "this phrasing has
    historically worked", which is exactly the kind of belief that should be
    replaced by measurement as soon as click data exists.
    """

    id: str
    hook_type: HookType
    pattern: str
    requires: tuple[str, ...] = ()
    weight: float = 1.0


C, K, A, F, S = (
    HookType.CURIOSITY,
    HookType.CONTROVERSY,
    HookType.AUTHORITY,
    HookType.FEAR,
    HookType.SURPRISE,
)
N, Q, T, G, P = (
    HookType.NUMBER,
    HookType.QUESTION,
    HookType.TRANSFORMATION,
    HookType.NEGATIVITY,
    HookType.SOCIAL_PROOF,
)


ENGLISH: tuple[Template, ...] = (
    # --- Curiosity: open a loop the viewer needs closed --------------------
    Template("cur.real_reason", C, "The real reason {topic_phrase} {outcome}", ("topic_phrase", "outcome"), 1.15),
    Template("cur.nobody_after", C, "Nobody talks about what happens after {topic_phrase}", ("topic_phrase",), 1.05),
    Template("cur.part_hidden", C, "The part of {topic_phrase} nobody shows you", ("topic_phrase",), 1.10),
    Template("cur.what_happened", C, "What actually happened with {topic_phrase}", ("topic_phrase",), 1.0),
    Template("cur.found_out", C, "I found out why {topic_phrase} {outcome}", ("topic_phrase", "outcome"), 1.05),
    Template("cur.never_told", C, "What I have never told anyone about {topic_phrase}", ("topic_phrase",), 1.20),
    Template("cur.number_hidden", C, "Where the {number} actually went", ("number",), 1.15),
    Template("cur.until_end", C, "Watch what {topic} does at the end", ("topic",), 0.90),
    Template("cur.one_detail", C, "One detail about {topic_phrase} changes everything", ("topic_phrase",), 1.0),
    Template("cur.quote_open", C, "“{quote}” — here's what he meant", ("quote",), 0.95),

    # --- Controversy: take a contestable position --------------------------
    Template("con.is_a_lie", K, "{topic_phrase} is a lie", ("topic_phrase",), 1.10),
    Template("con.everything_wrong", K, "Everything you know about {topic_phrase} is wrong", ("topic_phrase",), 1.05),
    Template("con.unpopular", K, "Unpopular opinion: {topic_phrase} is overrated", ("topic_phrase",), 1.15),
    Template("con.stop_doing", K, "Stop making this {topic} mistake", ("topic",), 1.05),
    Template("con.doesnt_work", K, "{topic_phrase} does not work. Here is the proof.", ("topic_phrase",), 1.10),
    Template("con.hate_this", K, "You are going to hate this take on {topic_phrase}", ("topic_phrase",), 1.0),
    Template("con.industry", K, "The {topic} industry doesn't want this out", ("topic",), 0.85),
    Template("con.disagree", K, "I disagree with everyone about {topic_phrase}", ("topic_phrase",), 1.0),

    # --- Authority: earn the claim -----------------------------------------
    Template("aut.cost_me", A, "I lost {number} learning this", ("number",), 1.25),
    Template("aut.after_time", A, "After {timeframe}, here is what I know about {topic_phrase}", ("timeframe", "topic_phrase"), 1.10),
    Template("aut.taught_me", A, "{number} taught me one thing about {topic_phrase}", ("number", "topic_phrase"), 1.15),
    Template("aut.done_it", A, "I have {outcome} {number}. This is what matters.", ("outcome", "number"), 1.10),
    Template("aut.experience", A, "{timeframe} of {topic_phrase}, in ninety seconds", ("timeframe", "topic_phrase"), 0.95),
    Template("aut.i_was_there", A, "I was there when {topic_phrase} {outcome}", ("topic_phrase", "outcome"), 1.05),
    Template("aut.paid_to_learn", A, "This lesson cost me {number}", ("number",), 1.20),

    # --- Fear: loss aversion ------------------------------------------------
    Template("fea.mistake_cost", F, "This mistake cost me {number}", ("number",), 1.25),
    Template("fea.dont_until", F, "Do not touch {topic_phrase} until you watch this", ("topic_phrase",), 1.15),
    Template("fea.ruins", F, "The {topic} mistake that ruins everything", ("topic",), 1.10),
    Template("fea.about_to_lose", F, "You're about to lose {number} doing this", ("number",), 1.15),
    Template("fea.too_late", F, "By the time you notice {topic_phrase}, it is too late", ("topic_phrase",), 1.05),
    Template("fea.warning", F, "If {topic_phrase} feels fine, watch this", ("topic_phrase",), 1.10),
    Template("fea.no_one_warns", F, "Nobody warns you about {topic_phrase}", ("topic_phrase",), 1.10),

    # --- Surprise: violate expectation --------------------------------------
    Template("sur.cost_me", S, "{topic_phrase} cost me {number}", ("topic_phrase", "number"), 1.20),
    Template("sur.didnt_expect", S, "I did not expect {topic_phrase} to {outcome_base}", ("topic_phrase", "outcome_base"), 1.05),
    Template("sur.turns_out", S, "Turns out {topic_phrase} was the problem", ("topic_phrase",), 1.10),
    Template("sur.number_alone", S, "{number}. That is what {topic_phrase} actually cost.", ("number", "topic_phrase"), 1.15),
    Template("sur.opposite", S, "{topic_phrase} did the opposite of what I expected", ("topic_phrase",), 1.0),
    Template("sur.plot_twist", S, "Then {topic_phrase} {outcome}", ("topic_phrase", "outcome"), 0.95),

    # --- Number: specificity as the draw ------------------------------------
    Template("num.reasons", N, "3 reasons {topic_phrase} {outcome}", ("topic_phrase", "outcome"), 1.05),
    Template("num.how_i", N, "How I {outcome} {number}", ("outcome", "number"), 1.20),
    Template("num.in_time", N, "{number} in {timeframe}", ("number", "timeframe"), 1.15),
    Template("num.breakdown", N, "The {number} breakdown nobody shows", ("number",), 1.0),
    Template("num.exact", N, "Exactly how {topic_phrase} became {number}", ("topic_phrase", "number"), 1.10),

    # --- Question: direct address -------------------------------------------
    Template("que.why_did", Q, "Why did {topic_phrase} {outcome}?", ("topic_phrase", "outcome"), 1.05),
    Template("que.what_would", Q, "What would you do with {number}?", ("number",), 1.10),
    Template("que.would_you", Q, "Would you {outcome_base} for {number}?", ("outcome_base", "number"), 1.15),
    Template("que.ever_wonder", Q, "Ever wonder why {topic_phrase} fails?", ("topic_phrase",), 0.95),
    Template("que.am_i_wrong", Q, "{topic_phrase} is broken. Am I wrong?", ("topic_phrase",), 1.05),

    # --- Transformation: before/after ---------------------------------------
    Template("tra.from_to", T, "From {number} to nothing in {timeframe}", ("number", "timeframe"), 1.20),
    Template("tra.went_from", T, "How {topic_phrase} went from working to broken", ("topic_phrase",), 1.05),
    Template("tra.before_after", T, "Before {topic_phrase}, and after", ("topic_phrase",), 0.90),
    Template("tra.rebuilt", T, "I rebuilt {topic_phrase} after losing {number}", ("topic_phrase", "number"), 1.15),

    # --- Negativity: mistakes and warnings ----------------------------------
    Template("neg.worst", G, "The worst decision I made with {topic_phrase}", ("topic_phrase",), 1.15),
    Template("neg.never_do", G, "Never do this with {topic_phrase}", ("topic_phrase",), 1.10),
    Template("neg.biggest_mistake", G, "My biggest {topic} mistake cost {number}", ("topic", "number"), 1.20),
    Template("neg.wish_known", G, "What I wish I knew before {topic_phrase}", ("topic_phrase",), 1.10),
    Template("neg.wasted", G, "I wasted {timeframe} on {topic_phrase}", ("timeframe", "topic_phrase"), 1.15),

    # --- Social proof: everyone / nobody -------------------------------------
    Template("soc.everyone_wrong", P, "Everyone gets {topic_phrase} wrong", ("topic_phrase",), 1.10),
    Template("soc.nobody_tells", P, "Nobody tells you this about {topic_phrase}", ("topic_phrase",), 1.15),
    Template("soc.99_percent", P, "Most people never figure out {topic_phrase}", ("topic_phrase",), 1.0),
    Template("soc.they_all", P, "They all made the same {topic} mistake", ("topic",), 0.95),

    # --- Topic-free fallbacks ------------------------------------------------
    # These need nothing, and there are deliberately more than twenty of them.
    #
    # Extraction returns no topic when the clip gives it no evidence of one,
    # rather than naming its most frequent adjective. That is the right call,
    # but it only works if the bank can still fill a set of twenty without a
    # single slot — otherwise the honest path silently ships eight hooks.
    Template("fb.nobody_ready", C, "Nobody was ready for this", (), 0.75),
    Template("fb.watch_end", C, "Watch until the end", (), 0.55),
    Template("fb.still_think", C, "I still think about this", (), 0.70),
    Template("fb.wrong_about", C, "I was wrong about all of it", (), 0.78),
    Template("fb.took_years", C, "It took me years to understand this", (), 0.76),
    Template("fb.one_moment", C, "This is the moment it changed", (), 0.72),
    Template("fb.changed_mind", S, "This changed my mind completely", (), 0.75),
    Template("fb.saw_coming", S, "Nobody saw this coming. Including me.", (), 0.80),
    Template("fb.not_what_looks", S, "It is not what it looks like", (), 0.68),
    Template("fb.shouldnt_say", K, "I probably shouldn't say this", (), 0.85),
    Template("fb.unpopular_plain", K, "Unpopular opinion, and I mean it", (), 0.74),
    Template("fb.disagree_plain", K, "Most people are going to disagree", (), 0.70),
    Template("fb.hardest", A, "The hardest thing I've had to admit", (), 0.80),
    Template("fb.learned_hard", A, "I learned this the expensive way", (), 0.82),
    Template("fb.been_there", A, "I have been on both sides of this", (), 0.72),
    Template("fb.wish_earlier", G, "I wish someone had told me this earlier", (), 0.85),
    Template("fb.dont_repeat", G, "Do not make the call I made", (), 0.79),
    Template("fb.worst_call", G, "The worst call I ever made", (), 0.81),
    Template("fb.no_one_believes", P, "No one believes me when I say this", (), 0.80),
    Template("fb.everyone_does", P, "Everyone does this. It is a trap.", (), 0.77),
    Template("fb.nobody_admits", P, "Nobody admits this out loud", (), 0.79),
    Template("fb.should_ask", Q, "Would you have done it differently?", (), 0.71),
    Template("fb.what_would_you", Q, "What would you have said here?", (), 0.69),
    Template("fb.point_it_broke", T, "This is where it stopped working", (), 0.73),
    Template("fb.before_i_knew", T, "Before I knew any of this", (), 0.66),
    Template("fb.first_time", N, "The first time it happened I froze", (), 0.67),
    Template("fb.warning_plain", F, "Do not learn this the way I did", (), 0.83),
    Template("fb.costs_more", F, "It costs more than you think", (), 0.76),
)


BANK: dict[str, tuple[Template, ...]] = {"en": ENGLISH}


def for_language(language: str) -> tuple[Template, ...]:
    """Templates for a language, falling back to English.

    The fallback is deliberate and visible in `HookSet.stats`: producing
    English hooks for a Dutch clip is wrong, but producing none is worse, and
    the caller needs to be able to see which happened.
    """
    return BANK.get(language, ENGLISH)


def supported_languages() -> tuple[str, ...]:
    return tuple(BANK)


def render(template: Template, slots) -> str | None:
    """Fill a template, or None when a required slot is missing."""
    for name in template.requires:
        if not slots.has(name):
            return None

    values = slots.as_dict()
    try:
        text = template.pattern.format(**values)
    except (KeyError, IndexError):
        return None

    text = " ".join(text.split())
    if not text:
        return None

    if _repeats_a_word(text):
        return None

    # Capitalise the opening character without touching the rest — a slot
    # value may legitimately be an acronym or a dollar figure.
    return text[0].upper() + text[1:]


# Words that may legitimately appear twice in one hook.
_REPEATABLE = frozenset({
    "the", "a", "an", "to", "of", "in", "on", "at", "and", "or", "is", "was",
    "you", "i", "it", "this", "that", "what", "for", "with", "my", "me",
})


def _repeats_a_word(text: str) -> bool:
    """Whether a rendered hook says the same content word twice.

    Several templates hard-code a noun the extractor can also produce as the
    topic, which yields "The mistake mistake that ruins everything" and "My
    biggest mistake mistake cost $14 million". Rejecting the render is right
    rather than trying to repair it: the bank is over-provisioned precisely so
    that losing one template costs nothing.
    """
    tokens = [t.strip(".,;:!?\"'“”—…").lower() for t in text.split()]
    tokens = [t for t in tokens if t and t not in _REPEATABLE]

    for previous, current in zip(tokens, tokens[1:]):
        if previous == current:
            return True

    seen: set[str] = set()
    for token in tokens:
        # Only content words long enough to be meaningful; "cost" appearing
        # twice in a long hook is fine, "mistake" twice is not.
        if len(token) >= 5:
            if token in seen:
                return True
            seen.add(token)
    return False
