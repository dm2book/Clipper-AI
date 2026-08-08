"""Per-language typographic and line-breaking rules.

Captions are the most typography-sensitive thing this product renders, and the
rules genuinely differ per language. Ignoring them produces output that looks
fine in English and visibly broken everywhere else:

  French   requires a narrow non-breaking space *before* `; : ! ?` and inside
           `« »`. Elided forms (`l'`, `qu'`, `j'`) must never be split from
           the word they attach to — "l'" alone on a line is not a word.
  Spanish  opens questions and exclamations with `¿` and `¡`. Those glue to
           what follows; stranding one at a line end is a typo, not a break.
  German   capitalises all nouns and builds very long compounds. Overflow is
           the normal case, not the exception, so the fitter has to shrink.
  Dutch    shares German's compounding, plus the `'s` proclitic
           (`'s ochtends`) which is one unit despite the space.
  English  contractions are already single tokens; the main risk is orphaned
           articles at line ends.

Everything here is data. `chunking.py` consumes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .types import Language

# Narrow no-break space — the correct character for French punctuation spacing.
NNBSP = " "
NBSP = " "


@dataclass(frozen=True, slots=True)
class LanguageRules:
    """Line-breaking and typography rules for one language."""

    language: Language
    name: str

    # Words that must not be left at the end of a line — articles,
    # prepositions and conjunctions that belong to what follows.
    no_break_after: frozenset[str] = frozenset()

    # Tokens that must not start a line; they attach to the previous word.
    no_break_before: frozenset[str] = frozenset()

    # Prefixes that glue to the following token (French elision, Spanish
    # inverted punctuation, Dutch proclitic `'s`).
    glue_prefixes: tuple[str, ...] = ()

    # Punctuation that takes a preceding narrow no-break space (French).
    space_before_punctuation: frozenset[str] = frozenset()

    # Average characters per word — used to sanity-check chunk sizing across
    # languages, since German words are far longer than French ones.
    mean_word_chars: float = 5.0

    # Compounding languages overflow the line constantly; the fitter is
    # allowed to shrink further for them before giving up.
    heavy_compounding: bool = False


_SHARED_TERMINAL = frozenset({".", "!", "?", "…"})
_SHARED_MEDIAL = frozenset({",", ";", ":", "—", "–"})


ENGLISH = LanguageRules(
    language=Language.ENGLISH,
    name="English",
    no_break_after=frozenset({
        "a", "an", "the", "of", "to", "in", "on", "at", "for", "with", "and",
        "or", "but", "my", "your", "his", "her", "its", "our", "their", "is",
        "was", "be", "as", "by", "from", "that", "this",
    }),
    mean_word_chars=4.8,
)

DUTCH = LanguageRules(
    language=Language.DUTCH,
    name="Nederlands",
    no_break_after=frozenset({
        "de", "het", "een", "van", "in", "op", "aan", "bij", "met", "voor",
        "en", "of", "maar", "mijn", "jouw", "zijn", "haar", "ons", "onze",
        "hun", "is", "was", "te", "door", "dat", "dit", "die", "deze", "naar",
    }),
    # 's ochtends, 's avonds, 's-Hertogenbosch — one unit despite the space.
    glue_prefixes=("'s", "’s"),
    mean_word_chars=5.4,
    heavy_compounding=True,
)

GERMAN = LanguageRules(
    language=Language.GERMAN,
    name="Deutsch",
    no_break_after=frozenset({
        "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen",
        "einem", "einer", "eines", "und", "oder", "aber", "von", "zu", "in",
        "an", "auf", "bei", "mit", "für", "mein", "dein", "sein", "ihr",
        "unser", "ist", "war", "im", "am", "zum", "zur", "dass", "wenn",
    }),
    mean_word_chars=6.2,
    heavy_compounding=True,
)

FRENCH = LanguageRules(
    language=Language.FRENCH,
    name="Français",
    no_break_after=frozenset({
        "le", "la", "les", "un", "une", "des", "du", "de", "au", "aux", "et",
        "ou", "mais", "mon", "ton", "son", "ma", "ta", "sa", "notre", "votre",
        "leur", "est", "était", "en", "dans", "sur", "pour", "avec", "que",
        "qui", "ce", "cette", "ces",
    }),
    # Elided forms. Splitting after the apostrophe leaves a fragment, not a word.
    glue_prefixes=(
        "l'", "d'", "j'", "n'", "m'", "t'", "s'", "c'", "qu'", "jusqu'",
        "lorsqu'", "puisqu'", "quoiqu'",
        "l’", "d’", "j’", "n’", "m’", "t’", "s’", "c’", "qu’",
    ),
    space_before_punctuation=frozenset({";", ":", "!", "?", "»"}),
    mean_word_chars=4.9,
)

SPANISH = LanguageRules(
    language=Language.SPANISH,
    name="Español",
    no_break_after=frozenset({
        "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del",
        "al", "a", "en", "con", "por", "para", "y", "o", "pero", "mi", "tu",
        "su", "nuestro", "es", "era", "que", "se", "lo", "este", "esta",
        "ese", "esa",
    }),
    # Inverted opening marks belong to the clause that follows them.
    glue_prefixes=("¿", "¡"),
    mean_word_chars=5.0,
)


RULES: dict[Language, LanguageRules] = {
    Language.ENGLISH: ENGLISH,
    Language.DUTCH: DUTCH,
    Language.GERMAN: GERMAN,
    Language.FRENCH: FRENCH,
    Language.SPANISH: SPANISH,
}


def rules_for(language: Language) -> LanguageRules:
    return RULES[language]


def is_terminal(token: str) -> bool:
    """Whether a token ends a sentence — the strongest break opportunity."""
    stripped = token.rstrip("\"'»)]")
    return bool(stripped) and stripped[-1] in _SHARED_TERMINAL


def is_medial(token: str) -> bool:
    """Whether a token ends a clause — a decent break opportunity."""
    stripped = token.rstrip("\"'»)]")
    return bool(stripped) and stripped[-1] in _SHARED_MEDIAL


def normalise_token(token: str) -> str:
    """Lowercase and strip punctuation for rule lookup."""
    return token.strip(".,;:!?…\"'«»()[]¿¡—–").lower()


def glues_to_next(token: str, rules: LanguageRules) -> bool:
    """Whether this token must stay attached to the word after it.

    Covers French elision (`l'ami`), Spanish inverted punctuation (`¿qué`),
    and the Dutch proclitic (`'s ochtends`). Also catches any token ending in
    an apostrophe, which in French is elision by definition.
    """
    lowered = token.lower()
    if lowered in {p.lower() for p in rules.glue_prefixes}:
        return True
    if rules.language is Language.FRENCH and lowered.endswith(("'", "’")):
        return True
    if rules.language is Language.SPANISH and token.startswith(("¿", "¡")):
        # An opening mark glues only when it is the token by itself; attached
        # to its word (`¿Qué`) it is already one unit.
        return token in ("¿", "¡")
    return False


def orphan_risk(token: str, rules: LanguageRules) -> bool:
    """Whether ending a line on this token would strand a function word."""
    return normalise_token(token) in rules.no_break_after


def apply_typography(text: str, rules: LanguageRules) -> str:
    """Apply language-specific character-level typography.

    Currently French punctuation spacing: a narrow no-break space before
    `; : ! ?` and `»`, and after `«`. Rendered text without it reads as
    obviously machine-made to a French audience.
    """
    if not rules.space_before_punctuation:
        return text

    out = text
    for mark in rules.space_before_punctuation:
        # Replace an existing plain space, or insert one where there is none.
        out = out.replace(" " + mark, NNBSP + mark)
        out = out.replace(NNBSP + NNBSP + mark, NNBSP + mark)
        idx = 0
        while True:
            idx = out.find(mark, idx)
            if idx == -1:
                break
            if idx > 0 and out[idx - 1] not in (" ", NNBSP, NBSP):
                out = out[:idx] + NNBSP + out[idx:]
                idx += 2
            else:
                idx += 1
    out = out.replace("« ", "«" + NNBSP).replace("«" + NNBSP + NNBSP, "«" + NNBSP)
    return out


def detect(words: list[str]) -> Language:
    """Cheap language identification from function-word frequency.

    Not a substitute for the ASR's own language tag — this is a fallback for
    callers who do not have one. Function words are the right signal because
    they are the highest-frequency tokens in any transcript and barely overlap
    between these five languages.
    """
    markers: dict[Language, frozenset[str]] = {
        Language.ENGLISH: frozenset({"the", "and", "is", "that", "you", "it", "of", "to"}),
        Language.DUTCH: frozenset({"de", "het", "een", "en", "is", "dat", "niet", "van", "ik"}),
        Language.GERMAN: frozenset({"der", "die", "das", "und", "ist", "nicht", "ich", "mit"}),
        Language.FRENCH: frozenset({"le", "la", "les", "et", "est", "que", "pas", "je", "une"}),
        Language.SPANISH: frozenset({"el", "la", "los", "y", "es", "que", "no", "de", "un"}),
    }
    lowered = [normalise_token(w) for w in words]
    scores = {
        language: sum(1 for w in lowered if w in marker_set)
        for language, marker_set in markers.items()
    }
    best = max(scores, key=lambda k: scores[k])
    # Dutch and German share `die`/`is`-adjacent tokens; ties go to English
    # only when nothing matched at all.
    return best if scores[best] > 0 else Language.ENGLISH
