"""Laying the gameplay bed against the clip's timeline.

Three problems, none of which is "make the durations equal".

**Frame rate.** The output is 60fps. Gameplay is usually captured at 60 and
survives untouched; the speaker is usually 30 and has to be conformed. The
conform must be frame *duplication*, not motion interpolation — interpolating a
talking head smears the mouth on every plosive, which is far more visible than
the judder it was meant to remove. Interpolation is available and is never the
default.

**Looping.** A bed shorter than the clip repeats, and an arbitrary jump back to
zero is the clearest single tell that a video was machine-assembled. The engine
cannot find visually continuous loop points without decoding frames, so it uses
them when the asset declares them and marks the seam `visible` when it cannot.
Marking it is the point: a plan that quietly contains four hard cuts is worse
than one that says so.

**Where the seams land.** When a seam has to be visible, it should happen while
the speaker is *mid-sentence*, not during a pause. Attention sits on whichever
panel is doing something; during a pause it drifts down to the bed, which is
exactly when a discontinuity gets noticed. So seams are nudged into speech,
not away from it — the opposite of the intuitive placement.

**Reuse.** The same twenty seconds of Subway Surfers behind a creator's entire
library is noticed by their audience long before it is noticed by them. Start
offsets are chosen deterministically from a per-clip seed and steered away from
recently used ones, so a back catalogue spreads across the asset instead of
piling onto its first minute.
"""

from __future__ import annotations

import hashlib
import math
from typing import Sequence

from .types import GameplayAsset, GameplayTiming, LoopSegment, OUTPUT_FPS

#: A seam within this distance of a declared loop point is visually clean.
LOOP_POINT_TOLERANCE_S = 0.35

#: How far a seam may be moved to land inside a speech span.
MAX_SEAM_NUDGE_S = 1.5

#: Two clips whose start offsets are closer than this read as the same bed.
MIN_OFFSET_SEPARATION_S = 8.0

#: Candidate offsets tried before giving up on avoiding recent ones.
OFFSET_ATTEMPTS = 24


