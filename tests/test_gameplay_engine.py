"""Catalogue, layout, timing, filtergraph emission, and end-to-end composition."""

from __future__ import annotations

import unittest

import _support  # noqa: F401  (path setup)

from clipforge.gameplay import (
    Box,
    FaceSample,
    Game,
    GameplayAsset,
    GameplayConfig,
    GameplayEngine,
    LayoutStyle,
    OUTPUT_FPS,
    OUTPUT_HEIGHT,
    OUTPUT_WIDTH,
    PROFILES,
    SpeakerTrack,
    caption_zone,
    choose_offset,
    choose_style,
    command,
    compose,
    conform_mode,
    conform_note,
    cover_source,
    filtergraph,
    link_check,
    plan_timing,
    profile,
    recommend,
    salience_warning,
    sendcmd_script,
    speaker_share,
)
from clipforge.gameplay.catalog import CROP_SAFE_SPREAD, crops_cleanly
from clipforge.gameplay.layout import gameplay_scale_mode
from clipforge.stream.layout import SAFE_ZONES, Destination


def track(duration: float = 20.0, fps: float = 10.0) -> SpeakerTrack:
    samples = tuple(
        FaceSample(index / fps, Box(760, 300, 187, 240), 0.9, "host")
        for index in range(int(duration * fps))
    )
    return SpeakerTrack(samples, source_width=1920, source_height=1080)


VERTICAL = GameplayAsset("ss", Game.SUBWAY_SURFERS, 180.0, 1080, 1920, 60.0,
                         loop_points=(3.0, 90.0), lead_in_s=2.0)
WIDE = GameplayAsset("mc", Game.MINECRAFT_PARKOUR, 300.0, 1920, 1080, 60.0)
SHORT = GameplayAsset("rl", Game.ROCKET_LEAGUE, 12.0, 2560, 1440, 60.0)
SLOW = GameplayAsset("gta", Game.GTA_DRIVING, 90.0, 1920, 1080, 30.0)
SAT = GameplayAsset("sat", Game.SATISFYING, 240.0, 1440, 1440, 30.0,
                    loop_points=(0.0,))
LIBRARY = (VERTICAL, WIDE, SHORT, SLOW, SAT)


class TestCatalogue(unittest.TestCase):
    def test_every_game_has_a_profile(self):
        for game in Game:
            self.assertIn(game, PROFILES)
            self.assertEqual(PROFILES[game].game, game)

    def test_salience_ordering_matches_the_footage(self):
        # Rocket League steals the most attention; satisfying loops the least.
        order = sorted(Game, key=lambda g: profile(g).salience)
        self.assertEqual(order[0], Game.SATISFYING)
        self.assertEqual(order[-1], Game.ROCKET_LEAGUE)

    def test_profile_values_are_in_range(self):
        for entry in PROFILES.values():
            self.assertGreaterEqual(entry.salience, 0.0)
            self.assertLessEqual(entry.salience, 1.0)
            self.assertGreater(entry.band, 0.0)
            self.assertLess(entry.band, 1.0)
            for fraction in (entry.action_center_x, entry.action_center_y,
                             entry.action_spread):
                self.assertGreaterEqual(fraction, 0.0)
                self.assertLessEqual(fraction, 1.0)

    def test_wide_action_does_not_crop_cleanly(self):
        self.assertFalse(crops_cleanly(Game.ROCKET_LEAGUE))
        self.assertFalse(crops_cleanly(Game.GTA_DRIVING))
        self.assertTrue(crops_cleanly(Game.MINECRAFT_PARKOUR))
        self.assertTrue(crops_cleanly(Game.SUBWAY_SURFERS))

    def test_recommendation_is_inverse_to_speech_density(self):
        # Fast talking gets a quiet floor — that is the whole design axis.
        fast = profile(recommend(4.0)).salience
        slow = profile(recommend(1.2)).salience
        self.assertLess(fast, slow)

    def test_warns_when_a_loud_bed_sits_under_dense_speech(self):
        self.assertTrue(salience_warning(Game.ROCKET_LEAGUE, 4.0))
        self.assertFalse(salience_warning(Game.SATISFYING, 4.0))

    def test_warns_when_nothing_is_holding_attention(self):
        self.assertTrue(salience_warning(Game.SATISFYING, 1.0))


