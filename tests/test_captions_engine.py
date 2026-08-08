"""Animation keyframes, export formats, and end-to-end caption generation."""

from __future__ import annotations

import unittest

import _support  # noqa: F401  (path setup)

from clipforge.captions import (
    Animation,
    Box,
    CaptionConfig,
    CaptionEngine,
    Language,
    PRESETS,
    TimedWord,
    animate_cue,
    generate,
    to_ass,
    to_json,
    to_srt,
    to_vtt,
)
from clipforge.captions import styles
from clipforge.captions.export import _ass_color, _ass_time, _srt_time
from clipforge.captions.types import CaptionWord


def speech(text: str, speaker: str = "HOST", step: int = 380) -> list[TimedWord]:
    return [
        TimedWord(tok, i * step, i * step + step - 60, speaker)
        for i, tok in enumerate(text.split())
    ]


SAMPLES = {
    Language.ENGLISH: "So how much money did you actually lose in the end? "
                      "Eighteen million. That was the biggest mistake I ever made.",
    Language.DUTCH: "Dus hoeveel geld heb je verloren? Achttien miljoen. "
                    "Dat was de grootste fout die ik ooit maakte.",
    Language.GERMAN: "Wie viel Geld hast du verloren? Achtzehn Millionen. "
                     "Das war der größte Fehler meines Lebens.",
    Language.FRENCH: "Combien d'argent as-tu perdu ? Dix-huit millions. "
                     "C'était la plus grosse erreur de ma vie.",
    Language.SPANISH: "¿Cuánto dinero perdiste? Dieciocho millones. "
                      "Fue el mayor error de mi vida.",
}


class TestAnimation(unittest.TestCase):
    @staticmethod
    def cue_words() -> list[CaptionWord]:
        return [
            CaptionWord("HELLO", 1000, 1400),
            CaptionWord("WORLD", 1420, 1900),
        ]

    def test_every_style_produces_keyframes(self) -> None:
        for name, style in PRESETS.items():
            with self.subTest(style=name):
                words = self.cue_words()
                animate_cue(words, 1000, 2000, style)
                for word in words:
                    self.assertTrue(word.keyframes, f"{name} produced none")

    def test_keyframe_times_are_monotonic(self) -> None:
        for name, style in PRESETS.items():
            with self.subTest(style=name):
                words = self.cue_words()
                animate_cue(words, 1000, 2000, style)
                for word in words:
                    times = [k.t_ms for k in word.keyframes]
                    self.assertEqual(times, sorted(times))

    def test_keyframe_times_are_never_negative(self) -> None:
        words = [CaptionWord("EARLY", 0, 200)]
        animate_cue(words, 0, 1000, styles.PUNCH)
        self.assertTrue(all(k.t_ms >= 0 for k in words[0].keyframes))

    def test_pop_overshoots_then_settles(self) -> None:
        words = self.cue_words()
        animate_cue(words, 1000, 2000, styles.PUNCH)
        scales = [k.scale for k in words[0].keyframes]
        self.assertGreater(max(scales), 1.0)
        self.assertAlmostEqual(scales[-1], 1.0, places=3)

    def test_karaoke_does_not_move_the_text(self) -> None:
        words = self.cue_words()
        animate_cue(words, 1000, 2000, styles.KARAOKE)
        for keyframe in words[0].keyframes:
            self.assertEqual(keyframe.scale, 1.0)
            self.assertEqual(keyframe.offset_y, 0.0)

    def test_karaoke_changes_colour_at_the_word_start(self) -> None:
        words = self.cue_words()
        animate_cue(words, 1000, 2000, styles.KARAOKE)
        colors = [k.color for k in words[0].keyframes]
        self.assertIn(styles.KARAOKE.active_color, colors)

    def test_bounce_moves_vertically(self) -> None:
        words = self.cue_words()
        animate_cue(words, 1000, 2000, styles.BOUNCE)
        self.assertLess(min(k.offset_y for k in words[0].keyframes), 0.0)

    def test_emoji_pop_rather_than_karaoke(self) -> None:
        words = [CaptionWord("\U0001F525", 1000, 1400, is_emoji=True)]
        animate_cue(words, 1000, 2000, styles.KARAOKE)
        self.assertGreater(max(k.scale for k in words[0].keyframes), 1.0)
        self.assertTrue(all(k.color is None for k in words[0].keyframes))


class TestExportHelpers(unittest.TestCase):
    def test_ass_colour_is_byte_reversed(self) -> None:
        """ASS stores BGR, not RGB — easy to get backwards and hard to spot."""
        self.assertEqual(_ass_color("#FF0000"), "&H000000FF")
        self.assertEqual(_ass_color("#0000FF"), "&H00FF0000")

    def test_ass_alpha_is_inverted(self) -> None:
        """In ASS, 00 is opaque and FF is transparent."""
        self.assertTrue(_ass_color("#FFFFFF00").startswith("&HFF"))

    def test_ass_time_is_centiseconds(self) -> None:
        self.assertEqual(_ass_time(3_661_230), "1:01:01.23")
        self.assertEqual(_ass_time(-5), "0:00:00.00")

    def test_srt_time_is_milliseconds(self) -> None:
        self.assertEqual(_srt_time(3_661_230), "01:01:01,230")


