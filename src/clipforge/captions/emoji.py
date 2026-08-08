"""Multilingual emoji suggestion.

Premium captions use emoji as punctuation, not decoration — one per cue, on
the word that earns it. Spraying three per line is the single most reliable
way to make a clip look automated.

The lexicon is keyed by *concept*, with trigger words per language. It is not a
translation of an English list: each language's triggers are the words people
actually say, and a few concepts carry different weight per culture. Matching
is on normalised tokens plus a prefix pass, which handles the inflection these
five languages produce (German cases, Spanish/French verb endings, Dutch
plurals) without needing a lemmatiser.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .languages import normalise_token
from .types import Language

# Prefix matching below this length is unsafe — "be" would match "before".
MIN_PREFIX = 4


@dataclass(frozen=True, slots=True)
class Concept:
    emoji: str
    weight: float  # how strongly this concept deserves an emoji, 0..1
    triggers: dict[Language, tuple[str, ...]]


EN, NL, DE, FR, ES = (
    Language.ENGLISH,
    Language.DUTCH,
    Language.GERMAN,
    Language.FRENCH,
    Language.SPANISH,
)

LEXICON: tuple[Concept, ...] = (
    Concept("\U0001F4B0", 0.90, {   # money bag
        EN: ("money", "cash", "dollars", "revenue", "profit", "million", "salary"),
        NL: ("geld", "cash", "omzet", "winst", "miljoen", "salaris", "euro"),
        DE: ("geld", "umsatz", "gewinn", "million", "gehalt", "euro", "kosten"),
        FR: ("argent", "chiffre", "bénéfice", "million", "salaire", "euros"),
        ES: ("dinero", "ingresos", "beneficio", "millón", "sueldo", "euros"),
    }),
    Concept("\U0001F525", 0.75, {   # fire
        EN: ("fire", "insane", "incredible", "amazing", "unreal"),
        NL: ("vuur", "waanzinnig", "ongelooflijk", "geweldig"),
        DE: ("feuer", "wahnsinn", "unglaublich", "krass"),
        FR: ("feu", "incroyable", "dingue", "énorme"),
        ES: ("fuego", "increíble", "brutal", "tremendo"),
    }),
    Concept("\U0001F602", 0.70, {   # tears of joy
        EN: ("laugh", "hilarious", "funny", "joke"),
        NL: ("lachen", "grappig", "hilarisch", "grap"),
        DE: ("lachen", "lustig", "witzig", "witz"),
        FR: ("rire", "drôle", "marrant", "blague"),
        ES: ("reír", "gracioso", "divertido", "broma"),
    }),
    Concept("\U0001F92F", 0.80, {   # mind blown
        EN: ("shocked", "stunned", "unbelievable", "speechless"),
        NL: ("geschokt", "verbijsterd", "sprakeloos"),
        DE: ("schockiert", "fassungslos", "sprachlos"),
        FR: ("choqué", "stupéfait", "sidéré"),
        ES: ("impactado", "atónito", "flipando"),
    }),
    Concept("\U0001F914", 0.55, {   # thinking
        EN: ("think", "wonder", "question", "maybe", "perhaps"),
        NL: ("denken", "vraag", "misschien", "afvragen"),
        DE: ("denken", "frage", "vielleicht", "überlegen"),
        FR: ("penser", "question", "peut-être", "demander"),
        ES: ("pensar", "pregunta", "quizás", "preguntar"),
    }),
    Concept("⚠️", 0.70, {  # warning
        EN: ("warning", "danger", "careful", "mistake", "risk"),
        NL: ("waarschuwing", "gevaar", "voorzichtig", "fout", "risico"),
        DE: ("warnung", "gefahr", "vorsicht", "fehler", "risiko"),
        FR: ("attention", "danger", "erreur", "risque"),
        ES: ("advertencia", "peligro", "cuidado", "error", "riesgo"),
    }),
    Concept("\U0001F4A1", 0.65, {   # idea
        EN: ("idea", "insight", "realised", "realized", "lesson"),
        NL: ("idee", "inzicht", "besefte", "les"),
        DE: ("idee", "erkenntnis", "gemerkt", "lektion"),
        FR: ("idée", "compris", "leçon", "révélation"),
        ES: ("idea", "lección", "descubrí", "revelación"),
    }),
    Concept("\U0001F3C6", 0.80, {   # trophy
        EN: ("won", "win", "victory", "champion", "first"),
        NL: ("gewonnen", "winst", "overwinning", "kampioen"),
        DE: ("gewonnen", "sieg", "meister", "erster"),
        FR: ("gagné", "victoire", "champion", "premier"),
        ES: ("ganó", "victoria", "campeón", "primero"),
    }),
    Concept("\U0001F4C9", 0.70, {   # chart down
        EN: ("failed", "lost", "bankrupt", "collapsed", "crashed"),
        NL: ("mislukt", "verloren", "failliet", "ingestort"),
        DE: ("gescheitert", "verloren", "pleite", "zusammengebrochen"),
        FR: ("échoué", "perdu", "faillite", "effondré"),
        ES: ("fracasó", "perdió", "quiebra", "derrumbó"),
    }),
    Concept("\U0001F4C8", 0.70, {   # chart up
        EN: ("growth", "scaled", "doubled", "tripled", "grew"),
        NL: ("groei", "geschaald", "verdubbeld", "groeide"),
        DE: ("wachstum", "skaliert", "verdoppelt", "gewachsen"),
        FR: ("croissance", "doublé", "triplé", "augmenté"),
        ES: ("crecimiento", "duplicó", "triplicó", "creció"),
    }),
    Concept("\U0001F92B", 0.85, {   # shushing — secrets
        EN: ("secret", "nobody", "confidential", "privately"),
        NL: ("geheim", "niemand", "vertrouwelijk"),
        DE: ("geheim", "niemand", "vertraulich"),
        FR: ("secret", "personne", "confidentiel"),
        ES: ("secreto", "nadie", "confidencial"),
    }),
    Concept("⏰", 0.55, {       # alarm clock
        EN: ("time", "hours", "deadline", "minutes", "years"),
        NL: ("tijd", "uren", "deadline", "minuten", "jaren"),
        DE: ("zeit", "stunden", "frist", "minuten", "jahre"),
        FR: ("temps", "heures", "délai", "minutes", "années"),
        ES: ("tiempo", "horas", "plazo", "minutos", "años"),
    }),
    Concept("\U0001F680", 0.70, {   # rocket
        EN: ("launch", "started", "founded", "shipped"),
        NL: ("lancering", "gestart", "opgericht"),
        DE: ("start", "gestartet", "gegründet"),
        FR: ("lancement", "lancé", "fondé"),
        ES: ("lanzamiento", "lanzó", "fundó"),
    }),
    Concept("❤️", 0.60, { # heart
        EN: ("love", "loved", "grateful", "thank"),
        NL: ("liefde", "dankbaar", "bedankt"),
        DE: ("liebe", "dankbar", "danke"),
        FR: ("amour", "reconnaissant", "merci"),
        ES: ("amor", "agradecido", "gracias"),
    }),
    Concept("\U0001F62D", 0.65, {   # loudly crying
        EN: ("crying", "devastated", "heartbroken", "tears"),
        NL: ("huilen", "kapot", "gebroken", "tranen"),
        DE: ("weinen", "am boden", "gebrochen", "tränen"),
        FR: ("pleurer", "dévasté", "brisé", "larmes"),
        ES: ("llorar", "devastado", "roto", "lágrimas"),
    }),
    Concept("\U0001F621", 0.70, {   # angry
        EN: ("angry", "furious", "raging", "mad"),
        NL: ("boos", "woedend", "razend"),
        DE: ("wütend", "sauer", "rasend"),
        FR: ("colère", "furieux", "énervé"),
        ES: ("enfadado", "furioso", "cabreado"),
    }),
    Concept("\U0001F4AA", 0.55, {   # strong
        EN: ("strong", "tough", "survived", "resilient"),
        NL: ("sterk", "zwaar", "overleefd"),
        DE: ("stark", "hart", "überlebt"),
        FR: ("fort", "dur", "survécu"),
        ES: ("fuerte", "duro", "sobrevivió"),
    }),
    Concept("\U0001F3AF", 0.60, {   # target
        EN: ("goal", "target", "focus", "exactly"),
        NL: ("doel", "focus", "precies"),
        DE: ("ziel", "fokus", "genau"),
        FR: ("objectif", "cible", "exactement"),
        ES: ("objetivo", "meta", "exactamente"),
    }),
    Concept("\U0001F511", 0.55, {   # key
        EN: ("key", "crucial", "essential", "critical"),
        NL: ("sleutel", "cruciaal", "essentieel"),
        DE: ("schlüssel", "entscheidend", "wesentlich"),
        FR: ("clé", "crucial", "essentiel"),
        ES: ("clave", "crucial", "esencial"),
    }),
    Concept("\U0001F440", 0.50, {   # eyes
        EN: ("watch", "look", "saw", "noticed"),
        NL: ("kijken", "zag", "gemerkt"),
        DE: ("schauen", "sah", "bemerkt"),
        FR: ("regarder", "vu", "remarqué"),
        ES: ("mirar", "vio", "notó"),
    }),
    Concept("\U0001F3B5", 0.50, {   # music
        EN: ("music", "song", "sound", "track"),
        NL: ("muziek", "liedje", "geluid"),
        DE: ("musik", "lied", "sound"),
        FR: ("musique", "chanson", "son"),
        ES: ("música", "canción", "sonido"),
    }),
    Concept("\U0001F3E0", 0.45, {   # house
        EN: ("home", "house", "apartment", "family"),
        NL: ("huis", "thuis", "appartement", "familie"),
        DE: ("haus", "zuhause", "wohnung", "familie"),
        FR: ("maison", "appartement", "famille"),
        ES: ("casa", "hogar", "piso", "familia"),
    }),
    Concept("\U0001F4BB", 0.50, {   # laptop
        EN: ("code", "software", "computer", "startup"),
        NL: ("code", "software", "computer", "startup"),
        DE: ("code", "software", "rechner", "startup"),
        FR: ("code", "logiciel", "ordinateur", "startup"),
        ES: ("código", "software", "ordenador", "startup"),
    }),
    Concept("\U0001F634", 0.45, {   # sleeping
        EN: ("sleep", "tired", "exhausted", "burnout"),
        NL: ("slapen", "moe", "uitgeput", "burn-out"),
        DE: ("schlafen", "müde", "erschöpft", "burnout"),
        FR: ("dormir", "fatigué", "épuisé"),
        ES: ("dormir", "cansado", "agotado"),
    }),
    Concept("\U0001F389", 0.55, {   # party
        EN: ("celebrate", "party", "congratulations", "birthday"),
        NL: ("vieren", "feest", "gefeliciteerd", "verjaardag"),
        DE: ("feiern", "party", "glückwunsch", "geburtstag"),
        FR: ("fêter", "fête", "félicitations", "anniversaire"),
        ES: ("celebrar", "fiesta", "felicidades", "cumpleaños"),
    }),
)


# A shared prefix this long is a stem match. Spanish and French inflect the
# *ending* of a verb, so neither token is a prefix of the other:
# `celebrando` / `celebrar` share only `celebra`. Five characters is long
# enough to avoid collisions (`money`/`month` share three) and short enough to
# catch real inflection.
MIN_SHARED_STEM = 5


def _elision_variants(token: str) -> list[str]:
    """Forms to try for a token, accounting for French elision.

    `l'argent` must match the `argent` trigger. The elided article is glued to
    the noun by the language's own orthography, so without this the emoji
    lexicon is blind to most French nouns in running speech.
    """
    variants = [token]
    for apostrophe in ("'", "’"):
        if apostrophe in token:
            tail = token.rsplit(apostrophe, 1)[-1]
            if tail:
                variants.append(tail)
    return variants


def _shared_prefix_len(a: str, b: str) -> int:
    count = 0
    for x, y in zip(a, b):
        if x != y:
            break
        count += 1
    return count


def _matches(token: str, triggers: Sequence[str]) -> bool:
    """Exact match, prefix match, or a shared-stem match for inflected forms."""
    for candidate in _elision_variants(token):
        for trigger in triggers:
            if candidate == trigger:
                return True
            if len(trigger) >= MIN_PREFIX and candidate.startswith(trigger):
                return True
            # German and Dutch compounds embed the trigger.
            if len(candidate) >= MIN_PREFIX and trigger.startswith(candidate):
                return True
            # Romance inflection changes the ending, so compare stems.
            shared = _shared_prefix_len(candidate, trigger)
            if shared >= MIN_SHARED_STEM and shared >= 0.6 * min(
                len(candidate), len(trigger)
            ):
                return True
    return False


def suggest(
    tokens: Sequence[str],
    language: Language,
    threshold: float = 0.5,
) -> tuple[int, str, float] | None:
    """Best emoji for a run of words.

    Returns `(token_index, emoji, weight)` for the single strongest concept
    found, or None. One suggestion per call by design — the caller applies at
    most one per cue.
    """
    normalised = [normalise_token(t) for t in tokens]
    best: tuple[int, str, float] | None = None

    for concept in LEXICON:
        if concept.weight < threshold:
            continue
        triggers = concept.triggers.get(language, ())
        if not triggers:
            continue
        for index, token in enumerate(normalised):
            if not token:
                continue
            if _matches(token, triggers):
                if best is None or concept.weight > best[2]:
                    best = (index, concept.emoji, concept.weight)
                break

    return best


def contains_emoji(text: str) -> bool:
    """Whether text already carries an emoji, so we do not add a second."""
    return any(ord(char) > 0x2100 for char in text)


def concepts_for(language: Language) -> Iterable[Concept]:
    return (c for c in LEXICON if language in c.triggers)


def lexicon_size() -> int:
    return len(LEXICON)
