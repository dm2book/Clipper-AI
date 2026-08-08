"""Detector and transcript-normalisation tests."""

from __future__ import annotations

import unittest

from _support import solo, transcript

from clipforge.viral import transcript as tx
from clipforge.viral.detectors import DetectorContext, detect_all
from clipforge.viral.types import Signal, saturating_sum


def signals_for(text: str) -> dict[Signal, float]:
    """Strongest strength per signal for a single-utterance transcript."""
    out: dict[Signal, float] = {}
    for hit in detect_all(solo(text)):
        out[hit.signal] = max(out.get(hit.signal, 0.0), hit.strength)
    return out


class TestLexicalDetectors(unittest.TestCase):
    """Each detector fires on a clear positive and stays quiet on filler."""

    POSITIVES = {
        Signal.CONTROVERSY: "Unpopular opinion: most of this industry is overrated.",
        Signal.EMOTIONAL_SPIKE: "I was absolutely devastated. I will never forget it.",
        Signal.MONEY: "We raised $18 million and burned through it in nineteen months.",
        Signal.FUNNY: "And the projector broke. [laughs] It was ridiculous.",
        Signal.FAILURE: "That was the biggest mistake I ever made, we almost went bankrupt.",
        Signal.SUCCESS: "We hit $2 million ARR and tripled the team.",
        Signal.SECRET: "I've never told anyone this, but nobody talks about the real numbers.",
        Signal.LESSON: "The lesson here is simple. If I could go back, my advice would be to wait.",
        Signal.DEBATE: "The counterargument is survivorship bias. On the other hand, I take your point.",
    }

    def test_each_detector_fires_on_its_positive(self) -> None:
        for signal, text in self.POSITIVES.items():
            with self.subTest(signal=signal.value):
                found = signals_for(text)
                self.assertIn(signal, found, f"{signal.value} did not fire on: {text!r}")
                self.assertGreater(found[signal], 0.25)

    def test_filler_produces_no_signals(self) -> None:
        for text in (
            "Yeah, so, um, anyway.",
            "How was the flight in?",
            "Right, okay. Sure.",
            "Thanks for having me.",
        ):
            with self.subTest(text=text):
                self.assertEqual(signals_for(text), {})

    def test_strength_is_bounded(self) -> None:
        stacked = " ".join(self.POSITIVES.values())
        for signal, strength in signals_for(stacked).items():
            with self.subTest(signal=signal.value):
                self.assertGreaterEqual(strength, 0.0)
                self.assertLessEqual(strength, 1.0)


class TestArgumentDetector(unittest.TestCase):
    """Argument needs structural back-and-forth, not just angry words."""

    ADVERSARIAL = "No, that's not what I said. Let me finish."

    def test_fires_in_rapid_exchange(self) -> None:
        convo = transcript(
            ("A", "I think the data is clear."),
            ("B", "It really isn't."),
            ("A", self.ADVERSARIAL),
            ("B", "You're missing the point entirely."),
            ("A", "Hold on, I wasn't finished."),
        )
        hits = [h for h in detect_all(convo) if h.signal is Signal.ARGUMENT]
        self.assertTrue(hits)
        self.assertGreater(max(h.strength for h in hits), 0.5)

    def test_damped_inside_a_monologue(self) -> None:
        """Same words, one speaker: someone recounting a row, not having one."""
        monologue = transcript(
            ("A", "So I told him about the numbers."),
            ("A", "And he said, and I quote, " + self.ADVERSARIAL),
            ("A", "Which I thought was unfair."),
            ("A", "Anyway, we moved on."),
        )
        exchange = transcript(
            ("A", "So I told him about the numbers."),
            ("B", "And he said, and I quote, " + self.ADVERSARIAL),
            ("A", "Which I thought was unfair."),
            ("B", "Anyway, we moved on."),
        )

        def peak(t) -> float:
            hits = [h for h in detect_all(t) if h.signal is Signal.ARGUMENT]
            return max((h.strength for h in hits), default=0.0)

        self.assertLess(peak(monologue), peak(exchange))


class TestDebateDetector(unittest.TestCase):
    def test_heat_damps_debate(self) -> None:
        """A line that is both reasoned and hostile scores lower as debate."""
        calm = "The counterargument is survivorship bias."
        hostile = "The counterargument is survivorship bias, but no, that's just wrong."

        def strength(text: str) -> float:
            return signals_for(text).get(Signal.DEBATE, 0.0)

        self.assertGreater(strength(calm), strength(hostile))

    def test_single_speaker_damped(self) -> None:
        one = solo("Let me steelman the strongest case against my own position.")
        two = transcript(
            ("A", "Let me steelman the strongest case against my own position."),
            ("B", "Please do."),
        )

        def peak(t) -> float:
            hits = [h for h in detect_all(t) if h.signal is Signal.DEBATE]
            return max((h.strength for h in hits), default=0.0)

        self.assertLess(peak(one), peak(two))


