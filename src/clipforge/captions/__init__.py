"""ClipForge AI — caption generation.

Word-level, animated, multilingual captions in the premium short-form style.

    from clipforge.captions import generate, to_ass

    track = generate(words, style="punch", language="de")
    open("captions.ass", "w").write(to_ass(track, track_style))

Supported languages: English, Dutch, German, French, Spanish. Each carries its
own line-breaking and typography rules — French non-breaking spaces and
elision, Spanish inverted punctuation, German and Dutch compound overflow — so
the output is correct in all five rather than correct in English and
approximately right elsewhere.
"""

from .animation import animate_cue, keyframes_for
from .emoji import LEXICON, suggest
from .engine import CaptionConfig, CaptionEngine, generate
from .export import to_ass, to_json, to_srt, to_vtt
from .languages import RULES, apply_typography, detect, rules_for
from .measure import shrink_to_fit, text_width
from .styles import (
    BOUNCE,
    KARAOKE,
    MINIMAL,
    PRESETS,
    PUNCH,
    SPEAKER_PALETTE,
    TYPEWRITER,
    CaptionStyle,
)
from .types import (
    Animation,
    Box,
    CaptionCue,
    CaptionLine,
    CaptionTrack,
    CaptionWord,
    CaseTransform,
    Keyframe,
    Language,
    TimedWord,
)

__all__ = [
    "Animation",
    "BOUNCE",
    "Box",
    "CaptionConfig",
    "CaptionCue",
    "CaptionEngine",
    "CaptionLine",
    "CaptionStyle",
    "CaptionTrack",
    "CaptionWord",
    "CaseTransform",
    "KARAOKE",
    "Keyframe",
    "LEXICON",
    "Language",
    "MINIMAL",
    "PRESETS",
    "PUNCH",
    "RULES",
    "SPEAKER_PALETTE",
    "TYPEWRITER",
    "TimedWord",
    "animate_cue",
    "apply_typography",
    "detect",
    "generate",
    "keyframes_for",
    "rules_for",
    "shrink_to_fit",
    "suggest",
    "text_width",
    "to_ass",
    "to_json",
    "to_srt",
    "to_vtt",
]
