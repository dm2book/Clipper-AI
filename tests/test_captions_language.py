"""Measurement, language rules, emoji, and chunking."""

from __future__ import annotations

import unittest

import _support  # noqa: F401  (path setup)

from clipforge.captions import chunking, emoji, languages, measure, styles
from clipforge.captions.languages import NNBSP, rules_for
from clipforge.captions.types import CaptionWord, CaseTransform, Language, TimedWord


def words(*specs: tuple[str, int, int], speaker: str = "A") -> list[TimedWord]:
    return [TimedWord(t, s, e, speaker) for t, s, e in specs]


def evenly(text: str, speaker: str = "A", step: int = 400) -> list[TimedWord]:
    return [
        TimedWord(tok, i * step, i * step + step - 50, speaker)
        for i, tok in enumerate(text.split())
    ]


class TestMeasurement(unittest.TestCase):
    def test_uppercase_is_wider_than_lowercase(self) -> None:
        """Premium styles are ALL CAPS; measuring them as lowercase underfits."""
        self.assertGreater(measure.text_width("HELLO"), measure.text_width("hello"))

    def test_accents_measure_as_their_base_letter(self) -> None:
        for accented, base in (("é", "e"), ("ü", "u"), ("ñ", "n"), ("ç", "c")):
            with self.subTest(char=accented):
                self.assertAlmostEqual(
                    measure.char_width(accented), measure.char_width(base), places=6
                )

    def test_narrow_and_wide_glyphs_differ(self) -> None:
        self.assertLess(measure.char_width("i"), measure.char_width("m"))
        self.assertLess(measure.char_width("l"), measure.char_width("W"))

    def test_emoji_are_wide(self) -> None:
        self.assertGreater(measure.char_width("\U0001F525"), 1.0)

    def test_zero_width_joiners_cost_nothing(self) -> None:
        self.assertEqual(measure.char_width("‍"), 0.0)

    def test_max_em_scales_with_box_and_font(self) -> None:
        wide = measure.max_em_for(1000, 50)
        narrow = measure.max_em_for(500, 50)
        self.assertAlmostEqual(wide, narrow * 2, places=6)
        self.assertEqual(measure.max_em_for(1000, 0), 0.0)

    def test_shrink_only_when_needed(self) -> None:
        scale, shrunk = measure.shrink_to_fit("short", 100.0)
        self.assertEqual((scale, shrunk), (1.0, False))

    def test_shrink_respects_the_floor(self) -> None:
        scale, shrunk = measure.shrink_to_fit("A" * 200, 5.0, floor=0.62)
        self.assertTrue(shrunk)
        self.assertGreaterEqual(scale, 0.62)


class TestFrenchTypography(unittest.TestCase):
    RULES = rules_for(Language.FRENCH)

    def test_narrow_nbsp_inserted_before_punctuation(self) -> None:
        for mark in (";", ":", "!", "?"):
            with self.subTest(mark=mark):
                out = languages.apply_typography(f"vraiment{mark}", self.RULES)
                self.assertEqual(out, f"vraiment{NNBSP}{mark}")

    def test_existing_plain_space_is_upgraded(self) -> None:
        out = languages.apply_typography("vraiment ?", self.RULES)
        self.assertEqual(out, f"vraiment{NNBSP}?")

    def test_not_applied_to_other_languages(self) -> None:
        out = languages.apply_typography("really?", rules_for(Language.ENGLISH))
        self.assertEqual(out, "really?")

    def test_elision_glues_to_the_next_word(self) -> None:
        for token in ("l'", "qu'", "j'", "d'", "jusqu'"):
            with self.subTest(token=token):
                self.assertTrue(languages.glues_to_next(token, self.RULES))

    def test_ordinary_words_do_not_glue(self) -> None:
        self.assertFalse(languages.glues_to_next("maison", self.RULES))

    def test_articles_are_orphan_risks(self) -> None:
        self.assertTrue(languages.orphan_risk("le", self.RULES))
        self.assertFalse(languages.orphan_risk("maison", self.RULES))


class TestSpanishAndDutchRules(unittest.TestCase):
    def test_inverted_marks_glue_forward(self) -> None:
        rules = rules_for(Language.SPANISH)
        self.assertTrue(languages.glues_to_next("¿", rules))
        self.assertTrue(languages.glues_to_next("¡", rules))

    def test_mark_already_attached_is_not_a_separate_glue(self) -> None:
        rules = rules_for(Language.SPANISH)
        self.assertFalse(languages.glues_to_next("¿Qué", rules))

    def test_dutch_proclitic_glues(self) -> None:
        rules = rules_for(Language.DUTCH)
        self.assertTrue(languages.glues_to_next("'s", rules))

    def test_compounding_languages_are_flagged(self) -> None:
        self.assertTrue(rules_for(Language.GERMAN).heavy_compounding)
        self.assertTrue(rules_for(Language.DUTCH).heavy_compounding)
        self.assertFalse(rules_for(Language.FRENCH).heavy_compounding)