class TestExportFormats(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.track = generate(speech(SAMPLES[Language.ENGLISH]), style="punch",
                             language="en")
        cls.style = styles.PUNCH

    def test_ass_has_required_sections(self) -> None:
        out = to_ass(self.track, self.style)
        for section in ("[Script Info]", "[V4+ Styles]", "[Events]"):
            self.assertIn(section, out)
        self.assertIn("PlayResX: 1080", out)
        self.assertIn("PlayResY: 1920", out)

    def test_ass_carries_karaoke_timing(self) -> None:
        out = to_ass(self.track, self.style)
        self.assertIn("\\kf", out)

    def test_ass_dialogue_count_matches_cues(self) -> None:
        out = to_ass(self.track, self.style)
        dialogues = [l for l in out.splitlines() if l.startswith("Dialogue:")]
        self.assertEqual(len(dialogues), len(self.track.cues))

    def test_vtt_has_word_level_timestamps(self) -> None:
        out = to_vtt(self.track)
        self.assertTrue(out.startswith("WEBVTT"))
        self.assertIn("<00:00:", out)

    def test_vtt_can_omit_word_timing(self) -> None:
        self.assertNotIn("<00:00:", to_vtt(self.track, word_timing=False))

    def test_srt_is_numbered_with_arrows(self) -> None:
        out = to_srt(self.track)
        self.assertTrue(out.startswith("1\n"))
        self.assertIn(" --> ", out)
        self.assertNotIn("\\kf", out)

    def test_json_includes_the_style_spec(self) -> None:
        payload = to_json(self.track, self.style)
        self.assertEqual(payload["style_spec"]["name"], "punch")
        self.assertIn("cues", payload)

    def test_french_export_preserves_narrow_nbsp(self) -> None:
        track = generate(speech("Combien d'argent as-tu perdu ?"), language="fr")
        self.assertIn(" ?", to_srt(track))


class TestEngineEndToEnd(unittest.TestCase):
    def test_all_five_languages_produce_cues(self) -> None:
        for language, text in SAMPLES.items():
            with self.subTest(language=language.value):
                track = generate(speech(text), language=language.value)
                self.assertTrue(track.cues)
                self.assertIs(track.language, language)

    def test_no_transcript_content_is_dropped(self) -> None:
        """Every spoken character survives to the rendered captions.

        Counting tokens would be wrong: detached punctuation is deliberately
        merged into its neighbour (French `perdu ?` becomes one token), so the
        rendered word count is legitimately lower than the input's. What must
        never change is the text itself.
        """
        for language, text in SAMPLES.items():
            with self.subTest(language=language.value):
                track = generate(speech(text), language=language.value)
                rendered = "".join(
                    w.text for cue in track.cues for w in cue.words if not w.is_emoji
                )
                # Strip whitespace and the narrow no-break space the French
                # rules insert, then compare the raw character stream.
                # `casefold` rather than `lower`: uppercasing German ß yields
                # SS, which is correct orthography but is not reversible by
                # `lower`. Casefold maps both forms to the same key.
                actual = rendered.replace(" ", "").replace(" ", "").casefold()
                expected = "".join(text.split()).casefold()
                self.assertEqual(actual, expected)

    def test_german_eszett_uppercases_to_ss(self) -> None:
        """`größte` → `GRÖSSTE` is correct German, not corruption.

        Worth pinning: it looks like a bug in a diff, and the alternative
        (capital ẞ, U+1E9E) is absent from most display fonts and would
        render as tofu in exactly the all-caps styles that need it.
        """
        track = generate(speech("Der größte Fehler war das"), language="de")
        joined = " ".join(c.text for c in track.cues)
        self.assertIn("GRÖSSTE", joined)

    def test_cues_never_overlap(self) -> None:
        """Two captions on screen at once is the most visible failure mode."""
        for language, text in SAMPLES.items():
            with self.subTest(language=language.value):
                track = generate(speech(text), language=language.value)
                for current, following in zip(track.cues, track.cues[1:]):
                    self.assertLessEqual(current.end_ms, following.start_ms)

    def test_cues_are_chronological(self) -> None:
        track = generate(speech(SAMPLES[Language.ENGLISH]))
        starts = [c.start_ms for c in track.cues]
        self.assertEqual(starts, sorted(starts))

    def test_every_word_gets_keyframes(self) -> None:
        track = generate(speech(SAMPLES[Language.ENGLISH]))
        for cue in track.cues:
            for word in cue.words:
                self.assertTrue(word.keyframes)

    def test_speaker_highlighting_assigns_distinct_colours(self) -> None:
        words = speech("first speaker talking here", "HOST") + [
            TimedWord(t, 3_000 + i * 300, 3_000 + i * 300 + 250, "GUEST")
            for i, t in enumerate("second speaker replying now".split())
        ]
        track = generate(words)
        self.assertEqual(len(track.speaker_colors), 2)
        self.assertEqual(len(set(track.speaker_colors.values())), 2)

    def test_a_cue_never_mixes_speakers(self) -> None:
        words = speech("first speaker here", "HOST") + [
            TimedWord(t, 2_000 + i * 300, 2_000 + i * 300 + 250, "GUEST")
            for i, t in enumerate("second speaker now".split())
        ]
        track = generate(words)
        for cue in track.cues:
            self.assertEqual(len({w.speaker for w in cue.words}), 1)

    def test_emoji_are_spaced_out(self) -> None:
        track = generate(speech(SAMPLES[Language.ENGLISH]))
        emoji_cues = [c.index for c in track.cues if any(w.is_emoji for w in c.words)]
        for a, b in zip(emoji_cues, emoji_cues[1:]):
            self.assertGreater(b - a, 2, "emoji appeared in adjacent cues")

    def test_at_most_one_emoji_per_cue(self) -> None:
        track = generate(speech(SAMPLES[Language.SPANISH]), language="es")
        for cue in track.cues:
            self.assertLessEqual(sum(1 for w in cue.words if w.is_emoji), 1)

    def test_emoji_can_be_disabled(self) -> None:
        track = generate(speech(SAMPLES[Language.ENGLISH]), style="minimal")
        self.assertEqual(track.stats["emoji_added"], 0)

    def test_german_compound_shrinks_rather_than_clipping(self) -> None:
        words = speech("Die Rechtsschutzversicherung war nutzlos")
        track = CaptionEngine(
            CaptionConfig(style=styles.PUNCH, language=Language.GERMAN)
        ).generate(words)
        shrunk = [c for c in track.cues if c.shrunk]
        self.assertTrue(shrunk, "the long compound should have forced a shrink")
        for cue in shrunk:
            self.assertGreaterEqual(cue.font_scale, 0.5)
            self.assertLess(cue.font_scale, 1.0)

    def test_narrow_box_forces_more_shrinking(self) -> None:
        words = speech(SAMPLES[Language.ENGLISH])
        wide = generate(words, box=Box(64, 1180, 952, 340))
        narrow = generate(words, box=Box(64, 1180, 420, 340))
        self.assertGreater(narrow.stats["cues_shrunk"], wide.stats["cues_shrunk"])

    def test_language_is_auto_detected(self) -> None:
        track = generate(speech(SAMPLES[Language.SPANISH]))
        self.assertIs(track.language, Language.SPANISH)
        self.assertTrue(track.stats["language_detected"])

    def test_every_style_runs(self) -> None:
        for name in PRESETS:
            with self.subTest(style=name):
                track = generate(speech(SAMPLES[Language.ENGLISH]), style=name)
                self.assertTrue(track.cues)

    def test_punch_produces_more_cues_than_karaoke(self) -> None:
        """Two words at a time versus six should be visibly different pacing."""
        words = speech(SAMPLES[Language.ENGLISH])
        self.assertGreater(
            len(generate(words, style="punch").cues),
            len(generate(words, style="karaoke").cues),
        )

    def test_unknown_style_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            generate(speech("hello there"), style="nonexistent")


class TestEngineEdgeCases(unittest.TestCase):
    def test_empty_input(self) -> None:
        track = generate([])
        self.assertEqual(track.cues, [])
        self.assertIn("reason", track.stats)

    def test_single_word(self) -> None:
        track = generate([TimedWord("hello", 0, 400)])
        self.assertEqual(len(track.cues), 1)

    def test_sentence_level_subtitles_are_rejected(self) -> None:
        """Without word timing there is nothing to karaoke; fail loudly."""
        zero_duration = [TimedWord(t, i * 500, i * 500) for i, t in
                         enumerate("no timing here at all".split())]
        with self.assertRaises(ValueError) as ctx:
            generate(zero_duration)
        self.assertIn("word-level", str(ctx.exception))

    def test_accepts_plain_dicts(self) -> None:
        track = generate([
            {"text": "hello", "start_ms": 0, "end_ms": 400},
            {"text": "world", "start_ms": 420, "end_ms": 800},
        ])
        self.assertTrue(track.cues)

    def test_accepts_viral_engine_words(self) -> None:
        """The transcript pipeline's Word type must work without conversion."""
        from clipforge.viral.types import Word

        track = generate([Word("hello", 0, 400), Word("world", 420, 800)])
        self.assertTrue(track.cues)

    def test_out_of_order_input_is_sorted(self) -> None:
        track = generate([
            TimedWord("second", 1_000, 1_400),
            TimedWord("first", 0, 400),
        ])
        self.assertEqual(track.cues[0].words[0].text.lower(), "first")

    def test_minimum_cue_duration_is_enforced(self) -> None:
        track = generate([TimedWord("hi", 0, 40)])
        self.assertGreaterEqual(
            track.cues[0].duration_ms, styles.PUNCH.min_cue_ms - 1
        )

    def test_track_serialises(self) -> None:
        import json

        track = generate(speech(SAMPLES[Language.FRENCH]), language="fr")
        payload = json.loads(json.dumps(track.to_dict(), ensure_ascii=False))
        self.assertEqual(payload["language"], "fr")
        self.assertIn("keyframes", payload["cues"][0]["lines"][0]["words"][0])


if __name__ == "__main__":
    unittest.main()
