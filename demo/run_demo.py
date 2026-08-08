"""Run the viral detection engine over the sample transcript.

    python demo/run_demo.py            # heuristics only, no network
    python demo/run_demo.py --llm      # enable the two-tier LLM cascade
    python demo/run_demo.py --json     # machine-readable output

The `--llm` path needs the `anthropic` package and credentials in the
environment. Everything else runs offline.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from clipforge.viral import ViralConfig, ViralDetectionEngine, load_json  # noqa: E402

SAMPLE = Path(__file__).parent / "sample_transcript.json"


def timestamp(ms: int) -> str:
    total = ms // 1000
    return f"{total // 60:d}:{total % 60:02d}"


def bar(value: int, width: int = 20) -> str:
    filled = round(value / 100 * width)
    return "█" * filled + "·" * (width - filled)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llm", action="store_true", help="enable the LLM cascade")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--clips", type=int, default=6, help="how many clips to return")
    parser.add_argument("--transcript", type=Path, default=SAMPLE)
    args = parser.parse_args()

    transcript = load_json(args.transcript)
    config = ViralConfig(max_clips=args.clips)

    if args.llm:
        from clipforge.viral import build_default_judges

        config.triage_judge, config.deep_judge = build_default_judges()

    result = ViralDetectionEngine(config).detect(transcript)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0

    stats = result.stats
    print(f"\n  source        {result.source_id}")
    print(f"  duration      {timestamp(stats['duration_ms'])}  ({stats['utterances']} utterances)")
    print(f"  signal hits   {stats['signal_hits']}")
    print(f"  candidates    {stats['candidates']}  → {stats['deduped']} after dedupe")
    print(f"  llm verdicts  {stats['llm_verdicts']}")
    print(f"  elapsed       {stats['elapsed_ms']} ms")
    print(f"  weights       {stats['weights_version']}")

    if not result.top:
        print("\n  No clips cleared the virality threshold.\n")
        return 0

    print(f"\n  TOP {len(result.top)} CLIPS\n")
    for rank, moment in enumerate(result.top, start=1):
        s = moment.scores
        span = f"{timestamp(moment.start_ms)}–{timestamp(moment.end_ms)}"
        signals = ", ".join(
            sig.value for sig, v in sorted(moment.signals.items(), key=lambda kv: -kv[1])[:4] if v > 0
        )
        print(f"  {rank}. [{s.virality:3d}] {moment.title}")
        print(f"      {span}  ({moment.candidate.duration_s:.0f}s)   {signals or 'no categorical signal'}")
        print(f"      retention  {bar(s.retention)} {s.retention:3d}    "
              f"engagement {bar(s.engagement)} {s.engagement:3d}")
        print(f"      comment    {bar(s.comment)} {s.comment:3d}    "
              f"share      {bar(s.share)} {s.share:3d}")
        print(f"      {moment.rationale}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
