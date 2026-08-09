"""What each gameplay source actually is, and how it must be handled.

The five are not interchangeable beds that differ only in texture. They differ
in native aspect ratio, in where the action sits in frame, and — the property
that decides whether the finished clip works — in how much attention they take
away from the person talking.

### Salience is the whole design axis

A gameplay background is not there to be watched. It is there to occupy the
fraction of attention that would otherwise notice it is bored and scroll. When
the bed is *more* interesting than the speaker, it stops being a floor and
becomes competition, and the viewer finishes the clip having absorbed nothing.

So salience is matched **inversely** to how dense the speech is. A rapid,
information-heavy explanation wants a low-salience bed (satisfying loops,
steady parkour). A slow, quiet, emotional clip can carry a high-salience one.
Rocket League behind a dense technical explanation is the pairing that reliably
produces a clip with good retention and zero recall.

### Rights

Recorded gameplay is someone else's copyrighted audiovisual work, and the five
sources here do not sit under one policy. Mojang's usage guidelines are
permissive about monetised Minecraft content; Psyonix/Epic publish a fan
content policy; Take-Two has historically been the most aggressive rightsholder
in this list. "Everyone does it" is a description of enforcement patterns, not
a licence. Ship a per-game rights posture and a first-party or licensed asset
library before this is a paid feature — see the rights section in
`docs/ARCHITECTURE.md`. The engine records which asset it used on every plan so
that an asset can be traced and pulled.
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import Game


@dataclass(frozen=True, slots=True)
class GameProfile:
    """Composition-relevant properties of one gameplay source."""

    game: Game
    label: str

    #: 0..1. How much attention the footage takes from the speaker. The single
    #: most important number here — see the module docstring.
    salience: float

    #: True when the source is shot vertically and needs no crop at all.
    native_vertical: bool

    #: Where the action lives horizontally, as a fraction of frame width.
    #: Used to place the crop window when the footage must be narrowed.
    action_center_x: float
    #: How wide the action spreads. Above `CROP_SAFE_SPREAD` a 9:16 crop
    #: throws away the part of the frame that makes the footage legible.
    action_spread: float

    #: Vertical position of the action, same convention.
    action_center_y: float

    #: Recommended share of canvas height when this is the gameplay panel.
    #: High-salience footage gets a smaller band; it does not need the room and
    #: giving it room costs the speaker.
    band: float

    note: str


#: Above this spread a centre crop to 9:16 loses the context that makes the
#: footage readable, and the planner fits (letterboxes) instead of cropping.
CROP_SAFE_SPREAD = 0.62


PROFILES: dict[Game, GameProfile] = {
    # Played in portrait on a phone. It is the only source in the list that is
    # already the output aspect ratio, which is a large part of why the format
    # standardised on it: no crop, no lost context, no decisions.
    Game.SUBWAY_SURFERS: GameProfile(
        game=Game.SUBWAY_SURFERS,
        label="Subway Surfers",
        salience=0.55,
        native_vertical=True,
        action_center_x=0.50,
        action_spread=0.45,
        action_center_y=0.62,
        band=0.38,
        note="Natively vertical — keeps full width in the band, losing only "
             "a vertical slice rather than the sides.",
    ),
    # Forward motion down a corridor. The action is centred and narrow, which
    # is the best case for a 9:16 crop of 16:9 footage.
    Game.MINECRAFT_PARKOUR: GameProfile(
        game=Game.MINECRAFT_PARKOUR,
        label="Minecraft Parkour",
        salience=0.40,
        native_vertical=False,
        action_center_x=0.50,
        action_spread=0.40,
        action_center_y=0.55,
        band=0.40,
        note="Centre-weighted forward motion; crops to 9:16 with little loss.",
    ),
    # Wide horizontal motion. Cropping to 9:16 removes the road ahead, which is
    # the only thing that makes driving footage legible as driving.
    Game.GTA_DRIVING: GameProfile(
        game=Game.GTA_DRIVING,
        label="GTA Driving",
        salience=0.72,
        native_vertical=False,
        action_center_x=0.50,
        action_spread=0.78,
        action_center_y=0.58,
        band=0.34,
        note="Wide horizontal action; fit rather than crop, or the road is gone.",
    ),
    # The ball is the subject and it crosses the full width of the pitch at
    # speed. A fixed 9:16 crop loses it several times a minute, and a crop that
    # chases it is a second moving camera fighting the first.
    Game.ROCKET_LEAGUE: GameProfile(
        game=Game.ROCKET_LEAGUE,
        label="Rocket League",
        salience=0.85,
        native_vertical=False,
        action_center_x=0.50,
        action_spread=0.88,
        action_center_y=0.52,
        band=0.32,
        note="Highest salience and worst crop candidate; fit, and keep the band small.",
    ),
    # Slow, loopable, low-information. The best bed for dense speech and the
    # one with the fewest rights problems, since much of it can be produced
    # first-party.
    Game.SATISFYING: GameProfile(
        game=Game.SATISFYING,
        label="Satisfying",
        salience=0.22,
        native_vertical=False,
        action_center_x=0.50,
        action_spread=0.50,
        action_center_y=0.50,
        band=0.42,
        note="Lowest salience; the correct default behind information-dense speech.",
    ),
}


def profile(game: Game) -> GameProfile:
    return PROFILES[game]


def crops_cleanly(game: Game) -> bool:
    """Whether 9:16 cropping preserves what makes this footage readable."""
    entry = PROFILES[game]
    return entry.native_vertical or entry.action_spread <= CROP_SAFE_SPREAD


#: Words per second above which speech is "dense" enough that a high-salience
#: bed measurably competes with it. Conversational delivery sits near 2.5;
#: rapid explanation runs past 3.5.
DENSE_SPEECH_WPS = 3.0
SPARSE_SPEECH_WPS = 1.8


def recommend(words_per_second: float) -> Game:
    """The bed that suits this speech density.

    Inverse matching, per the module docstring: the faster someone is talking,
    the less the floor underneath them is allowed to do.
    """
    if words_per_second >= DENSE_SPEECH_WPS:
        return Game.SATISFYING
    if words_per_second <= SPARSE_SPEECH_WPS:
        return Game.SUBWAY_SURFERS
    return Game.MINECRAFT_PARKOUR


def salience_warning(game: Game, words_per_second: float) -> str:
    """A warning when bed and speech density are fighting, else empty."""
    entry = PROFILES[game]
    if words_per_second >= DENSE_SPEECH_WPS and entry.salience >= 0.70:
        return (
            f"{entry.label} (salience {entry.salience:.2f}) behind "
            f"{words_per_second:.1f} words/sec — the bed competes with the "
            f"speech instead of supporting it. Consider "
            f"{PROFILES[recommend(words_per_second)].label}."
        )
    if words_per_second <= SPARSE_SPEECH_WPS and entry.salience <= 0.25:
        return (
            f"{entry.label} (salience {entry.salience:.2f}) behind "
            f"{words_per_second:.1f} words/sec — neither panel is holding "
            f"attention. A livelier bed would carry the quiet stretches."
        )
    return ""
