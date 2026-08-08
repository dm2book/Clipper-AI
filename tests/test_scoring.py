"""Feature extraction, score synthesis, and ranking tests."""

from __future__ import annotations

import unittest

from _support import solo, transcript

from clipforge.viral import features, ranking, scoring
from clipforge.viral.taxonomy import (
    IDEAL_DURATION_S,
    MAX_DURATION_S,
    MIN_DURATION_S,
    VIRALITY_MIX,
    duration_fit,
)
from clipforge.viral.types import (
    Candidate,
    LlmVerdict,
    Moment,
    Scores,
    Signal,
    SignalHit,
)


def candidate(text: str, start_ms: int = 0, duration_ms: int = 28_000) -> Candidate:
    return Candidate(
        first_utterance=0,
        last_utterance=0,
        start_ms=start_ms,
        end_ms=start_ms + duration_ms,
        text=text,
    )


def moment(virality: int, start_ms: int, duration_ms: int = 28_000) -> Moment:
    return Moment(
        candidate=candidate("x", start_ms, duration_ms),
        scores=Scores(virality, 50, 50, 50, 50),
        features={},
        signals={},
    )


class TestDurationFit(unittest.TestCase):
    def test_sweet_spot_is_perfect(self) -> None:
        for seconds in (IDEAL_DURATION_S[0], 28.0, IDEAL_DURATION_S[1]):
            self.assertEqual(duration_fit(seconds), 1.0)

    def test_outside_hard_bounds_is_zero(self) -> None:
        self.assertEqual(duration_fit(MIN_DURATION_S - 0.1), 0.0)
        self.assertEqual(duration_fit(MAX_DURATION_S + 0.1), 0.0)

    def test_tapers_on_both_sides(self) -> None:
        self.assertLess(duration_fit(12.0), 1.0)
        self.assertLess(duration_fit(60.0), 1.0)
        self.assertGreater(duration_fit(12.0), 0.0)
        self.assertGreater(duration_fit(60.0), 0.0)

    def test_short_is_penalised_harder_than_long(self) -> None:
        """A clip 40% below the band should score worse than one 40% above."""
        below = IDEAL_DURATION_S[0] - (IDEAL_DURATION_S[0] - MIN_DURATION_S) * 0.4
        above = IDEAL_DURATION_S[1] + (MAX_DURATION_S - IDEAL_DURATION_S[1]) * 0.4
        self.assertLess(duration_fit(below), duration_fit(above))


class TestHookStrength(unittest.TestCase):
    def test_strong_openers_beat_weak(self) -> None:
        t = solo("x")
        strong = features.hook_strength(
            candidate("The biggest mistake I ever made cost me $18 million."), t
        )
        weak = features.hook_strength(
            candidate("So, yeah, I mean, we sort of looked at the numbers."), t
        )
        self.assertGreater(strong, weak)
        self.assertGreater(strong, 0.5)

    def test_weak_opener_is_penalised(self) -> None:
        t = solo("x")
        clean = features.hook_strength(candidate("Why did nobody see this coming?"), t)
        hedged = features.hook_strength(
            candidate("So, why did nobody see this coming?"), t
        )
        self.assertLess(hedged, clean)

    def test_only_the_opening_is_read(self) -> None:
        """A great line 40 words in cannot rescue a weak hook."""
        t = solo("x")
        filler = "and then we talked about it for a while and nothing much happened "
        buried = features.hook_strength(
            candidate(filler * 3 + "I lost $18 million in one day."), t
        )
        self.assertLess(buried, 0.4)

    def test_empty_text(self) -> None:
        self.assertEqual(features.hook_strength(candidate(""), solo("x")), 0.0)


class TestStandalone(unittest.TestCase):
    def test_dangling_reference_is_penalised(self) -> None:
        t = solo("x")
        clear = features.standalone(
            candidate("Venture funding is a deadline, not a product."), t
        )
        dangling = features.standalone(
            candidate("That's why it completely fell apart for them."), t
        )
        self.assertLess(dangling, clear)

    def test_unnamed_pronouns_penalised(self) -> None:
        t = solo("x")
        named = features.standalone(
            candidate("Dana told the board they were wrong about the timeline."), t
        )
        anonymous = features.standalone(
            candidate("He told them they were wrong and they told him it was his call."), t
        )
        self.assertLess(anonymous, named)

    def test_bounded(self) -> None:
        t = solo("x")
        for text in ("", "That's it.", "A clean standalone statement of fact."):
            value = features.standalone(candidate(text), t)
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)


