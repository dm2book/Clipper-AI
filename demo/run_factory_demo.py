"""Channel factory demo — seven channels, one cycle, end to end.

    python demo/run_factory_demo.py            # create, run, report
    python demo/run_factory_demo.py --niches   # what differs between niches
    python demo/run_factory_demo.py --rights   # the rights gate
    python demo/run_factory_demo.py --quota    # shared-quota contention
    python demo/run_factory_demo.py --isolation  # one channel fails, six run
    python demo/run_factory_demo.py --json

Runs offline. Source transcripts are synthetic and every platform reply is
scripted, so the whole factory — detection, hooks, captions, composition,
scheduling — executes for real without credentials or network.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from clipforge.captions.types import TimedWord  # noqa: E402
from clipforge.factory import (  # noqa: E402
    ChannelFactory,
    ChannelState,
    FactoryConfig,
    Niche,
    PipelineConfig,
    RegistrySourceFinder,
    Rights,
    RightsBasis,
    Source,
    SourceKind,
    Stage,
    profile,
)
from clipforge.gameplay import Game, GameplayAsset  # noqa: E402
from clipforge.publish import (  # noqa: E402
    Account,
    Platform,
    PublishConfig,
    PublishingSystem,
    TokenSet,
)

NOW = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


#: Filler that surrounds the moment worth clipping. A real source is an hour
#: of ordinary conversation with thirty good seconds somewhere inside it —
#: feeding the detector only the good part tests nothing, because finding it
#: is the entire job.
FILLER = [
    "So thanks for having me on, it is good to be here again.",
    "We were talking before we started about how the market has changed.",
    "I think a lot of it depends on what stage you are at, honestly.",
    "There is a version of this where none of it matters very much.",
    "Anyway, that is roughly where we ended up on that question.",
    "It is hard to generalise because every situation is a bit different.",
    "We tried a few things and most of them were fine, nothing dramatic.",
    "I do not have a strong view on that one way or the other.",
    "That is probably a longer conversation than we have time for today.",
    "Right, so where were we before that tangent.",
    "People ask me about this a lot and I never know what to say.",
    "It went about how you would expect, which is to say slowly.",
]

#: The moment each source is worth clipping for.
MOMENTS: dict[str, str] = {
    "src-motivation": (
        "I quit. Everyone told me not to. I had eleven days of runway and no "
        "plan and I quit anyway. That was the moment everything changed. "
        "People think the hard part is starting. It is not. The hard part is "
        "the eighteen months where nothing works and nobody believes you. "
        "I lost everything I had built and I would do it again tomorrow."
    ),
    "src-business": (
        "The raise was the mistake. We went from twelve people to ninety in "
        "seven months and we almost went bankrupt doing it. We burned "
        "fourteen million dollars in nineteen months and had almost nothing "
        "to show for it. Nobody tells you that headcount is not progress. "
        "I confused the two for two years and it nearly killed the company."
    ),
    "src-ai": (
        "Everyone is wrong about what these models actually cost. The "
        "inference bill is not the problem. The problem is that ninety "
        "percent of teams are running a frontier model on a task a small one "
        "handles for a fortieth of the price. We cut our spend by eighty "
        "percent and the quality went up, because we finally measured it. "
        "Nobody wants to hear that the expensive answer was the lazy one."
    ),
    "src-history": (
        "In nineteen fourteen nobody thought it would last past Christmas. "
        "The plans assumed six weeks. Every general on every side had "
        "modelled a short war because a long one was unthinkable. They were "
        "wrong by four years and seventeen million lives. The lesson nobody "
        "learned is that a plan everyone agrees on is not the same as a plan "
        "that works. They agreed their way into a catastrophe."
    ),
    "src-cars": (
        "This thing costs four hundred thousand dollars and it is slower than "
        "a used sedan in a straight line. That is not the point. The point is "
        "what happens when the road stops being straight. I have driven every "
        "supercar built in the last decade and this is the only one that has "
        "ever genuinely scared me. I got out shaking and I paid for it anyway."
    ),
    "src-luxury": (
        "The watch costs more than the car. People find that offensive and I "
        "understand exactly why. But nothing on this planet is made the way "
        "this is made. Four hundred hours of hand finishing on parts that "
        "nobody will ever see. That is either the stupidest thing you have "
        "ever heard or the only thing left worth paying for."
    ),
}


def build_transcript(source_id: str, seed: int = 3) -> str:
    """Bury the moment in a realistic amount of ordinary conversation."""
    import random

    rng = random.Random(seed + len(source_id))
    before = [rng.choice(FILLER) for _ in range(6)]
    after = [rng.choice(FILLER) for _ in range(5)]
    return " ".join(before + [MOMENTS[source_id]] + after)


TRANSCRIPTS: dict[str, str] = {
    source_id: build_transcript(source_id) for source_id in MOMENTS
}


def words_for(source_id: str) -> list[TimedWord]:
    """Turn a paragraph into plausible word-level timings."""
    text = TRANSCRIPTS[source_id]
    words: list[TimedWord] = []
    cursor = 0
    for raw in text.split():
        # Roughly 2.7 words a second, longer words taking longer.
        span = int(240 + len(raw) * 22)
        words.append(TimedWord(text=raw, start_ms=cursor,
                               end_ms=cursor + span, speaker="host"))
        # Real speech leaves a beat at a sentence end; tight timings collapse
        # the whole recording into one utterance.
        cursor += span + (420 if raw.endswith((".", "?", "!")) else 45)
    return words


LICENSED = Rights(
    basis=RightsBasis.LICENSED, reference="LIC-2026-114",
    verified_at=NOW - timedelta(days=30),
    expires_at=NOW + timedelta(days=400),
)
OWNED = Rights(basis=RightsBasis.OWNED, reference="first-party",
               verified_at=NOW - timedelta(days=200))


def build_registry() -> RegistrySourceFinder:
    common = dict(kind=SourceKind.PODCAST, duration_s=3600.0,
                  has_transcript=True, published_at=NOW - timedelta(days=10))

    sources = [
        Source("src-motivation", "Founder keynote", rights=OWNED,
               creator="ClipForge", topics=("motivation", "mindset"), **common),
        Source("src-business", "The raise was the mistake", rights=LICENSED,
               creator="Podcast Co", topics=("business", "startups"), **common),
        Source("src-ai", "What models actually cost", rights=LICENSED,
               creator="Podcast Co", topics=("ai", "engineering"), **common),
        Source("src-history", "Nobody thought it would last", rights=OWNED,
               creator="ClipForge", topics=("history",), **common),

        Source("src-cars", "Four hundred thousand dollars",
               kind=SourceKind.LONGFORM_VIDEO, duration_s=1200.0,
               has_transcript=True, published_at=NOW - timedelta(days=5),
               rights=LICENSED, creator="Motor Channel", topics=("cars",)),

        # No rights basis recorded. This one must not publish.
        Source("src-luxury", "The watch costs more than the car",
               kind=SourceKind.LONGFORM_VIDEO, duration_s=900.0,
               has_transcript=True, published_at=NOW - timedelta(days=3),
               creator="Unknown reupload", topics=("luxury", "watches")),

        # A licence that lapses inside the scheduling horizon.
        Source("src-luxury-2", "Hand finishing, four hundred hours",
               kind=SourceKind.LONGFORM_VIDEO, duration_s=800.0,
               has_transcript=True, published_at=NOW - timedelta(days=20),
               rights=Rights(basis=RightsBasis.LICENSED, reference="LIC-OLD",
                             expires_at=NOW - timedelta(days=2)),
               creator="Atelier Films", topics=("luxury",)),
    ]
    return RegistrySourceFinder(sources)


BEDS = (
    GameplayAsset("ss-1", Game.SUBWAY_SURFERS, 190.0, 1080, 1920, 60.0,
                  loop_points=(4.0, 62.5), lead_in_s=2.0),
    GameplayAsset("sat-1", Game.SATISFYING, 240.0, 1440, 1440, 30.0,
                  loop_points=(0.0, 120.0)),
    GameplayAsset("mc-1", Game.MINECRAFT_PARKOUR, 420.0, 1920, 1080, 60.0,
                  lead_in_s=1.5),
)


CHANNELS = [
    ("Redline", Niche.CARS, ("cars",)),
    ("Atelier", Niche.LUXURY, ("luxury", "watches")),
    ("Momentum", Niche.MOTIVATION, ("motivation", "mindset")),
    ("Runway", Niche.BUSINESS, ("business", "startups")),
    ("Clutch", Niche.GAMING, ("gaming",)),
    ("Inference", Niche.AI, ("ai", "engineering")),
    ("Antiquity", Niche.HISTORY, ("history",)),
]


def build_factory() -> ChannelFactory:
    publisher = PublishingSystem(
        # The factory places several posts a day per channel; the distribution
        # spacing floor would otherwise reject them before quota does.
        PublishConfig(enforce_spacing=False),
        timezone="Europe/Amsterdam",
    )

    horizons = {Platform.YOUTUBE: 3650, Platform.TIKTOK: 365,
                Platform.INSTAGRAM: 60}
    for platform in Platform:
        for name, _, _ in CHANNELS:
            account_id = f"{platform.value}-{name.lower()}"
            publisher.connect(
                Account(account_id, platform, "org1", handle=f"@{name.lower()}",
                        external_id=f"ext-{account_id}",
                        timezone="Europe/Amsterdam",
                        direct_post_approved=True, business_account=True),
                TokenSet(account_id, platform, "at", "rt",
                         expires_at=NOW + timedelta(hours=1),
                         refresh_valid_until=NOW + timedelta(
                             days=horizons[platform]),
                         obtained_at=NOW),
            )

    factory = ChannelFactory(
        publisher=publisher,
        finder=build_registry(),
        config=FactoryConfig(
            pipeline=PipelineConfig(gameplay_library=BEDS),
            sources_per_cycle=6,
        ),
    )

    for name, niche, topics in CHANNELS:
        accounts = {
            platform: f"{platform.value}-{name.lower()}"
            for platform in profile(niche).platforms
        }
        channel = factory.create_channel(
            name, niche, accounts=accounts, topics=topics,
            budget_cents=15_000, timezone="Europe/Amsterdam",
        )
        factory.activate(channel.channel_id)

    return factory


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n  {title}\n{'=' * 72}")


def show_niches() -> None:
    section("SEVEN NICHES — a niche is a configuration, not a label")
    print(f"\n  {'CHANNEL':<12} {'BED':<18} {'CAPTIONS':<12} {'CLIP':<10} "
          f"{'FLOOR':<6} CADENCE")
    for niche in Niche:
        entry = profile(niche)
        bed = entry.gameplay_bed.value if entry.gameplay_bed else "— none —"
        clip = f"{entry.duration_s[0]:.0f}-{entry.duration_s[1]:.0f}s"
        print(f"  {entry.label:<12} {bed:<18} {entry.caption_style:<12} "
              f"{clip:<10} {entry.quality_floor:<6.0f} "
              f"{entry.cadence_per_day}/day")

    print("\n  Why three of them get no gameplay bed:")
    for niche in (Niche.CARS, Niche.LUXURY, Niche.GAMING):
        print(f"    {profile(niche).label:<11} {profile(niche).note}")
    print("\n  Why two of them get the quietest one:")
    for niche in (Niche.BUSINESS, Niche.AI):
        print(f"    {profile(niche).label:<11} {profile(niche).note}")


def show_rights(factory: ChannelFactory) -> None:
    section("RIGHTS GATE — the default for unattributed material is 'nowhere'")
    report = factory.rights_report(now=NOW)
    print(f"\n  library      {report['sources']} sources")
    for basis, count in report["by_basis"].items():
        mark = "⚠" if basis == "unverified" else " "
        print(f"    {mark} {basis:<22} {count}")
    print(f"\n  channels accepting unverified material: "
          f"{report['channels_accepting_unverified'] or 'none'}")

    if report["expiring_within_90_days"]:
        print("\n  ⚠ LICENCES LAPSING INSIDE THE SCHEDULING HORIZON")
        for entry in report["expiring_within_90_days"]:
            print(f"    {entry['source_id']:<16} expires {entry['expires']} "
                  f"({entry['days']}d)")


def show_quota(factory: ChannelFactory) -> None:
    section("SHARED QUOTA — where 'run independently' stops being true")
    plan = factory.quota_plan()

    print(f"\n  {'CHANNEL':<12} {'PLATFORM':<12} WANTED  GRANTED")
    for allocation in plan.allocations:
        if allocation.platform is not Platform.YOUTUBE:
            continue
        channel = factory.channels[allocation.channel_id]
        flag = "  ← short" if allocation.shortfall else ""
        print(f"  {channel.name:<12} {allocation.platform.value:<12} "
              f"{allocation.wanted:>6}  {allocation.granted:>7}{flag}")

    if plan.warnings():
        print()
        for warning in plan.warnings():
            print(f"  ⚠ {warning}")
    else:
        print("\n  no contention")


def show_cycle(factory: ChannelFactory) -> None:
    section("ONE CYCLE — seven channels, isolated")
    transcripts = {sid: words_for(sid) for sid in TRANSCRIPTS}
    reports = factory.run_cycle(transcripts=transcripts, now=NOW)

    for channel_id, report in reports.items():
        channel = factory.channels[channel_id]
        entry = profile(channel.niche)
        head = f"  {channel.name} ({entry.label})"
        if not report.ran:
            print(f"\n{head}\n      · not run: {report.reason}")
            continue

        print(f"\n{head}   {report.scheduled} scheduled, "
              f"{report.blocked} blocked, {report.failed} failed   "
              f"{report.spent_cents}c")

        for item in report.items:
            if item.stage is Stage.SCHEDULED:
                print(f"      ✓ {item.source.source_id}")
                print(f"          virality {item.moment.scores.virality:.0f}  "
                      f"{item.moment.candidate.duration_ms / 1000:.0f}s  "
                      f"{len(item.caption_track.cues)} cues  "
                      f"{item.gameplay_plan.style.value}")
                print(f"          hook  “{item.best_hook.text}”")
                print(f"                {item.best_hook.hook_type.value}, "
                      f"predicted {item.best_hook.estimate.percent}")
                print(f"          → {len(item.scheduled_post_ids)} post(s) queued")
            else:
                mark = "✗" if item.stage is Stage.FAILED else "·"
                print(f"      {mark} {item.source.source_id}: {item.reason}")


def show_isolation(factory: ChannelFactory) -> None:
    section("ISOLATION — one channel breaks, the rest keep running")

    victim = next(c for c in factory.channels.values()
                  if c.niche is Niche.MOTIVATION)
    victim.accounts.clear()          # tokens revoked, accounts gone
    print(f"\n  broke {victim.name}: publishing accounts removed")

    starved = next(c for c in factory.channels.values()
                   if c.niche is Niche.HISTORY)
    starved.budget.monthly_cents = 50
    print(f"  starved {starved.name}: monthly budget cut to $0.50")

    transcripts = {sid: words_for(sid) for sid in TRANSCRIPTS}
    reports = factory.run_cycle(transcripts=transcripts, now=NOW)

    print()
    for channel_id, report in reports.items():
        channel = factory.channels[channel_id]
        state = "ran" if report.ran else "skipped"
        detail = report.reason
        if not detail:
            detail = f"{report.scheduled} scheduled"
            if not report.scheduled and report.items:
                # A channel can run and still place nothing. The reason is on
                # the items, not the report, and it is the interesting part.
                detail += f" — {report.items[0].reason[:74]}"
        print(f"  {channel.name:<12} {state:<8} {detail}")

    healthy = sum(1 for r in reports.values() if r.ran)
    print(f"\n  {healthy}/{len(reports)} channels still working. A channel "
          f"with no accounts and a\n  channel with no budget both degrade to "
          f"zero output and say why —\n  neither takes the factory down.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--niches", action="store_true")
    parser.add_argument("--rights", action="store_true")
    parser.add_argument("--quota", action="store_true")
    parser.add_argument("--isolation", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.niches:
        show_niches()
        print()
        return 0

    factory = build_factory()

    if args.rights:
        show_rights(factory)
        print()
        return 0
    if args.quota:
        show_quota(factory)
        print()
        return 0
    if args.isolation:
        show_isolation(factory)
        print()
        return 0
    if args.json:
        transcripts = {sid: words_for(sid) for sid in TRANSCRIPTS}
        factory.run_cycle(transcripts=transcripts, now=NOW)
        print(json.dumps(factory.status(now=NOW), indent=2, default=str))
        return 0

    show_rights(factory)
    show_quota(factory)
    show_cycle(factory)

    section("FACTORY STATUS")
    status = factory.status(now=NOW)
    print(f"\n  channels     {status['active']}/{status['channels']} active")
    print(f"  by state     {status['by_state']}")
    print(f"  budget       {status['budget_cents']['spent']}c spent of "
          f"{status['budget_cents']['allocated']}c")
    print(f"  queued       {status['publisher']['total']} posts")
    print(f"  quota short  {status['quota']['total_shortfall']}/day")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
