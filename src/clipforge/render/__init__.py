"""Executing render plans: filtergraph in, finished MP4 out.

    from clipforge.render import RenderEngine, RenderConfig

    engine = RenderEngine(db, "ten_acme",
                          config=RenderConfig(workspace="/var/lib/clipforge/renders"))
    engine.enqueue(clip_id, plan, speaker_path,
                   gameplay_path=bed, subtitles=to_ass(track, style))
    engine.run(limit=2)

`gameplay.compose` decides the composition and `gameplay.render` turns it into
an ffmpeg filtergraph. This package is the part that runs it, measures what
came out, and records the asset — because ffmpeg exits zero on plenty of files
that are not what was asked for.
"""

from __future__ import annotations

from .engine import RENDER_JOB, RenderConfig, RenderEngine, verify_output
from .types import (
    FfmpegMissing,
    OutputRejected,
    RenderError,
    RenderFailed,
    RenderRequest,
    RenderResult,
    RenderState,
)

__all__ = [
    "RenderEngine",
    "RenderConfig",
    "RENDER_JOB",
    "verify_output",
    "RenderRequest",
    "RenderResult",
    "RenderState",
    "RenderError",
    "RenderFailed",
    "OutputRejected",
    "FfmpegMissing",
]
