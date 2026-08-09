"""Statistics, matched-age comparison, exploration validity, and reports.

The heart of these tests is the pair that check the engine on *known* data:
`test_finds_a_planted_effect` and `test_finds_nothing_in_pure_noise`. An
analytics engine that cannot pass both is worse than none, because it produces
confident recommendations at a volume nobody audits.
"""

from __future__ import annotations

import json
import math
import random
import unittest
from datetime import datetime, timedelta, timezone

import _support  # noqa: F401  (path setup)

from clipforge.analytics import (
    AnalyticsConfig,
    AnalyticsEngine,
    AnalyticsStore,
    Baselines,
    DEFAULT_FDR,
    ExplorationPolicy,
    MIN_EXPLORED,
    MIN_GROUP_N,
    MIN_MATERIAL_EFFECT,
    PRIMARY_CHECKPOINT_H,
    PostMetrics,
    PostRecord,
    RecordedSource,
    RetentionCurve,
    Snapshot,
    assess,
    benjamini_hochberg,
    bootstrap_ci,
    calibration,
    compare,
    diagnose_retention,
    minimum_detectable_effect,
    permutation_p,
    samples_needed,
    trimmed_mean,
)
from clipforge.publish import Platform

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


def curve(hook_hold: float, completion: float) -> RetentionCurve:
    return RetentionCurve((
        (0.0, 1.0), (0.1, hook_hold), (0.5, (hook_hold + completion) / 2),
        (1.0, completion),
    ))


def snapshot(age: float, views: int = 1000, **kwargs) -> Snapshot:
    defaults = dict(
        likes=int(views * 0.05), comments=int(views * 0.01),
        shares=int(views * 0.008), follows=int(views * 0.004),
        impressions=views * 4, avg_watch_pct=0.5,
    )
    defaults.update(kwargs)
    return Snapshot(NOW - timedelta(hours=1), age, views=views, **defaults)


def record(
    post_id: str = "p1", published_days_ago: float = 5.0,
    platform: Platform = Platform.TIKTOK, ages=(1.0, 24.0),
    views: int = 1000, **kwargs
) -> PostRecord:
    published = NOW - timedelta(days=published_days_ago)
    metrics = PostMetrics(post_id, platform, published)
    for age in ages:
        metrics.record(snapshot(age, views=int(views * (0.3 if age < 24 else 1))))
    fields = dict(
        channel_id="ch1", channel_name="Runway", niche="business",
        timezone="Europe/Amsterdam", hook_type="curiosity",
        topic="raise", creator="Podcast Co", clip_duration_s=28.0,
        predicted_lift=1.2, hook_weights_version="hook-heuristic-v1",
    )
    fields.update(kwargs)
    return PostRecord(post_id=post_id, metrics=metrics, **fields)


class TestRetentionCurve(unittest.TestCase):
    def test_interpolates_between_points(self):
        c = RetentionCurve(((0.0, 1.0), (1.0, 0.0)))
        self.assertAlmostEqual(c.at(0.5), 0.5, places=3)

    def test_clamps_outside_the_range(self):
        c = RetentionCurve(((0.2, 0.8), (0.8, 0.4)))
        self.assertEqual(c.at(0.0), 0.8)
        self.assertEqual(c.at(1.0), 0.4)

    def test_points_are_sorted_on_construction(self):
        c = RetentionCurve(((1.0, 0.2), (0.0, 1.0), (0.5, 0.6)))
        self.assertEqual([p[0] for p in c.points], [0.0, 0.5, 1.0])

    def test_unavailable_when_empty(self):
        self.assertFalse(RetentionCurve().available)
        self.assertFalse(RetentionCurve(((0.0, 1.0),)).available)

    def test_mid_drop_share_uses_the_right_denominator(self):
        # 63% get past the hook, 32% finish. As an absolute that is a 31-point
        # decline; as a share of those who gave the clip a chance it is half.
        c = curve(0.63, 0.32)
        self.assertAlmostEqual(c.mid_drop, 0.31, places=2)
        self.assertAlmostEqual(c.mid_drop_share, 0.492, places=2)

    def test_hook_failure_is_diagnosed(self):
        self.assertIn("hook", curve(0.35, 0.30).diagnosis)

    def test_payoff_failure_is_diagnosed(self):
        # Hook works, but half of those who stayed leave.
        self.assertIn("payoff", curve(0.80, 0.30).diagnosis)

    def test_a_healthy_curve_is_not_flagged(self):
        self.assertIn("healthy", curve(0.85, 0.70).diagnosis)

    def test_two_clips_with_the_same_average_get_opposite_diagnoses(self):
        # The reason the curve is kept rather than just the average.
        hook_problem = curve(0.40, 0.36)
        payoff_problem = curve(0.95, 0.20)
        self.assertAlmostEqual(
            (hook_problem.hook_hold + hook_problem.completion) / 2,
            (payoff_problem.hook_hold + payoff_problem.completion) / 2,
            delta=0.20,
        )
        self.assertNotEqual(hook_problem.diagnosis, payoff_problem.diagnosis)