class TestEmotionDetector(unittest.TestCase):
    def test_shouting_counts(self) -> None:
        quiet = signals_for("That was not what we expected at all.")
        loud = signals_for("That was NOT what we EXPECTED at ALL.")
        self.assertGreater(
            loud.get(Signal.EMOTIONAL_SPIKE, 0.0),
            quiet.get(Signal.EMOTIONAL_SPIKE, 0.0),
        )


class TestDetectorContext(unittest.TestCase):
    def test_turn_density_extremes(self) -> None:
        alternating = transcript(*[(("A", "B")[i % 2], "line") for i in range(9)])
        monologue = transcript(*[("A", "line") for _ in range(9)])
        self.assertEqual(DetectorContext(alternating, 4).turn_density(), 1.0)
        self.assertEqual(DetectorContext(monologue, 4).turn_density(), 0.0)

    def test_edges_have_no_neighbours(self) -> None:
        t = transcript(("A", "first"), ("B", "second"))
        self.assertIsNone(DetectorContext(t, 0).previous)
        self.assertIsNone(DetectorContext(t, 1).following)


class TestTranscriptNormalisation(unittest.TestCase):
    def test_rows_are_sorted_and_reindexed(self) -> None:
        t = tx.from_utterances(
            "s",
            [
                {"start_ms": 5000, "end_ms": 6000, "text": "second"},
                {"start_ms": 0, "end_ms": 1000, "text": "first"},
            ],
        )
        self.assertEqual([u.text for u in t.utterances], ["first", "second"])
        self.assertEqual([u.index for u in t.utterances], [0, 1])

    def test_whitespace_is_collapsed(self) -> None:
        t = tx.from_utterances(
            "s", [{"start_ms": 0, "end_ms": 1, "text": "  a\n\n  b  "}]
        )
        self.assertEqual(t.utterances[0].text, "a b")

    def test_words_split_on_speaker_change(self) -> None:
        words = [
            {"text": "hello", "start_ms": 0, "end_ms": 400, "speaker": "A"},
            {"text": "there", "start_ms": 400, "end_ms": 800, "speaker": "A"},
            {"text": "hi", "start_ms": 900, "end_ms": 1200, "speaker": "B"},
        ]
        t = tx.from_words("s", words)
        self.assertEqual(len(t.utterances), 2)
        self.assertEqual(t.utterances[0].text, "hello there")
        self.assertEqual(t.utterances[1].speaker, "B")

    def test_words_split_on_silence_gap(self) -> None:
        words = [
            {"text": "one", "start_ms": 0, "end_ms": 300, "speaker": "A"},
            {"text": "two", "start_ms": 5000, "end_ms": 5300, "speaker": "A"},
        ]
        t = tx.from_words("s", words, max_gap_ms=700)
        self.assertEqual(len(t.utterances), 2)

    def test_words_split_on_sentence_end(self) -> None:
        words = [
            {"text": "Done.", "start_ms": 0, "end_ms": 300, "speaker": "A"},
            {"text": "Next", "start_ms": 320, "end_ms": 600, "speaker": "A"},
        ]
        t = tx.from_words("s", words)
        self.assertEqual(len(t.utterances), 2)

    def test_empty_word_stream(self) -> None:
        self.assertEqual(tx.from_words("s", []).utterances, ())

    def test_speakers_preserve_first_appearance_order(self) -> None:
        t = transcript(("B", "x"), ("A", "y"), ("B", "z"))
        self.assertEqual(t.speakers, ("B", "A"))


class TestSaturatingSum(unittest.TestCase):
    def test_never_exceeds_one(self) -> None:
        self.assertLess(saturating_sum([0.9, 0.9, 0.9, 0.9]), 1.0)

    def test_weak_evidence_stays_weak(self) -> None:
        """Three marginal matches must not outrank one strong one."""
        self.assertLess(saturating_sum([0.3, 0.3, 0.3]), 0.9)

    def test_empty_is_zero(self) -> None:
        self.assertEqual(saturating_sum([]), 0.0)


if __name__ == "__main__":
    unittest.main()
