"""ClipForge AI — gameplay backgrounds.

Composites a talking-head clip over a gameplay bed at 1080x1920, 60fps.

    from clipforge.gameplay import GameplayAsset, Game, SpeakerTrack, compose

    plan = compose(duration_s=28.0, track=track, assets=library,
                   game=Game.SUBWAY_SURFERS, word_count=84)
    print(plan.to_dict())

The engine emits a **render plan** plus the ffmpeg filtergraph and camera
script that execute it. It decodes no frames and detects no faces: the speaker
track comes from an upstream detector, exactly as the caption engine takes
word-level timings rather than inventing them.

What it does solve is the part that decides whether the output is publishable —
turning noisy, gappy, low-rate face detections into a camera path a human will
watch without noticing it. See `camera.py`.

**Salience is the design axis.** A gameplay bed exists to occupy the attention
that would otherwise scroll, not to compete with the speaker. The catalogue
rates each source and the engine matches it *inversely* to speech density:
fast talking gets a quiet floor. See `catalog.py`, which also carries the
per-game rights posture — recorded gameplay is someone else's copyrighted work
and the five sources do not sit under one policy.
"""

from .camera import OneEuroFilter, plan_crop_size, solve
from .catalog import GameProfile, PROFILES, profile, recommend, salience_warning
from .engine import GameplayConfig, GameplayEngine, compose
from .layout import caption_zone, choose_style, cover_source, speaker_share
from .render import command, filtergraph, link_check, sendcmd_script
from .timing import choose_offset, conform_mode, conform_note
from .timing import plan as plan_timing
from .types import (
    OUTPUT_FPS,
    OUTPUT_HEIGHT,
    OUTPUT_WIDTH,
    Box,
    CameraPath,
    CropKeyframe,
    FaceSample,
    Game,
    GameplayAsset,
    GameplayPlan,
    GameplayTiming,
    LayoutStyle,
    LoopSegment,
    Motion,
    Panel,
    SpeakerTrack,
)

__all__ = [
    "Box",
    "CameraPath",
    "CropKeyframe",
    "FaceSample",
    "Game",
    "GameProfile",
    "GameplayAsset",
    "GameplayConfig",
    "GameplayEngine",
    "GameplayPlan",
    "GameplayTiming",
    "LayoutStyle",
    "LoopSegment",
    "Motion",
    "OUTPUT_FPS",
    "OUTPUT_HEIGHT",
    "OUTPUT_WIDTH",
    "OneEuroFilter",
    "PROFILES",
    "Panel",
    "SpeakerTrack",
    "caption_zone",
    "choose_offset",
    "choose_style",
    "command",
    "compose",
    "conform_mode",
    "conform_note",
    "cover_source",
    "filtergraph",
    "link_check",
    "plan_crop_size",
    "plan_timing",
    "profile",
    "recommend",
    "salience_warning",
    "sendcmd_script",
    "solve",
    "speaker_share",
]