class TestMatchedAge(unittest.TestCase):
    def test_returns_the_reading_nearest_the_checkpoint(self):
        metrics = PostMetrics("p", Platform.TIKTOK, NOW - timedelta(days=3))
        metrics.record(snapshot(1.0, views=100))
        metrics.record(snapshot(24.0, views=900))
        metrics.record(snapshot(168.0, views=2000))
        self.assertEqual(metrics.at_age(24.0).views, 900)

    def test_a_young_post_is_excluded_rather_than_substituted(self):
        # Substituting the latest reading is how "recent posts are
        # underperforming" gets reported when they are merely recent.
        metrics = PostMetrics("p", Platform.TIKTOK, NOW - timedelta(hours=2))
        metrics.record(snapshot(1.0, views=100))
        self.assertIsNone(metrics.at_age(24.0))
        self.assertFalse(metrics.mature_at(24.0))

    def test_no_snapshots_yields_nothing(self):
        metrics = PostMetrics("p", Platform.TIKTOK, NOW)
        self.assertIsNone(metrics.at_age(24.0))

    def test_record_keeps_snapshots_ordered(self):
        metrics = PostMetrics("p", Platform.TIKTOK, NOW - timedelta(days=9))
        metrics.record(snapshot(168.0))
        metrics.record(snapshot(1.0))
        self.assertEqual([s.age_hours for s in metrics.snapshots], [1.0, 168.0])

    def test_velocity_is_the_first_hour_share(self):
        metrics = PostMetrics("p", Platform.TIKTOK, NOW - timedelta(days=3))
        metrics.record(snapshot(1.0, views=300))
        metrics.record(snapshot(24.0, views=1000))
        self.assertAlmostEqual(metrics.velocity(), 0.3, places=3)

    def test_immature_posts_are_dropped_from_selection(self):
        store = AnalyticsStore([
            record("old", 5.0),
            record("fresh", 0.02, ages=(1.0,)),
        ])
        selected = store.select(checkpoint_h=24.0)
        self.assertEqual([r.post_id for r in selected], ["old"])


