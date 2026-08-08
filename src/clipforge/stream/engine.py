"""The stream clipper engine — orchestration.

    session (chat + events + optional transcript + scene regions)
      → classify chat, events, transcript into signal samples
      → find chat spikes against a rolling baseline
      → lag-correct spike onsets into anchors
      → merge anchors, rank by intensity
      → for each anchor, cut 15 / 30 / 45 / 60s variants
      → score every variant, plan its vertical layout
      → return the best cut per moment, plus every variant

Runs entirely offline: no model calls, no network. Chat is the signal.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Sequence

from . import anchors as anchor_mod
from . import layout as layout_mod
from . import scoring, signals as signals_mod
from .layout import Destination, LayoutStyle
from .types import (
    Anchor,
    ClipperResult,
    StreamClip,
    StreamSession,
    CLIP_DURATIONS_S,
)


@dataclass(slots=True)
class ClipperConfig:
    """Tuning surface for one clipping run."""

    # Which lengths to cut. All four by default.
    durations_s: tuple[int, ...] = CLIP_DURATIONS_S

    # How many distinct moments to return.
    max_moments: int = 10

    # Return only the best-scoring length per moment, rather than all four.
    # The full set is always available on `ClipperResult.clips`.
    best_variant_only: bool = True

    # Clips below this virality score are dropped.
    min_virality: int = 40

    # Where the clips are going — decides caption safe zones.
    destination: Destination = Destination.TIKTOK

    # Vertical composition. Inferred from the session's scene regions when None.
    layout_style: LayoutStyle | None = None

    # Burn a chat panel into the frame. Showing chat react is a large part of
    # why stream clips work, so this defaults on.
    include_chat_overlay: bool = True

    # Override the platform's default chat reaction lag, in milliseconds.
    # Worth tuning per channel: a streamer running low-latency mode with an
    # engaged chat can be a full two seconds faster than the platform default.
    reaction_lag_ms: int | None = None

    # Two clips overlapping by more than this are the same moment; the
    # lower-scoring one is dropped. Only applied when returning one cut per
    # moment — with `best_variant_only` off, the four lengths of a single
    # moment overlap by design and suppressing them would defeat the point.
    overlap_threshold: float = 0.4


class StreamClipperEngine:
    """Finds clippable moments in a recorded stream and cuts them vertically."""

    def __init__(self, config: ClipperConfig | None = None) -> None:
        self.config = config or ClipperConfig()

    def clip(self, session: StreamSession) -> ClipperResult:
        started = time.perf_counter()
        cfg = self.config

        if session.duration_ms <= 0 or (not session.chat and not session.events):
            return ClipperResult(
                session_id=session.session_id,
                clips=[],
                anchors=[],
                stats={
                    "reason": "no chat or events to analyse",
                    "weights_version": scoring.WEIGHTS_VERSION,
                },
            )

        # 1. Signals ---------------------------------------------------------
        samples = signals_mod.collect(session)

        # 2. Chat velocity and spikes ----------------------------------------
        counts = signals_mod.bucket_chat(session.chat, session.duration_ms)
        baseline = signals_mod.rolling_baseline(counts)
        spikes = signals_mod.find_spikes(counts, baseline)

        # 3. Anchors, lag-corrected ------------------------------------------
        found = anchor_mod.detect(session, samples, spikes, cfg.reaction_lag_ms)
        moments = found[: cfg.max_moments]

        # 4. Variants, scored -------------------------------------------------
        plan = layout_mod.plan(
            regions=session.regions,
            source_width=session.source_width,
            source_height=session.source_height,
            destination=cfg.destination,
            style=cfg.layout_style,
            include_chat=cfg.include_chat_overlay,
        )

        clips: list[StreamClip] = []
        for anchor in moments:
            variants = self._cut(session, anchor, plan)
            if not variants:
                continue
            keep = (
                [max(variants, key=lambda c: c.scores.virality)]
                if cfg.best_variant_only
                else variants
            )
            clips.extend(c for c in keep if c.scores.virality >= cfg.min_virality)

        clips.sort(key=lambda c: (-c.scores.virality, c.start_ms))
        if cfg.best_variant_only:
            # Distinct anchors can still land on overlapping windows — a long
            # reaction produces neighbouring spikes that survive anchor merging
            # but describe the same play. Suppress on the output as well.
            clips = _suppress_overlaps(clips, cfg.overlap_threshold)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return ClipperResult(
            session_id=session.session_id,
            clips=clips,
            anchors=moments,
            stats={
                "weights_version": scoring.WEIGHTS_VERSION,
                "platform": session.platform.value,
                "duration_ms": session.duration_ms,
                "chat_messages": len(session.chat),
                "events": len(session.events),
                "signal_samples": len(samples),
                "chat_spikes": len(spikes),
                "anchors": len(found),
                "reaction_lag_ms": anchor_mod.reaction_lag(
                    session.platform, cfg.reaction_lag_ms
                ),
                "layout": plan.name,
                "destination": cfg.destination.value,
                "returned": len(clips),
                "elapsed_ms": elapsed_ms,
            },
        )

    @staticmethod
    def _overlap(a: StreamClip, b: StreamClip) -> float:
        """Intersection-over-union on the time axis."""
        lo = max(a.start_ms, b.start_ms)
        hi = min(a.end_ms, b.end_ms)
        intersection = max(0, hi - lo)
        union = (a.end_ms - a.start_ms) + (b.end_ms - b.start_ms) - intersection
        return intersection / union if union > 0 else 0.0

    def _cut(
        self, session: StreamSession, anchor: Anchor, plan
    ) -> list[StreamClip]:
        """Every requested length for one anchor, each scored independently."""
        out: list[StreamClip] = []
        title = scoring.title_for(anchor)

        for duration_s, start_ms, end_ms in anchor_mod.variants(
            anchor, session, self.config.durations_s
        ):
            # A stream shorter than the requested clip cannot produce it.
            if end_ms - start_ms < duration_s * 1000:
                continue

            features = scoring.clip_features(
                anchor, duration_s, start_ms, end_ms, session.duration_ms
            )
            out.append(
                StreamClip(
                    session_id=session.session_id,
                    platform=session.platform,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    duration_s=duration_s,
                    anchor=anchor,
                    scores=scoring.score(anchor, duration_s, features),
                    layout=plan,
                    title=title,
                    signals=dict(anchor.signals),
                    features=features,
                )
            )
        return out


def _suppress_overlaps(
    clips: Sequence[StreamClip], threshold: float
) -> list[StreamClip]:
    """Greedy non-maximum suppression over clip time ranges.

    Input must already be sorted best-first, so the surviving cut of each
    moment is the highest-scoring one.
    """
    kept: list[StreamClip] = []
    for clip in clips:
        if any(
            StreamClipperEngine._overlap(clip, existing) > threshold
            for existing in kept
        ):
            continue
        kept.append(clip)
    return kept