class TestLayout(unittest.TestCase):
    def test_split_panels_tile_the_canvas_exactly(self):
        for style in (LayoutStyle.SPLIT, LayoutStyle.SPEAKER_DOMINANT,
                      LayoutStyle.GAMEPLAY_DOMINANT):
            plan = compose(20.0, track(), LIBRARY, Game.MINECRAFT_PARKOUR)
            plan = GameplayEngine(
                GameplayConfig(game=Game.MINECRAFT_PARKOUR, style=style)
            ).compose(20.0, track=track(), assets=LIBRARY)

            speaker = plan.panel("speaker")
            gameplay = plan.panel("gameplay")
            self.assertEqual(speaker.y, 0)
            self.assertEqual(gameplay.y, speaker.height)
            self.assertEqual(speaker.height + gameplay.height, OUTPUT_HEIGHT)
            self.assertEqual(speaker.width, OUTPUT_WIDTH)
            self.assertEqual(gameplay.width, OUTPUT_WIDTH)

    def test_a_louder_bed_gets_a_smaller_band(self):
        loud = speaker_share(LayoutStyle.SPLIT, Game.ROCKET_LEAGUE)
        quiet = speaker_share(LayoutStyle.SPLIT, Game.SATISFYING)
        self.assertGreater(loud, quiet, "high salience should cost the bed room")

    def test_speaker_share_stays_inside_its_bounds(self):
        for style in LayoutStyle:
            for game in list(Game) + [None]:
                share = speaker_share(style, game)
                self.assertGreater(share, 0.0)
                self.assertLessEqual(share, 1.0)

    def test_cover_source_matches_the_destination_aspect(self):
        _, _, w, h = cover_source(1920, 1080, 1080, 700)
        self.assertAlmostEqual(w / h, 1080 / 700, delta=0.02)

    def test_cover_source_stays_inside_the_frame(self):
        x, y, w, h = cover_source(1920, 1080, 1080, 700, center_x=0.95)
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + w, 1920)
        self.assertLessEqual(y + h, 1080)

    def test_vertical_source_keeps_its_full_width(self):
        # The actual advantage of natively vertical footage: the band takes a
        # vertical slice rather than cutting the sides off.
        _, _, w, _ = cover_source(1080, 1920, 1080, 700)
        self.assertEqual(w, 1080)

    def test_wide_action_is_fitted_not_cropped(self):
        self.assertEqual(gameplay_scale_mode(Game.ROCKET_LEAGUE, SHORT), "fit")
        self.assertEqual(gameplay_scale_mode(Game.MINECRAFT_PARKOUR, WIDE), "cover")

    def test_fitted_panels_report_the_whole_frame_as_source(self):
        # Reporting a crop rect the renderer will not use makes the plan lie.
        plan = compose(20.0, track(), LIBRARY, Game.ROCKET_LEAGUE)
        panel = plan.panel("gameplay")
        self.assertEqual(panel.scale_mode, "fit")
        self.assertEqual((panel.source_width, panel.source_height),
                         (SHORT.width, SHORT.height))

    def test_caption_zone_respects_platform_chrome(self):
        for destination in Destination:
            x, y, w, h = caption_zone(destination, 1150)
            safe = SAFE_ZONES[destination]
            self.assertGreaterEqual(y, int(OUTPUT_HEIGHT * safe.top))
            self.assertLessEqual(
                y + h, int(OUTPUT_HEIGHT * (1.0 - safe.bottom)) + 1)
            self.assertGreaterEqual(x, int(OUTPUT_WIDTH * safe.left))
            self.assertLessEqual(x + w, OUTPUT_WIDTH)

    def test_caption_zone_tracks_the_seam(self):
        high = caption_zone(Destination.TIKTOK, 900)[1]
        low = caption_zone(Destination.TIKTOK, 1300)[1]
        self.assertLess(high, low)

    def test_style_follows_speech_density(self):
        self.assertIs(choose_style(4.0, True, Game.SATISFYING),
                      LayoutStyle.SPEAKER_DOMINANT)
        self.assertIs(choose_style(1.0, True, Game.SATISFYING),
                      LayoutStyle.GAMEPLAY_DOMINANT)
        self.assertIs(choose_style(2.5, True, Game.SATISFYING),
                      LayoutStyle.SPLIT)

    def test_no_bed_means_speaker_only(self):
        self.assertIs(choose_style(2.5, True, None), LayoutStyle.SPEAKER_ONLY)