class TestStatistics(unittest.TestCase):
    def test_permutation_p_is_calibrated_under_the_null(self):
        # If this drifts, every significance claim in the product is wrong.
        rates = []
        for trial in range(120):
            rng = random.Random(trial)
            a = [math.exp(rng.gauss(7.0, 0.7)) for _ in range(20)]
            b = [math.exp(rng.gauss(7.0, 0.7)) for _ in range(60)]
            rates.append(permutation_p(a, b, trimmed_mean, iterations=300,
                                       seed=f"n{trial}"))
        false_positives = sum(1 for p in rates if p < 0.05) / len(rates)
        self.assertLess(false_positives, 0.12)

    def test_permutation_p_detects_a_real_gap(self):
        rng = random.Random(3)
        a = [rng.gauss(200, 30) for _ in range(20)]
        b = [rng.gauss(100, 30) for _ in range(40)]
        self.assertLess(permutation_p(a, b, seed="real"), 0.01)

    def test_permutation_p_never_returns_zero(self):
        a, b = [1000.0] * 20, [1.0] * 20
        self.assertGreater(permutation_p(a, b, iterations=100), 0.0)

    def test_bootstrap_ci_brackets_the_statistic(self):
        rng = random.Random(5)
        values = [rng.gauss(100, 20) for _ in range(40)]
        low, high = bootstrap_ci(values, seed="ci")
        self.assertLess(low, trimmed_mean(values))
        self.assertGreater(high, trimmed_mean(values))

    def test_bootstrap_is_deterministic(self):
        values = [float(v) for v in range(30)]
        self.assertEqual(bootstrap_ci(values, seed="x"),
                         bootstrap_ci(values, seed="x"))

    def test_trimmed_mean_resists_one_viral_outlier(self):
        typical = [100.0] * 20
        with_outlier = typical + [1_000_000.0]
        self.assertLess(
            abs(trimmed_mean(with_outlier) - 100.0),
            abs(sum(with_outlier) / len(with_outlier) - 100.0) / 100,
        )

    def test_benjamini_hochberg_rejects_a_family_of_nulls(self):
        self.assertEqual(
            benjamini_hochberg([0.4, 0.6, 0.8, 0.9, 0.95]), [False] * 5
        )

    def test_benjamini_hochberg_keeps_a_strong_finding(self):
        flags = benjamini_hochberg([0.0001, 0.5, 0.6, 0.7])
        self.assertTrue(flags[0])
        self.assertFalse(any(flags[1:]))

    def test_benjamini_hochberg_is_less_brutal_than_bonferroni(self):
        pvalues = [0.01, 0.02, 0.03, 0.9, 0.95]
        flags = benjamini_hochberg(pvalues, fdr=0.10)
        bonferroni = [p < 0.10 / len(pvalues) for p in pvalues]
        self.assertGreaterEqual(sum(flags), sum(bonferroni))

    def test_minimum_detectable_effect_shrinks_with_sample_size(self):
        small = minimum_detectable_effect(10, 10, 30.0, 100.0)
        large = minimum_detectable_effect(200, 200, 30.0, 100.0)
        self.assertGreater(small, large)

    def test_samples_needed_grows_as_the_target_shrinks(self):
        self.assertGreater(
            samples_needed(0.10, 30.0, 100.0), samples_needed(0.50, 30.0, 100.0)
        )


class TestComparison(unittest.TestCase):
    def noise(self, groups: int = 6, n: int = 14, seed: int = 1):
        rng = random.Random(seed)
        return {
            f"g{i}": [math.exp(rng.gauss(7.0, 0.7)) for _ in range(n)]
            for i in range(groups)
        }

    def test_finds_nothing_in_pure_noise(self):
        # The failure this whole module exists to prevent.
        for seed in range(6):
            result = compare("hour", "views", self.noise(seed=seed),
                             seed=f"s{seed}")
            self.assertIsNone(
                result.winner,
                f"seed {seed} manufactured a winner from noise: "
                f"{result.summary()}",
            )

    def test_finds_a_planted_effect(self):
        # n=25, comfortably above the detection floor for this variance. At
        # n=14 the same effect sits right on it and is correctly declined —
        # see the test below, which pins that behaviour deliberately.
        groups = self.noise(n=25, seed=42)
        rng = random.Random(99)
        groups["g2"] = [math.exp(rng.gauss(7.0 + 0.7, 0.7)) for _ in range(25)]
        result = compare("hour", "views", groups, seed="planted")
        self.assertIsNotNone(result.winner, result.summary())
        self.assertEqual(result.winner.name, "g2")

    def test_declines_an_effect_sitting_on_the_detection_floor(self):
        # The cost of FDR 0.05, made explicit: a real +59% effect at n=14 is
        # reported as unresolvable rather than claimed. The engine says how
        # much more data would settle it instead of guessing.
        groups = self.noise(n=14, seed=42)
        rng = random.Random(99)
        groups["g2"] = [math.exp(rng.gauss(7.0 + 0.7, 0.7)) for _ in range(14)]
        result = compare("hour", "views", groups, seed="planted")

        self.assertIsNone(result.winner)
        top = result.ranked[0]
        self.assertEqual(top.name, "g2")
        self.assertGreater(top.lift, 0.4)
        self.assertIn("cannot detect", top.verdict)

    def test_a_ranking_always_has_a_top_row_but_not_always_a_winner(self):
        result = compare("hour", "views", self.noise(seed=2), seed="s")
        self.assertTrue(result.ranked)
        self.assertIsNone(result.winner)
        self.assertFalse(result.conclusive)
        self.assertIn("no clear winner", result.summary())

    def test_small_groups_are_excluded_not_ranked(self):
        groups = {"big": [100.0] * 20, "tiny": [500.0] * 3}
        result = compare("hour", "views", groups)
        self.assertEqual([g.name for g in result.ranked], ["big"])
        self.assertEqual(result.excluded, (("tiny", 3),))

    def test_no_eligible_group_is_reported_honestly(self):
        result = compare("hour", "views", {"a": [1.0] * 3, "b": [2.0] * 2})
        self.assertEqual(result.ranked, ())
        self.assertIsNone(result.winner)
        self.assertIn("no group has", result.summary().lower())

    def test_verdict_reports_the_detection_floor(self):
        result = compare("hour", "views", self.noise(seed=4), seed="s")
        self.assertIn("cannot detect", result.ranked[0].verdict)

    def test_a_real_but_tiny_difference_is_not_a_finding(self):
        # The mirror-image failure: with a tight enough spread, a 2%
        # difference is statistically undeniable and worth nothing.
        rng = random.Random(6)
        groups = {
            f"g{i}": [1000.0 + rng.uniform(-8, 8) for _ in range(40)]
            for i in range(4)
        }
        groups["g1"] = [1020.0 + rng.uniform(-8, 8) for _ in range(40)]

        result = compare("hour", "views", groups, seed="tiny")
        top = next(g for g in result.ranked if g.name == "g1")
        self.assertTrue(top.significant, "a 2% gap here is genuinely real")
        self.assertFalse(top.material)
        self.assertIsNone(result.winner, "but it must not be a finding")
        self.assertIn("below the 10% worth acting on", top.verdict)

    def test_material_threshold_is_relative(self):
        self.assertLess(MIN_MATERIAL_EFFECT, 0.5)
        self.assertGreater(MIN_MATERIAL_EFFECT, 0.0)

    def test_results_serialise(self):
        payload = json.loads(json.dumps(
            compare("hour", "views", self.noise()).to_dict()
        ))
        self.assertIn("groups", payload)
        self.assertIn("summary", payload)


