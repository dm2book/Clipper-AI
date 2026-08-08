"""Anchor timing, vertical layout, and end-to-end clipping."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from _support import ROOT

from clipforge.stream import (
    ClipperConfig,
    Destination,
    LayoutStyle,
    Platform,
    StreamClipperEngine,
    VideoRegion,
    build_session,
)
from clipforge.stream import anchors as anchor_mod
from clipforge.stream import layout as layout_mod
from clipforge.stream import scoring, signals as signals_mod
from clipforge.stream.types import (
    Anchor,
    ChatMessage,
    EventKind,
    StreamEvent,
    StreamSession,
    StreamSignal,
)

SAMPLE = ROOT / "demo" / "sample_stream.json"


def session_with(chat, duration_ms=600_000, platform=Platform.TWITCH, **kwargs):
    return StreamSession(
        session_id="t",
        platform=platform,
        duration_ms=duration_ms,
        chat=tuple(chat),
        **kwargs,
    )


class TestReactionLag(unittest.TestCase):
    """The single most important behaviour in the whole engine."""

    def test_anchor_precedes_the_chat_spike(self) -> None:
        chat = [ChatMessage(s * 1000 + i * 10, f"u{i}", "hi")
                for s in range(200) for i in range(2)]
        # Burst at t=200s.
        chat += [ChatMessage(200_000 + i * 20, f"u{i}", "KEKW") for i in range(120)]

        session = session_with(chat)
        samples = signals_mod.collect(session)
        counts = signals_mod.bucket_chat(session.chat, session.duration_ms)
        spikes = signals_mod.find_spikes(counts, signals_mod.rolling_baseline(counts))
        self.assertTrue(spikes)

        found = anchor_mod.from_spikes(session, samples, spikes)
        anchor = found[0]
        self.assertLess(
            anchor.offset_ms,
            spikes[0].onset_ms,
            "the anchor must sit before chat reacted, not on it",
        )
        expected_lag = anchor_mod.REACTION_LAG_MS[Platform.TWITCH]
        self.assertEqual(spikes[0].onset_ms - anchor.offset_ms, expected_lag)

    def test_youtube_lag_exceeds_twitch(self) -> None:
        self.assertGreater(
            anchor_mod.REACTION_LAG_MS[Platform.YOUTUBE_LIVE],
            anchor_mod.REACTION_LAG_MS[Platform.TWITCH],
        )

    def test_lag_override(self) -> None:
        self.assertEqual(anchor_mod.reaction_lag(Platform.TWITCH, 1234), 1234)

    def test_anchor_never_goes_negative(self) -> None:
        chat = [ChatMessage(i * 100, f"u{i}", "KEKW") for i in range(60)]
        session = session_with(chat, duration_ms=60_000)
        samples = signals_mod.collect(session)
        counts = signals_mod.bucket_chat(session.chat, session.duration_ms)
        spikes = signals_mod.find_spikes(counts, signals_mod.rolling_baseline(counts))
        for anchor in anchor_mod.from_spikes(session, samples, spikes):
            self.assertGreaterEqual(anchor.offset_ms, 0)


class TestAnchorMerging(unittest.TestCase):
    @staticmethod
    def anchor(offset_ms: int, signal: StreamSignal, strength: float) -> Anchor:
        return Anchor(offset_ms=offset_ms, signals={signal: strength}, intensity=strength)

    def test_nearby_anchors_merge_and_union_signals(self) -> None:
        merged = anchor_mod.merge([
            self.anchor(100_000, StreamSignal.WIN, 0.9),
            self.anchor(104_000, StreamSignal.DONATION, 0.6),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(
            set(merged[0].signals), {StreamSignal.WIN, StreamSignal.DONATION}
        )

    def test_distant_anchors_stay_separate(self) -> None:
        merged = anchor_mod.merge([
            self.anchor(100_000, StreamSignal.WIN, 0.9),
            self.anchor(400_000, StreamSignal.FAIL, 0.8),
        ])
        self.assertEqual(len(merged), 2)

    def test_merge_keeps_the_strongest_timing(self) -> None:
        merged = anchor_mod.merge([
            self.anchor(100_000, StreamSignal.WIN, 0.4),
            self.anchor(105_000, StreamSignal.RAGE, 0.95),
        ])
        self.assertEqual(merged[0].offset_ms, 105_000)

    def test_empty(self) -> None:
        self.assertEqual(anchor_mod.merge([]), [])


class TestWindowPlacement(unittest.TestCase):
    def setUp(self) -> None:
        self.session = session_with([], duration_ms=1_800_000)
        self.anchor = Anchor(600_000, {StreamSignal.WIN: 0.9}, 0.9)

    def test_every_duration_is_exact(self) -> None:
        for duration_s, start, end in anchor_mod.variants(
            self.anchor, self.session, (15, 30, 45, 60)
        ):
            with self.subTest(duration=duration_s):
                self.assertEqual(end - start, duration_s * 1000)

    def test_moment_lands_in_the_first_half(self) -> None:
        for duration_s, start, end in anchor_mod.variants(
            self.anchor, self.session, (15, 30, 45, 60)
        ):
            position = (self.anchor.offset_ms - start) / (end - start)
            with self.subTest(duration=duration_s):
                self.assertGreater(position, 0.15)
                self.assertLess(position, 0.5)

    def test_longer_clips_get_more_setup(self) -> None:
        _, short_start, _ = anchor_mod.window_for(self.anchor, 15, self.session), 0, 0
        short = anchor_mod.window_for(self.anchor, 15, self.session)
        long = anchor_mod.window_for(self.anchor, 60, self.session)
        self.assertGreater(
            self.anchor.offset_ms - long[0], self.anchor.offset_ms - short[0]
        )

    def test_clamps_at_the_stream_start_without_shortening(self) -> None:
        early = Anchor(2_000, {StreamSignal.WIN: 0.9}, 0.9)
        start, end = anchor_mod.window_for(early, 60, self.session)
        self.assertEqual(start, 0)
        self.assertEqual(end - start, 60_000)

    def test_clamps_at_the_stream_end_without_shortening(self) -> None:
        late = Anchor(1_795_000, {StreamSignal.WIN: 0.9}, 0.9)
        start, end = anchor_mod.window_for(late, 60, self.session)
        self.assertEqual(end, self.session.duration_ms)
        self.assertEqual(end - start, 60_000)

    def test_stream_shorter_than_the_clip(self) -> None:
        tiny = session_with([], duration_ms=10_000)
        start, end = anchor_mod.window_for(self.anchor, 60, tiny)
        self.assertEqual((start, end), (0, 10_000))


class TestLayout(unittest.TestCase):
    GAMEPLAY = VideoRegion("gameplay", 0, 0, 1920, 1080)
    FACECAM = VideoRegion("facecam", 1420, 760, 480, 270)

    def test_cover_crop_matches_destination_aspect(self) -> None:
        cropped = layout_mod.cover_crop(self.GAMEPLAY, 1080, 1920)
        self.assertAlmostEqual(cropped.width / cropped.height, 1080 / 1920, places=2)

    def test_cover_crop_stays_inside_the_source(self) -> None:
        cropped = layout_mod.cover_crop(self.GAMEPLAY, 1080, 1920)
        self.assertGreaterEqual(cropped.x, self.GAMEPLAY.x)
        self.assertLessEqual(
            cropped.x + cropped.width, self.GAMEPLAY.x + self.GAMEPLAY.width
        )

    def test_facecam_stacks_above_gameplay_with_no_gap(self) -> None:
        plan = layout_mod.plan((self.FACECAM, self.GAMEPLAY), 1920, 1080)
        self.assertEqual(plan.name, LayoutStyle.FACECAM_OVER_GAMEPLAY.value)
        face, game = plan.crops
        self.assertEqual(face.dest_y, 0)
        self.assertEqual(game.dest_y, face.dest_height)
        self.assertEqual(face.dest_height + game.dest_height, layout_mod.OUTPUT_HEIGHT)

    def test_degrades_gracefully_without_a_facecam(self) -> None:
        """Asking for the stacked layout with no facecam must still ship a clip."""
        plan = layout_mod.plan(
            (self.GAMEPLAY,), 1920, 1080, style=LayoutStyle.FACECAM_OVER_GAMEPLAY
        )
        self.assertEqual(plan.name, LayoutStyle.GAMEPLAY_ONLY.value)
        self.assertEqual(len(plan.crops), 1)

    def test_no_regions_falls_back_to_full_frame(self) -> None:
        plan = layout_mod.plan((), 1920, 1080)
        self.assertEqual(len(plan.crops), 1)
        self.assertEqual(plan.crops[0].dest_height, layout_mod.OUTPUT_HEIGHT)

    def test_output_is_vertical(self) -> None:
        plan = layout_mod.plan((self.FACECAM, self.GAMEPLAY), 1920, 1080)
        self.assertEqual((plan.width, plan.height), (1080, 1920))

    def test_captions_clear_every_platform_chrome(self) -> None:
        for destination in Destination:
            with self.subTest(destination=destination.value):
                plan = layout_mod.plan((self.GAMEPLAY,), 1920, 1080, destination=destination)
                x, y, w, h = plan.caption_zone
                safe = layout_mod.SAFE_ZONES[destination]
                self.assertGreaterEqual(y, layout_mod.OUTPUT_HEIGHT * safe.top)
                self.assertLessEqual(
                    y + h, layout_mod.OUTPUT_HEIGHT * (1.0 - safe.bottom) + 1
                )
                self.assertLessEqual(
                    x + w, layout_mod.OUTPUT_WIDTH * (1.0 - safe.right) + 1
                )

    def test_reels_reserves_more_bottom_space_than_shorts(self) -> None:
        self.assertGreater(
            layout_mod.SAFE_ZONES[Destination.REELS].bottom,
            layout_mod.SAFE_ZONES[Destination.SHORTS].bottom,
        )

    def test_chat_overlay_sits_above_the_captions(self) -> None:
        plan = layout_mod.plan((self.GAMEPLAY,), 1920, 1080, include_chat=True)
        self.assertIsNotNone(plan.chat_overlay)
        assert plan.chat_overlay is not None
        self.assertLess(
            plan.chat_overlay[1] + plan.chat_overlay[3], plan.caption_zone[1] + 1
        )

    def test_layout_serialises(self) -> None:
        plan = layout_mod.plan((self.FACECAM, self.GAMEPLAY), 1920, 1080, include_chat=True)
        payload = json.loads(json.dumps(plan.to_dict()))
        self.assertEqual(payload["width"], 1080)
        self.assertEqual(len(payload["crops"]), 2)


class TestDurationPreference(unittest.TestCase):
    def test_fast_moments_prefer_short_clips(self) -> None:
        for signal in (StreamSignal.WIN, StreamSignal.FAIL, StreamSignal.FUNNY):
            with self.subTest(signal=signal.value):
                self.assertGreater(
                    scoring.duration_preference(signal, 15),
                    scoring.duration_preference(signal, 60),
                )

    def test_slow_moments_prefer_long_clips(self) -> None:
        for signal in (StreamSignal.ARGUMENT, StreamSignal.EMOTIONAL):
            with self.subTest(signal=signal.value):
                self.assertGreater(
                    scoring.duration_preference(signal, 45),
                    scoring.duration_preference(signal, 15),
                )

    def test_unknown_signal_is_handled(self) -> None:
        self.assertGreater(scoring.duration_preference(None, 30), 0.0)


class TestEngineEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        raw = json.loads(SAMPLE.read_text())
        cls.raw = raw
        cls.session = build_session(
            session_id=raw["session_id"],
            platform=Platform(raw["platform"]),
            duration_ms=raw["duration_ms"],
            raw_chat=raw["chat"],
            raw_events=raw["events"],
            regions=[VideoRegion(**r) for r in raw["regions"]],
        )

    def test_finds_every_scripted_moment(self) -> None:
        """Each planted moment should be recovered within a few seconds."""
        result = StreamClipperEngine().clip(self.session)
        for entry in self.raw["ground_truth_moments"]:
            expected_ms = entry["offset_s"] * 1000
            nearest = min(
                (abs(a.offset_ms - expected_ms) for a in result.anchors), default=None
            )
            with self.subTest(moment=entry["label"]):
                self.assertIsNotNone(nearest)
                self.assertLess(
                    nearest / 1000, 4.0, f"{entry['label']} was off by {nearest / 1000:.1f}s"
                )

    def test_returns_clips_ordered_by_virality(self) -> None:
        result = StreamClipperEngine().clip(self.session)
        scores = [c.scores.virality for c in result.clips]
        self.assertTrue(scores)
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_best_variant_mode_returns_no_overlapping_clips(self) -> None:
        result = StreamClipperEngine().clip(self.session)
        for i, a in enumerate(result.clips):
            for b in result.clips[i + 1 :]:
                with self.subTest(a=a.start_ms, b=b.start_ms):
                    self.assertLessEqual(
                        StreamClipperEngine._overlap(a, b), 0.4
                    )

    def test_all_variants_mode_produces_four_lengths_per_moment(self) -> None:
        result = StreamClipperEngine(
            ClipperConfig(best_variant_only=False, min_virality=0, max_moments=3)
        ).clip(self.session)
        by_anchor: dict[int, set[int]] = {}
        for clip in result.clips:
            by_anchor.setdefault(clip.anchor.offset_ms, set()).add(clip.duration_s)
        self.assertTrue(by_anchor)
        for anchor_ms, durations in by_anchor.items():
            with self.subTest(anchor=anchor_ms):
                self.assertEqual(durations, {15, 30, 45, 60})

    def test_clip_durations_are_exact(self) -> None:
        result = StreamClipperEngine(
            ClipperConfig(best_variant_only=False, min_virality=0)
        ).clip(self.session)
        for clip in result.clips:
            with self.subTest(start=clip.start_ms):
                self.assertEqual(clip.end_ms - clip.start_ms, clip.duration_s * 1000)

    def test_clips_stay_inside_the_stream(self) -> None:
        result = StreamClipperEngine().clip(self.session)
        for clip in result.clips:
            self.assertGreaterEqual(clip.start_ms, 0)
            self.assertLessEqual(clip.end_ms, self.session.duration_ms)

    def test_every_clip_carries_a_vertical_layout(self) -> None:
        result = StreamClipperEngine().clip(self.session)
        for clip in result.clips:
            self.assertEqual((clip.layout.width, clip.layout.height), (1080, 1920))
            self.assertTrue(clip.layout.crops)

    def test_signals_discriminate_between_moments(self) -> None:
        """Share-based scoring must not saturate every signal to 1.0."""
        result = StreamClipperEngine().clip(self.session)
        dominants = {c.anchor.dominant for c in result.clips}
        self.assertGreater(
            len(dominants), 2, f"expected varied dominant signals, got {dominants}"
        )

    def test_result_serialises(self) -> None:
        result = StreamClipperEngine().clip(self.session)
        payload = json.loads(json.dumps(result.to_dict()))
        self.assertEqual(payload["session_id"], "demo-twitch-vod")
        self.assertIn("layout", payload["clips"][0])

    def test_min_virality_filters(self) -> None:
        result = StreamClipperEngine(ClipperConfig(min_virality=85)).clip(self.session)
        for clip in result.clips:
            self.assertGreaterEqual(clip.scores.virality, 85)


class TestEngineEdgeCases(unittest.TestCase):
    def test_no_chat_and_no_events(self) -> None:
        result = StreamClipperEngine().clip(
            StreamSession("empty", Platform.TWITCH, 600_000)
        )
        self.assertEqual(result.clips, [])
        self.assertIn("reason", result.stats)

    def test_zero_duration(self) -> None:
        result = StreamClipperEngine().clip(
            StreamSession("zero", Platform.KICK, 0, chat=(ChatMessage(0, "u", "KEKW"),))
        )
        self.assertEqual(result.clips, [])

    def test_flat_chat_produces_no_spikes(self) -> None:
        chat = [ChatMessage(s * 1000 + i * 300, f"u{i}", "hi")
                for s in range(600) for i in range(3)]
        result = StreamClipperEngine().clip(session_with(chat, duration_ms=600_000))
        self.assertEqual(result.stats["chat_spikes"], 0)

    def test_events_alone_can_produce_clips(self) -> None:
        """A stream with a dead chat but a huge donation is still clippable."""
        session = StreamSession(
            session_id="quiet",
            platform=Platform.YOUTUBE_LIVE,
            duration_ms=600_000,
            events=(
                StreamEvent(120_000, EventKind.DONATION, "whale", amount=500.0,
                            message="thanks for everything"),
            ),
        )
        result = StreamClipperEngine(ClipperConfig(min_virality=0)).clip(session)
        self.assertTrue(result.clips)
        self.assertIn(
            StreamSignal.DONATION, result.clips[0].signals
        )

    def test_all_three_platforms_run(self) -> None:
        chat = [ChatMessage(s * 1000, f"u{s}", "hi") for s in range(200)]
        chat += [ChatMessage(150_000 + i * 30, f"u{i}", "KEKW") for i in range(90)]
        for platform in Platform:
            with self.subTest(platform=platform.value):
                result = StreamClipperEngine(ClipperConfig(min_virality=0)).clip(
                    session_with(chat, platform=platform)
                )
                self.assertEqual(result.stats["platform"], platform.value)
                self.assertTrue(result.clips)


if __name__ == "__main__":
    unittest.main()