class TestTiming(unittest.TestCase):
    def test_conform_modes(self):
        self.assertEqual(conform_mode(60.0), "native")
        self.assertEqual(conform_mode(30.0), "duplicate")
        self.assertEqual(conform_mode(120.0), "decimate")

    def test_exact_ratio_is_reported_as_exact(self):
        self.assertIn("Exact ratio", conform_note(30.0, "Speaker"))

    def test_ntsc_rate_is_not_called_clean(self):
        # 29.97 into 60 is 2.001x, not 2x — about one extra duplicated frame
        # every 17s. Invisible, but claiming "no judder" overstates it.
        note = conform_note(29.97, "Speaker")
        self.assertNotIn("Exact ratio", note)
        self.assertIn("not a clean ratio", note)

    def test_badly_uneven_ratio_is_flagged(self):
        self.assertIn("does not divide", conform_note(24.0, "Speaker"))

    def test_native_rate_needs_no_note(self):
        self.assertEqual(conform_note(60.0, "Speaker"), "")

    def test_long_asset_needs_no_loop(self):
        timing = plan_timing(WIDE, 30.0, seed="a")
        self.assertEqual(len(timing.segments), 1)
        self.assertEqual(timing.loops, 0)
        self.assertEqual(timing.segments[0].seam, "none")

    def test_short_asset_loops_and_says_so(self):
        timing = plan_timing(SHORT, 40.0, seed="a")
        self.assertGreater(timing.loops, 0)
        self.assertGreater(timing.visible_seams, 0)
        self.assertTrue(any("seam" in note for note in timing.notes))

    def test_segments_tile_the_timeline_exactly(self):
        timing = plan_timing(SHORT, 40.0, seed="a")
        self.assertEqual(timing.segments[0].out_start, 0.0)
        self.assertAlmostEqual(timing.segments[-1].out_end, 40.0, places=5)
        for first, second in zip(timing.segments, timing.segments[1:]):
            self.assertAlmostEqual(first.out_end, second.out_start, places=5)

    def test_no_segment_reads_past_the_end_of_the_asset(self):
        for asset, duration in ((SHORT, 40.0), (VERTICAL, 500.0), (SAT, 61.0)):
            timing = plan_timing(asset, duration, seed="a")
            for segment in timing.segments:
                self.assertLessEqual(
                    segment.in_start + segment.duration,
                    asset.duration_s + 1e-6,
                    f"{asset.asset_id} segment overruns the source",
                )

    def test_declared_loop_points_make_seams_clean(self):
        timing = plan_timing(SAT, 700.0, seed="a")
        self.assertGreater(timing.loops, 0)
        self.assertEqual(timing.visible_seams, 0)

    def test_seams_are_nudged_into_speech(self):
        # A discontinuity is least noticeable while the speaker is talking,
        # because attention is on the other panel.
        speech = ((0.0, 4.0), (5.0, 20.0), (21.0, 40.0))
        timing = plan_timing(SHORT, 40.0, seed="a", speech=speech)
        seams = [s.out_start for s in timing.segments[1:]]
        self.assertTrue(seams)
        for seam in seams:
            inside = any(start <= seam <= end for start, end in speech)
            self.assertTrue(inside, f"seam at {seam:.2f}s landed in a pause")

    def test_offsets_are_deterministic(self):
        first = plan_timing(WIDE, 20.0, seed="clip-1").segments[0].in_start
        again = plan_timing(WIDE, 20.0, seed="clip-1").segments[0].in_start
        self.assertEqual(first, again)

    def test_different_clips_get_different_offsets(self):
        offsets = {
            plan_timing(WIDE, 20.0, seed=f"clip-{i}").segments[0].in_start
            for i in range(8)
        }
        self.assertGreater(len(offsets), 5)

    def test_offsets_avoid_recently_used_ones(self):
        recent = [choose_offset(WIDE, 20.0, "clip-1")]
        fresh = choose_offset(WIDE, 20.0, "clip-1", recent_offsets=recent)
        self.assertGreater(abs(fresh - recent[0]), 5.0)

    def test_offset_respects_the_lead_in(self):
        offset = choose_offset(VERTICAL, 20.0, "clip-1")
        self.assertGreaterEqual(offset, VERTICAL.lead_in_s)

    def test_asset_shorter_than_the_clip_starts_at_the_lead_in(self):
        timing = plan_timing(VERTICAL, 400.0, seed="a")
        self.assertEqual(timing.segments[0].in_start, VERTICAL.lead_in_s)

    def test_gameplay_audio_is_always_muted(self):
        self.assertEqual(plan_timing(WIDE, 20.0).audio, "muted")

    def test_interpolation_is_opt_in(self):
        self.assertEqual(plan_timing(SLOW, 20.0).fps_conform, "duplicate")
        opted = plan_timing(SLOW, 20.0, allow_interpolation=True)
        self.assertEqual(opted.fps_conform, "interpolate")
        self.assertTrue(any("smears" in note for note in opted.notes))

    def test_asset_with_no_usable_footage(self):
        empty = GameplayAsset("x", Game.SATISFYING, 2.0, lead_in_s=5.0)
        timing = plan_timing(empty, 20.0)
        self.assertEqual(timing.segments, ())
        self.assertTrue(timing.notes)