class TestPayoffAndQuestions(unittest.TestCase):
    def test_resolution_marker_in_the_tail_scores(self) -> None:
        t = transcript(("A", "We tried everything."), ("A", "And that's why we shut it down."))
        c = Candidate(0, 1, 0, 10_000, t.text_between(0, 1))
        self.assertGreater(features.payoff(c, t), 0.4)

    def test_unresolved_ending_scores_low(self) -> None:
        t = transcript(("A", "We tried everything."), ("A", "But who really knows?"))
        c = Candidate(0, 1, 0, 10_000, t.text_between(0, 1))
        self.assertLess(features.payoff(c, t), 0.4)

    def test_direct_audience_question(self) -> None:
        t = solo("x")
        self.assertEqual(
            features.audience_question(candidate("Am I wrong? Tell me I'm wrong."), t), 1.0
        )
        self.assertEqual(features.audience_question(candidate("A flat statement."), t), 0.0)

    def test_speaker_balance_penalises_choppy(self) -> None:
        rapid = transcript(*[(("A", "B")[i % 2], "yes") for i in range(10)], seconds_each=0.5)
        c = Candidate(0, 9, 0, 5_000, rapid.text_between(0, 9))
        self.assertLess(features.speaker_balance(c, rapid), 1.0)

    def test_speaker_balance_single_voice(self) -> None:
        mono = transcript(("A", "one"), ("A", "two"))
        c = Candidate(0, 1, 0, 10_000, mono.text_between(0, 1))
        self.assertEqual(features.speaker_balance(c, mono), 1.0)


class TestBehaviourScores(unittest.TestCase):
    def test_controversy_drives_comments_hardest(self) -> None:
        scores = scoring.behaviour_scores({Signal.CONTROVERSY: 1.0})
        self.assertEqual(max(scores, key=lambda k: scores[k]), "comment")

    def test_lesson_drives_shares_hardest(self) -> None:
        scores = scoring.behaviour_scores({Signal.LESSON: 1.0})
        self.assertEqual(max(scores, key=lambda k: scores[k]), "share")

    def test_secret_drives_retention_hardest(self) -> None:
        scores = scoring.behaviour_scores({Signal.SECRET: 1.0})
        self.assertEqual(max(scores, key=lambda k: scores[k]), "retention")

    def test_no_signals_is_all_zero(self) -> None:
        self.assertEqual(set(scoring.behaviour_scores({}).values()), {0.0})

    def test_extra_signals_never_reduce_a_score(self) -> None:
        one = scoring.behaviour_scores({Signal.FUNNY: 0.8})
        two = scoring.behaviour_scores({Signal.FUNNY: 0.8, Signal.MONEY: 0.6})
        for behaviour, value in one.items():
            self.assertGreaterEqual(two[behaviour], value - 1e-9)

    def test_bounded(self) -> None:
        everything = {signal: 1.0 for signal in Signal}
        for value in scoring.behaviour_scores(everything).values():
            self.assertLessEqual(value, 1.0)


class TestModifiersAndVirality(unittest.TestCase):
    def test_modifiers_only_ever_reduce(self) -> None:
        raw = {b: 0.8 for b in scoring.BEHAVIOURS}
        perfect = scoring.apply_modifiers(raw, {k: 1.0 for k in (
            "hook_strength", "standalone", "payoff", "duration_fit", "audience_question"
        )})
        for behaviour, value in perfect.items():
            self.assertAlmostEqual(value, raw[behaviour], places=6)

    def test_zero_features_damp_heavily(self) -> None:
        raw = {b: 0.8 for b in scoring.BEHAVIOURS}
        damped = scoring.apply_modifiers(raw, {})
        for behaviour, value in damped.items():
            self.assertLess(value, raw[behaviour])

    def test_virality_weights_retention_above_comment(self) -> None:
        self.assertGreater(VIRALITY_MIX["retention"], VIRALITY_MIX["comment"])
        retention_heavy = scoring.virality(
            {"retention": 1.0, "share": 0.0, "engagement": 0.0, "comment": 0.0}
        )
        comment_heavy = scoring.virality(
            {"retention": 0.0, "share": 0.0, "engagement": 0.0, "comment": 1.0}
        )
        self.assertGreater(retention_heavy, comment_heavy)

    def test_virality_mix_sums_to_one(self) -> None:
        self.assertAlmostEqual(sum(VIRALITY_MIX.values()), 1.0, places=6)


