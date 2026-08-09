"""Gameplay background demo.

    python demo/run_gameplay_demo.py                    # default composition
    python demo/run_gameplay_demo.py --game rocket_league
    python demo/run_gameplay_demo.py --all              # every bed compared
    python demo/run_gameplay_demo.py --camera           # camera path detail
    python demo/run_gameplay_demo.py --ffmpeg           # filtergraph + argv
    python demo/run_gameplay_demo.py --json

Runs offline. The speaker track below is synthetic: a person who sits mostly
still, drifts, leans in, and whose detector drops out for half a second —
which is what a real track looks like and what the camera has to survive.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from clipforge.gameplay import (  # noqa: E402
    Box,
    FaceSample,
    Game,
    GameplayAsset,
    GameplayConfig,
    GameplayEngine,
    Motion,
    PROFILES,
    SpeakerTrack,
    command,
    filtergraph,
    sendcmd_script,
)

DURATION = 28.0
WORD_COUNT = 78


def build_track(seed: int = 7) -> SpeakerTrack:
    """A synthetic but realistically nasty face track.

    Detector jitter, a slow drift, a lean-in, a dropout, and a second speaker
    taking over near the end. Anything that survives this survives a real one.
    """
    rng = random.Random(seed)
    samples: list[FaceSample] = []
    detector_fps = 10.0

    steps = int(DURATION * detector_fps)
    for index in range(steps):
        t = index / detector_fps

        # A detector blackout between 12.0s and 12.7s.
        if 12.0 <= t < 12.7:
            continue

        if t < 19.0:
            speaker = "host"
            base_x = 700 + 60 * math.sin(t / 5.0)      # slow drift
            base_y = 300 + 18 * math.sin(t / 3.1)
            size = 240 + (60 if t > 14.0 else 0)       # leans in at 14s
        else:
            # A second person takes over. The camera should cut, not pan.
            speaker = "guest"
            base_x = 1320 + 25 * math.sin(t / 4.0)
            base_y = 330
            size = 225

        jitter_x = rng.uniform(-6, 6)
        jitter_y = rng.uniform(-5, 5)
        jitter_s = rng.uniform(-8, 8)

        samples.append(
            FaceSample(
                t=t,
                box=Box(
                    x=base_x + jitter_x,
                    y=base_y + jitter_y,
                    width=(size + jitter_s) * 0.78,
                    height=size + jitter_s,
                ),
                confidence=rng.uniform(0.72, 0.98),
                speaker_id=speaker,
            )
        )

    return SpeakerTrack(samples=tuple(samples), source_width=1920,
                        source_height=1080, detector_fps=detector_fps)


LIBRARY: tuple[GameplayAsset, ...] = (
    GameplayAsset("ss-001", Game.SUBWAY_SURFERS, duration_s=190.0,
                  width=1080, height=1920, fps=60.0,
                  path="beds/subway_surfers_01.mp4",
                  loop_points=(4.0, 62.5, 121.0), lead_in_s=2.0),
    GameplayAsset("mc-004", Game.MINECRAFT_PARKOUR, duration_s=420.0,
                  width=1920, height=1080, fps=60.0,
                  path="beds/minecraft_parkour_04.mp4", lead_in_s=1.5),
    GameplayAsset("gta-002", Game.GTA_DRIVING, duration_s=95.0,
                  width=1920, height=1080, fps=30.0,
                  path="beds/gta_driving_02.mp4", lead_in_s=3.0),
    GameplayAsset("rl-007", Game.ROCKET_LEAGUE, duration_s=18.0,
                  width=2560, height=1440, fps=60.0,
                  path="beds/rocket_league_07.mp4"),
    GameplayAsset("sat-011", Game.SATISFYING, duration_s=240.0,
                  width=1440, height=1440, fps=30.0,
                  path="beds/satisfying_11.mp4",
                  loop_points=(0.0, 120.0), lead_in_s=0.0),
)

#: Speech spans, as the caption engine would supply them from word timings.
SPEECH: tuple[tuple[float, float], ...] = (
    (0.4, 6.2), (6.9, 11.8), (12.4, 18.6), (19.3, 24.1), (24.8, 27.6),
)


def show(plan, verbose: bool = False) -> None:
    speaker = plan.panel("speaker")
    gameplay = plan.panel("gameplay")
    stats = plan.stats

    print(f"  output      {plan.width}x{plan.height} @ {plan.fps}fps  "
          f"({plan.duration_s:.0f}s)")
    print(f"  style       {plan.style.value}"
          f"{'  bed=' + plan.game.value if plan.game else '  (no bed)'}")
    print(f"  speech      {stats['words_per_second']:.1f} words/sec"
          + (f"   bed salience {stats['salience']:.2f}"
             if stats["salience"] is not None else ""))

    print(f"  speaker     panel {speaker.width}x{speaker.height} at "
          f"y={speaker.y}   source crop {speaker.source_width}x"
          f"{speaker.source_height}")
    if gameplay:
        print(f"  gameplay    panel {gameplay.width}x{gameplay.height} at "
              f"y={gameplay.y}   {gameplay.scale_mode} from "
              f"{gameplay.source_width}x{gameplay.source_height}")

    x, y, w, h = plan.caption_zone
    print(f"  captions    {w}x{h} at ({x}, {y})")

    camera = plan.camera
    print(f"  camera      {camera.tracking}   {len(camera.keyframes)} keyframes  "
          f"{len(camera.cuts)} cuts  {camera.hold_ratio * 100:.0f}% held")
    if camera.cuts:
        print("              cuts at " +
              ", ".join(f"{c:.1f}s" for c in camera.cuts))

    if plan.timing:
        timing = plan.timing
        print(f"  bed         {timing.asset_id}  {len(timing.segments)} segment(s)  "
              f"{timing.loops} loop(s)  fps={timing.fps_conform}  "
              f"audio={timing.audio}")
        if verbose:
            for segment in timing.segments:
                print(f"              {segment.out_start:6.2f}-"
                      f"{segment.out_end:6.2f}s  from {segment.in_start:6.2f}s  "
                      f"seam={segment.seam}")

    print(f"  render      {stats['keyframes']} keyframes, "
          f"{stats['filtergraph_chains']} filter chains, "
          f"{plan.elapsed_ms}ms")

    if plan.warnings:
        print()
        for warning in plan.warnings:
            print(f"    ⚠ {warning}")


def show_camera(plan) -> None:
    print("\n  CAMERA PATH\n")
    print("      TIME   MOTION       X      Y")
    camera = plan.camera
    shown = camera.keyframes
    if len(shown) > 30:
        shown = shown[:14] + shown[-14:]
    previous_t = None
    for keyframe in shown:
        if previous_t is not None and keyframe.t < previous_t:
            print("       ...")
        marker = {Motion.CUT: "✂", Motion.PAN: "→", Motion.HOLD: "·"}[keyframe.motion]
        print(f"    {keyframe.t:7.3f}  {marker} {keyframe.motion.value:<8} "
              f"{keyframe.x:5d}  {keyframe.y:5d}")
        previous_t = keyframe.t
    if len(camera.keyframes) > 30:
        print(f"\n    ({len(camera.keyframes)} keyframes total, "
              f"{len(camera.keyframes) - 28} elided)")

    print(f"\n  A {plan.duration_s:.0f}s clip at {plan.fps}fps is "
          f"{int(plan.duration_s * plan.fps)} frames. The deadband collapses "
          f"them\n  to {len(camera.keyframes)} keyframes, which is what makes "
          f"the path executable\n  as a sendcmd script rather than a "
          f"per-frame expression.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", default=None,
                        choices=sorted(g.value for g in Game))
    parser.add_argument("--all", action="store_true",
                        help="compose against every bed and compare")
    parser.add_argument("--camera", action="store_true",
                        help="print the solved camera path")
    parser.add_argument("--ffmpeg", action="store_true",
                        help="print the filtergraph, camera script and argv")
    parser.add_argument("--no-track", action="store_true",
                        help="compose with no face track at all")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--words", type=int, default=WORD_COUNT)
    args = parser.parse_args()

    track = None if args.no_track else build_track()

    def build(game: Game | None):
        engine = GameplayEngine(GameplayConfig(game=game, seed="clip-8823",
                                               speaker_fps=30.0))
        return engine.compose(DURATION, track=track, assets=LIBRARY,
                              word_count=args.words, speech=SPEECH)

    if args.all:
        for game in Game:
            print(f"\n{'=' * 68}")
            print(f"  {PROFILES[game].label.upper()}")
            print(f"  {PROFILES[game].note}")
            print("=" * 68)
            show(build(game))
        print()
        return 0

    game = Game(args.game) if args.game else None
    plan = build(game)

    if args.json:
        print(json.dumps(plan.to_dict(), indent=2))
        return 0

    print()
    show(plan, verbose=True)

    if args.camera:
        show_camera(plan)

    if args.ffmpeg:
        print("\n  FILTERGRAPH\n")
        for line in filtergraph(plan).split("\n"):
            print(f"    {line}")
        print("\n  CAMERA SCRIPT (camera.cmd)\n")
        lines = sendcmd_script(plan).rstrip().split("\n")
        for line in lines[:10]:
            print(f"    {line}")
        if len(lines) > 10:
            print(f"    ... {len(lines) - 10} more")
        print("\n  ARGV\n")
        argv = command(plan, "speaker.mp4", "beds/bed.mp4", "out.mp4")
        rendered = []
        for token in argv:
            rendered.append(token if "\n" not in token else "<filtergraph>")
        print("    " + " ".join(
            t if " " not in t else f'"{t}"' for t in rendered
        ))

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
