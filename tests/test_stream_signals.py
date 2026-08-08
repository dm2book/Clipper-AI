"""Emote taxonomy, platform adapters, and chat signal extraction."""

from __future__ import annotations

import unittest

import _support  # noqa: F401  (path setup)

from clipforge.stream import adapters, emotes, signals
from clipforge.stream.types import ChatMessage, Platform, StreamSignal


def classify(text: str, emotes_tuple: tuple[str, ...] = (), platform=Platform.TWITCH):
    return emotes.classify_message(text, emotes_tuple, platform)


class TestEmoteClassification(unittest.TestCase):
    def test_core_vocabulary(self) -> None:
        cases = {
            "KEKW": StreamSignal.FUNNY,
            "MALDING": StreamSignal.RAGE,
            "POGGERS": StreamSignal.WIN,
            "PepeHands": StreamSignal.EMOTIONAL,
            "monkaS": StreamSignal.REACTION,
            "COPE": StreamSignal.ARGUMENT,
            "choked": StreamSignal.FAIL,
        }
        for token, expected in cases.items():
            with self.subTest(token=token):
                found = classify(token)
                self.assertIn(expected, found)
                self.assertEqual(max(found, key=lambda s: found[s]), expected)

    def test_ambiguous_emotes_carry_both_meanings(self) -> None:
        """OMEGALUL is laughter *at* a failure; both readings must survive."""
        found = classify("OMEGALUL")
        self.assertIn(StreamSignal.FUNNY, found)
        self.assertIn(StreamSignal.FAIL, found)
        self.assertGreater(found[StreamSignal.FUNNY], found[StreamSignal.FAIL])

    def test_stretched_emotes_read_louder(self) -> None:
        """Chat conveys intensity by mashing the key: COPEEEE > cope."""
        plain = classify("cope")[StreamSignal.ARGUMENT]
        stretched = classify("copeeeee")[StreamSignal.ARGUMENT]
        self.assertGreater(stretched, plain)

    def test_amplifiers_cannot_push_past_full_strength(self) -> None:
        """An already-maximal emote saturates rather than overflowing.

        KEKW is a top-weight emote and all-caps, so the shout amplifier alone
        reaches the ceiling; stretching it further has nowhere to go. That is
        intended — 1.0 means 'unambiguously this signal'.
        """
        self.assertEqual(classify("KEKW")[StreamSignal.FUNNY], 1.0)
        self.assertEqual(classify("KEKWWWWWWWW")[StreamSignal.FUNNY], 1.0)

    def test_case_insensitive(self) -> None:
        self.assertIn(StreamSignal.FUNNY, classify("kekw"))
        self.assertIn(StreamSignal.FUNNY, classify("KeKw"))

    def test_bare_letters_only_count_alone(self) -> None:
        """A standalone 'W' is a win; a 'w' inside a sentence is a typo."""
        self.assertIn(StreamSignal.WIN, classify("W"))
        self.assertNotIn(StreamSignal.WIN, classify("what a w game that was"))

    def test_emoji_are_classified(self) -> None:
        self.assertIn(StreamSignal.FUNNY, classify("\U0001F602\U0001F602"))
        self.assertIn(StreamSignal.RAGE, classify("\U0001F92C"))
        self.assertIn(StreamSignal.WIN, classify("\U0001F3C6"))

    def test_shouting_amplifies(self) -> None:
        quiet = classify("that was insane")
        loud = classify("THAT WAS INSANE")
        self.assertGreaterEqual(
            loud.get(StreamSignal.WIN, 0.0), quiet.get(StreamSignal.WIN, 0.0)
        )

    def test_filler_classifies_as_nothing(self) -> None:
        for text in ("hi chat", "what game is this", "lurking", "gm"):
            with self.subTest(text=text):
                self.assertEqual(classify(text), {})

    def test_structured_emote_metadata_is_used(self) -> None:
        found = classify("", ("KEKW",))
        self.assertIn(StreamSignal.FUNNY, found)

    def test_strengths_are_bounded(self) -> None:
        found = classify("KEKW OMEGALUL POGGERS MALDING PepeHands COPE monkaS")
        for signal, value in found.items():
            with self.subTest(signal=signal.value):
                self.assertGreaterEqual(value, 0.0)
                self.assertLessEqual(value, 1.0)

    def test_vocabulary_is_substantial(self) -> None:
        self.assertGreater(emotes.vocabulary_size(), 80)