class TestExploration(unittest.TestCase):
    def test_assignment_is_deterministic(self):
        policy = ExplorationPolicy()
        self.assertEqual(policy.assign(20, "p1").index,
                         policy.assign(20, "p1").index)

    def test_rate_is_roughly_honoured(self):
        policy = ExplorationPolicy(rate=0.20)
        explored = sum(
            1 for i in range(2000) if policy.assign(20, f"p{i}").explored
        )
        self.assertGreater(explored / 2000, 0.15)
        self.assertLess(explored / 2000, 0.25)

    def test_exploration_never_picks_rank_zero(self):
        # Picking the top would inflate the explored count without producing
        # any unconfounded contrast.
        policy = ExplorationPolicy(rate=1.0)
        for i in range(200):
            assignment = policy.assign(20, f"p{i}")
            self.assertTrue(assignment.explored)
            self.assertGreater(assignment.index, 0)

    def test_exploration_stays_inside_the_depth(self):
        policy = ExplorationPolicy(rate=1.0, depth=5)
        for i in range(200):
            self.assertLess(policy.assign(20, f"p{i}").index, 5)

    def test_disabled_always_takes_the_top(self):
        policy = ExplorationPolicy(enabled=False)
        for i in range(50):
            self.assertFalse(policy.assign(20, f"p{i}").explored)

    def test_a_single_variant_cannot_be_explored(self):
        self.assertFalse(ExplorationPolicy(rate=1.0).assign(1, "p").explored)

    def test_hook_comparison_is_confounded_without_exploration(self):
        records = [record(f"p{i}", explored=False) for i in range(30)]
        validity = assess(records, "hook_type")
        self.assertFalse(validity.causal)
        self.assertIn("confounded", validity.caveat)

    def test_hook_comparison_becomes_causal_with_enough_exploration(self):
        records = [
            record(f"p{i}", explored=i < MIN_EXPLORED + 2) for i in range(40)
        ]
        self.assertTrue(assess(records, "hook_type").causal)

    def test_non_chosen_dimensions_are_observational_not_confounded(self):
        # Posting time is not something the model ranked, so it carries the
        # usual observational caveat rather than the selection-loop one.
        validity = assess([record(f"p{i}") for i in range(30)], "hour")
        self.assertFalse(validity.causal)
        self.assertIn("observational", validity.caveat)
        self.assertNotIn("confounded", validity.caveat)


