"""Slot extraction and template rendering.

These are the two places the hook generator can produce output that is
grammatically broken rather than merely weak, so they get the closest tests.
A hook reading "Nobody talks about what happens after months" is not a bad
hook — it is a bug that shipped.
"""

from __future__ import annotations

import unittest

import _support  # noqa: F401  (path setup)

from clipforge.hooks import ClipContext, HookType, Slots, extract
from clipforge.hooks.extraction import (
    extract_entity,
    extract_number,
    extract_outcome,
    extract_quote,
    extract_timeframe,
    extract_topic,
)
from clipforge.hooks.templates import ENGLISH, Template, render
from clipforge.hooks.templates import _repeats_a_word


FOUNDER = (
    "The raise was the mistake. We went from twelve people to ninety in "
    "seven months and we almost went bankrupt doing it. I lost everything "
    "I'd built. The culture, the speed, all of it. We burned fourteen "
    "million dollars in nineteen months and had almost nothing to show "
    "for it. Eleven days of runway. I was terrified."
)


class TestNumberExtraction(unittest.TestCase):
    def test_money_beats_a_bare_count(self):
        text = "We had 47 employees and we burned $18 million doing it."
        self.assertEqual(extract_number(text), "$18 million")

    def test_currency_suffix_form(self):
        self.assertEqual(extract_number("It cost 250,000 dollars."), "250,000 dollars")

    def test_percentage_when_no_money(self):
        self.assertEqual(extract_number("Churn hit 40 percent."), "40 percent")

    def test_spelled_out_scale(self):
        self.assertEqual(extract_number(FOUNDER), "fourteen million")

    def test_bare_count_is_the_last_resort(self):
        self.assertEqual(extract_number("There were 32 of them."), "32")

    def test_no_figure_yields_empty(self):
        self.assertEqual(extract_number("It went badly and we moved on."), "")

    def test_whitespace_is_normalised(self):
        self.assertEqual(extract_number("we lost  $18   million"), "$18 million")


class TestTimeframeExtraction(unittest.TestCase):
    def test_preposition_is_stripped(self):
        # Templates supply their own preposition; leaving it in produces
        # "After in seven months, here is what I know".
        self.assertEqual(extract_timeframe(FOUNDER), "seven months")

    def test_digit_form(self):
        self.assertEqual(extract_timeframe("We rebuilt it in 18 months."), "18 months")

    def test_no_timeframe(self):
        self.assertEqual(extract_timeframe("It happened and then it stopped."), "")


class TestOutcomeExtraction(unittest.TestCase):
    def test_finds_a_strong_past_tense_verb(self):
        self.assertEqual(extract_outcome(FOUNDER), "lost")

    def test_lowercased(self):
        self.assertEqual(extract_outcome("Lost the round entirely."), "lost")

    def test_weak_verbs_are_not_outcomes(self):
        self.assertEqual(extract_outcome("We considered it and we discussed it."), "")

    def test_base_form_is_paired_with_the_outcome(self):
        # "I did not expect the raise to lost" is what happens without this.
        slots = extract(ClipContext(text=FOUNDER))
        self.assertEqual(slots.outcome, "lost")
        self.assertEqual(slots.outcome_base, "lose")

    def test_outcome_without_a_known_base_leaves_the_slot_empty(self):
        slots = extract(ClipContext(text="We beat them in the end."))
        self.assertEqual(slots.outcome, "beat")
        # Rather than guessing an inflection, the slot is filled only when the
        # mapping is known; templates needing it are skipped.
        self.assertEqual(slots.outcome_base, "beat")


class TestEntityExtraction(unittest.TestCase):
    def test_skips_sentence_initial_capitals(self):
        # "Everything" opens the sentence, so its capital proves nothing.
        self.assertEqual(
            extract_entity("Everything changed when Sequoia passed on us."),
            "Sequoia",
        )

    def test_no_proper_noun(self):
        self.assertEqual(extract_entity("we shipped it and it broke."), "")


class TestQuoteExtraction(unittest.TestCase):
    def test_prefers_a_short_sentence_with_consequence(self):
        self.assertEqual(extract_quote(FOUNDER), "I lost everything I'd built")

    def test_long_sentences_are_rejected(self):
        long_sentence = "We " + "really " * 20 + "lost it."
        self.assertEqual(extract_quote(long_sentence), "")

    def test_sentences_without_stakes_are_rejected(self):
        self.assertEqual(extract_quote("It was a Tuesday. Then it was not."), "")