class TestAdapters(unittest.TestCase):
    def test_twitch_offsets_and_emotes(self) -> None:
        chat = adapters.twitch_chat([
            {
                "content_offset_seconds": 12.5,
                "commenter": {"display_name": "alice"},
                "message": {
                    "body": "KEKW that was awful",
                    "fragments": [
                        {"text": "KEKW", "emoticon": {"emoticon_id": "1"}},
                        {"text": " that was awful"},
                    ],
                    "user_badges": [],
                },
            }
        ])
        self.assertEqual(len(chat), 1)
        self.assertEqual(chat[0].offset_ms, 12_500)
        self.assertEqual(chat[0].emotes, ("KEKW",))

    def test_twitch_bits_convert_to_dollars(self) -> None:
        events = adapters.twitch_events([
            {"type": "cheer", "offset_seconds": 10, "user_name": "bob", "bits": 500}
        ])
        self.assertAlmostEqual(events[0].amount, 5.0)

    def test_twitch_unknown_event_types_are_dropped(self) -> None:
        self.assertEqual(adapters.twitch_events([{"type": "mystery"}]), [])

    def test_kick_inline_emotes_are_parsed_and_stripped(self) -> None:
        chat = adapters.kick_chat(
            [
                {
                    "created_at": "2026-01-01T10:00:30Z",
                    "content": "[emote:12345:KEKW] no way",
                    "sender": {"username": "carol", "identity": {"badges": []}},
                }
            ],
            stream_started_at="2026-01-01T10:00:00Z",
        )
        self.assertEqual(chat[0].offset_ms, 30_000)
        self.assertEqual(chat[0].emotes, ("KEKW",))
        self.assertEqual(chat[0].text, "no way")

    def test_kick_requires_a_stream_start(self) -> None:
        """Kick timestamps are wall-clock; without an origin they are useless."""
        with self.assertRaises(ValueError):
            adapters.build_session(
                session_id="s", platform=Platform.KICK, duration_ms=1000
            )

    def test_youtube_superchat_amount(self) -> None:
        events = adapters.youtube_events([
            {
                "snippet": {
                    "type": "superChatEvent",
                    "videoOffsetTimeMsec": "45000",
                    "superChatDetails": {
                        "amountMicros": "20000000",
                        "currency": "USD",
                        "userComment": "great stream",
                    },
                },
                "authorDetails": {"displayName": "dave"},
            }
        ])
        self.assertEqual(events[0].offset_ms, 45_000)
        self.assertAlmostEqual(events[0].amount, 20.0)
        self.assertEqual(events[0].message, "great stream")

    def test_youtube_chat_offsets(self) -> None:
        chat = adapters.youtube_chat([
            {
                "snippet": {"displayMessage": "\U0001F602", "videoOffsetTimeMsec": "7000"},
                "authorDetails": {"displayName": "erin"},
            }
        ])
        self.assertEqual(chat[0].offset_ms, 7_000)

    def test_messages_are_sorted(self) -> None:
        chat = adapters.twitch_chat([
            {"content_offset_seconds": 30, "commenter": {}, "message": {"body": "b"}},
            {"content_offset_seconds": 10, "commenter": {}, "message": {"body": "a"}},
        ])
        self.assertEqual([m.offset_ms for m in chat], [10_000, 30_000])


