"""Run the stream clipper over the sample Twitch VOD.

    python demo/run_stream_demo.py              # best cut per moment
    python demo/run_stream_demo.py --all        # every 15/30/45/60s variant
    python demo/run_stream_demo.py --json       # machine-readable output
    python demo/run_stream_demo.py --dest reels # different caption safe zones

The fixture records where each moment was scripted, so the demo also reports
how close the lag-corrected anchors landed to the truth.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from clipforge.stream import (  # noqa: E402
    ClipperConfig,
    Destination,
    Platform,
    StreamClipperEngine,
    VideoRegion,
    build_session,
)

SAMPLE = Path(__file__).parent / "sample_stream.json"


def timestamp(ms: int) -> str:
    total = ms // 1000
    return f"{total // 60:d}:{total % 60:02d}"


def bar(value: int, width: int = 18) -> str:
    return "█" * round(value / 100 * width) + "·" * (width - round(value / 100 * width))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="show every duration")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dest", default="tiktok", choices=[d.value for d in Destination])
    parser.add_argument("--no-chat", action="store_true", help="drop the chat overlay")
    parser.add_argument("--source", type=Path, default=SAMPLE)
    args = parser.parse_args()

    raw = json.loads(args.source.read_text())
    session = build_session(
        session_id=raw["session_id"],
        platform=Platform(raw["platform"]),
        duration_ms=raw["duration_ms"],
        raw_chat=raw["chat"],
        raw_events=raw["events"],
        regions=[VideoRegion(**r) for r in raw.get("regions", ())],
        source_width=raw.get("source_width", 1920),
        source_height=raw.get("source_height", 1080),
    )

    config = ClipperConfig(
        best_variant_only=not args.all,
        destination=Destination(args.dest),
        include_chat_overlay=not args.no_chat,
    )
    result = StreamClipperEngine(config).clip(session)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0

    stats = result.stats
    print(f"\n  session       {result.session_id}  ({stats['platform']})")
    print(f"  duration      {timestamp(stats['duration_ms'])}")
    print(f"  chat          {stats['chat_messages']} messages, {stats['events']} events")
    print(f"  spikes        {stats['chat_spikes']} → {stats['anchors']} anchors")
    print(f"  lag applied   −{stats['reaction_lag_ms'] / 1000:.1f}s (platform default)")
    print(f"  layout        {stats['layout']} → {stats['destination']}")
    print(f"  elapsed       {stats['elapsed_ms']} ms")

    truth = raw.get("ground_truth_moments", [])
    if truth:
        print("\n  ANCHOR ACCURACY vs scripted moments\n")
        for entry in truth:
            expected_ms = entry["offset_s"] * 1000
            nearest = min(
                result.anchors, key=lambda a: abs(a.offset_ms - expected_ms), default=None
            )
            if nearest is None:
                print(f"    {entry['label']:<10} {timestamp(expected_ms)}   MISSED")
                continue
            delta = (nearest.offset_ms - expected_ms) / 1000
            hit = "✓" if abs(delta) <= 3.0 else "~" if abs(delta) <= 6.0 else "✗"
            print(
                f"    {hit} {entry['label']:<10} scripted {timestamp(expected_ms)}   "
                f"detected {timestamp(nearest.offset_ms)}   Δ {delta:+.1f}s"
            )

    if not result.clips:
        print("\n  No clips cleared the threshold.\n")
        return 0

    heading = "ALL VARIANTS" if args.all else "BEST CUT PER MOMENT"
    print(f"\n  {heading} — {len(result.clips)} clips\n")
    for clip in result.clips:
        s = clip.scores
        span = f"{timestamp(clip.start_ms)}–{timestamp(clip.end_ms)}"
        sig = ", ".join(
            k.value for k, v in sorted(clip.signals.items(), key=lambda kv: -kv[1])[:3] if v > 0
        )
        print(f"  [{s.virality:3d}] {clip.duration_s:>2}s  {span}   {clip.title}")
        print(f"        {sig}   ·   moment lands {clip.anchor_position:.0%} in")
        print(f"        hype {bar(s.hype)} {s.hype:3d}   "
              f"retention {bar(s.retention)} {s.retention:3d}   "
              f"clarity {bar(s.clarity)} {s.clarity:3d}")
        print()

    example = result.clips[0].layout
    print(f"  VERTICAL LAYOUT  ({example.name}, {example.width}x{example.height})")
    for crop in example.crops:
        src = crop.source
        print(f"    {src.name:<10} src {src.width}x{src.height}+{src.x}+{src.y}"
              f"  →  dest {crop.dest_width}x{crop.dest_height}+{crop.dest_x}+{crop.dest_y}")
    cz = example.caption_zone
    print(f"    captions   {cz[2]}x{cz[3]}+{cz[0]}+{cz[1]}  (clear of {args.dest} chrome)")
    if example.chat_overlay:
        co = example.chat_overlay
        print(f"    chat       {co[2]}x{co[3]}+{co[0]}+{co[1]}")
    print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