class TestTopicExtraction(unittest.TestCase):
    def test_returns_bare_noun_and_noun_phrase(self):
        bare, phrase = extract_topic(FOUNDER)
        self.assertEqual(bare, "raise")
        self.assertEqual(phrase, "the raise")

    def test_hint_overrides_extraction(self):
        self.assertEqual(extract_topic(FOUNDER, hint="Series B"), ("Series B", "Series B"))

    def test_units_are_never_the_topic(self):
        # The regression that started this: "months" is the most frequent
        # content word in the founder clip, and the clip is not about months.
        bare, _ = extract_topic(FOUNDER)
        self.assertNotIn(bare, {"months", "million", "dollars", "people"})

    def test_adjective_loses_to_the_head_noun_it_modifies(self):
        # "a guaranteed win" — the determiner attaches to the phrase, so the
        # head is "win", not the participle in front of it.
        bare, phrase = extract_topic("That was a guaranteed win and he threw it.")
        self.assertEqual(bare, "win")
        self.assertEqual(phrase, "a win")

    def test_complementizer_that_is_not_a_determiner(self):
        # "the lesson is that headcount is not progress" — "that" introduces a
        # clause. Treating it as a determiner yields the phrase "that
        # headcount", which is ungrammatical in every nominal template.
        _, phrase = extract_topic(
            "The lesson here is that headcount is not progress. "
            "I confused the two for years and it nearly killed the company."
        )
        self.assertFalse(phrase.startswith("that "))

    def test_short_head_nouns_are_admitted(self):
        # A four-character floor excluded exactly the nouns short-form clips
        # are about.
        bare, _ = extract_topic("The bug was in production for a week.")
        self.assertEqual(bare, "bug")

    def test_filler_is_not_a_topic(self):
        bare, _ = extract_topic("Yeah okay well maybe, dude, back down.")
        self.assertEqual(bare, "")

    def test_phrase_defaults_to_a_definite_article(self):
        # Templates use the phrase nominally; a bare noun reads as a dropped
        # word ("what happens after runway"). Two mentions and no determiner,
        # so the topic is earned but the phrase has to be synthesised.
        bare, phrase = extract_topic(
            "Runway disappeared before anyone noticed. "
            "Runway is what quietly kills companies."
        )
        self.assertEqual(bare, "runway")
        self.assertEqual(phrase, "the runway")

    def test_a_single_bare_mention_is_not_a_topic(self):
        # One unremarkable content word is not evidence of subjecthood.
        # Returning nothing lets the slotless fallbacks carry the set, which
        # beats twenty hooks confidently naming the wrong thing.
        self.assertEqual(extract_topic("Runway disappeared before anyone noticed."),
                         ("", ""))

    def test_adverbs_are_never_the_topic(self):
        self.assertEqual(
            extract_topic("No. Absolutely not. I am absolutely done with it."),
            ("", ""),
        )

    def test_empty_text_yields_empty_slots(self):
        self.assertEqual(extract_topic(""), ("", ""))


class TestExtractAll(unittest.TestCase):
    def test_founder_clip_fills_most_slots(self):
        slots = extract(ClipContext(text=FOUNDER))
        self.assertEqual(slots.topic, "raise")
        self.assertEqual(slots.topic_phrase, "the raise")
        self.assertEqual(slots.number, "fourteen million")
        self.assertEqual(slots.timeframe, "seven months")
        self.assertTrue(slots.quote)

    def test_thin_clip_degrades_rather_than_failing(self):
        slots = extract(ClipContext(text="No. No. Absolutely not."))
        self.assertIsInstance(slots, Slots)
        self.assertEqual(slots.number, "")

    def test_has_reports_slot_presence(self):
        slots = extract(ClipContext(text=FOUNDER))
        self.assertTrue(slots.has("number"))
        self.assertFalse(slots.has("nonexistent"))


class TestTemplateRendering(unittest.TestCase):
    def test_missing_slot_skips_the_template(self):
        template = Template("t", HookType.FEAR, "This cost me {number}", ("number",))
        self.assertIsNone(render(template, Slots()))
        self.assertEqual(
            render(template, Slots(number="$18 million")),
            "This cost me $18 million",
        )

    def test_first_character_is_capitalised_without_touching_the_rest(self):
        template = Template("t", HookType.NUMBER, "{number} in {timeframe}",
                            ("number", "timeframe"))
        rendered = render(template, Slots(number="$18M", timeframe="six weeks"))
        self.assertEqual(rendered, "$18M in six weeks")

    def test_slot_casing_survives(self):
        template = Template("t", HookType.AUTHORITY, "I left {entity}", ("entity",))
        self.assertEqual(render(template, Slots(entity="Sequoia")), "I left Sequoia")

    def test_repeated_content_word_rejects_the_render(self):
        # "The mistake mistake that ruins everything"
        template = Template("t", HookType.FEAR, "The {topic} mistake that ruins all",
                            ("topic",))
        self.assertIsNone(render(template, Slots(topic="mistake")))

    def test_function_words_may_repeat(self):
        self.assertFalse(_repeats_a_word("The real reason the raise failed"))

    def test_adjacent_duplicates_are_caught_at_any_length(self):
        self.assertTrue(_repeats_a_word("The win win of the year"))

    def test_distant_short_duplicates_are_tolerated(self):
        # "cost" twice in a long hook is acceptable; "mistake" twice is not.
        self.assertFalse(_repeats_a_word("It cost me the round and cost me the team"))
        self.assertTrue(_repeats_a_word("The biggest mistake was the second mistake"))


class TestTemplateBank(unittest.TestCase):
    def test_ids_are_unique(self):
        ids = [t.id for t in ENGLISH]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_declared_slot_exists_on_slots(self):
        valid = set(Slots().as_dict())
        for template in ENGLISH:
            for name in template.requires:
                self.assertIn(name, valid, f"{template.id} requires unknown slot {name}")

    def test_every_braced_field_is_declared_as_required(self):
        # A field in the pattern that is not in `requires` renders as an empty
        # string instead of skipping the template.
        import string

        for template in ENGLISH:
            fields = {
                name
                for _, name, _, _ in string.Formatter().parse(template.pattern)
                if name
            }
            self.assertEqual(
                fields,
                set(template.requires),
                f"{template.id}: pattern fields and requires disagree",
            )

    def test_all_ten_types_are_represented(self):
        covered = {t.hook_type for t in ENGLISH}
        self.assertEqual(covered, set(HookType))

    def test_fallbacks_need_no_slots(self):
        slotless = [t for t in ENGLISH if not t.requires]
        self.assertGreaterEqual(len(slotless), 5)
        for template in slotless:
            self.assertIsNotNone(render(template, Slots()))


if __name__ == "__main__":
    unittest.main()