class TestLlmBlend(unittest.TestCase):
    def test_no_verdict_leaves_features_alone_but_seeds_quotability(self) -> None:
        merged = scoring.blend_llm({"standalone": 0.3}, None, {"standalone": 0.9})
        self.assertEqual(merged["standalone"], 0.3)
        self.assertEqual(merged["quotability"], 0.5)

    def test_verdict_overrides_toward_llm(self) -> None:
        verdict = LlmVerdict(
            candidate_span=(0, 0),
            hook_strength=1.0,
            standalone=1.0,
            payoff=1.0,
            quotability=1.0,
            title="t",
            rationale="r",
        )
        merged = scoring.blend_llm(
            {"standalone": 0.0, "hook_strength": 0.0, "payoff": 0.0},
            verdict,
            {"standalone": 0.85, "hook_strength": 0.75, "payoff": 0.6, "quotability": 1.0},
        )
        self.assertAlmostEqual(merged["standalone"], 0.85)
        self.assertAlmostEqual(merged["hook_strength"], 0.75)
        self.assertAlmostEqual(merged["payoff"], 0.6)
        self.assertAlmostEqual(merged["quotability"], 1.0)


class TestScoreCandidate(unittest.TestCase):
    def test_scores_are_percentages(self) -> None:
        c = Candidate(
            0, 0, 0, 28_000, "text",
            hits=(SignalHit(Signal.SECRET, 0.9, 0, "evidence"),),
        )
        scores, signals, merged = scoring.score_candidate(
            c, features.extract(c, solo("text"))
        )
        for name, value in scores.as_dict().items():
            with self.subTest(score=name):
                self.assertIsInstance(value, int)
                self.assertGreaterEqual(value, 0)
                self.assertLessEqual(value, 100)
        self.assertIn(Signal.SECRET, signals)
        self.assertIn("quotability", merged)

    def test_llm_only_signals_are_admitted(self) -> None:
        c = Candidate(0, 0, 0, 28_000, "text")
        _, signals, _ = scoring.score_candidate(
            c, features.extract(c, solo("text")), extra_signals=(Signal.FUNNY,)
        )
        self.assertIn(Signal.FUNNY, signals)

    def test_detector_strength_wins_over_llm_default(self) -> None:
        """An LLM-reported signal must not overwrite a stronger detector hit."""
        c = Candidate(
            0, 0, 0, 28_000, "text",
            hits=(SignalHit(Signal.FUNNY, 0.95, 0, "e"),),
        )
        _, signals, _ = scoring.score_candidate(
            c, features.extract(c, solo("text")), extra_signals=(Signal.FUNNY,)
        )
        self.assertAlmostEqual(signals[Signal.FUNNY], 0.95)


class TestRanking(unittest.TestCase):
    def test_overlapping_windows_collapse_to_the_best(self) -> None:
        kept = ranking.suppress_overlaps(
            [moment(90, 0), moment(70, 1_000), moment(60, 2_000)]
        )
        self.assertEqual([m.scores.virality for m in kept], [90])

    def test_disjoint_windows_all_survive(self) -> None:
        kept = ranking.suppress_overlaps([moment(90, 0), moment(70, 200_000)])
        self.assertEqual(len(kept), 2)

    def test_diversity_caps_a_hot_region(self) -> None:
        crowded = [moment(90 - i, i * 30_000) for i in range(6)]  # all in bucket 0
        kept = ranking.enforce_diversity(crowded, bucket_ms=600_000, per_bucket=2)
        self.assertEqual(len(kept), 2)

    def test_min_virality_filters(self) -> None:
        top, ranked = ranking.select(
            [moment(80, 0), moment(20, 500_000)], limit=5, min_virality=50
        )
        self.assertEqual(len(ranked), 1)
        self.assertEqual(len(top), 1)

    def test_backfill_when_diversity_starves_output(self) -> None:
        """A short source has few buckets; the cap must not starve the result."""
        crowded = [moment(90 - i, i * 40_000) for i in range(5)]
        top, _ = ranking.select(
            crowded, limit=4, bucket_ms=600_000, per_bucket=1, iou_threshold=0.9
        )
        self.assertEqual(len(top), 4)

    def test_output_is_sorted_by_virality(self) -> None:
        top, _ = ranking.select(
            [moment(40, 0), moment(90, 200_000), moment(65, 400_000)], limit=3
        )
        self.assertEqual([m.scores.virality for m in top], [90, 65, 40])

    def test_overlap_ratio(self) -> None:
        a = candidate("a", 0, 10_000)
        self.assertEqual(a.overlap_ratio(candidate("b", 0, 10_000)), 1.0)
        self.assertEqual(a.overlap_ratio(candidate("b", 50_000, 10_000)), 0.0)


if __name__ == "__main__":
    unittest.main()