class TestLanguageDetection(unittest.TestCase):
    SAMPLES = {
        Language.ENGLISH: "the money is gone and that is the whole story",
        Language.DUTCH: "het geld is weg en dat is het hele verhaal",
        Language.GERMAN: "das geld ist weg und das ist die ganze geschichte",
        Language.FRENCH: "le argent est parti et que est la histoire",
        Language.SPANISH: "el dinero se ha ido y que es la historia",
    }

    def test_detects_each_language(self) -> None:
        for expected, text in self.SAMPLES.items():
            with self.subTest(language=expected.value):
                self.assertIs(languages.detect(text.split()), expected)

    def test_falls_back_to_english_on_no_signal(self) -> None:
        self.assertIs(languages.detect(["xyz", "qqq"]), Language.ENGLISH)


class TestEmojiLexicon(unittest.TestCase):
    def test_money_concept_in_every_language(self) -> None:
        cases = {
            Language.ENGLISH: "we lost money fast",
            Language.DUTCH: "we verloren geld snel",
            Language.GERMAN: "wir verloren Geld schnell",
            Language.FRENCH: "on a perdu de l'argent",
            Language.SPANISH: "perdimos dinero rápido",
        }
        for language, text in cases.items():
            with self.subTest(language=language.value):
                found = emoji.suggest(text.split(), language)
                self.assertIsNotNone(found, f"no emoji for {text!r}")
                self.assertEqual(found[1], "\U0001F4B0")

    def test_returns_none_for_neutral_text(self) -> None:
        self.assertIsNone(emoji.suggest(["and", "then", "the"], Language.ENGLISH))

    def test_threshold_filters_weak_concepts(self) -> None:
        self.assertIsNone(
            emoji.suggest(["music"], Language.ENGLISH, threshold=0.95)
        )

    def test_prefix_matching_handles_inflection(self) -> None:
        """German cases and Spanish verb endings must still match."""
        self.assertIsNotNone(emoji.suggest(["millionen"], Language.GERMAN))
        self.assertIsNotNone(emoji.suggest(["celebrando"], Language.SPANISH))

    def test_detects_existing_emoji(self) -> None:
        self.assertTrue(emoji.contains_emoji("nice \U0001F525"))
        self.assertFalse(emoji.contains_emoji("nice"))

    def test_lexicon_covers_all_languages(self) -> None:
        for language in Language:
            with self.subTest(language=language.value):
                self.assertGreater(len(list(emoji.concepts_for(language))), 20)


class TestPunctuationMerging(unittest.TestCase):
    def test_french_detached_question_mark_merges_back(self) -> None:
        """`perdu ?` is correct French and must not become its own cue."""
        rules = rules_for(Language.FRENCH)
        merged = chunking.merge_punctuation(
            words(("perdu", 0, 400), ("?", 420, 500)), rules
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].text, f"perdu{NNBSP}?")
        self.assertEqual(merged[0].end_ms, 500)

    def test_spanish_opening_mark_attaches_forward(self) -> None:
        rules = rules_for(Language.SPANISH)
        merged = chunking.merge_punctuation(
            words(("¿", 0, 60), ("Cuánto", 70, 500)), rules
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].text, "¿Cuánto")

    def test_english_merges_without_a_space(self) -> None:
        rules = rules_for(Language.ENGLISH)
        merged = chunking.merge_punctuation(
            words(("really", 0, 400), ("?", 420, 500)), rules
        )
        self.assertEqual(merged[0].text, "really?")

    def test_no_content_is_dropped(self) -> None:
        rules = rules_for(Language.SPANISH)
        merged = chunking.merge_punctuation(words(("¿", 0, 60)), rules)
        self.assertEqual(len(merged), 1)

    def test_ordinary_words_untouched(self) -> None:
        rules = rules_for(Language.ENGLISH)
        original = words(("hello", 0, 400), ("world", 420, 800))
        self.assertEqual(len(chunking.merge_punctuation(original, rules)), 2)


class TestCueSplitting(unittest.TestCase):
    STYLE = styles.PUNCH

    def test_respects_the_word_limit(self) -> None:
        rules = rules_for(Language.ENGLISH)
        cues = chunking.split_into_cues(
            evenly("one two three four five six seven eight"), rules, self.STYLE
        )
        for cue in cues:
            self.assertLessEqual(len(cue), self.STYLE.max_words)

    def test_speaker_change_always_breaks(self) -> None:
        rules = rules_for(Language.ENGLISH)
        mixed = [
            TimedWord("hello", 0, 300, "A"),
            TimedWord("there", 320, 600, "A"),
            TimedWord("hi", 620, 900, "B"),
        ]
        cues = chunking.split_into_cues(mixed, rules, styles.KARAOKE)
        for cue in cues:
            with self.subTest(cue=[w.text for w in cue]):
                self.assertEqual(len({w.speaker for w in cue}), 1)

    def test_long_pause_breaks(self) -> None:
        rules = rules_for(Language.ENGLISH)
        paused = [
            TimedWord("before", 0, 300, "A"),
            TimedWord("after", 5_000, 5_300, "A"),
        ]
        cues = chunking.split_into_cues(paused, rules, styles.KARAOKE)
        self.assertEqual(len(cues), 2)

    def test_no_words_are_lost(self) -> None:
        rules = rules_for(Language.ENGLISH)
        source = evenly("the quick brown fox jumps over the lazy dog again and again")
        cues = chunking.split_into_cues(source, rules, self.STYLE)
        flattened = [w.text for cue in cues for w in cue]
        self.assertEqual(flattened, [w.text for w in source])

    def test_empty_input(self) -> None:
        self.assertEqual(
            chunking.split_into_cues([], rules_for(Language.ENGLISH), self.STYLE), []
        )