class TestBaselines(unittest.TestCase):
    def test_defaults_are_used_until_there_is_history(self):
        baselines = Baselines()
        self.assertFalse(baselines.is_observed(Platform.TIKTOK))
        self.assertGreater(baselines.get(Platform.TIKTOK, "views_24h"), 0)

    def test_learns_the_accounts_own_median(self):
        metrics = []
        for i in range(20):
            m = PostMetrics(f"p{i}", Platform.TIKTOK, NOW - timedelta(days=3))
            m.record(snapshot(24.0, views=500))
            metrics.append(m)
        baselines = Baselines()
        baselines.learn(metrics)
        self.assertTrue(baselines.is_observed(Platform.TIKTOK))
        self.assertEqual(baselines.get(Platform.TIKTOK, "views_24h"), 500.0)

    def test_a_single_viral_post_does_not_redefine_normal(self):
        metrics = []
        for i in range(20):
            m = PostMetrics(f"p{i}", Platform.TIKTOK, NOW - timedelta(days=3))
            m.record(snapshot(24.0, views=10_000_000 if i == 0 else 500))
            metrics.append(m)
        baselines = Baselines()
        baselines.learn(metrics)
        self.assertEqual(baselines.get(Platform.TIKTOK, "views_24h"), 500.0)

    def test_index_makes_platforms_comparable(self):
        baselines = Baselines()
        at_baseline = baselines.index(
            Platform.TIKTOK, "views_24h",
            baselines.get(Platform.TIKTOK, "views_24h"),
        )
        self.assertAlmostEqual(at_baseline, 100.0, places=3)


class TestCalibration(unittest.TestCase):
    def test_reports_insufficient_data(self):
        result = calibration([record(f"p{i}") for i in range(3)])
        self.assertIn("not enough", result.verdict)

    def test_detects_a_predictive_model(self):
        records = []
        for i in range(30):
            views = 500 + i * 120
            records.append(record(
                f"p{i}", views=views, predicted_lift=0.7 + i * 0.03,
            ))
        result = calibration(records, "predicted_lift", "views")
        self.assertGreater(result.correlation, 0.5)
        self.assertIn("predictive", result.verdict)

    def test_detects_a_useless_model(self):
        rng = random.Random(4)
        records = [
            record(f"p{i}", views=rng.randint(100, 5000),
                   predicted_lift=rng.uniform(0.7, 1.7))
            for i in range(40)
        ]
        result = calibration(records, "predicted_lift", "views")
        self.assertIn("chance", result.verdict)

    def test_detects_an_inverted_model(self):
        records = [
            record(f"p{i}", views=5000 - i * 120, predicted_lift=0.7 + i * 0.03)
            for i in range(30)
        ]
        result = calibration(records, "predicted_lift", "views")
        self.assertIn("inverted", result.verdict)

    def test_records_the_weights_version(self):
        result = calibration([record(f"p{i}") for i in range(12)])
        self.assertEqual(result.weights_version, "hook-heuristic-v1")


class TestRetentionDiagnosis(unittest.TestCase):
    def test_no_curve_is_reported_not_imputed(self):
        result = diagnose_retention([record(f"p{i}") for i in range(10)])
        self.assertEqual(result.n, 0)
        self.assertIn("no platform", result.dominant_problem)

    def test_hook_problem_across_posts(self):
        records = []
        for i in range(10):
            r = record(f"p{i}", platform=Platform.YOUTUBE)
            r.metrics.snapshots[-1] = snapshot(24.0, retention=curve(0.35, 0.30))
            records.append(r)
        self.assertIn("hooks", diagnose_retention(records).dominant_problem)

    def test_payoff_problem_across_posts(self):
        records = []
        for i in range(10):
            r = record(f"p{i}", platform=Platform.YOUTUBE)
            r.metrics.snapshots[-1] = snapshot(24.0, retention=curve(0.85, 0.25))
            records.append(r)
        problem = diagnose_retention(records).dominant_problem
        self.assertIn("payoff", problem)
        self.assertIn("not better hooks", problem)


