"""End-to-end engine tests, including LLM-tier integration via a fake judge."""

from __future__ import annotations

import json
import unittest

from _support import DEMO_TRANSCRIPT, transcript

from clipforge.viral import (
    NullJudge,
    Transcript,
    ViralConfig,
    ViralDetectionEngine,
    load_json,
)
from clipforge.viral.llm import AnthropicJudge
from clipforge.viral.types import Candidate, LlmVerdict, Signal


class RecordingJudge:
    """A judge that records what it saw and returns a fixed verdict."""

    def __init__(self, **verdict_fields: float) -> None:
        self.seen: list[list[Candidate]] = []
        self.fields = {
            "hook_strength": 0.9,
            "standalone": 0.9,
            "payoff": 0.9,
            "quotability": 0.9,
            **verdict_fields,
        }

    def judge(self, candidates):
        self.seen.append(list(candidates))
        return [
            LlmVerdict(
                candidate_span=c.span,
                title="Judged title",
                rationale="Judged rationale",
                signals=(Signal.SECRET,),
                **self.fields,
            )
            for c in candidates
        ]


class ExplodingJudge:
    def judge(self, candidates):
        raise RuntimeError("provider is down")


class TestEngineEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.transcript = load_json(DEMO_TRANSCRIPT)

    def test_returns_clips_from_the_sample(self) -> None:
        result = ViralDetectionEngine(ViralConfig(max_clips=6)).detect(self.transcript)
        self.assertTrue(result.top)
        self.assertLessEqual(len(result.top), 6)

    def test_results_are_ordered_by_virality(self) -> None:
        result = ViralDetectionEngine().detect(self.transcript)
        viralities = [m.scores.virality for m in result.top]
        self.assertEqual(viralities, sorted(viralities, reverse=True))

    def test_clips_do_not_substantially_overlap(self) -> None:
        result = ViralDetectionEngine().detect(self.transcript)
        for i, a in enumerate(result.top):
            for b in result.top[i + 1 :]:
                with self.subTest(a=a.start_ms, b=b.start_ms):
                    self.assertLessEqual(a.candidate.overlap_ratio(b.candidate), 0.35)

    def test_clip_boundaries_are_inside_the_source(self) -> None:
        result = ViralDetectionEngine().detect(self.transcript)
        for moment in result.top:
            self.assertGreaterEqual(moment.start_ms, 0)
            self.assertLessEqual(moment.end_ms, self.transcript.duration_ms)
            self.assertLess(moment.start_ms, moment.end_ms)

    def test_min_virality_is_respected(self) -> None:
        result = ViralDetectionEngine(ViralConfig(min_virality=60)).detect(self.transcript)
        for moment in result.top:
            self.assertGreaterEqual(moment.scores.virality, 60)

    def test_high_threshold_returns_nothing_rather_than_filler(self) -> None:
        result = ViralDetectionEngine(ViralConfig(min_virality=100)).detect(self.transcript)
        self.assertEqual(result.top, [])

    def test_every_signal_category_appears_somewhere(self) -> None:
        """The fixture exercises all ten categories; the engine should see them."""
        engine = ViralDetectionEngine(ViralConfig(max_clips=40, min_virality=0))
        result = engine.detect(self.transcript)
        found: set[Signal] = set()
        for moment in result.ranked:
            found.update(s for s, v in moment.signals.items() if v > 0)
        self.assertEqual(found, set(Signal), f"missing: {set(Signal) - found}")

    def test_result_serialises_to_json(self) -> None:
        result = ViralDetectionEngine().detect(self.transcript)
        payload = json.loads(json.dumps(result.to_dict()))
        self.assertEqual(payload["source_id"], "demo-founder-interview")
        clip = payload["clips"][0]
        self.assertEqual(
            set(clip["scores"]),
            {"virality", "engagement", "retention", "comment", "share"},
        )

    def test_stats_are_populated(self) -> None:
        stats = ViralDetectionEngine().detect(self.transcript).stats
        for key in ("weights_version", "candidates", "signal_hits", "returned"):
            self.assertIn(key, stats)
        self.assertGreater(stats["signal_hits"], 0)


class TestEngineEdgeCases(unittest.TestCase):
    def test_empty_transcript(self) -> None:
        result = ViralDetectionEngine().detect(Transcript("empty", ()))
        self.assertEqual(result.top, [])
        self.assertIn("reason", result.stats)

    def test_transcript_too_short_for_any_window(self) -> None:
        """Below the minimum clip length there is nothing valid to return."""
        tiny = transcript(("A", "Hello there."), seconds_each=2.0)
        result = ViralDetectionEngine().detect(tiny)
        self.assertEqual(result.top, [])

    def test_source_with_no_detector_hits_still_produces_candidates(self) -> None:
        """A calm explainer trips no keyword patterns but must not vanish."""
        calm = transcript(
            *[("A", f"Step {i} is to open the configuration file and edit it.")
              for i in range(12)],
            seconds_each=4.0,
        )
        result = ViralDetectionEngine(ViralConfig(min_virality=0)).detect(calm)
        self.assertTrue(result.stats["used_fallback_windows"])
        self.assertGreater(result.stats["candidates"], 0)

    def test_null_judge_produces_no_verdicts(self) -> None:
        result = ViralDetectionEngine(
            ViralConfig(triage_judge=NullJudge(), deep_judge=NullJudge())
        ).detect(load_json(DEMO_TRANSCRIPT))
        self.assertEqual(result.stats["llm_verdicts"], 0)
        self.assertFalse(any(m.judged_by_llm for m in result.top))


class TestLlmTierIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.transcript = load_json(DEMO_TRANSCRIPT)

    def test_verdicts_are_applied_to_moments(self) -> None:
        judge = RecordingJudge()
        result = ViralDetectionEngine(
            ViralConfig(triage_judge=judge, deep_judge=judge, max_clips=5)
        ).detect(self.transcript)

        self.assertGreater(result.stats["llm_verdicts"], 0)
        judged = [m for m in result.top if m.judged_by_llm]
        self.assertTrue(judged)
        self.assertEqual(judged[0].title, "Judged title")
        self.assertEqual(judged[0].rationale, "Judged rationale")

    def test_judge_only_sees_the_shortlist(self) -> None:
        """The expensive tier must not be handed the whole candidate pool."""
        judge = RecordingJudge()
        config = ViralConfig(triage_judge=judge, deep_judge=NullJudge(), deep_judge_limit=10)
        result = ViralDetectionEngine(config).detect(self.transcript)

        self.assertTrue(judge.seen)
        self.assertLessEqual(len(judge.seen[0]), 10)
        self.assertLess(len(judge.seen[0]), result.stats["candidates"])

    def test_a_failing_judge_degrades_instead_of_raising(self) -> None:
        config = ViralConfig(triage_judge=ExplodingJudge(), deep_judge=ExplodingJudge())
        with self.assertLogs("clipforge.viral.engine", level="ERROR"):
            result = ViralDetectionEngine(config).detect(self.transcript)
        self.assertTrue(result.top)
        self.assertEqual(result.stats["llm_verdicts"], 0)

    def test_positive_verdicts_raise_scores(self) -> None:
        base = ViralDetectionEngine(ViralConfig(min_virality=0)).detect(self.transcript)
        judged = ViralDetectionEngine(
            ViralConfig(
                min_virality=0,
                triage_judge=RecordingJudge(),
                deep_judge=RecordingJudge(),
            )
        ).detect(self.transcript)
        self.assertGreater(
            judged.top[0].scores.virality, base.top[0].scores.virality
        )

    def test_negative_verdicts_lower_scores(self) -> None:
        harsh = RecordingJudge(
            hook_strength=0.0, standalone=0.0, payoff=0.0, quotability=0.0
        )
        base = ViralDetectionEngine(ViralConfig(min_virality=0)).detect(self.transcript)
        judged = ViralDetectionEngine(
            ViralConfig(min_virality=0, triage_judge=harsh, deep_judge=harsh)
        ).detect(self.transcript)
        self.assertLess(judged.top[0].scores.virality, base.top[0].scores.virality)


class TestAnthropicJudgeParsing(unittest.TestCase):
    """Response parsing is pure and testable without touching the network."""

    def setUp(self) -> None:
        self.batch = [
            Candidate(0, 1, 0, 20_000, "first candidate"),
            Candidate(2, 3, 30_000, 55_000, "second candidate"),
        ]

    def parse(self, payload: dict) -> list[LlmVerdict]:
        return AnthropicJudge._parse(json.dumps(payload), self.batch)

    def test_parses_a_well_formed_response(self) -> None:
        verdicts = self.parse({
            "verdicts": [{
                "id": 1,
                "hook_strength": 0.8,
                "standalone": 0.6,
                "payoff": 0.7,
                "quotability": 0.5,
                "signals": ["secret", "money"],
                "title": "A title",
                "rationale": "Because.",
            }]
        })
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0].candidate_span, self.batch[1].span)
        self.assertEqual(verdicts[0].signals, (Signal.SECRET, Signal.MONEY))

    def test_out_of_range_scores_are_clamped(self) -> None:
        """The JSON schema cannot express numeric bounds, so the parser must."""
        verdicts = self.parse({
            "verdicts": [{
                "id": 0,
                "hook_strength": 4.2,
                "standalone": -1.0,
                "payoff": 0.5,
                "quotability": 0.5,
                "signals": [],
                "title": "t",
                "rationale": "r",
            }]
        })
        self.assertEqual(verdicts[0].hook_strength, 1.0)
        self.assertEqual(verdicts[0].standalone, 0.0)

    def test_unknown_signal_names_are_dropped(self) -> None:
        verdicts = self.parse({
            "verdicts": [{
                "id": 0, "hook_strength": 0.5, "standalone": 0.5,
                "payoff": 0.5, "quotability": 0.5,
                "signals": ["money", "vibes"], "title": "t", "rationale": "r",
            }]
        })
        self.assertEqual(verdicts[0].signals, (Signal.MONEY,))

    def test_out_of_range_ids_are_skipped(self) -> None:
        with self.assertLogs("clipforge.viral.llm", level="WARNING"):
            verdicts = self.parse({
                "verdicts": [{
                    "id": 99, "hook_strength": 0.5, "standalone": 0.5,
                    "payoff": 0.5, "quotability": 0.5,
                    "signals": [], "title": "t", "rationale": "r",
                }]
            })
        self.assertEqual(verdicts, [])

    def test_empty_verdict_list(self) -> None:
        self.assertEqual(self.parse({"verdicts": []}), [])


if __name__ == "__main__":
    unittest.main()