class TestLineBreaking(unittest.TestCase):
    @staticmethod
    def caption_words(*texts: str) -> list[CaptionWord]:
        out = []
        for i, text in enumerate(texts):
            word = CaptionWord(text=text, start_ms=i * 300, end_ms=i * 300 + 250)
            word.width_em = measure.text_width(text)
            out.append(word)
        return out

    def test_glued_tokens_are_never_split_across_lines(self) -> None:
        """A French `l'` alone at a line end is not a word."""
        rules = rules_for(Language.FRENCH)
        items = self.caption_words("beaucoup", "vraiment", "l'", "argent")
        lines = chunking.layout_lines(items, rules, styles.KARAOKE, max_em=8.0)
        for line in lines:
            with self.subTest(line=line.text):
                self.assertFalse(
                    line.words[-1].text.endswith("'"),
                    f"line ends on an elided form: {line.text!r}",
                )

    def test_dutch_proclitic_stays_with_its_word(self) -> None:
        rules = rules_for(Language.DUTCH)
        items = self.caption_words("gisteren", "ochtend", "'s", "ochtends")
        lines = chunking.layout_lines(items, rules, styles.KARAOKE, max_em=9.0)
        for line in lines:
            with self.subTest(line=line.text):
                self.assertNotEqual(line.words[-1].text, "'s")

    def test_respects_the_line_limit(self) -> None:
        rules = rules_for(Language.ENGLISH)
        items = self.caption_words(*["overflowing"] * 12)
        lines = chunking.layout_lines(items, rules, styles.PUNCH, max_em=6.0)
        self.assertLessEqual(len(lines), styles.PUNCH.max_lines)

    def test_no_words_lost_when_folding_overflow(self) -> None:
        rules = rules_for(Language.ENGLISH)
        items = self.caption_words(*[f"word{i}" for i in range(12)])
        lines = chunking.layout_lines(items, rules, styles.PUNCH, max_em=6.0)
        flattened = [w.text for line in lines for w in line.words]
        self.assertEqual(len(flattened), 12)

    def test_balance_evens_a_lopsided_pair(self) -> None:
        long_line = chunking.CaptionLine(self.caption_words("aaaa", "bbbb", "cccc"))
        short_line = chunking.CaptionLine(self.caption_words("d"))
        balanced = chunking.balance_lines([long_line, short_line])
        self.assertEqual(len(balanced[0].words), 2)

    def test_balance_leaves_a_good_pair_alone(self) -> None:
        first = chunking.CaptionLine(self.caption_words("aaaa", "bbbb"))
        second = chunking.CaptionLine(self.caption_words("cccc", "dddd"))
        balanced = chunking.balance_lines([first, second])
        self.assertEqual(len(balanced[0].words), 2)


class TestCaseTransform(unittest.TestCase):
    def test_german_is_exempt_from_lowercasing(self) -> None:
        """Noun capitalisation is grammar in German, not styling."""
        self.assertEqual(
            styles.apply_case("Das Geld", CaseTransform.LOWER, Language.GERMAN),
            "Das Geld",
        )

    def test_other_languages_lowercase_normally(self) -> None:
        self.assertEqual(
            styles.apply_case("The Money", CaseTransform.LOWER, Language.ENGLISH),
            "the money",
        )

    def test_uppercase_applies_everywhere(self) -> None:
        for language in Language:
            with self.subTest(language=language.value):
                self.assertEqual(
                    styles.apply_case("geld", CaseTransform.UPPER, language), "GELD"
                )

    def test_speaker_colors_are_stable_and_distinct(self) -> None:
        colors = styles.assign_speaker_colors(["HOST", "GUEST"], styles.PUNCH)
        self.assertEqual(len(set(colors.values())), 2)
        self.assertEqual(
            colors, styles.assign_speaker_colors(["HOST", "GUEST"], styles.PUNCH)
        )

    def test_styles_that_disable_highlighting_get_no_colors(self) -> None:
        self.assertEqual(styles.assign_speaker_colors(["A", "B"], styles.MINIMAL), {})


if __name__ == "__main__":
    unittest.main()