class TestEngineAndReport(unittest.TestCase):
    def build(self, posts: int = 60, explored_every: int = 4) -> AnalyticsEngine:
        engine = AnalyticsEngine(AnalyticsConfig(seed="test"))
        rng = random.Random(8)
        for i in range(posts):
            engine.track(record(
                f"p{i}",
                published_days_ago=1.5 + i * 0.6,
                platform=Platform.YOUTUBE if i % 2 else Platform.TIKTOK,
                # Tight spread on purpose: this fixture is used to check that a
                # genuinely flat week reports flat, and wide random views would
                # make that assertion fire on chance alone.
                views=1000 + rng.randint(-60, 60),
                hook_type=("curiosity", "authority", "fear")[i % 3],
                topic=("raise", "hiring", "runway")[i % 3],
                creator=("Podcast Co", "Studio Nine")[i % 2],
                explored=(i % explored_every == 0),
            ))
        return engine

    def test_report_builds(self):
        report = self.build().report(NOW)
        self.assertGreater(len(report.insights), 8)
        self.assertIsNotNone(report.retention)
        self.assertTrue(report.notes)

    def test_report_renders_as_text(self):
        text = self.build().report(NOW).render()
        self.assertIn("WEEKLY REPORT", text)
        self.assertIn("FINDINGS", text)

    def test_report_separates_answerable_from_not(self):
        report = self.build().report(NOW)
        self.assertEqual(
            len(report.actionable) + len(report.waiting), len(report.insights)
        )

    def test_report_notes_the_lookback_window(self):
        report = self.build().report(NOW)
        self.assertTrue(any("not over this week alone" in n
                            for n in report.notes))

    def test_report_excludes_immature_posts_and_says_so(self):
        engine = self.build()
        engine.track(record("fresh", published_days_ago=0.02, ages=(1.0,)))
        report = engine.report(NOW)
        self.assertGreater(report.posts, report.mature_posts)
        self.assertTrue(any("younger than" in n for n in report.notes))

    def test_a_flat_week_is_reported_as_flat(self):
        # These views vary by ~6%, which is statistically detectable at this
        # sample size and operationally nothing. Neither bar alone would
        # suppress it; both together do.
        report = self.build().report(NOW)
        self.assertTrue(
            all(d.arrow == "=" for d in report.deltas),
            [d.describe() for d in report.deltas],
        )
        self.assertTrue(any("too small to matter" in d.describe()
                            for d in report.deltas))

    def test_report_serialises(self):
        payload = json.loads(json.dumps(
            self.build().report(NOW).to_dict(), default=str
        ))
        self.assertIn("actionable", payload)
        self.assertIn("waiting", payload)

    def test_per_channel_reports_plus_a_combined_one(self):
        reports = self.build().reports_for_channels(NOW)
        self.assertIn("__all__", reports)
        self.assertIn("ch1", reports)

    def test_ingest_collects_and_survives_a_bad_post(self):
        engine = AnalyticsEngine()
        engine.track(record("p1", ages=()))

        class Angry(RecordedSource):
            def fetch(self, post_id, platform):
                raise RuntimeError("platform is down")

        result = engine.ingest(Angry(), now=NOW)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["collected"], 0)

    def test_ingest_records_snapshots(self):
        engine = AnalyticsEngine()
        engine.track(record("p1", ages=()))
        source = RecordedSource({"p1": [snapshot(24.0, views=900)]})
        self.assertEqual(engine.ingest(source, now=NOW)["collected"], 1)
        self.assertEqual(engine.store.get("p1").metrics.at_age(24.0).views, 900)

    def test_due_checkpoints_finds_gaps(self):
        engine = AnalyticsEngine()
        engine.track(record("p1", published_days_ago=10.0, ages=(1.0,)))
        due = dict(engine.due_checkpoints(NOW))
        self.assertIn("p1", due)

    def test_readiness_reports_what_is_missing(self):
        readiness = self.build(posts=20, explored_every=100).readiness()
        self.assertFalse(readiness["hook_questions_causal"])
        self.assertGreater(readiness["explored_needed"], 0)

    def test_readiness_clears_once_exploration_has_run(self):
        self.assertTrue(
            self.build(posts=60, explored_every=2)
            .readiness()["hook_questions_causal"]
        )

    def test_status_serialises(self):
        payload = json.loads(json.dumps(self.build().status(), default=str))
        self.assertIn("readiness", payload)
        self.assertIn("calibration", payload)

    def test_next_report_time_follows_the_schedule(self):
        engine = self.build()
        following = engine.next_report_at(NOW)
        self.assertIsNotNone(following)
        self.assertEqual(following.weekday(), 0)   # Monday


if __name__ == "__main__":
    unittest.main()