class TestChatVelocity(unittest.TestCase):
    @staticmethod
    def messages(spec: list[tuple[int, int]]) -> list[ChatMessage]:
        """spec = [(second, count), ...]"""
        out = []
        for second, count in spec:
            for i in range(count):
                out.append(ChatMessage(second * 1000 + i, f"u{i}", "hi"))
        return out

    def test_bucketing(self) -> None:
        counts = signals.bucket_chat(self.messages([(0, 3), (5, 7)]), 10_000)
        self.assertEqual(counts[0], 3)
        self.assertEqual(counts[5], 7)
        self.assertEqual(counts[1], 0)

    def test_baseline_ignores_a_single_huge_spike(self) -> None:
        """Median baseline: one 500-message second must not raise the floor."""
        counts = [2] * 200 + [500] + [2] * 50
        baseline = signals.rolling_baseline(counts)
        self.assertLess(baseline[-1], 5.0)

    def test_baseline_has_a_floor(self) -> None:
        baseline = signals.rolling_baseline([0] * 50)
        self.assertTrue(all(b >= signals.MIN_BASELINE_RATE for b in baseline))

    def test_spike_detection_records_onset_not_peak(self) -> None:
        counts = [2] * 100 + [10, 30, 60, 40, 20] + [2] * 100
        baseline = signals.rolling_baseline(counts)
        spikes = signals.find_spikes(counts, baseline)
        self.assertEqual(len(spikes), 1)
        spike = spikes[0]
        self.assertLess(spike.onset_ms, spike.peak_ms)
        self.assertAlmostEqual(spike.onset_ms / 1000, 100, delta=1)
        self.assertGreater(spike.magnitude, 5.0)

    def test_flat_chat_produces_no_spikes(self) -> None:
        counts = [3] * 300
        self.assertEqual(signals.find_spikes(counts, signals.rolling_baseline(counts)), [])

    def test_hysteresis_does_not_split_one_burst(self) -> None:
        """A momentary dip mid-burst must not become two spikes."""
        counts = [2] * 100 + [40, 45, 12, 44, 40] + [2] * 100
        spikes = signals.find_spikes(counts, signals.rolling_baseline(counts))
        self.assertEqual(len(spikes), 1)


class TestSignalAggregation(unittest.TestCase):
    def test_strength_is_a_share_of_chat_not_a_count(self) -> None:
        """The fix that makes signals discriminative at chat scale.

        Two funny messages inside a 500-message burst is a busy moment that
        happens to contain a joke, not a funny moment.
        """
        samples = signals.chat_signals(
            [ChatMessage(1000, "u", "KEKW"), ChatMessage(1100, "u", "KEKW")],
            Platform.TWITCH,
        )
        dominated = signals.aggregate_window(samples, 0, 10_000, chat_in_window=4)
        diluted = signals.aggregate_window(samples, 0, 10_000, chat_in_window=500)
        self.assertGreater(dominated[StreamSignal.FUNNY], diluted[StreamSignal.FUNNY])

    def test_saturation_coverage_reaches_full_strength(self) -> None:
        samples = signals.chat_signals(
            [ChatMessage(i * 100, "u", "KEKW") for i in range(30)], Platform.TWITCH
        )
        found = signals.aggregate_window(samples, 0, 10_000, chat_in_window=60)
        self.assertGreater(found[StreamSignal.FUNNY], 0.7)

    def test_event_signals_scale_within_the_stream(self) -> None:
        from clipforge.stream.types import EventKind, StreamEvent

        events = [
            StreamEvent(1000, EventKind.DONATION, "a", amount=5.0),
            StreamEvent(2000, EventKind.DONATION, "b", amount=5.0),
            StreamEvent(3000, EventKind.DONATION, "c", amount=500.0),
        ]
        samples = [s for s in signals.event_signals(events)
                   if s.signal is StreamSignal.DONATION]
        small, _, large = sorted(samples, key=lambda s: s.offset_ms)
        self.assertGreater(large.strength, small.strength)

    def test_follows_are_ignored(self) -> None:
        from clipforge.stream.types import EventKind, StreamEvent

        samples = signals.event_signals([StreamEvent(0, EventKind.FOLLOW, "a")])
        self.assertEqual(samples, [])

    def test_donation_with_a_message_also_emits_a_reaction(self) -> None:
        from clipforge.stream.types import EventKind, StreamEvent

        samples = signals.event_signals([
            StreamEvent(0, EventKind.DONATION, "a", amount=50.0, message="read this out")
        ])
        self.assertIn(StreamSignal.REACTION, {s.signal for s in samples})


if __name__ == "__main__":
    unittest.main()