class TestFiltergraph(unittest.TestCase):
    def graphs(self):
        for game in Game:
            for style in LayoutStyle:
                plan = GameplayEngine(
                    GameplayConfig(game=game, style=style)
                ).compose(30.0, track=track(), assets=LIBRARY)
                yield game, style, filtergraph(plan)

    def test_every_style_and_game_produces_a_well_formed_graph(self):
        for game, style, graph in self.graphs():
            self.assertEqual(
                link_check(graph), [],
                f"{game.value} / {style.value} produced a malformed graph",
            )

    def test_looping_uses_split_not_a_repeated_input_pad(self):
        # ffmpeg rejects a filtergraph that names one input twice.
        plan = compose(40.0, track(), (SHORT,), Game.ROCKET_LEAGUE)
        graph = filtergraph(plan)
        self.assertGreater(plan.timing.loops, 0)
        self.assertIn("split=", graph)
        self.assertEqual(graph.count("[1:v]"), 1)
        self.assertEqual(link_check(graph), [])

    def test_link_check_catches_a_dangling_pad(self):
        problems = link_check("[0:v]scale=2:2[a];[0:a]anull[v]")
        self.assertTrue(any("never consumed" in p for p in problems))

    def test_link_check_catches_a_missing_producer(self):
        problems = link_check("[nope]scale=2:2[v]")
        self.assertTrue(any("never produced" in p for p in problems))

    def test_link_check_catches_a_doubly_consumed_input(self):
        problems = link_check("[0:v]trim=0:1[a];[0:v]trim=1:2[b];[a][b]concat[v]")
        self.assertTrue(any("consumed 2 times" in p for p in problems))

    def test_link_check_requires_an_output(self):
        self.assertTrue(any("no [v]" in p for p in link_check("[0:v]null[x]")))

    def test_graph_ends_in_a_single_output_pad(self):
        for _, _, graph in self.graphs():
            self.assertTrue(graph.rstrip().endswith("[v]"))

    def test_speaker_crop_dimensions_are_constant(self):
        # ffmpeg cannot resize a filter's output mid-stream, which is the
        # second reason the camera pans but never zooms.
        plan = compose(30.0, track(), LIBRARY, Game.MINECRAFT_PARKOUR)
        graph = filtergraph(plan)
        self.assertIn(f"crop@spk=w={plan.camera.width}:h={plan.camera.height}",
                      graph)
        self.assertNotIn("crop@spk w ", sendcmd_script(plan))
        self.assertNotIn("crop@spk h ", sendcmd_script(plan))

    def test_sendcmd_carries_every_keyframe(self):
        plan = compose(30.0, track(), LIBRARY, Game.MINECRAFT_PARKOUR)
        script = sendcmd_script(plan)
        self.assertEqual(len(script.strip().split("\n")),
                         len(plan.camera.keyframes))

    def test_sendcmd_timestamps_are_ordered(self):
        plan = compose(30.0, track(), LIBRARY, Game.MINECRAFT_PARKOUR)
        times = [float(line.split()[0])
                 for line in sendcmd_script(plan).strip().split("\n")]
        self.assertEqual(times, sorted(times))

    def test_command_never_maps_gameplay_audio(self):
        plan = compose(30.0, track(), LIBRARY, Game.MINECRAFT_PARKOUR)
        argv = command(plan, "speaker.mp4", "bed.mp4", "out.mp4")
        self.assertIn("0:a?", argv)
        self.assertNotIn("1:a", argv)
        self.assertNotIn("1:a?", argv)

    def test_command_does_not_loop_the_speaker_input(self):
        # `-stream_loop` placed before the wrong `-i` silently repeats the
        # person talking.
        plan = compose(30.0, track(), LIBRARY, Game.MINECRAFT_PARKOUR)
        argv = command(plan, "speaker.mp4", "bed.mp4", "out.mp4")
        self.assertNotIn("-stream_loop", argv)

    def test_command_pins_the_output_rate_and_duration(self):
        plan = compose(28.5, track(), LIBRARY, Game.MINECRAFT_PARKOUR)
        argv = command(plan, "speaker.mp4", "bed.mp4")
        self.assertEqual(argv[argv.index("-r") + 1], str(OUTPUT_FPS))
        self.assertEqual(argv[argv.index("-t") + 1], "28.500")

    def test_speaker_only_needs_no_second_input(self):
        plan = compose(20.0, track(), ())
        argv = command(plan, "speaker.mp4", "", "out.mp4")
        self.assertEqual(argv.count("-i"), 1)


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.plan = compose(28.0, track(28.0), LIBRARY,
                            Game.MINECRAFT_PARKOUR, word_count=78)

    def test_output_format_is_fixed(self):
        self.assertEqual(
            (self.plan.width, self.plan.height, self.plan.fps),
            (1080, 1920, 60),
        )

    def test_all_panel_dimensions_are_even(self):
        for panel in self.plan.panels:
            for value in (panel.width, panel.height,
                          panel.source_width, panel.source_height):
                self.assertEqual(value % 2, 0, f"{panel.name}: {value} is odd")

    def test_camera_crop_fits_inside_the_source(self):
        camera = self.plan.camera
        for keyframe in camera.keyframes:
            self.assertGreaterEqual(keyframe.x, 0)
            self.assertGreaterEqual(keyframe.y, 0)
            self.assertLessEqual(keyframe.x + camera.width, 1920)
            self.assertLessEqual(keyframe.y + camera.height, 1080)

    def test_plan_is_deterministic(self):
        again = compose(28.0, track(28.0), LIBRARY,
                        Game.MINECRAFT_PARKOUR, word_count=78)
        self.assertEqual(self.plan.to_dict(), again.to_dict())

    def test_stats_report_the_pipeline(self):
        stats = self.plan.stats
        self.assertAlmostEqual(stats["words_per_second"], 78 / 28.0, places=2)
        self.assertEqual(stats["tracking"], "tracked")
        self.assertEqual(stats["speakers"], 1)
        self.assertGreater(stats["keyframes"], 0)

    def test_serialises_through_json(self):
        import json

        payload = json.loads(json.dumps(self.plan.to_dict()))
        self.assertEqual(payload["output"]["fps"], 60)
        self.assertEqual(payload["style"], self.plan.style.value)
        self.assertEqual(len(payload["panels"]), len(self.plan.panels))

    def test_warnings_are_deduplicated(self):
        self.assertEqual(len(self.plan.warnings), len(set(self.plan.warnings)))

    def test_longest_matching_asset_is_preferred(self):
        short = GameplayAsset("mc-short", Game.MINECRAFT_PARKOUR, 20.0)
        plan = compose(28.0, track(28.0), (short, WIDE),
                       Game.MINECRAFT_PARKOUR)
        self.assertEqual(plan.timing.asset_id, WIDE.asset_id)

    def test_missing_game_falls_back_and_says_so(self):
        plan = compose(20.0, track(), (SAT,), Game.GTA_DRIVING)
        self.assertIs(plan.game, Game.SATISFYING)
        self.assertTrue(any("fell back" in w for w in plan.warnings))

    def test_empty_library_produces_a_speaker_only_plan(self):
        plan = compose(20.0, track(), ())
        self.assertIs(plan.style, LayoutStyle.SPEAKER_ONLY)
        self.assertIsNone(plan.panel("gameplay"))
        self.assertIsNone(plan.timing)
        self.assertEqual(plan.panel("speaker").height, OUTPUT_HEIGHT)

    def test_no_track_warns_and_still_composes(self):
        plan = compose(20.0, None, LIBRARY, Game.SATISFYING)
        self.assertEqual(plan.camera.tracking, "static")
        self.assertTrue(any("no speaker track" in w for w in plan.warnings))
        self.assertEqual(len(plan.camera.keyframes), 1)

    def test_bed_and_speech_mismatch_is_warned(self):
        plan = compose(20.0, track(), LIBRARY, Game.ROCKET_LEAGUE,
                       word_count=90)   # 4.5 words/sec
        self.assertTrue(any("competes" in w for w in plan.warnings))

    def test_engine_picks_a_bed_when_none_is_named(self):
        dense = compose(20.0, track(), LIBRARY, None, word_count=90)
        sparse = compose(20.0, track(), LIBRARY, None, word_count=20)
        self.assertLess(profile(dense.game).salience,
                        profile(sparse.game).salience)

    def test_zero_duration_is_rejected(self):
        with self.assertRaises(ValueError):
            compose(0.0, track(), LIBRARY)

    def test_two_speakers_are_counted(self):
        samples = tuple(
            FaceSample(index / 10.0,
                       Box(400 if index < 100 else 1350, 300, 187, 240),
                       0.9, "host" if index < 100 else "guest")
            for index in range(200)
        )
        plan = compose(20.0, SpeakerTrack(samples), LIBRARY, Game.SATISFYING)
        self.assertEqual(plan.stats["speakers"], 2)
        self.assertGreaterEqual(len(plan.camera.cuts), 1)


if __name__ == "__main__":
    unittest.main()
