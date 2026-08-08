"""Emote and slang taxonomy — what chat is actually saying.

Chat is the strongest signal available for stream content: thousands of people
labelling the interesting moments in real time, for free. But chat does not
speak English. It speaks emotes, and the mapping from emote to meaning is the
part a generic text classifier cannot do.

Three vocabularies are handled:

  named emotes  Twitch/BTTV/FFZ/7TV tokens (LUL, Sadge, monkaS). Case-sensitive
                on the wire, but chatters type variants, so matching is
                case-insensitive with a normalisation pass.
  emoji         Dominant on YouTube Live and heavily used on Kick.
  slang         Bare tokens that carry meaning in stream chat and nowhere else:
                W, L, F, COPE, RATIO, EZ.

Coverage is deliberately broad rather than exhaustive: emote culture drifts
fast, so the design goal is that an unknown emote costs recall on one signal
rather than breaking classification. Unknown tokens still contribute to raw
message velocity, which is signal-agnostic.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Mapping

from .types import Platform, StreamSignal

# Weight = how strongly this token implies the signal, 0..1.
EmoteWeights = Mapping[StreamSignal, float]

# --- The bank ----------------------------------------------------------------
#
# Several tokens are deliberately multi-signal. OMEGALUL is laughter *at* a
# failure; treating it as purely funny loses the fail, and treating it as
# purely fail loses why the clip works. Chat is genuinely ambiguous here and
# the scorer is built to carry that ambiguity rather than resolve it early.

_BANK: dict[str, EmoteWeights] = {
    # -- laughter -------------------------------------------------------------
    "lul": {StreamSignal.FUNNY: 0.85},
    "lulw": {StreamSignal.FUNNY: 0.85},
    "omegalul": {StreamSignal.FUNNY: 0.95, StreamSignal.FAIL: 0.35},
    "kekw": {StreamSignal.FUNNY: 0.95},
    "kekl": {StreamSignal.FUNNY: 0.85},
    "kek": {StreamSignal.FUNNY: 0.70},
    "pepelaugh": {StreamSignal.FUNNY: 0.80, StreamSignal.REACTION: 0.30},
    "icant": {StreamSignal.FUNNY: 0.85},
    "4head": {StreamSignal.FUNNY: 0.55},
    "xdd": {StreamSignal.FUNNY: 0.70},
    "lmao": {StreamSignal.FUNNY: 0.75},
    "lmfao": {StreamSignal.FUNNY: 0.80},
    "haha": {StreamSignal.FUNNY: 0.50},

    # -- hype / wins ----------------------------------------------------------
    "pogchamp": {StreamSignal.WIN: 0.75, StreamSignal.REACTION: 0.55},
    "poggers": {StreamSignal.WIN: 0.80, StreamSignal.REACTION: 0.55},
    "pog": {StreamSignal.WIN: 0.70, StreamSignal.REACTION: 0.50},
    "pogu": {StreamSignal.WIN: 0.75, StreamSignal.REACTION: 0.55},
    "poggies": {StreamSignal.WIN: 0.75},
    "ez": {StreamSignal.WIN: 0.70},
    "ezclap": {StreamSignal.WIN: 0.85},
    "gigachad": {StreamSignal.WIN: 0.70},
    "letsgo": {StreamSignal.WIN: 0.80, StreamSignal.REACTION: 0.40},
    "letsgoo": {StreamSignal.WIN: 0.80},
    "gg": {StreamSignal.WIN: 0.55},
    "clutch": {StreamSignal.WIN: 0.85},
    "insane": {StreamSignal.WIN: 0.45, StreamSignal.REACTION: 0.45},
    "w": {StreamSignal.WIN: 0.60},

    # -- fails ----------------------------------------------------------------
    "sadge": {StreamSignal.FAIL: 0.55, StreamSignal.EMOTIONAL: 0.65},
    "l": {StreamSignal.FAIL: 0.60},
    "throw": {StreamSignal.FAIL: 0.70},
    "thrown": {StreamSignal.FAIL: 0.75},
    "choke": {StreamSignal.FAIL: 0.75},
    "choked": {StreamSignal.FAIL: 0.80},
    "residentsleeper": {StreamSignal.FAIL: 0.40},
    "yikes": {StreamSignal.FAIL: 0.50, StreamSignal.REACTION: 0.35},
    "oof": {StreamSignal.FAIL: 0.60},
    "rip": {StreamSignal.FAIL: 0.55},
    "f": {StreamSignal.FAIL: 0.50, StreamSignal.EMOTIONAL: 0.30},

    # -- rage -----------------------------------------------------------------
    "malding": {StreamSignal.RAGE: 0.95},
    "madge": {StreamSignal.RAGE: 0.85},
    "mald": {StreamSignal.RAGE: 0.90},
    "peperage": {StreamSignal.RAGE: 0.90},
    "ragey": {StreamSignal.RAGE: 0.85},
    "tilted": {StreamSignal.RAGE: 0.85},
    "tilt": {StreamSignal.RAGE: 0.70},
    "rage": {StreamSignal.RAGE: 0.80},
    "seethe": {StreamSignal.RAGE: 0.80},
    "seething": {StreamSignal.RAGE: 0.85},
    "pepega": {StreamSignal.RAGE: 0.30, StreamSignal.FUNNY: 0.50},

    # -- tension / reaction ---------------------------------------------------
    "monkas": {StreamSignal.REACTION: 0.80},
    "monkaw": {StreamSignal.REACTION: 0.85},
    "monkahmm": {StreamSignal.REACTION: 0.55},
    "pausechamp": {StreamSignal.REACTION: 0.70},
    "eyes": {StreamSignal.REACTION: 0.55},
    "wtf": {StreamSignal.REACTION: 0.75},
    "huh": {StreamSignal.REACTION: 0.60},
    "weirdchamp": {StreamSignal.REACTION: 0.60, StreamSignal.ARGUMENT: 0.25},
    "wut": {StreamSignal.REACTION: 0.60},
    "omg": {StreamSignal.REACTION: 0.65},
    "no way": {StreamSignal.REACTION: 0.70},
    "noway": {StreamSignal.REACTION: 0.70},
    "actually": {StreamSignal.REACTION: 0.25},

    # -- emotional ------------------------------------------------------------
    "pepehands": {StreamSignal.EMOTIONAL: 0.90},
    "feelsbadman": {StreamSignal.EMOTIONAL: 0.80},
    "widepeeposad": {StreamSignal.EMOTIONAL: 0.85},
    "feelsstrongman": {StreamSignal.EMOTIONAL: 0.80},
    "peeposad": {StreamSignal.EMOTIONAL: 0.85},
    "sad": {StreamSignal.EMOTIONAL: 0.50},
    "crying": {StreamSignal.EMOTIONAL: 0.70},
    "wholesome": {StreamSignal.EMOTIONAL: 0.70},
    "respect": {StreamSignal.EMOTIONAL: 0.55},

    # -- argument / discourse -------------------------------------------------
    "cope": {StreamSignal.ARGUMENT: 0.75},
    "copium": {StreamSignal.ARGUMENT: 0.70, StreamSignal.EMOTIONAL: 0.25},
    "ratio": {StreamSignal.ARGUMENT: 0.80},
    "clueless": {StreamSignal.ARGUMENT: 0.60},
    "erm": {StreamSignal.ARGUMENT: 0.40},
    "based": {StreamSignal.ARGUMENT: 0.35},
    "cringe": {StreamSignal.ARGUMENT: 0.45},
    "touchgrass": {StreamSignal.ARGUMENT: 0.55},
    "wrong": {StreamSignal.ARGUMENT: 0.40},
    "debate": {StreamSignal.ARGUMENT: 0.60},
}

# Emoji carry most of the load on YouTube Live and a growing share on Kick.
_EMOJI: dict[str, EmoteWeights] = {
    "\U0001F602": {StreamSignal.FUNNY: 0.85},   # face with tears of joy
    "\U0001F923": {StreamSignal.FUNNY: 0.90},   # rolling on the floor laughing
    "\U0001F480": {StreamSignal.FUNNY: 0.80},   # skull — "I'm dead"
    "☠": {StreamSignal.FUNNY: 0.70},       # skull and crossbones
    "\U0001F62D": {StreamSignal.FUNNY: 0.45, StreamSignal.EMOTIONAL: 0.55},
    "\U0001F621": {StreamSignal.RAGE: 0.85},    # pouting face
    "\U0001F620": {StreamSignal.RAGE: 0.75},    # angry face
    "\U0001F92C": {StreamSignal.RAGE: 0.90},    # face with symbols on mouth
    "\U0001F525": {StreamSignal.WIN: 0.65},     # fire
    "\U0001F44F": {StreamSignal.WIN: 0.60},     # clapping
    "\U0001F3C6": {StreamSignal.WIN: 0.80},     # trophy
    "\U0001F4AA": {StreamSignal.WIN: 0.55},     # flexed biceps
    "\U0001F62E": {StreamSignal.REACTION: 0.65},  # face with open mouth
    "\U0001F628": {StreamSignal.REACTION: 0.70},  # fearful face
    "\U0001F440": {StreamSignal.REACTION: 0.55},  # eyes
    "❓": {StreamSignal.REACTION: 0.50},    # question mark
    "\U0001F614": {StreamSignal.EMOTIONAL: 0.65},  # pensive
    "\U0001F97A": {StreamSignal.EMOTIONAL: 0.75},  # pleading face
    "❤": {StreamSignal.EMOTIONAL: 0.55},   # heart
    "\U0001F615": {StreamSignal.FAIL: 0.40},    # confused face
}

# Platform-specific aliases folded onto canonical tokens. Kick and YouTube
# inherited Twitch's emote culture but not its emote names.
_ALIASES: dict[Platform, dict[str, str]] = {
    Platform.KICK: {
        "kekw": "kekw",
        "lulw": "lul",
        "emotesorrow": "pepehands",
        "kappa": "erm",
    },
    Platform.YOUTUBE_LIVE: {
        "poggers": "poggers",
        "lulw": "lul",
        "kekw": "kekw",
    },
    Platform.TWITCH: {},
}

# Repeated characters are how chat conveys intensity: "LULLLL", "POGGGG",
# "WHATTTT". Collapse them so the token matches, but count the stretch as
# amplitude — a 12-character LUL is louder than a 3-character one.
_REPEAT = re.compile(r"(.)\1{2,}")
_TOKEN = re.compile(r"[A-Za-z0-9_]+")

# Bare-letter slang (W, L, F) is only meaningful standing alone. Inside a
# sentence a stray "w" is a typo, not a win, so single characters are matched
# only when they are the whole message.
_STANDALONE_ONLY = {"w", "l", "f"}


def normalise(token: str) -> tuple[str, float]:
    """Fold a chat token to its canonical form and measure its stretch.

    Returns `(canonical, amplitude)` where amplitude is 1.0 for a plain token
    and rises toward 1.6 for heavily stretched ones.
    """
    lowered = token.lower()
    collapsed = _REPEAT.sub(r"\1", lowered)
    stretch = len(lowered) - len(collapsed)
    amplitude = 1.0 + min(0.6, stretch * 0.06)
    return collapsed, amplitude


def _iter_emoji(text: str) -> Iterable[str]:
    """Yield emoji characters, stripping variation selectors and skin tones."""
    for char in text:
        if char in ("️", "︎") or "\U0001F3FB" <= char <= "\U0001F3FF":
            continue
        category = unicodedata.category(char)
        if category in ("So", "Sk") or ord(char) > 0x1F000:
            yield char


def classify_message(
    text: str,
    emotes: tuple[str, ...] = (),
    platform: Platform = Platform.TWITCH,
) -> dict[StreamSignal, float]:
    """Map one chat message to signal strengths.

    Structured emote metadata (`emotes`) is trusted over text scraping when the
    platform provides it — Twitch and YouTube both do, and it avoids false
    positives from users typing an emote name in prose.
    """
    aliases = _ALIASES.get(platform, {})
    found: dict[StreamSignal, list[float]] = {}

    def add(weights: EmoteWeights, amplitude: float) -> None:
        for signal, weight in weights.items():
            found.setdefault(signal, []).append(min(1.0, weight * amplitude))

    for emote in emotes:
        canonical, amplitude = normalise(emote)
        canonical = aliases.get(canonical, canonical)
        if canonical in _BANK:
            add(_BANK[canonical], amplitude)

    tokens = _TOKEN.findall(text)
    single_token_message = len(tokens) == 1
    for token in tokens:
        canonical, amplitude = normalise(token)
        canonical = aliases.get(canonical, canonical)
        if canonical in _STANDALONE_ONLY and not single_token_message:
            continue
        if canonical in _BANK:
            add(_BANK[canonical], amplitude)

    for char in _iter_emoji(text):
        if char in _EMOJI:
            add(_EMOJI[char], 1.0)

    # A message in caps is shouting, which lifts whatever it already carries.
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 4 and all(c.isupper() for c in letters):
        found = {s: [min(1.0, v * 1.15) for v in vs] for s, vs in found.items()}

    from .types import saturating_sum

    return {signal: saturating_sum(values) for signal, values in found.items()}


def is_known(token: str, platform: Platform = Platform.TWITCH) -> bool:
    """Whether a token carries meaning in the taxonomy. Used for diagnostics."""
    canonical, _ = normalise(token)
    canonical = _ALIASES.get(platform, {}).get(canonical, canonical)
    return canonical in _BANK


def vocabulary_size() -> int:
    return len(_BANK) + len(_EMOJI)
