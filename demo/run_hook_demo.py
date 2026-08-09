"""Hook generator demo.

    python demo/run_hook_demo.py                 # 20 ranked hooks
    python demo/run_hook_demo.py --clip stream   # a different sample clip
    python demo/run_hook_demo.py --by-type       # grouped by hook type
    python demo/run_hook_demo.py --json
    python demo/run_hook_demo.py --baseline 3.2  # project onto your own CTR

Runs offline. The `--llm` path needs the `anthropic` package and credentials.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from clipforge.hooks import (  # noqa: E402
    ClipContext,
    HookConfig,
    HookGenerator,
    HookType,
)

CLIPS: dict[str, ClipContext] = {
    "founder": ClipContext(
        text=(
            "The raise was the mistake. We went from twelve people to ninety in "
            "seven months and we almost went bankrupt doing it. I lost everything "
            "I'd built. The culture, the speed, all of it. We burned fourteen "
            "million dollars in nineteen months and had almost nothing to show "
            "for it. Eleven days of runway. I was terrified."
        ),
        signals=("failure", "money", "emotional_spike", "secret"),
        duration_s=31.0,
    ),
    "stream": ClipContext(
        text=(
            "No no no. That was a guaranteed win and he threw it. I have never "
            "seen anyone choke that hard. Chat is losing it. I genuinely cannot "
            "believe what I just watched happen."
        ),
        signals=("fail", "rage", "funny", "reaction"),
        duration_s=15.0,
    ),
    "advice": ClipContext(
        text=(
            "The lesson here is that headcount is not progress. I confused the "
            "two for about two years and it nearly killed the company. If I could "
            "go back, I'd tell myself to stay small for twice as long as feels "
            "comfortable. Before you hire anyone, write down what specifically "
            "gets worse if you don't."
        ),
        signals=("lesson", "failure"),
        duration_s=28.0,
    ),
}


def bar(lift: float, width: int = 16) -> str:
    # Scale the bar across the model's actual output range so the difference
    # between hooks is visible rather than compressed into the top decile.
    from clipforge.hooks import LIFT_MAX, LIFT_MIN

    fraction = (lift - LIFT_MIN) / (LIFT_MAX - LIFT_MIN)
    filled = max(0, min(width, round(fraction * width)))
    return "█" * filled + "·" * (width - filled)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clip", default="founder", choices=sorted(CLIPS))
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--baseline", type=float, default=5.0)
    parser.add_argument("--by-type", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--llm", action="store_true", help="add LLM-written hooks")
    args = parser.parse_args()

    context = CLIPS[args.clip]
    config = HookConfig(count=args.count, baseline_ctr=args.baseline)

    if args.llm:
        from clipforge.hooks import AnthropicWriter

        config.writer = AnthropicWriter()

    result = HookGenerator(config).generate(context)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0

    stats = result.stats
    print(f"\n  clip        {args.clip}  ({context.duration_s:.0f}s)")
    print(f"  signals     {', '.join(context.signals)}")
    print(f"  slots       " + "  ".join(
        f"{k}={v!r}" for k, v in result.slots.as_dict().items() if v
    ))
    print(f"  candidates  {stats['templates_rendered']} templates"
          f" + {stats['llm_hooks']} llm → {stats['after_dedupe']} after dedupe")
    print(f"  returned    {stats['returned']} hooks across "
          f"{stats['types_covered']} types  ({stats['elapsed_ms']}ms)")
    print(f"  baseline    {stats['baseline_ctr']:.1f}%  "
          f"— projected CTR inherits this number's error")

    if args.by_type:
        print()
        for hook_type, hooks in sorted(
            result.by_type().items(), key=lambda kv: kv[0].value
        ):
            print(f"  {hook_type.value.upper()}")
            for hook in hooks:
                print(f"     {hook.estimate.percent:>5}  {hook.text}")
            print()
        return 0

    print(f"\n  {'CTR':>5}  {'LIFT':>5}  {'TYPE':<15} HOOK\n")
    for rank, hook in enumerate(result.hooks, start=1):
        marker = "★" if rank == 1 else " "
        print(f" {marker}{hook.estimate.percent:>5}  {hook.estimate.lift:>5.2f}  "
              f"{hook.hook_type.value:<15} {hook.text}")
        if hook.penalties:
            print(f"        {'':>12} ⚠ {', '.join(hook.penalties)}")
    print()

    best = result.best
    if best:
        print(f"  BEST: “{best.text}”")
        top_features = sorted(
            ((k, v) for k, v in best.features.items() if v > 0),
            key=lambda kv: -kv[1],
        )[:5]
        print("  driven by: " + ", ".join(f"{k} {v:.2f}" for k, v in top_features))
        print(f"  confidence: {best.estimate.confidence} "
              f"(uncalibrated — no click data yet)")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
