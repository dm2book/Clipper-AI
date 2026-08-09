"""Niche profiles: what actually differs between a Cars channel and a History one.

A niche is not a label on a folder. It is a *coherent configuration across
every stage of the pipeline*, and the configurations genuinely conflict — a
setting that is right for Business is wrong for Cars in a way that shows up in
the finished video.

Three decisions carry most of the difference:

**Whether a gameplay bed helps at all.** The split-screen format exists to give
the eye something to do while someone talks. Cars, Luxury and Gaming footage is
already the visual — putting Subway Surfers under a Lamborghini clip does not
add retention, it competes with the only thing worth looking at. Those niches
get no bed. Motivation, Business, AI and History are talking heads, and there
the bed does its job.

**Which hook type lands.** A finance channel and a comedy channel have opposite
type rankings. Authority hooks work for Business and AI because the credential
*is* the draw; on a Gaming clip the same phrasing reads as pompous. This is a
prior, not a measurement — `HookSet.feature_rows()` exists so it can become one.

**Clip length.** History narration needs room to land; a Gaming fail needs
fifteen seconds and no more. Forcing one duration across seven niches produces
clips that are either truncated or padded, and both lose the viewer.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from ..gameplay.types import Game
from ..hooks.types import HookType
from ..publish.types import Platform


class Niche(str, enum.Enum):
    CARS = "cars"
    LUXURY = "luxury"
    MOTIVATION = "motivation"
    BUSINESS = "business"
    GAMING = "gaming"
    AI = "ai"
    HISTORY = "history"


class SourceKind(str, enum.Enum):
    """Where a niche's raw material comes from.

    Decides which detection engine runs: a podcast goes to the viral engine
    (transcript in, ranked moments out) while a livestream goes to the stream
    clipper (chat spikes in, lag-corrected windows out). Running the wrong one
    produces clips that are technically valid and consistently miss the moment.
    """

    PODCAST = "podcast"
    LONGFORM_VIDEO = "longform_video"
    LIVESTREAM = "livestream"
    OWNED_UPLOAD = "owned_upload"
    STOCK_LIBRARY = "stock_library"


@dataclass(frozen=True, slots=True)
class NicheProfile:
    """One niche's configuration across every stage."""

    niche: Niche
    label: str

    #: Viral/stream engine signals this niche should weight up. Names come
    #: from those engines' own vocabularies, so the profile composes with both.
    signals: tuple[str, ...]

    #: Hook types to prefer when ranking. A prior on what suits the audience.
    hook_types: tuple[HookType, ...]

    #: Caption preset. Grouping is the real difference between them: two words
    #: at a time reads as high-energy, six reads as editorial.
    caption_style: str

    #: Gameplay bed, or None when the source footage is already the visual.
    gameplay_bed: Game | None

    #: Preferred clip length, in seconds.
    duration_s: tuple[float, float]

    #: Where this niche's raw material comes from.
    source_kinds: tuple[SourceKind, ...]

    #: Posts per day per platform, before any cross-channel contention.
    cadence_per_day: int

    platforms: tuple[Platform, ...]

    #: Minimum virality score a moment needs to be worth publishing. Higher for
    #: niches where the audience is unforgiving of filler.
    quality_floor: float

    #: Vocabulary that signals a moment matters *in this domain*.
    #:
    #: The viral engine's detectors were built against founder and podcast
    #: material, and their lexicons show it: a Cars or History clip can be the
    #: best thirty seconds in an hour and register zero signal hits, because
    #: "horsepower" and "besieged" are not in a taxonomy built around funding
    #: rounds and arguments. Rather than widen the general detectors — which
    #: would make every niche noisier to fix three — each niche carries the
    #: words that mean something to it, and the factory re-ranks with them.
    domain_terms: tuple[str, ...] = ()

    language: str = "en"
    note: str = ""


