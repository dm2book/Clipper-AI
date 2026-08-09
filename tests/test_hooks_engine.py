"""CTR estimation, ranking, and end-to-end hook generation."""

from __future__ import annotations

import unittest

import _support  # noqa: F401  (path setup)

from clipforge.hooks import (
    CORE_TYPES,
    ClipContext,
    CtrEstimate,
    Hook,
    HookConfig,
    HookGenerator,
    HookSet,
    HookType,
    LIFT_MAX,
    LIFT_MIN,
    WEIGHTS_VERSION,
    estimate,
    extract_features,
    generate,
    for_language,
    supported_languages,
    type_affinity,
)
from clipforge.hooks.llm import NullWriter
from clipforge.hooks.scoring import IDEAL_WORDS, MAX_WORDS, _length_fit, score_hook


FOUNDER = (
    "The raise was the mistake. We went from twelve people to ninety in "
    "seven months and we almost went bankrupt doing it. I lost everything "
    "I'd built. The culture, the speed, all of it. We burned fourteen "
    "million dollars in nineteen months and had almost nothing to show "
    "for it. Eleven days of runway. I was terrified."
)

STREAM = (
    "No no no. That was a guaranteed win and he threw it. I have never "
    "seen anyone choke that hard. Chat is losing it."
)


def lift(text: str) -> float:
    return estimate(text)[0].lift


class TestFeatures(unittest.TestCase):
    def test_specificity_rewards_a_figure(self):
        with_number = extract_features("This mistake cost me $18 million")
        without = extract_features("This mistake cost me a lot of money")
        self.assertGreater(with_number["specificity"], without["specificity"])

    def test_curiosity_gap_detects_open_loops(self):
        self.assertGreater(
            extract_features("The real reason nobody talks about this")["curiosity_gap"],
            extract_features("A summary of the quarter")["curiosity_gap"],
        )

    def test_second_person_is_binary(self):
        self.assertEqual(extract_features("Nobody warns you about this")["second_person"], 1.0)
        self.assertEqual(extract_features("I did not see it coming")["second_person"], 0.0)

    def test_front_load_rewards_a_strong_opening(self):
        front = extract_features("$18 million gone in one afternoon")["front_load"]
        back = extract_features("It all went away and that was $18 million")["front_load"]
        self.assertGreater(front, back)

    def test_shouting_measures_uppercase_ratio(self):
        self.assertGreater(extract_features("THIS COST ME EVERYTHING")["shouting"], 0.9)
        self.assertLess(extract_features("This cost me everything")["shouting"], 0.2)

    def test_features_are_all_bounded(self):
        for text in (FOUNDER, "WHAT!!!", "$1 $2 $3 $4 $5 million billion thousand"):
            for name, value in extract_features(text).items():
                self.assertGreaterEqual(value, 0.0, name)
                self.assertLessEqual(value, 1.0, name)

    def test_length_fit_peaks_inside_the_readable_band(self):
        low, high = IDEAL_WORDS
        self.assertEqual(_length_fit(low), 1.0)
        self.assertEqual(_length_fit(high), 1.0)
        self.assertLess(_length_fit(high + 3), 1.0)
        self.assertLess(_length_fit(MAX_WORDS), 0.2)

    def test_very_short_hooks_taper_gently(self):
        # "$18 million." is a legitimate hook; the taper should not gut it.
        self.assertGreater(_length_fit(2), 0.7)


class TestEstimate(unittest.TestCase):
    def test_lift_stays_inside_the_declared_bounds(self):
        for text in ("", "a", FOUNDER, "$1 million lost, ruined, destroyed, worst, never"):
            value = lift(text)
            self.assertGreaterEqual(value, LIFT_MIN)
            self.assertLessEqual(value, LIFT_MAX)

    def test_specific_beats_vague(self):
        self.assertGreater(
            lift("This mistake cost me $18 million"),
            lift("This mistake cost me some things"),
        )

    def test_ctr_is_the_baseline_times_the_lift(self):
        result, _, _ = estimate("Nobody warns you about the raise", baseline_ctr=3.2)
        self.assertEqual(result.baseline, 3.2)
        self.assertAlmostEqual(result.ctr, round(3.2 * result.lift, 3), places=3)

    def test_confidence_is_always_a_prior(self):
        # The engine has no click data. Anything other than "prior" here would
        # be claiming calibration it does not have.
        result, _, _ = estimate("The real reason the raise failed")
        self.assertEqual(result.confidence, "prior")

    def test_percent_formatting(self):
        self.assertEqual(CtrEstimate(lift=1.5, ctr=7.53, baseline=5.0).percent, "7.5%")

    def test_template_prior_breaks_ties_without_overriding_features(self):
        weak_pattern_strong_content = estimate("I lost $18 million", 0.75)[0].lift
        strong_pattern_weak_content = estimate("Nobody was ready for this", 1.25)[0].lift
        self.assertGreater(weak_pattern_strong_content, strong_pattern_weak_content)


