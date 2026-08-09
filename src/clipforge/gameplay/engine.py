"""Gameplay background engine — orchestration.

    clip (speaker source + optional face track + optional speech spans)
      → pick a bed, or honour the caller's choice
      → choose a layout from speech density
      → solve the speaker camera path
      → compose the 1080x1920 panels
      → lay the gameplay bed against the timeline
      → emit the plan, the filtergraph and the camera script

The engine composes with the rest of the system rather than duplicating it:
speech spans come from the caption engine's word timings, signals from the
viral or stream detectors, and platform safe zones from the stream clipper.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Sequence

from . import camera as camera_mod
from . import catalog, layout as layout_mod, render, timing as timing_mod
from ..stream.layout import Destination
from .types import (
    OUTPUT_FPS,
    OUTPUT_HEIGHT,
    OUTPUT_WIDTH,
    Game,
    GameplayAsset,
    GameplayPlan,
    LayoutStyle,
    SpeakerTrack,
)


@dataclass(slots=True)
class GameplayConfig:
    """Tuning surface for one composition."""

    #: Which bed to use. `None` lets the engine pick from speech density.
    game: Game | None = None

    #: Which layout to use. `None` derives it from the clip.
    style: LayoutStyle | None = None

    destination: Destination = Destination.TIKTOK

    #: Frame rate of the speaker source. Needed to report the conform
    #: honestly — a 29.97 source does not divide 60 evenly.
    speaker_fps: float = 30.0

    #: Seed for deterministic gameplay-offset selection. Use the clip id, so
    #: the same clip always renders identically and different clips do not
    #: land on the same twenty seconds of footage.
    seed: str = "clip"

    #: Start offsets recently used for this asset, to spread a back catalogue
    #: across the bed instead of piling it onto the first minute.
    recent_offsets: tuple[float, ...] = ()

    #: Off by default, and it should stay off for anything but gameplay.
    allow_interpolation: bool = False


class GameplayEngine:
    """Composes a speaker clip with a gameplay background."""

    def __init__(self, config: GameplayConfig | None = None) -> None:
        self.config = config or GameplayConfig()

    def compose(
        self,
        duration_s: float,
        track: SpeakerTrack | None = None,
        assets: Sequence[GameplayAsset] = (),
        word_count: int = 0,
        speech: Sequence[tuple[float, float]] = (),
    ) -> GameplayPlan:
        """Build the render plan for one clip.

        `assets` is the available library. The engine picks from it by game;
        an empty library produces a speaker-only plan rather than an error,
        because a clip with no bed is a worse clip, not a failed render.
        """
        started = time.perf_counter()
        cfg = self.config
        warnings: list[str] = []

        if duration_s <= 0:
            raise ValueError("duration_s must be positive")

        track = track or SpeakerTrack()
        words_per_second = word_count / duration_s if duration_s else 0.0

        asset = self._pick_asset(assets, words_per_second, warnings)
        game = asset.game if asset else None

        style = cfg.style or layout_mod.choose_style(
            words_per_second, not track.is_empty, game
        )
        if asset is None:
            style = LayoutStyle.SPEAKER_ONLY

        if game is not None:
            note = catalog.salience_warning(game, words_per_second)
            if note:
                warnings.append(note)
            if not catalog.crops_cleanly(game):
                warnings.append(
                    f"{catalog.profile(game).label} is fitted rather than "
                    f"cropped — {catalog.profile(game).note}"
                )

        # The camera needs the speaker panel's aspect ratio, and the panel
        # needs the camera's crop size. Resolve the aspect from the layout's
        # height share first, then solve, then compose for real.
        share = layout_mod.speaker_share(style, game)
        panel_h = max(1, int(OUTPUT_HEIGHT * share))
        aspect = OUTPUT_WIDTH / panel_h
        if style is LayoutStyle.INSET:
            aspect = layout_mod.INSET_WIDTH * OUTPUT_WIDTH / (panel_h or 1)

        path = camera_mod.solve(track, duration_s, aspect, OUTPUT_FPS)
        if path.tracking == "static" and not track.is_empty:
            warnings.append(
                "speaker track supplied but no detection cleared the "
                "confidence floor — framing fell back to a static crop"
            )
        warnings.extend(path.notes)

        panels, caption_band = layout_mod.compose(
            style, path.width, path.height, asset, cfg.destination
        )

        bed_timing = None
        if asset is not None:
            bed_timing = timing_mod.plan(
                asset,
                duration_s,
                seed=cfg.seed,
                speech=speech,
                recent_offsets=cfg.recent_offsets,
                speaker_fps=cfg.speaker_fps,
                allow_interpolation=cfg.allow_interpolation,
            )
            warnings.extend(bed_timing.notes)

        plan = GameplayPlan(
            width=OUTPUT_WIDTH,
            height=OUTPUT_HEIGHT,
            fps=OUTPUT_FPS,
            duration_s=duration_s,
            style=style,
            panels=panels,
            camera=path,
            timing=bed_timing,
            caption_zone=caption_band,
            game=game,
            warnings=tuple(dict.fromkeys(warnings)),
        )

        problems = render.link_check(render.filtergraph(plan))
        if problems:
            # A malformed graph is an engine bug, not a caller error, but
            # failing loudly beats handing the renderer something ffmpeg will
            # reject twenty minutes into a batch.
            raise AssertionError(
                "generated filtergraph is malformed: " + "; ".join(problems)
            )

        plan.stats = {
            "words_per_second": round(words_per_second, 2),
            "speaker_share": round(share, 3),
            "salience": (
                round(catalog.profile(game).salience, 2) if game else None
            ),
            "tracking": path.tracking,
            "speakers": len(track.speaker_ids),
            **render.summary(plan),
        }
        plan.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return plan

    def _pick_asset(
        self,
        assets: Sequence[GameplayAsset],
        words_per_second: float,
        warnings: list[str],
    ) -> GameplayAsset | None:
        if not assets:
            return None

        wanted = self.config.game or catalog.recommend(words_per_second)
        matching = [a for a in assets if a.game is wanted]
        if matching:
            # Longest first: a longer bed means fewer loop seams.
            return max(matching, key=lambda a: a.duration_s)

        if self.config.game is not None:
            warnings.append(
                f"no {catalog.profile(wanted).label} asset in the library — "
                f"fell back to {catalog.profile(assets[0].game).label}"
            )
        return max(assets, key=lambda a: a.duration_s)


def compose(
    duration_s: float,
    track: SpeakerTrack | None = None,
    assets: Sequence[GameplayAsset] = (),
    game: Game | None = None,
    word_count: int = 0,
    speech: Sequence[tuple[float, float]] = (),
    seed: str = "clip",
) -> GameplayPlan:
    """Convenience wrapper for the common case."""
    return GameplayEngine(GameplayConfig(game=game, seed=seed)).compose(
        duration_s, track=track, assets=assets,
        word_count=word_count, speech=speech,
    )
