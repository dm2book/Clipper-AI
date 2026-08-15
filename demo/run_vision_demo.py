#!/usr/bin/env python3
"""Face detection and automatic framing, end to end.

    python demo/run_vision_demo.py                 # builds its own fixtures
    python demo/run_vision_demo.py path/to.mp4     # your own file

Builds (or takes) a video, detects faces, tracks them into speakers, solves the
camera path, and prints what the framing would be — including the cases where
it deliberately gives up and says so.

Needs ffmpeg on PATH (or `CLIPFORGE_FFMPEG`) only for the built-in fixtures;
running it against your own file needs just opencv-python-headless.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from clipforge.gameplay import camera as camera_mod           # noqa: E402
from clipforge.vision import FaceTrackEngine                  # noqa: E402

BAR = "─" * 72


def show(engine: FaceTrackEngine, path: str, label: str) -> None:
    print(f"\n{BAR}\n{label}\n{BAR}")
    result = engine.track_video(path)

    detector = result.detector
    if detector is not None:
        line = f"  detector    {detector.name} {detector.version} on {detector.device}"
        print(line)
        if detector.device_note:
            print(f"              {detector.device_note}")
    print(f"  source      {result.source_width}x{result.source_height}, "
          f"{result.duration_s:.1f}s")
    print(f"  sampled     {result.frames_sampled} frames at "
          f"{result.sample_fps:g}fps in {result.elapsed_ms}ms")
    print(f"  detected    {result.frames_with_face} frames with a face "
          f"({result.detection_rate:.0%}), up to {result.max_simultaneous} "
          f"at once")

    if result.people:
        print("  speakers")
        for person in result.people:
            print(f"    {person.speaker_id:8} {person.first_t:5.1f}s → "
                  f"{person.last_t:5.1f}s  {person.samples:4d} samples  "
                  f"conf {person.mean_confidence:.2f}  "
                  f"activity {person.mean_activity:.2f}"
                  + (f"  gap {person.longest_gap_s:.1f}s"
                     if person.longest_gap_s > 0.15 else ""))

    if result.fallback:
        print(f"  FALLBACK    {result.fallback}")
    for note in result.notes:
        print(f"  note        {note}")

    solved = camera_mod.solve(
        result.track, result.duration_s or 1.0, 1080 / 1920, fps=30,
    )
    print(f"  camera      {solved.tracking}, {solved.width}x{solved.height} "
          f"crop, {len(solved.keyframes)} keyframes, "
          f"{len(solved.cuts)} cuts, still {solved.hold_ratio:.0%} of the time")
    if solved.cuts:
        print(f"              cuts at "
              f"{', '.join(f'{c:.1f}s' for c in solved.cuts)}")
    for note in solved.notes:
        print(f"              {note}")


def main() -> int:
    engine = FaceTrackEngine()
    available = engine.availability()
    print(f"detector: {'ready' if available.ready else 'UNAVAILABLE'} — "
          f"{available.detail}")
    if not available.ready:
        return 1

    if len(sys.argv) > 1:
        for path in sys.argv[1:]:
            if not os.path.isfile(path):
                print(f"no such file: {path}")
                return 1
            show(engine, path, os.path.basename(path))
        return 0

    try:
        from fixtures.faces import FFMPEG, FixtureCache
    except ImportError:
        print("built-in fixtures need the tests directory")
        return 1
    if not FFMPEG:
        print("building the demo fixtures needs ffmpeg — "
              "set CLIPFORGE_FFMPEG, or pass a video path")
        return 1

    labels = {
        "single_speaker": "One speaker, talking, swaying",
        "two_speakers": "Two speakers taking turns",
        "enter_exit": "A speaker who walks in and leaves",
        "occlusion": "A speaker hidden by a passing pillar",
        "no_faces": "Content with no faces in it at all",
    }
    with tempfile.TemporaryDirectory(prefix="clipforge-vision-demo-") as tmp:
        cache = FixtureCache(tmp)
        for name, label in labels.items():
            path, _scenario = cache.get(name)
            show(engine, path, label)

    print(f"\n{BAR}")
    print("Note: these fixtures are constructed. The primary speaker is a real")
    print("photograph moved along a known path; the second is drawn. What that")
    print("proves is the pipeline and the tracker, not detection rates on real")
    print("footage — see the README.")
    engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
