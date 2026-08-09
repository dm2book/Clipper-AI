"""Analytics intelligence demo.

    python demo/run_analytics_demo.py            # a weekly report
    python demo/run_analytics_demo.py --honesty  # what it refuses to claim
    python demo/run_analytics_demo.py --young    # the same engine, 3 weeks in
    python demo/run_analytics_demo.py --retention
    python demo/run_analytics_demo.py --calibration
    python demo/run_analytics_demo.py --json

Runs offline. The synthetic history below has **two real effects planted in it**
and everything else is noise, so the engine can be checked on both counts: it
should find the two, and it should refuse to name a winner among the rest.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from clipforge.analytics import (  # noqa: E402
    AnalyticsConfig,
    AnalyticsEngine,
    ExplorationPolicy,
    PostMetrics,
    PostRecord,
    RetentionCurve,
    Snapshot,
    calibration,
    diagnose_retention,
)
from clipforge.publish import Platform  # noqa: E402

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)

HOOK_TYPES = ("curiosity", "authority", "surprise", "fear", "number",
              "controversy", "negativity", "social_proof")
TOPICS = ("raise", "hiring", "runway", "pricing", "burnout", "founders")
CREATORS = ("Podcast Co", "Studio Nine", "The Long Table", "Atrium Media")
DURATIONS = (14.0, 22.0, 30.0, 40.0, 52.0)
STYLES = ("punch", "karaoke", "minimal")
CHANNELS = (
    ("ch-runway", "Runway", "business", Platform.TIKTOK),
    ("ch-runway", "Runway", "business", Platform.YOUTUBE),
    ("ch-inference", "Inference", "ai", Platform.TIKTOK),
    ("ch-inference", "Inference", "ai", Platform.YOUTUBE),
)

LOCAL = ZoneInfo("Europe/Amsterdam")

#: Two effects are planted, deliberately on opposite sides of what this much
#: data can see. The engine should find the first, and decline the second
#: while saying how much more data it would take.
PLANTED_HOUR = 20          # local, because that is what a creator schedules
PLANTED_HOUR_LIFT = 0.55   # comfortably above the detection floor
PLANTED_CREATOR = "Studio Nine"
PLANTED_CREATOR_LIFT = 0.45  # right at it


def build_history(weeks: int = 10, seed: int = 11) -> AnalyticsEngine:
    rng = random.Random(seed)
    policy = ExplorationPolicy(rate=0.20)
    engine = AnalyticsEngine(AnalyticsConfig(exploration=policy, seed="demo"))

    start = NOW - timedelta(weeks=weeks)
    post_number = 0

    for day in range(weeks * 7):
        published_day = start + timedelta(days=day)
        for _ in range(rng.choice((1, 2, 2, 3))):
            post_number += 1
            post_id = f"p{post_number:04d}"
            channel_id, channel_name, niche, platform = rng.choice(CHANNELS)

            # Scheduled in local wall-clock time, the way the publishing
            # engine's recurrences work — so the hour a creator chose survives
            # the DST change in the middle of this window.
            hour = rng.choice((9, 11, 14, 17, 20, 20, 22))
            local = published_day.astimezone(LOCAL).replace(
                hour=hour, minute=rng.choice((0, 15, 30, 45)),
                second=0, microsecond=0,
            )
            published_at = local.astimezone(timezone.utc)

            creator = rng.choice(CREATORS)
            topic = rng.choice(TOPICS)
            duration = rng.choice(DURATIONS)
            hook_type = rng.choice(HOOK_TYPES)

            assignment = policy.assign(20, post_id)

            # Baseline reach, log-normal because view counts are heavy-tailed
            # and a symmetric generator would make the statistics look far
            # better behaved than they are in reality.
            base = math.exp(rng.gauss(7.6, 0.75))
            if hour == PLANTED_HOUR:
                base *= 1.0 + PLANTED_HOUR_LIFT
            if creator == PLANTED_CREATOR:
                base *= 1.0 + PLANTED_CREATOR_LIFT

            views_24h = max(50, int(base))
            impressions = int(views_24h / max(0.05, rng.gauss(0.28, 0.06)))

            hook_hold = min(0.95, max(0.25, rng.gauss(0.62, 0.11)))
            completion = min(hook_hold, max(0.05, hook_hold - rng.gauss(0.30, 0.10)))

            record = PostRecord(
                post_id=post_id,
                metrics=PostMetrics(post_id, platform, published_at),
                channel_id=channel_id, channel_name=channel_name, niche=niche,
                account_id=f"{platform.value}-{channel_id}",
                timezone="Europe/Amsterdam",
                hook_text=f"Hook for {post_id}", hook_type=hook_type,
                predicted_lift=round(rng.uniform(0.7, 1.7), 3),
                hook_rank=assignment.index, explored=assignment.explored,
                topic=topic, source_id=f"src-{post_number % 40}",
                creator=creator, clip_duration_s=duration,
                caption_style=rng.choice(STYLES),
                gameplay_bed="satisfying" if niche in ("business", "ai") else "",
                predicted_virality=round(rng.uniform(45, 85), 1),
                hook_weights_version="hook-heuristic-v1",
                viral_weights_version="heuristic-v1",
            )

            for checkpoint, share in ((1.0, 0.30), (24.0, 1.0), (168.0, 2.4)):
                age_views = int(views_24h * share)
                if published_at + timedelta(hours=checkpoint) > NOW:
                    break

                # YouTube reports a retention curve; the other two do not.
                curve = RetentionCurve()
                if platform is Platform.YOUTUBE:
                    curve = RetentionCurve(tuple(
                        (position, max(0.02, hook_hold - (hook_hold - completion)
                                       * (position - 0.1) / 0.9)
                         if position >= 0.1 else
                         1.0 - (1.0 - hook_hold) * position / 0.1)
                        for position in (0.0, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0)
                    ))

                engagement = max(0.01, rng.gauss(0.07, 0.02))
                record.metrics.record(Snapshot(
                    taken_at=published_at + timedelta(hours=checkpoint),
                    age_hours=checkpoint,
                    views=age_views,
                    likes=int(age_views * engagement * 0.75),
                    comments=int(age_views * engagement * 0.10),
                    shares=int(age_views * engagement * 0.12),
                    saves=int(age_views * engagement * 0.03),
                    follows=int(age_views * max(0.0, rng.gauss(0.004, 0.002))),
                    impressions=int(impressions * share),
                    avg_watch_pct=(hook_hold + completion) / 2.0,
                    retention=curve,
                ))

            engine.track(record)

    engine.baselines.learn(
        [r.metrics for r in engine.store.records], engine.config.checkpoint_h
    )
    return engine


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n  {title}\n{'=' * 74}")


def show_honesty(engine: AnalyticsEngine) -> None:
    section("WHAT IT REFUSES TO CLAIM")
    report = engine.report(NOW)

    print(f"\n  Two effects are planted, on opposite sides of what this much")
    print(f"  data can resolve:")
    print(f"    · posting at {PLANTED_HOUR}:00 local → "
          f"{PLANTED_HOUR_LIFT:+.0%} reach  (above the floor)")
    print(f"    · {PLANTED_CREATOR} as source → "
          f"{PLANTED_CREATOR_LIFT:+.0%} reach  (right at it)")
    print(f"  Everything else — hook type, topic, clip length, caption style,")
    print(f"  weekday — is noise.\n")

    print(f"  {'QUESTION':<46} VERDICT")
    for insight in report.insights:
        winner = insight.comparison.winner
        verdict = f"→ {winner.name} ({winner.lift:+.0%})" if winner else "no claim"
        mark = "✓" if winner else "·"
        print(f"  {mark} {insight.question:<44} {verdict}")

    found = [i.comparison.winner.name for i in report.insights
             if i.comparison.winner]
    mature = len([r for r in engine.store.records if r.mature(24.0)])
    print(f"\n  {len(found)} claims from {len(report.insights)} families of "
          f"comparison over {mature} posts.")

    creators = next(
        i for i in report.insights
        if i.comparison.dimension == "source creator"
        and i.metric == "views"
    )
    top = creators.comparison.ranked[0] if creators.comparison.ranked else None
    if top:
        print(f"\n  The near-miss is the interesting one. {top.name} ranks "
              f"first at\n  {top.lift:+.0%} — the planted effect — and the "
              f"engine still refuses to\n  call it:")
        print(f"\n    {creators.recommendation}")
        print(f"\n  A ranking always has a top row. Printing that row as a "
              f"finding is\n  how a creator reorganises their week around "
              f"noise.")


def show_retention(engine: AnalyticsEngine) -> None:
    section("RETENTION — where the audience actually leaves")
    mature = [r for r in engine.store.records if r.mature(24.0)]
    diagnosis = diagnose_retention(mature)

    print(f"\n  {diagnosis.n} posts reported a curve "
          f"(of {len(mature)} — only YouTube supplies one)")
    print(f"    hook hold   {diagnosis.median_hook_hold * 100:.0f}%")
    print(f"    mid drop    {diagnosis.median_mid_drop * 100:.0f}%")
    print(f"    completion  {diagnosis.median_completion * 100:.0f}%")
    print(f"\n  {diagnosis.dominant_problem}")
    print(f"\n  The average watch percentage cannot tell these apart: a clip")
    print(f"  losing 40% in the first second and one drifting off evenly both")
    print(f"  average the same, and they need opposite fixes.")


def show_calibration(engine: AnalyticsEngine) -> None:
    section("ARE THE PRIORS ANY GOOD?")
    mature = [r for r in engine.store.records if r.mature(24.0)]

    for prediction, metric in (
        ("predicted_lift", "view_through_rate"),
        ("predicted_virality", "views"),
    ):
        result = calibration(mature, prediction, metric)
        print(f"\n  {result.model}  ({result.weights_version})")
        print(f"    n={result.n}  rho={result.correlation:+.3f}")
        print(f"    {result.verdict}")

    print(f"\n  This is what `predicted_lift` and the weights versions have been")
    print(f"  persisted for since those engines were built. In this synthetic")
    print(f"  history the predictions are random, so the honest answer is that")
    print(f"  they carry no signal — which is exactly what should be reported.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--honesty", action="store_true")
    parser.add_argument("--young", action="store_true",
                        help="the same engine three weeks in")
    parser.add_argument("--retention", action="store_true")
    parser.add_argument("--calibration", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    engine = build_history(weeks=3 if args.young else 10)

    if args.honesty:
        show_honesty(engine)
        print()
        return 0
    if args.retention:
        show_retention(engine)
        print()
        return 0
    if args.calibration:
        show_calibration(engine)
        print()
        return 0
    if args.json:
        print(json.dumps(engine.report(NOW).to_dict(), indent=2, default=str))
        return 0

    readiness = engine.readiness()
    section("READINESS")
    print(f"\n  tracked {readiness['tracked']}   "
          f"mature {readiness['mature']}   "
          f"explored {readiness['explored']}")
    print(f"  hook questions are causal: "
          f"{'yes' if readiness['hook_questions_causal'] else 'no'}"
          + (f" (need {readiness['explored_needed']} more explored posts)"
             if not readiness['hook_questions_causal'] else ""))
    print(f"  retention curves on {readiness['with_retention_curve']} posts")
    print(f"  baselines learned for: "
          f"{', '.join(readiness['baselines_observed']) or 'none yet'}")

    print()
    print(engine.report(NOW, scope="all channels").render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