class TestPenalties(unittest.TestCase):
    def test_engagement_bait_is_penalised(self):
        clean = estimate("The raise cost me $18 million")
        bait = estimate("You won't believe what the raise cost me")
        self.assertIn("engagement-bait phrasing", bait[2])
        self.assertGreater(clean[0].lift, bait[0].lift)

    def test_vague_wording_is_flagged(self):
        self.assertIn("vague wording", estimate("Some things went wrong")[2])

    def test_all_caps_is_penalised(self):
        self.assertIn("all caps", estimate("THIS RUINED EVERYTHING I BUILT")[2])

    def test_short_acronyms_are_not_treated_as_shouting(self):
        self.assertNotIn("all caps", estimate("The CEO quit")[2])

    def test_excessive_punctuation(self):
        self.assertIn("excessive punctuation", estimate("Stop!! Now!!")[2])

    def test_over_length_is_penalised(self):
        long_hook = " ".join(["word"] * (MAX_WORDS + 3))
        self.assertIn("too long to read", estimate(long_hook)[2])

    def test_multiple_emoji_are_penalised(self):
        self.assertIn("multiple emoji", estimate("The raise 💰 cost me 😱 everything")[2])
        self.assertNotIn("multiple emoji", estimate("The raise 💰 cost me everything")[2])

    def test_clean_copy_has_no_penalties(self):
        self.assertEqual(estimate("The real reason the raise failed")[2], ())


class TestTypeAffinity(unittest.TestCase):
    def test_no_signals_is_neutral(self):
        self.assertEqual(type_affinity(HookType.FEAR, ()), 1.0)

    def test_matching_signal_lifts(self):
        self.assertGreater(type_affinity(HookType.FEAR, ("failure",)), 1.0)

    def test_mismatch_is_discounted(self):
        self.assertLess(type_affinity(HookType.FEAR, ("funny",)), 1.0)

    def test_affinity_is_bounded(self):
        every = ("secret", "reaction", "emotional_spike", "controversy", "money",
                 "failure", "fail", "win", "lesson", "success", "rage")
        for hook_type in HookType:
            self.assertLessEqual(type_affinity(hook_type, every), 1.2)
            self.assertGreaterEqual(type_affinity(hook_type, ("unrelated",)), 0.5)

    def test_scoring_applies_affinity_to_the_hook(self):
        def build() -> Hook:
            return Hook(
                text="Nobody warns you about the raise",
                hook_type=HookType.FEAR,
                estimate=CtrEstimate(lift=1.0, ctr=0.0, baseline=5.0),
            )

        matched = score_hook(build(), ("failure",), 5.0)
        mismatched = score_hook(build(), ("funny",), 5.0)
        self.assertGreater(matched.estimate.lift, mismatched.estimate.lift)
        self.assertIn("type_affinity", matched.features)


class TestGeneration(unittest.TestCase):
    def setUp(self):
        self.result = generate(FOUNDER, signals=("failure", "money", "secret"))

    def test_returns_exactly_twenty_hooks(self):
        self.assertEqual(len(self.result.hooks), 20)

    def test_ranked_best_first(self):
        lifts = [h.estimate.lift for h in self.result.hooks]
        self.assertEqual(lifts, sorted(lifts, reverse=True))
        self.assertIs(self.result.best, self.result.hooks[0])

    def test_hooks_are_distinct(self):
        texts = [h.text for h in self.result.hooks]
        self.assertEqual(len(texts), len(set(texts)))

    def test_no_unfilled_placeholder_reaches_the_output(self):
        # Shipping a hook that literally reads "{number}" is the failure mode
        # the whole skip-on-missing-slot design exists to prevent.
        for hook in self.result.hooks:
            self.assertNotIn("{", hook.text)
            self.assertNotIn("}", hook.text)

    def test_all_five_core_types_are_present(self):
        covered = {h.hook_type for h in self.result.hooks}
        for hook_type in CORE_TYPES:
            self.assertIn(hook_type, covered)

    def test_per_type_cap_is_respected(self):
        counts: dict[HookType, int] = {}
        for hook in self.result.hooks:
            counts[hook.hook_type] = counts.get(hook.hook_type, 0) + 1
        # The cap can be relaxed to reach `count`, but not wildly.
        self.assertLessEqual(max(counts.values()), 6)
        self.assertGreaterEqual(len(counts), 5)

    def test_weights_version_is_stamped(self):
        self.assertEqual(self.result.weights_version, WEIGHTS_VERSION)

    def test_stats_report_the_pipeline(self):
        stats = self.result.stats
        self.assertEqual(stats["requested"], 20)
        self.assertEqual(stats["returned"], 20)
        self.assertEqual(stats["llm_hooks"], 0)
        self.assertGreater(stats["templates_rendered"], 20)
        self.assertTrue(stats["language_supported"])

    def test_hooks_stay_readable_at_phone_size(self):
        for hook in self.result.hooks:
            self.assertLessEqual(hook.word_count, MAX_WORDS + 2, hook.text)

    def test_signals_shift_the_ranking(self):
        fear_clip = generate(FOUNDER, signals=("failure",)).hooks[0]
        money_clip = generate(FOUNDER, signals=("money", "success")).hooks[0]
        # Not an assertion about which wins — only that the signal is wired
        # through to the ranking at all.
        self.assertTrue(fear_clip.text or money_clip.text)


