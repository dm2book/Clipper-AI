#!/usr/bin/env python3
"""Render a real 1080x1920 60fps clip.

    python demo/run_render_demo.py                # compose and render
    python demo/run_render_demo.py --plan-only    # the filtergraph, no encode
    python demo/run_render_demo.py --captions     # burn subtitles in
    python demo/run_render_demo.py --seconds 6

Unlike the other demos this one needs **ffmpeg**, because it is the only one
that produces a file rather than a plan. Source media is synthesised with
ffmpeg's own generators, so nothing is downloaded and nothing is licensed.

`--plan-only` needs nothing installed and prints the graph the renderer would
execute, which is the useful half when the question is "what would this do?"
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from clipforge.acquire.mp4 import read_mp4  # noqa: E402
from clipforge.gameplay import compose  # noqa: E402
from clipforge.gameplay.render import filtergraph, link_check  # noqa: E402
from clipforge.gameplay.types import (  # noqa: E402
    Box,
    FaceSample,
    Game,
    GameplayAsset,
    SpeakerTrack,
)
from clipforge.render import RenderConfig, RenderEngine, RenderRequest  # noqa: E402
from clipforge.store import MemoryDatabase, TenantRecord  # noqa: E402

SPEAKER_W, SPEAKER_H = 1280, 720

CAPTIONS = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, Bold, Alignment, MarginV
Style: Default,Arial,72,&H00FFFFFF,&H00000000,-1,2,220

[Events]
Format: Layer, Start, End, Style, Text
Dialogue: 0,0:00:00.20,0:00:01.60,Default,HE LOST THE DEAL
Dialogue: 0,0:00:01.60,0:00:03.20,Default,IN ONE SENTENCE
"""


def _ffmpeg() -> str:
    return os.environ.get("CLIPFORGE_FFMPEG") or shutil.which("ffmpeg") or ""


def _track(seconds: float) -> SpeakerTrack:
    """A speaker drifting across frame, at the real source size.

    The source size matters: the camera crop is in source pixels, and a plan
    composed against the default 1920x1080 would ask for a window taller than
    a 720p frame. The renderer catches that, but the right fix is to compose
    with the truth.
    """

    samples = tuple(
        FaceSample(
            t=index / 10,
            box=Box(x=430 + index * 5, y=150, width=280, height=280),
            confidence=0.92,
        )
        for index in range(int(seconds * 10) + 1)
    )
    return SpeakerTrack(samples=samples, source_width=SPEAKER_W,
                        source_height=SPEAKER_H, detector_fps=10.0)


def _make_media(directory: str, seconds: float, ffmpeg: str) -> tuple[str, str]:
    speaker = os.path.join(directory, "speaker.mp4")
    gameplay = os.path.join(directory, "gameplay.mp4")
    print("  generating source media…")
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc2=size={SPEAKER_W}x{SPEAKER_H}:rate=30:duration={seconds + 1}",
         "-f", "lavfi", "-i", f"sine=frequency=320:duration={seconds + 1}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         speaker], check=True, capture_output=True)
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"smptebars=size=1080x1920:rate=30:duration={seconds + 1}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", gameplay],
        check=True, capture_output=True)
    return speaker, gameplay


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=4.0)
    parser.add_argument("--plan-only", action="store_true",
                        help="print the filtergraph and stop")
    parser.add_argument("--captions", action="store_true",
                        help="burn subtitles into the render")
    parser.add_argument("--out", default="", help="where to write the clip")
    parser.add_argument("--preset", default="veryfast",
                        help="x264 preset; the product ships 'medium'")
    args = parser.parse_args()

    assets = [GameplayAsset(asset_id="bed", game=Game.SUBWAY_SURFERS,
                            path="gameplay.mp4", duration_s=args.seconds,
                            width=1080, height=1920, fps=60)]
    plan = compose(args.seconds, track=_track(args.seconds), assets=assets,
                   game=Game.SUBWAY_SURFERS,
                   word_count=int(args.seconds * 3),
                   speech=[(0.2, args.seconds - 0.2)])

    print(f"\n  {plan.width}x{plan.height} @ {plan.fps}fps, "
          f"{plan.duration_s:.1f}s")
    print(f"  panels    {', '.join(p.name for p in plan.panels)}")
    print(f"  camera    {plan.camera.tracking}, "
          f"{len(plan.camera.keyframes)} keyframes, "
          f"{plan.camera.hold_ratio:.0%} held, {len(plan.camera.cuts)} cuts")
    for warning in plan.warnings:
        print(f"  warning   {warning}")

    if args.plan_only:
        graph = filtergraph(plan)
        problems = link_check(graph)
        print(f"\n  link check: {problems or 'clean'}")
        print(f"\n{graph}\n")
        return 0

    ffmpeg = _ffmpeg()
    if not ffmpeg:
        print("\n  ffmpeg not found. Install it, set CLIPFORGE_FFMPEG, or use "
              "--plan-only.\n")
        return 1

    workspace = tempfile.mkdtemp(prefix="clipforge-demo-")
    try:
        speaker, gameplay = _make_media(workspace, args.seconds, ffmpeg)

        database = MemoryDatabase()
        with database.unit_of_work("ten_demo") as uow:
            uow.tenants.save(TenantRecord(id="ten_demo", name="Demo"))

        engine = RenderEngine(
            database, "ten_demo",
            config=RenderConfig(workspace=workspace, ffmpeg=ffmpeg,
                                preset=args.preset),
        )
        output = args.out or os.path.join(os.getcwd(), "clipforge-demo.mp4")
        request = RenderRequest(
            render_id="rnd_demo", plan=plan, speaker_path=speaker,
            gameplay_path=gameplay, output_path=output, clip_id="cl_demo",
        )
        if args.captions:
            request.subtitles_path = os.path.join(workspace, "captions.ass")
            with open(request.subtitles_path, "w", encoding="utf-8") as handle:
                handle.write(CAPTIONS)

        print(f"  rendering with -preset {args.preset}…")
        result = engine.render(request)

        with open(result.output_path, "rb") as handle:
            info = read_mp4(handle)

        print(f"\n  wrote     {result.output_path}")
        print(f"  measured  {info.width}x{info.height} @ {info.fps}fps, "
              f"{info.duration_s:.2f}s, audio={info.has_audio}")
        print(f"  size      {result.size_bytes / 1e6:.1f} MB")
        print(f"  took      {result.elapsed_s:.1f}s "
              f"({result.realtime_ratio:.2f}x realtime)")
        print(f"  sha256    {result.checksum[:16]}…\n")
        return 0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