PROFILES: dict[Niche, NicheProfile] = {
    Niche.CARS: NicheProfile(
        niche=Niche.CARS,
        label="Cars",
        signals=("success", "money", "secret", "emotional_spike"),
        hook_types=(HookType.SURPRISE, HookType.NUMBER, HookType.CURIOSITY),
        caption_style="punch",
        gameplay_bed=None,
        duration_s=(15.0, 30.0),
        source_kinds=(SourceKind.LONGFORM_VIDEO, SourceKind.OWNED_UPLOAD),
        cadence_per_day=2,
        platforms=(Platform.TIKTOK, Platform.INSTAGRAM, Platform.YOUTUBE),
        quality_floor=62.0,
        domain_terms=(
            "horsepower", "supercar", "engine", "torque", "handling", "track",
            "brakes", "grip", "chassis", "turbo", "manual", "steering",
            "scared", "fastest", "slower", "faster", "drove", "driving",
            "cost", "costs", "worth", "sedan", "straight", "road",
        ),
        note="The car is the visual. A gameplay bed competes with the only "
             "thing worth looking at.",
    ),
    Niche.LUXURY: NicheProfile(
        niche=Niche.LUXURY,
        label="Luxury",
        signals=("money", "success", "secret"),
        hook_types=(HookType.SURPRISE, HookType.CURIOSITY, HookType.SOCIAL_PROOF),
        caption_style="minimal",
        gameplay_bed=None,
        duration_s=(15.0, 25.0),
        source_kinds=(SourceKind.LONGFORM_VIDEO, SourceKind.STOCK_LIBRARY),
        cadence_per_day=2,
        platforms=(Platform.INSTAGRAM, Platform.TIKTOK),
        quality_floor=60.0,
        domain_terms=(
            "watch", "handmade", "finishing", "craft", "atelier", "movement",
            "gold", "platinum", "bespoke", "rare", "collection", "auction",
            "offensive", "expensive", "costs", "worth", "hours", "hand",
            "nobody", "stupidest", "paying",
        ),
        note="Minimal captions on purpose — heavy word-by-word animation "
             "reads as cheap against aspirational footage.",
    ),
    Niche.MOTIVATION: NicheProfile(
        niche=Niche.MOTIVATION,
        label="Motivation",
        signals=("emotional_spike", "failure", "success", "lesson"),
        hook_types=(HookType.AUTHORITY, HookType.FEAR, HookType.TRANSFORMATION),
        caption_style="punch",
        gameplay_bed=Game.SUBWAY_SURFERS,
        duration_s=(20.0, 35.0),
        source_kinds=(SourceKind.PODCAST, SourceKind.LONGFORM_VIDEO),
        cadence_per_day=3,
        platforms=(Platform.TIKTOK, Platform.INSTAGRAM, Platform.YOUTUBE),
        quality_floor=55.0,
        domain_terms=(
            "quit", "gave", "believed", "nobody", "everything", "changed",
            "hardest", "afraid", "terrified", "again", "kept", "stopped",
            "runway", "moment", "started", "believes",
        ),
        note="Talking head with sparse, emphatic delivery — the bed carries "
             "the pauses.",
    ),
    Niche.BUSINESS: NicheProfile(
        niche=Niche.BUSINESS,
        label="Business",
        signals=("money", "lesson", "failure", "secret", "controversy"),
        hook_types=(HookType.AUTHORITY, HookType.NUMBER, HookType.CURIOSITY),
        caption_style="karaoke",
        gameplay_bed=Game.SATISFYING,
        duration_s=(25.0, 45.0),
        source_kinds=(SourceKind.PODCAST, SourceKind.LONGFORM_VIDEO),
        cadence_per_day=2,
        platforms=(Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM),
        quality_floor=58.0,
        domain_terms=(
            "raise", "runway", "burn", "burned", "headcount", "hire", "hiring",
            "revenue", "margin", "churn", "bankrupt", "valuation", "investors",
            "board", "payroll", "progress", "company",
        ),
        note="Dense speech, so the lowest-salience bed: anything livelier "
             "competes with the information.",
    ),
    Niche.GAMING: NicheProfile(
        niche=Niche.GAMING,
        label="Gaming",
        signals=("rage", "funny", "fail", "win", "reaction"),
        hook_types=(HookType.SURPRISE, HookType.CURIOSITY, HookType.NEGATIVITY),
        caption_style="bounce",
        gameplay_bed=None,
        duration_s=(15.0, 30.0),
        source_kinds=(SourceKind.LIVESTREAM,),
        cadence_per_day=4,
        platforms=(Platform.TIKTOK, Platform.YOUTUBE),
        quality_floor=52.0,
        domain_terms=(
            "clutch", "whiffed", "throw", "threw", "choke", "insane", "cracked",
            "griefed", "rage", "tilt", "comeback", "ace", "wiped", "lobby",
        ),
        note="Routed to the stream clipper, not the viral engine — chat "
             "spikes find these moments, transcripts do not.",
    ),
    Niche.AI: NicheProfile(
        niche=Niche.AI,
        label="AI",
        signals=("secret", "lesson", "controversy", "money"),
        hook_types=(HookType.CURIOSITY, HookType.AUTHORITY, HookType.CONTROVERSY),
        caption_style="karaoke",
        gameplay_bed=Game.SATISFYING,
        duration_s=(25.0, 45.0),
        source_kinds=(SourceKind.PODCAST, SourceKind.LONGFORM_VIDEO),
        cadence_per_day=2,
        platforms=(Platform.YOUTUBE, Platform.TIKTOK),
        quality_floor=58.0,
        domain_terms=(
            "model", "models", "inference", "training", "tokens", "context",
            "benchmark", "prompt", "agents", "latency", "frontier", "spend",
            "measured", "cheaper", "percent", "quality",
        ),
        note="Same shape as Business: technical density punishes a busy bed.",
    ),
    Niche.HISTORY: NicheProfile(
        niche=Niche.HISTORY,
        label="History",
        signals=("secret", "lesson", "emotional_spike", "failure"),
        hook_types=(HookType.CURIOSITY, HookType.SURPRISE, HookType.SOCIAL_PROOF),
        caption_style="typewriter",
        gameplay_bed=Game.MINECRAFT_PARKOUR,
        duration_s=(30.0, 50.0),
        source_kinds=(SourceKind.PODCAST, SourceKind.LONGFORM_VIDEO,
                      SourceKind.STOCK_LIBRARY),
        cadence_per_day=1,
        platforms=(Platform.YOUTUBE, Platform.TIKTOK),
        quality_floor=60.0,
        domain_terms=(
            "war", "empire", "siege", "besieged", "treaty", "battle", "king",
            "queen", "revolution", "collapse", "century", "generals", "plans",
            "lives", "catastrophe", "unthinkable", "lesson", "learned",
        ),
        note="Narration needs room to land, so the longest clips and the "
             "slowest cadence of the seven.",
    ),
}