def _stable_unit(seed: str, salt: str = "") -> float:
    """A deterministic float in [0, 1) from a string.

    `hash()` is salted per process for str, so it cannot be used anywhere the
    same input must give the same plan across runs — which is the whole point
    of a cacheable render spec.
    """
    digest = hashlib.blake2b(f"{seed}|{salt}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / float(1 << 64)


def conform_mode(source_fps: float, output_fps: int = OUTPUT_FPS) -> str:
    """How to get `source_fps` onto the output timeline."""
    if abs(source_fps - output_fps) < 0.01:
        return "native"
    if source_fps > output_fps:
        return "decimate"
    return "duplicate"


def conform_note(source_fps: float, label: str, output_fps: int = OUTPUT_FPS) -> str:
    """A human-readable warning when a conform is lossy, else empty."""
    mode = conform_mode(source_fps, output_fps)
    if mode == "native":
        return ""
    if mode == "decimate":
        return (
            f"{label} is {source_fps:g}fps and will be decimated to "
            f"{output_fps} — no quality cost, but shutter cadence changes."
        )
    if not source_fps:
        return f"{label} reports no frame rate."

    ratio = output_fps / source_fps
    nearest = max(1, round(ratio))
    # Frames per second of output that the nearest whole-number duplication
    # fails to account for, and which the conform has to absorb by repeating
    # an extra frame now and then.
    drift = abs(output_fps - nearest * source_fps)

    if drift < 1e-6:
        return (
            f"{label} is {source_fps:g}fps, duplicated {nearest}x to "
            f"{output_fps}. Exact ratio, no judder introduced."
        )

    if drift < 0.5:
        # NTSC rates land here: 29.97 into 60 is 2.001x, not 2x. Close enough
        # to be invisible, not close enough to call clean.
        period = 1.0 / drift
        return (
            f"{label} is {source_fps:g}fps — near {nearest}x into "
            f"{output_fps}, but not exact: about one extra duplicated frame "
            f"every {period:.0f}s. Not visible, but it is not a clean ratio."
        )

    return (
        f"{label} is {source_fps:g}fps, which does not divide {output_fps} "
        f"evenly — duplication will produce uneven frame cadence "
        f"({drift:g} frames/sec unaccounted for)."
    )


def choose_offset(
    asset: GameplayAsset,
    needed_s: float,
    seed: str,
    recent_offsets: Sequence[float] = (),
) -> float:
    """A deterministic start offset inside the asset, avoiding recent ones."""
    lead_in = max(0.0, asset.lead_in_s)
    latest = asset.duration_s - needed_s
    if latest <= lead_in:
        return lead_in

    span = latest - lead_in
    best = lead_in + _stable_unit(seed) * span
    if not recent_offsets:
        return best

    def separation(candidate: float) -> float:
        return min(abs(candidate - used) for used in recent_offsets)

    best_gap = separation(best)
    for attempt in range(OFFSET_ATTEMPTS):
        if best_gap >= MIN_OFFSET_SEPARATION_S:
            break
        candidate = lead_in + _stable_unit(seed, f"retry{attempt}") * span
        gap = separation(candidate)
        if gap > best_gap:
            best, best_gap = candidate, gap

    return best


def _seam_quality(asset: GameplayAsset, resume_at: float) -> tuple[float, str]:
    """Where to resume after a seam, and whether that seam will be visible."""
    if not asset.loop_points:
        return max(0.0, asset.lead_in_s), "visible"

    nearest = min(asset.loop_points, key=lambda p: abs(p - resume_at))
    if abs(nearest - resume_at) <= LOOP_POINT_TOLERANCE_S:
        return nearest, "clean"
    # Resuming at a declared loop point is still better than an arbitrary
    # frame, even when it is not where the timeline wanted the seam.
    return asset.loop_points[0], "clean"


def _nudge_into_speech(
    seam: float, speech: Sequence[tuple[float, float]], budget: float
) -> float:
    """Move a seam into a speech span if one is close enough.

    A discontinuity in the bed is least noticeable while the speaker is
    talking, because attention is on the other panel.
    """
    if budget <= 0 or not speech:
        return seam

    for start, end in speech:
        if start <= seam <= end:
            return seam

    best = seam
    best_cost = math.inf
    for start, end in speech:
        if end - start < 0.25:
            continue
        # Aim a little inside the span, not at its edge.
        for target in (start + 0.3, (start + end) / 2.0, end - 0.3):
            cost = abs(target - seam)
            if cost <= budget and cost < best_cost:
                best, best_cost = target, cost
    return best


def plan(
    asset: GameplayAsset,
    duration_s: float,
    seed: str = "clip",
    speech: Sequence[tuple[float, float]] = (),
    recent_offsets: Sequence[float] = (),
    speaker_fps: float = 30.0,
    allow_interpolation: bool = False,
) -> GameplayTiming:
    """Lay `asset` across `duration_s` of output timeline."""
    notes: list[str] = []
    lead_in = max(0.0, asset.lead_in_s)
    usable = max(0.0, asset.duration_s - lead_in)

    if usable <= 0:
        return GameplayTiming(
            segments=(),
            asset_id=asset.asset_id,
            fps_conform=conform_mode(asset.fps),
            notes=("asset has no usable footage after its lead-in",),
        )

    bed_note = conform_note(asset.fps, f"Gameplay ({asset.game.value})")
    speaker_note = conform_note(speaker_fps, "Speaker source")
    if bed_note:
        notes.append(bed_note)
    if speaker_note:
        notes.append(speaker_note)

    fps_conform = conform_mode(asset.fps)
    if allow_interpolation and fps_conform == "duplicate":
        fps_conform = "interpolate"
        notes.append(
            "motion interpolation enabled — acceptable on gameplay, but never "
            "apply it to the speaker panel: it smears mouth shapes."
        )

    if asset.has_audio:
        notes.append(
            "gameplay audio muted — it competes with the speech, and the "
            "music on most gameplay beds carries its own separate claim."
        )

    # Single pass, no loop needed.
    if usable >= duration_s:
        offset = choose_offset(asset, duration_s, seed, recent_offsets)
        return GameplayTiming(
            segments=(LoopSegment(0.0, duration_s, offset, "none"),),
            asset_id=asset.asset_id,
            fps_conform=fps_conform,
            notes=tuple(notes),
        )

    # Looping. The first pass starts at the lead-in rather than a random
    # offset: with the asset already too short, a random start only shortens
    # the first segment and adds a seam.
    budget = min(MAX_SEAM_NUDGE_S, usable * 0.15)
    stride = max(0.5, usable - budget)
    count = math.ceil(duration_s / stride)

    segments: list[LoopSegment] = []
    cursor = 0.0
    resume = lead_in
    seam_kind = "none"

    for index in range(count):
        raw_end = min(duration_s, cursor + stride)
        if index == count - 1 or raw_end >= duration_s:
            end = duration_s
        else:
            end = _nudge_into_speech(raw_end, speech, budget)
            end = min(max(end, cursor + 0.5), duration_s)

        segments.append(LoopSegment(cursor, end, resume, seam_kind))
        if end >= duration_s:
            break

        resume, seam_kind = _seam_quality(asset, lead_in)
        cursor = end

    visible = sum(1 for s in segments if s.seam == "visible")
    if visible:
        notes.append(
            f"{visible} visible loop seam(s): the asset is "
            f"{asset.duration_s:.0f}s for a {duration_s:.0f}s clip and "
            f"declares no loop points. A longer bed removes them entirely."
        )
    if len(segments) > 3:
        notes.append(
            f"{len(segments)} passes over the same footage — the repetition "
            f"is noticeable well before the clip ends."
        )

    return GameplayTiming(
        segments=tuple(segments),
        asset_id=asset.asset_id,
        fps_conform=fps_conform,
        notes=tuple(notes),
    )
