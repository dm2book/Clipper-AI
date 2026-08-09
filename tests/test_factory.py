"""Niche profiles, the rights gate, quota fairness, isolation, and the pipeline."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

import _support  # noqa: F401  (path setup)

from clipforge.captions.types import TimedWord
from clipforge.factory import (
    Budget,
    CIRCUIT_COOLDOWN,
    Channel,
    ChannelFactory,
    ChannelState,
    DEFAULT_ACCEPTED_RIGHTS,
    FACTORY_NICHES,
    FAILURE_THRESHOLD,
    FactoryConfig,
    ITEM_COST_CENTS,
    Niche,
    NullTranscriber,
    PROFILES,
    Pipeline,
    PipelineConfig,
    RegistrySourceFinder,
    Rights,
    RightsBasis,
    Source,
    SourceKind,
    Stage,
    clear,
    daily_capacity,
    domain_affinity,
    expiring_soon,
    hook_preference,
    max_min_fair,
    plan_quota,
    profile,
    rights_summary,
    uses_stream_clipper,
)
from clipforge.gameplay import Game, GameplayAsset
from clipforge.hooks.types import HookType
from clipforge.publish import (
    Account,
    Platform,
    PublishConfig,
    PublishingSystem,
    TokenSet,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)

OWNED = Rights(basis=RightsBasis.OWNED, reference="first-party",
               verified_at=NOW - timedelta(days=10))
LICENSED = Rights(basis=RightsBasis.LICENSED, reference="LIC-1",
                  expires_at=NOW + timedelta(days=400))

BEDS = (
    GameplayAsset("ss", Game.SUBWAY_SURFERS, 190.0, 1080, 1920, 60.0),
    GameplayAsset("sat", Game.SATISFYING, 240.0, 1440, 1440, 30.0),
    GameplayAsset("mc", Game.MINECRAFT_PARKOUR, 420.0, 1920, 1080, 60.0),
)

MOMENT = (
    "The raise was the mistake. We went from twelve people to ninety in "
    "seven months and we almost went bankrupt doing it. We burned fourteen "
    "million dollars in nineteen months and had almost nothing to show for "
    "it. Nobody tells you that headcount is not progress. I confused the two "
    "for two years and it nearly killed the company."
)
FILLER = "I do not have a strong view on that one way or the other. "


def words(text: str) -> list[TimedWord]:
    out: list[TimedWord] = []
    cursor = 0
    for raw in text.split():
        span = 240 + len(raw) * 22
        out.append(TimedWord(raw, cursor, cursor + span, "host"))
        cursor += span + (420 if raw.endswith((".", "?", "!")) else 45)
    return out


def source(source_id="src-1", rights=None, kind=SourceKind.PODCAST,
           topics=("business", "startups")) -> Source:
    return Source(
        source_id, "A source", kind=kind,
        rights=rights if rights is not None else LICENSED,
        creator="Podcast Co", duration_s=3600.0, topics=topics,
        has_transcript=True, published_at=NOW - timedelta(days=5),
    )


def build_factory(niches=(Niche.BUSINESS,), sources=None,
                  budget_cents=20_000) -> ChannelFactory:
    publisher = PublishingSystem(PublishConfig(enforce_spacing=False))
    for platform in Platform:
        for niche in niches:
            account_id = f"{platform.value}-{niche.value}"
            publisher.connect(
                Account(account_id, platform, "org1", external_id=f"e-{account_id}",
                        direct_post_approved=True, business_account=True),
                TokenSet(account_id, platform, "at", "rt",
                         expires_at=NOW + timedelta(hours=1),
                         refresh_valid_until=NOW + timedelta(days=3650),
                         obtained_at=NOW),
            )

    factory = ChannelFactory(
        publisher=publisher,
        finder=RegistrySourceFinder(sources if sources is not None else [source()]),
        config=FactoryConfig(pipeline=PipelineConfig(gameplay_library=BEDS)),
    )
    for niche in niches:
        channel = factory.create_channel(
            profile(niche).label, niche,
            accounts={p: f"{p.value}-{niche.value}"
                      for p in profile(niche).platforms},
            topics=("business", "startups", niche.value),
            budget_cents=budget_cents,
        )
        factory.activate(channel.channel_id)
    return factory


class TestNiches(unittest.TestCase):
    def test_all_seven_exist(self):
        self.assertEqual(len(FACTORY_NICHES), 7)
        for niche in Niche:
            self.assertIn(niche, PROFILES)
            self.assertEqual(PROFILES[niche].niche, niche)

    def test_visually_rich_niches_get_no_gameplay_bed(self):
        # Putting Subway Surfers under a Lamborghini clip competes with the
        # only thing worth looking at.
        for niche in (Niche.CARS, Niche.LUXURY, Niche.GAMING):
            self.assertIsNone(profile(niche).gameplay_bed, niche.value)

    def test_talking_head_niches_get_one(self):
        for niche in (Niche.MOTIVATION, Niche.BUSINESS, Niche.AI, Niche.HISTORY):
            self.assertIsNotNone(profile(niche).gameplay_bed, niche.value)

    def test_dense_niches_get_the_quietest_bed(self):
        from clipforge.gameplay import profile as bed_profile

        dense = bed_profile(profile(Niche.BUSINESS).gameplay_bed).salience
        sparse = bed_profile(profile(Niche.MOTIVATION).gameplay_bed).salience
        self.assertLess(dense, sparse)

    def test_history_gets_the_longest_clips_and_slowest_cadence(self):
        longest = max(Niche, key=lambda n: profile(n).duration_s[1])
        self.assertIs(longest, Niche.HISTORY)
        self.assertEqual(
            profile(Niche.HISTORY).cadence_per_day,
            min(profile(n).cadence_per_day for n in Niche),
        )

    def test_durations_are_well_formed(self):
        for niche in Niche:
            low, high = profile(niche).duration_s
            self.assertLess(low, high)
            self.assertGreaterEqual(low, 10.0)

    def test_only_gaming_routes_to_the_stream_clipper(self):
        routed = {n for n in Niche if uses_stream_clipper(n)}
        self.assertEqual(routed, {Niche.GAMING})

    def test_hook_preference_favours_the_niche_types(self):
        preferred = profile(Niche.BUSINESS).hook_types[0]
        self.assertGreater(hook_preference(Niche.BUSINESS, preferred), 1.0)
        off_type = next(
            t for t in HookType if t not in profile(Niche.BUSINESS).hook_types
        )
        self.assertLess(hook_preference(Niche.BUSINESS, off_type), 1.0)

    def test_first_preference_beats_third(self):
        types = profile(Niche.BUSINESS).hook_types
        self.assertGreater(
            hook_preference(Niche.BUSINESS, types[0]),
            hook_preference(Niche.BUSINESS, types[-1]),
        )

    def test_every_niche_carries_domain_vocabulary(self):
        # The viral detectors were tuned on founder material; without this a
        # Cars or History clip registers no signal at all.
        for niche in Niche:
            self.assertTrue(profile(niche).domain_terms, niche.value)

    def test_domain_affinity_discriminates(self):
        car_text = "the supercar has more horsepower and the handling is grip"
        self.assertGreater(domain_affinity(Niche.CARS, car_text), 0.5)
        self.assertEqual(domain_affinity(Niche.CARS, "we raised a seed round"), 0.0)

    def test_domain_affinity_saturates(self):
        text = " ".join(profile(Niche.CARS).domain_terms)
        self.assertEqual(domain_affinity(Niche.CARS, text), 1.0)


class TestRightsGate(unittest.TestCase):
    def test_unverified_material_publishes_nowhere(self):
        result = clear(source(rights=Rights()), DEFAULT_ACCEPTED_RIGHTS)
        self.assertFalse(result.cleared)
        self.assertIn("no rights basis", result.reason)

    def test_unverified_is_not_accepted_by_default(self):
        self.assertNotIn(RightsBasis.UNVERIFIED, DEFAULT_ACCEPTED_RIGHTS)

    def test_owned_and_licensed_clear(self):
        for rights in (OWNED, LICENSED):
            self.assertTrue(clear(source(rights=rights),
                                  DEFAULT_ACCEPTED_RIGHTS).cleared)

    def test_an_expired_licence_is_not_a_licence(self):
        expired = Rights(basis=RightsBasis.LICENSED, reference="old",
                         expires_at=NOW - timedelta(days=1))
        result = clear(source(rights=expired), DEFAULT_ACCEPTED_RIGHTS, now=NOW)
        self.assertFalse(result.cleared)
        self.assertIn("expired", result.reason)

    def test_no_derivatives_blocks_clipping(self):
        # Clipping is a derivative work; a licence forbidding them forbids this.
        rights = Rights(basis=RightsBasis.LICENSED, derivatives=False)
        result = clear(source(rights=rights), DEFAULT_ACCEPTED_RIGHTS)
        self.assertFalse(result.cleared)
        self.assertIn("derivative", result.reason)

    def test_non_commercial_licence_blocks_a_monetised_channel(self):
        rights = Rights(basis=RightsBasis.CREATIVE_COMMONS,
                        attribution="by Someone", commercial_use=False)
        accepted = DEFAULT_ACCEPTED_RIGHTS | {RightsBasis.CREATIVE_COMMONS}
        self.assertFalse(clear(source(rights=rights), accepted,
                               monetised=True).cleared)
        self.assertTrue(clear(source(rights=rights), accepted,
                              monetised=False).cleared)

    def test_creative_commons_without_attribution_is_void(self):
        rights = Rights(basis=RightsBasis.CREATIVE_COMMONS, attribution="")
        accepted = DEFAULT_ACCEPTED_RIGHTS | {RightsBasis.CREATIVE_COMMONS}
        result = clear(source(rights=rights), accepted)
        self.assertFalse(result.cleared)
        self.assertIn("attribution", result.reason)

    def test_attribution_is_returned_for_the_caption(self):
        rights = Rights(basis=RightsBasis.STOCK, attribution="Footage: Studio X")
        result = clear(source(rights=rights), DEFAULT_ACCEPTED_RIGHTS)
        self.assertTrue(result.cleared)
        self.assertEqual(result.required_attribution, "Footage: Studio X")

    def test_a_channel_can_narrow_what_it_accepts(self):
        strict = frozenset({RightsBasis.OWNED})
        self.assertFalse(clear(source(rights=LICENSED), strict).cleared)
        self.assertTrue(clear(source(rights=OWNED), strict).cleared)

    def test_rights_summary_counts_by_basis(self):
        counts = rights_summary([
            source("a", OWNED), source("b", LICENSED), source("c", Rights()),
        ])
        self.assertEqual(counts["unverified"], 1)
        self.assertEqual(counts["owned"], 1)

    def test_expiring_soon_finds_licences_inside_the_horizon(self):
        soon = Rights(basis=RightsBasis.LICENSED,
                      expires_at=NOW + timedelta(days=30))
        far = Rights(basis=RightsBasis.LICENSED,
                     expires_at=NOW + timedelta(days=400))
        found = expiring_soon([source("a", soon), source("b", far)],
                              within_days=60, now=NOW)
        self.assertEqual([s.source_id for s, _ in found], ["a"])

    def test_fingerprint_is_stable_and_distinct(self):
        self.assertEqual(source("a").fingerprint, source("a").fingerprint)
        self.assertNotEqual(source("a").fingerprint, source("b").fingerprint)


class TestSourceFinder(unittest.TestCase):
    def setUp(self):
        self.finder = RegistrySourceFinder([
            source("pod", topics=("business",)),
            source("vid", kind=SourceKind.LONGFORM_VIDEO, topics=("cars",)),
            source("other", topics=("gardening",)),
        ])

    def test_filters_by_source_kind(self):
        found = self.finder.find(Niche.BUSINESS, ("business",), 10)
        self.assertEqual([s.source_id for s in found], ["pod"])

    def test_filters_by_topic(self):
        found = self.finder.find(Niche.CARS, ("cars",), 10)
        self.assertEqual([s.source_id for s in found], ["vid"])

    def test_no_match_returns_nothing(self):
        self.assertEqual(self.finder.find(Niche.CARS, ("boats",), 10), [])

    def test_limit_is_honoured(self):
        finder = RegistrySourceFinder([
            source(f"s{i}", topics=("business",)) for i in range(10)
        ])
        self.assertEqual(len(finder.find(Niche.BUSINESS, ("business",), 3)), 3)

    def test_prefers_sources_that_already_have_a_transcript(self):
        without = Source("no-tx", "t", SourceKind.PODCAST, rights=LICENSED,
                         topics=("business",), has_transcript=False)
        finder = RegistrySourceFinder([without, source("with-tx")])
        found = finder.find(Niche.BUSINESS, ("business",), 10)
        self.assertEqual(found[0].source_id, "with-tx")


class TestQuotaFairness(unittest.TestCase):
    def test_equal_split(self):
        self.assertEqual(max_min_fair({"a": 10, "b": 10}, 6), {"a": 3, "b": 3})

    def test_a_modest_claimant_is_fully_served(self):
        # Plain proportional division would give "a" less than it asked for
        # purely because "b" is greedy.
        granted = max_min_fair({"a": 1, "b": 10}, 6)
        self.assertEqual(granted["a"], 1)
        self.assertEqual(granted["b"], 5)

    def test_unused_capacity_flows_to_those_who_want_it(self):
        granted = max_min_fair({"a": 1, "b": 1, "c": 10}, 9)
        self.assertEqual(granted, {"a": 1, "b": 1, "c": 7})

    def test_never_exceeds_capacity(self):
        for capacity in range(0, 15):
            granted = max_min_fair({"a": 5, "b": 5, "c": 5}, capacity)
            self.assertLessEqual(sum(granted.values()), capacity)

    def test_never_grants_more_than_wanted(self):
        granted = max_min_fair({"a": 2, "b": 2}, 100)
        self.assertEqual(granted, {"a": 2, "b": 2})

    def test_scarcity_below_one_each_is_shared_not_hoarded(self):
        granted = max_min_fair({"a": 5, "b": 5, "c": 5}, 2)
        self.assertEqual(sum(granted.values()), 2)
        self.assertEqual(max(granted.values()), 1)

    def test_zero_capacity(self):
        self.assertEqual(max_min_fair({"a": 3}, 0), {"a": 0})

    def test_seven_channels_oversubscribe_youtube(self):
        # The arithmetic that breaks the independence claim: a project-scoped
        # cap of six against seven channels asking for more.
        factory = build_factory(niches=tuple(Niche))
        plan = plan_quota(list(factory.channels.values()))
        self.assertIn("youtube", plan.oversubscribed)
        wanted, capacity = plan.oversubscribed["youtube"]
        self.assertGreater(wanted, capacity)
        self.assertEqual(capacity, 6)
        self.assertGreater(plan.total_shortfall, 0)

    def test_the_warning_names_the_shortfall_and_the_scope(self):
        factory = build_factory(niches=tuple(Niche))
        warnings = plan_quota(list(factory.channels.values())).warnings()
        joined = " ".join(warnings)
        self.assertIn("project-scoped", joined)
        self.assertIn("adding accounts does not help", joined)

    def test_a_single_channel_is_not_oversubscribed(self):
        factory = build_factory(niches=(Niche.HISTORY,))
        self.assertTrue(plan_quota(list(factory.channels.values())).healthy)

    def test_daily_capacity_totals_the_grants(self):
        factory = build_factory(niches=tuple(Niche))
        capacity = daily_capacity(list(factory.channels.values()))
        self.assertLessEqual(capacity["youtube"], 6)


class TestBudgetAndHealth(unittest.TestCase):
    def test_budget_refuses_what_it_cannot_cover(self):
        budget = Budget(monthly_cents=100)
        self.assertTrue(budget.can_afford(100))
        self.assertFalse(budget.can_afford(101))

    def test_budget_rolls_at_a_month_boundary(self):
        budget = Budget(monthly_cents=100, period="2026-08")
        budget.charge(100)
        self.assertTrue(budget.exhausted)
        budget.roll(NOW)
        self.assertFalse(budget.exhausted)
        self.assertEqual(budget.period, "2026-09")

    def test_circuit_opens_after_repeated_failures(self):
        channel = Channel("c1", "C", Niche.BUSINESS)
        for _ in range(FAILURE_THRESHOLD):
            channel.health.record_failure("boom", NOW)
        self.assertTrue(channel.health.circuit_open(NOW))

    def test_circuit_reopens_after_the_cooldown(self):
        channel = Channel("c1", "C", Niche.BUSINESS)
        for _ in range(FAILURE_THRESHOLD):
            channel.health.record_failure("boom", NOW)
        later = channel.health.opened_at + CIRCUIT_COOLDOWN + timedelta(minutes=1)
        self.assertFalse(channel.health.circuit_open(later))

    def test_a_success_resets_the_failure_streak(self):
        channel = Channel("c1", "C", Niche.BUSINESS)
        channel.health.record_failure("boom", NOW)
        channel.health.record_success()
        self.assertEqual(channel.health.consecutive_failures, 0)

    def test_a_blocked_item_does_not_trip_the_breaker(self):
        # The rights gate working correctly must not take a channel offline.
        channel = Channel("c1", "C", Niche.BUSINESS)
        for _ in range(FAILURE_THRESHOLD * 2):
            channel.health.record_blocked("rights: unverified")
        self.assertFalse(channel.health.circuit_open(NOW))
        self.assertEqual(channel.health.consecutive_failures, 0)

    def test_runnable_reports_why_not(self):
        channel = Channel("c1", "C", Niche.BUSINESS)
        self.assertEqual(channel.runnable(NOW), (False, "not activated"))

        channel.state = ChannelState.ACTIVE
        self.assertIn("no publishing accounts", channel.runnable(NOW)[1])

        channel.accounts[Platform.TIKTOK] = "tt-1"
        self.assertTrue(channel.runnable(NOW)[0])

        channel.budget.charge(channel.budget.monthly_cents)
        self.assertIn("budget", channel.runnable(NOW)[1])

    def test_platforms_are_the_intersection_with_connected_accounts(self):
        channel = Channel("c1", "C", Niche.BUSINESS,
                          accounts={Platform.TIKTOK: "tt-1"})
        self.assertEqual(channel.platforms, (Platform.TIKTOK,))


class TestPipeline(unittest.TestCase):
    def setUp(self):
        self.pipeline = Pipeline(PipelineConfig(gameplay_library=BEDS))
        self.channel = Channel(
            "c1", "Runway", Niche.BUSINESS,
            accounts={p: f"{p.value}-1" for p in profile(Niche.BUSINESS).platforms},
        )
        self.channel.state = ChannelState.ACTIVE
        self.text = FILLER * 4 + MOMENT + FILLER * 3

    def run_item(self, **kwargs):
        return self.pipeline.run(
            kwargs.pop("channel", self.channel),
            kwargs.pop("source", source()),
            transcript_words=kwargs.pop("words", words(self.text)),
            now=NOW,
        )

    def test_a_good_source_reaches_scheduled(self):
        item = self.run_item()
        self.assertIs(item.stage, Stage.SCHEDULED, item.reason)
        self.assertIsNotNone(item.moment)
        self.assertTrue(item.hooks)
        self.assertTrue(item.caption_track.cues)
        self.assertIsNotNone(item.gameplay_plan)
        self.assertTrue(item.post_specs)

    def test_stages_are_recorded_in_order(self):
        item = self.run_item()
        stages = [s for s, _ in item.history]
        self.assertEqual(stages[0], Stage.CLEARED.value)
        self.assertEqual(stages[-1], Stage.SCHEDULED.value)

    def test_cost_accumulates_per_stage(self):
        item = self.run_item()
        self.assertEqual(item.cost_cents, ITEM_COST_CENTS)

    def test_unverified_rights_stop_at_the_gate(self):
        item = self.run_item(source=source(rights=Rights()))
        self.assertIs(item.stage, Stage.BLOCKED)
        self.assertIn("rights", item.reason)
        # And it costs nothing: the gate is the first stage for a reason.
        self.assertEqual(item.cost_cents, 0)

    def test_a_duplicate_source_is_refused(self):
        first = self.run_item()
        self.assertIs(first.stage, Stage.SCHEDULED)
        self.channel.used_fingerprints.add(source().fingerprint)
        again = self.run_item()
        self.assertIs(again.stage, Stage.BLOCKED)
        self.assertIn("already used", again.reason)

    def test_an_unaffordable_item_is_refused_before_any_work(self):
        self.channel.budget = Budget(monthly_cents=50)
        item = self.run_item()
        self.assertIs(item.stage, Stage.BLOCKED)
        self.assertIn("budget", item.reason)
        self.assertEqual(item.cost_cents, 0)

    def test_a_weak_source_is_stopped_by_the_quality_floor(self):
        item = self.run_item(words=words(FILLER * 12))
        self.assertIs(item.stage, Stage.BLOCKED)

    def test_no_transcriber_blocks_rather_than_inventing_timings(self):
        pipeline = Pipeline(PipelineConfig(transcriber=NullTranscriber()))
        item = pipeline.run(self.channel, source(), now=NOW)
        self.assertIs(item.stage, Stage.BLOCKED)
        self.assertIn("no transcriber", item.reason)

    def test_a_gaming_channel_is_routed_away_from_the_viral_engine(self):
        channel = Channel("c2", "Clutch", Niche.GAMING,
                          accounts={Platform.TIKTOK: "tt-1"})
        channel.state = ChannelState.ACTIVE
        item = self.run_item(
            channel=channel,
            source=source(kind=SourceKind.LIVESTREAM, topics=("gaming",)),
        )
        self.assertIs(item.stage, Stage.BLOCKED)
        self.assertIn("stream clipper", item.reason)

    def test_a_missing_bed_blocks_rather_than_silently_dropping_it(self):
        pipeline = Pipeline(PipelineConfig(gameplay_library=()))
        item = pipeline.run(self.channel, source(),
                            transcript_words=words(self.text), now=NOW)
        self.assertIs(item.stage, Stage.BLOCKED)
        self.assertIn("library has none", item.reason)

    def test_a_visually_rich_niche_composes_without_a_bed(self):
        channel = Channel(
            "c3", "Redline", Niche.CARS,
            accounts={p: f"{p.value}-1" for p in profile(Niche.CARS).platforms},
        )
        channel.state = ChannelState.ACTIVE
        car_text = FILLER * 3 + (
            "This supercar costs four hundred thousand dollars and it is "
            "slower than a used sedan in a straight line. The handling is "
            "what you pay for. I have driven every supercar built and the "
            "grip on this one scared me on the track."
        ) + FILLER * 2
        item = self.pipeline.run(
            channel, source(kind=SourceKind.LONGFORM_VIDEO, topics=("cars",)),
            transcript_words=words(car_text), now=NOW,
        )
        self.assertIs(item.stage, Stage.SCHEDULED, item.reason)
        self.assertIsNone(item.gameplay_plan.game)

    def test_attribution_is_carried_into_the_caption(self):
        rights = Rights(basis=RightsBasis.STOCK, attribution="Footage: Studio X")
        item = self.run_item(source=source(rights=rights))
        self.assertIs(item.stage, Stage.SCHEDULED, item.reason)
        self.assertIn("Footage: Studio X", item.post_specs[0].caption)

    def test_provenance_travels_with_the_post(self):
        item = self.run_item()
        metadata = item.post_specs[0].metadata
        self.assertEqual(metadata["source_id"], "src-1")
        self.assertEqual(metadata["rights_basis"], "licensed")
        self.assertEqual(metadata["niche"], "business")

    def test_one_spec_per_connected_platform(self):
        self.assertEqual(
            len(self.run_item().post_specs), len(self.channel.platforms)
        )

    def test_the_pipeline_never_raises(self):
        broken = Source("bad", "t", SourceKind.PODCAST, rights=LICENSED,
                        topics=("business",))
        item = self.pipeline.run(self.channel, broken, transcript_words=[],
                                 now=NOW)
        self.assertTrue(item.blocked)

    def test_item_serialises(self):
        payload = json.loads(json.dumps(self.run_item().to_dict()))
        self.assertEqual(payload["stage"], "scheduled")
        self.assertTrue(payload["hook"])


class TestFactory(unittest.TestCase):
    def test_creates_a_channel_from_a_niche(self):
        factory = build_factory()
        channel = next(iter(factory.channels.values()))
        self.assertIs(channel.niche, Niche.BUSINESS)
        self.assertIs(channel.state, ChannelState.ACTIVE)
        self.assertEqual(channel.cadence_per_day,
                         profile(Niche.BUSINESS).cadence_per_day)

    def test_activation_requires_accounts(self):
        factory = build_factory()
        orphan = factory.create_channel("Orphan", Niche.CARS, accounts={})
        with self.assertRaises(ValueError):
            factory.activate(orphan.channel_id)

    def test_a_cycle_schedules_posts(self):
        factory = build_factory()
        reports = factory.run_cycle({"src-1": words(FILLER * 4 + MOMENT)},
                                    now=NOW)
        report = next(iter(reports.values()))
        self.assertTrue(report.ran)
        self.assertEqual(report.scheduled, 1)
        self.assertGreater(len(factory.publisher.calendar), 0)

    def test_a_paused_channel_does_no_work(self):
        factory = build_factory()
        channel_id = next(iter(factory.channels))
        factory.pause(channel_id)
        report = factory.run_cycle({"src-1": words(MOMENT)}, now=NOW)[channel_id]
        self.assertFalse(report.ran)
        self.assertIn("paused", report.reason)

    def test_channels_are_isolated_from_each_other(self):
        factory = build_factory(niches=(Niche.BUSINESS, Niche.AI, Niche.HISTORY))
        broken = next(c for c in factory.channels.values()
                      if c.niche is Niche.AI)
        broken.accounts.clear()

        reports = factory.run_cycle({"src-1": words(FILLER * 4 + MOMENT)},
                                    now=NOW)
        self.assertFalse(reports[broken.channel_id].ran)
        others = [r for cid, r in reports.items() if cid != broken.channel_id]
        self.assertTrue(all(r.ran for r in others))

    def test_a_channel_that_throws_does_not_stop_the_others(self):
        factory = build_factory(niches=(Niche.BUSINESS, Niche.AI))
        victim = next(iter(factory.channels))

        original = factory.run_channel

        def exploding(channel_id, *args, **kwargs):
            if channel_id == victim:
                raise RuntimeError("orchestrator bug")
            return original(channel_id, *args, **kwargs)

        factory.run_channel = exploding
        reports = factory.run_cycle({"src-1": words(MOMENT)}, now=NOW)

        self.assertEqual(len(reports), 2)
        self.assertIn("isolated failure", reports[victim].reason)
        self.assertTrue(any(r.ran for cid, r in reports.items() if cid != victim))

    def test_a_used_source_is_not_reused_next_cycle(self):
        factory = build_factory()
        transcripts = {"src-1": words(FILLER * 4 + MOMENT)}
        first = factory.run_cycle(transcripts, now=NOW)
        second = factory.run_cycle(transcripts, now=NOW + timedelta(days=1))
        self.assertEqual(next(iter(first.values())).scheduled, 1)
        self.assertEqual(next(iter(second.values())).scheduled, 0)

    def test_budget_is_charged_and_reported(self):
        factory = build_factory()
        report = next(iter(
            factory.run_cycle({"src-1": words(FILLER * 4 + MOMENT)},
                              now=NOW).values()
        ))
        self.assertEqual(report.spent_cents, ITEM_COST_CENTS)

    def test_an_exhausted_budget_changes_the_channel_state(self):
        factory = build_factory(budget_cents=ITEM_COST_CENTS)
        channel_id = next(iter(factory.channels))
        factory.run_cycle({"src-1": words(FILLER * 4 + MOMENT)}, now=NOW)
        self.assertIs(factory.channels[channel_id].state,
                      ChannelState.BUDGET_EXHAUSTED)

    def test_rights_report_surfaces_unverified_material(self):
        factory = build_factory(sources=[source("a", OWNED),
                                         source("b", Rights())])
        report = factory.rights_report(now=NOW)
        self.assertEqual(report["unverified"], 1)
        self.assertEqual(report["channels_accepting_unverified"], [])

    def test_rights_report_flags_a_lapsing_licence(self):
        soon = Rights(basis=RightsBasis.LICENSED,
                      expires_at=NOW + timedelta(days=20))
        factory = build_factory(sources=[source("a", soon)])
        report = factory.rights_report(now=NOW)
        self.assertEqual(len(report["expiring_within_90_days"]), 1)

    def test_status_serialises(self):
        factory = build_factory(niches=tuple(Niche))
        payload = json.loads(json.dumps(factory.status(now=NOW), default=str))
        self.assertEqual(payload["channels"], 7)
        self.assertIn("quota", payload)
        self.assertIn("rights", payload)


if __name__ == "__main__":
    unittest.main()