def profile(niche: Niche) -> NicheProfile:
    return PROFILES[niche]


def uses_stream_clipper(niche: Niche) -> bool:
    """Whether this niche's material comes from live chat rather than speech."""
    return SourceKind.LIVESTREAM in PROFILES[niche].source_kinds


def hook_preference(niche: Niche, hook_type: HookType) -> float:
    """Multiplier applied when re-ranking hooks for this niche.

    A gentle thumb on the scale rather than a filter: the hook engine's own
    features still decide, and a genuinely strong off-type hook should win.
    """
    preferred = PROFILES[niche].hook_types
    if hook_type in preferred:
        # First preference is worth more than third.
        return 1.15 - 0.04 * preferred.index(hook_type)
    return 0.92


def domain_affinity(niche: Niche, text: str) -> float:
    """How much this text reads as *this niche's* subject, 0.0 to 1.0.

    Used to re-rank the viral engine's output rather than to replace it. The
    generic detectors decide what is a strong moment; the niche decides which
    strong moment belongs on this channel. Keeping the two separate is what
    stops a Cars channel publishing a good clip about hiring.
    """
    terms = PROFILES[niche].domain_terms
    if not terms:
        return 0.0

    tokens = {
        token.strip(".,;:!?\"'()").lower() for token in text.split()
    }
    hits = sum(1 for term in terms if term in tokens)
    # Saturating: four domain words is as strong a signal as twelve.
    return min(1.0, hits / 4.0)


def target_duration(niche: Niche) -> float:
    low, high = PROFILES[niche].duration_s
    return (low + high) / 2.0


def all_niches() -> tuple[Niche, ...]:
    return tuple(PROFILES)