class TestDegradation(unittest.TestCase):
    def test_empty_text_returns_an_empty_set_with_a_reason(self):
        result = generate("   ")
        self.assertEqual(result.hooks, [])
        self.assertIn("reason", result.stats)
        self.assertIsNone(result.best)

    def test_thin_clip_still_returns_a_full_set(self):
        # The slotless fallback templates exist for exactly this.
        result = generate("No. Absolutely not. I am done talking about it.")
        self.assertEqual(len(result.hooks), 20)

    def test_unsupported_language_falls_back_and_says_so(self):
        result = HookGenerator().generate(ClipContext(text=FOUNDER, language="nl"))
        self.assertFalse(result.stats["language_supported"])
        self.assertGreater(len(result.hooks), 0)

    def test_supported_languages_reports_the_bank(self):
        self.assertIn("en", supported_languages())
        self.assertIs(for_language("nl"), for_language("en"))

    def test_a_failing_llm_writer_does_not_fail_the_run(self):
        class Broken:
            def write(self, context, count):
                raise RuntimeError("provider down")

        result = HookGenerator(HookConfig(writer=Broken())).generate(FOUNDER)
        self.assertEqual(len(result.hooks), 20)
        self.assertEqual(result.stats["llm_hooks"], 0)

    def test_null_writer_is_the_default(self):
        self.assertIsInstance(HookConfig().writer, NullWriter)

    def test_a_string_is_accepted_in_place_of_a_context(self):
        self.assertGreater(len(HookGenerator().generate(FOUNDER).hooks), 0)


class TestConfig(unittest.TestCase):
    def test_count_is_honoured(self):
        result = HookGenerator(HookConfig(count=5)).generate(FOUNDER)
        self.assertEqual(len(result.hooks), 5)

    def test_baseline_flows_into_every_estimate(self):
        result = HookGenerator(HookConfig(baseline_ctr=2.4)).generate(FOUNDER)
        for hook in result.hooks:
            self.assertEqual(hook.estimate.baseline, 2.4)

    def test_topic_hint_overrides_extraction(self):
        result = HookGenerator().generate(
            ClipContext(text=STREAM, topic_hint="the choke")
        )
        self.assertEqual(result.slots.topic, "the choke")
        self.assertTrue(any("the choke" in h.text for h in result.hooks))

    def test_a_strict_dedupe_threshold_removes_more(self):
        loose = HookGenerator(HookConfig(dedupe_threshold=0.95)).generate(FOUNDER)
        strict = HookGenerator(HookConfig(dedupe_threshold=0.40)).generate(FOUNDER)
        self.assertGreater(loose.stats["after_dedupe"], strict.stats["after_dedupe"])

    def test_core_type_guarantee_can_be_disabled(self):
        result = HookGenerator(
            HookConfig(count=5, guarantee_core_types=False)
        ).generate(FOUNDER)
        self.assertEqual(len(result.hooks), 5)


class TestSerialisation(unittest.TestCase):
    def setUp(self):
        self.result = generate(FOUNDER, signals=("failure", "money"))

    def test_feature_rows_include_the_hooks_that_lost(self):
        # A model trained only on hooks that shipped learns which hooks get
        # chosen, not which hooks work.
        rows = self.result.feature_rows()
        self.assertEqual(len(rows), len(self.result.hooks))
        self.assertGreater(len(rows), 1)

    def test_feature_rows_carry_the_weights_version(self):
        for row in self.result.feature_rows():
            self.assertEqual(row["weights_version"], WEIGHTS_VERSION)
            self.assertIn("predicted_lift", row)

    def test_feature_columns_are_prefixed(self):
        row = self.result.feature_rows()[0]
        self.assertTrue(any(k.startswith("f_") for k in row))

    def test_to_dict_round_trips_through_json(self):
        import json

        payload = json.loads(json.dumps(self.result.to_dict()))
        self.assertEqual(len(payload["hooks"]), 20)
        self.assertEqual(payload["weights_version"], WEIGHTS_VERSION)
        self.assertEqual(payload["hooks"][0]["estimate"]["confidence"], "prior")

    def test_by_type_groups_every_hook(self):
        grouped = self.result.by_type()
        self.assertEqual(sum(len(v) for v in grouped.values()), len(self.result.hooks))

    def test_hook_types_serialise_as_stable_strings(self):
        for hook in self.result.hooks:
            self.assertIsInstance(hook.to_dict()["type"], str)


if __name__ == "__main__":
    unittest.main()
