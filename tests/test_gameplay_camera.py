"""The virtual camera: smoothing, deadband, cuts, gaps, and framing.

These are behavioural tests on the shape of the motion, not on exact pixel
values. What matters is that the camera is still when it should be, moves
smoothly when it moves, cuts instead of panning across the frame, and never
recentres during a detector dropout — the four things a viewer notices.
"""

from __future__ import annotations

import math
import unittest

import _support  # noqa: F401  (path setup)

from clipforge.gameplay import (
    Box,
    FaceSample,
    Motion,
    OneEuroFilter,
    SpeakerTrack,
    plan_crop_size,
    solve,
)
from clipforge.gameplay.camera import (
    DEADBAND,
    EYELINE,
    EYES_IN_BOX,
    MAX_PAN_SPEED,
    MIN_SHOT_S,
    TARGET_FACE_HEIGHT,
    _desired,
)

ASPECT = 1080 / 1152   # a typical speaker panel


def still_track(
    duration: float = 6.0,
    fps: float = 10.0,
    x: float = 800.0,
    y: float = 300.0,
    size: float = 240.0,
    jitter: float = 0.0,
    speaker: str = "host",
) -> list[FaceSample]:
    """A speaker who does not move, optionally with detector jitter."""
    samples = []
    steps = int(duration * fps)
    for index in range(steps):
        wobble = jitter * math.sin(index * 2.1)
        samples.append(
            FaceSample(
                t=index / fps,
                box=Box(x + wobble, y + wobble * 0.6, size * 0.78, size),
                confidence=0.9,
                speaker_id=speaker,
            )
        )
    return samples


def path_steps(path, fps: int = 60) -> list[float]:
    """Per-second movement between consecutive non-cut keyframes."""
    steps = []
    for first, second in zip(path.keyframes, path.keyframes[1:]):
        if second.motion is Motion.CUT:
            continue
        dt = second.t - first.t
        if dt <= 0:
            continue
        steps.append(math.hypot(second.x - first.x, second.y - first.y) / dt)
    return steps


class TestOneEuroFilter(unittest.TestCase):
    def test_first_sample_passes_through(self):
        filt = OneEuroFilter()
        self.assertEqual(filt(100.0, 1 / 60), 100.0)

    def test_converges_on_a_constant_signal(self):
        filt = OneEuroFilter()
        filt(0.0, 1 / 60)
        for _ in range(240):
            out = filt(100.0, 1 / 60)
        self.assertAlmostEqual(out, 100.0, delta=1.0)

    def test_attenuates_jitter(self):
        filt = OneEuroFilter()
        filt(500.0, 1 / 60)
        outputs = []
        for index in range(180):
            noisy = 500.0 + (8.0 if index % 2 else -8.0)
            outputs.append(filt(noisy, 1 / 60))

        settled = outputs[60:]
        spread = max(settled) - min(settled)
        self.assertLess(spread, 4.0, "16px of jitter should not survive intact")

    def test_tracks_a_ramp_without_running_ahead_of_it(self):
        filt = OneEuroFilter()
        filt(0.0, 1 / 60)
        outputs = [filt(index * 2.0, 1 / 60) for index in range(1, 120)]

        # Lags the input (it is a low-pass filter) but never overshoots it.
        for index, out in enumerate(outputs, start=1):
            self.assertLessEqual(out, index * 2.0 + 0.01)
        # And still gets most of the way there.
        self.assertGreater(outputs[-1], 119 * 2.0 * 0.85)

    def test_reset_clears_velocity(self):
        filt = OneEuroFilter()
        for value in (0.0, 50.0, 100.0, 150.0):
            filt(value, 1 / 60)
        filt.reset(900.0)
        self.assertEqual(filt.value, 900.0)
        self.assertAlmostEqual(filt(900.0, 1 / 60), 900.0, delta=0.01)


class TestCropSizing(unittest.TestCase):
    def test_face_occupies_the_target_share_of_the_crop(self):
        track = SpeakerTrack(tuple(still_track(size=240.0)))
        _, crop_h = plan_crop_size(track, ASPECT)
        self.assertAlmostEqual(240.0 / crop_h, TARGET_FACE_HEIGHT, delta=0.03)

    def test_crop_matches_the_requested_aspect(self):
        track = SpeakerTrack(tuple(still_track()))
        crop_w, crop_h = plan_crop_size(track, ASPECT)
        self.assertAlmostEqual(crop_w / crop_h, ASPECT, delta=0.02)

    def test_crop_never_exceeds_the_source(self):
        track = SpeakerTrack(tuple(still_track(size=900.0)),
                             source_width=1920, source_height=1080)
        crop_w, crop_h = plan_crop_size(track, ASPECT)
        self.assertLessEqual(crop_w, 1920)
        self.assertLessEqual(crop_h, 1080)

    def test_dimensions_are_even(self):
        # Odd dimensions shift the chroma plane in yuv420p.
        track = SpeakerTrack(tuple(still_track(size=237.0)))
        crop_w, crop_h = plan_crop_size(track, ASPECT)
        self.assertEqual(crop_w % 2, 0)
        self.assertEqual(crop_h % 2, 0)

    def test_sized_for_the_largest_face_not_the_average(self):
        # A speaker who leans in must not overflow the crop chosen from the
        # mean of the clip.
        samples = still_track(duration=5.0, size=200.0)
        samples += [
            FaceSample(5.0 + i / 10.0, Box(800, 300, 250, 320), 0.9)
            for i in range(20)
        ]
        track = SpeakerTrack(tuple(samples))
        _, crop_h = plan_crop_size(track, ASPECT)
        self.assertGreater(crop_h, 320 / TARGET_FACE_HEIGHT * 0.85)

    def test_empty_track_falls_back_to_a_full_cover_crop(self):
        crop_w, crop_h = plan_crop_size(SpeakerTrack(), ASPECT)
        self.assertGreater(crop_w, 0)
        self.assertGreater(crop_h, 0)


class TestFraming(unittest.TestCase):
    def test_eyes_land_on_the_eyeline_not_the_centre(self):
        # Centring the face box leaves a slab of dead space above the head.
        box = Box(800, 300, 190, 240)
        crop_w, crop_h = 826, 882
        x, y = _desired(box, crop_w, crop_h, 1920, 1080)

        eye_y = box.y + box.height * EYES_IN_BOX
        self.assertAlmostEqual((eye_y - y) / crop_h, EYELINE, delta=0.01)

    def test_face_is_horizontally_centred(self):
        box = Box(800, 300, 190, 240)
        x, _ = _desired(box, 826, 882, 1920, 1080)
        self.assertAlmostEqual(x + 826 / 2, box.cx, delta=1.0)

    def test_crop_is_clamped_inside_the_source(self):
        # A face at the very edge must not produce a negative origin.
        box = Box(10, 10, 190, 240)
        x, y = _desired(box, 826, 882, 1920, 1080)
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)

        box = Box(1880, 1050, 190, 240)
        x, y = _desired(box, 826, 882, 1920, 1080)
        self.assertLessEqual(x + 826, 1920)
        self.assertLessEqual(y + 882, 1080)


class TestStillness(unittest.TestCase):
    def test_a_motionless_speaker_produces_a_motionless_camera(self):
        track = SpeakerTrack(tuple(still_track(duration=8.0)))
        path = solve(track, 8.0, ASPECT)
        self.assertGreater(path.hold_ratio, 0.95)
        self.assertEqual(len(path.cuts), 0)

    def test_detector_jitter_does_not_move_the_camera(self):
        # 12px of wobble on an 826px crop is well inside the deadband. A
        # camera that follows it vibrates.
        track = SpeakerTrack(tuple(still_track(duration=8.0, jitter=6.0)))
        path = solve(track, 8.0, ASPECT)
        self.assertGreater(path.hold_ratio, 0.90)

        positions = {(k.x, k.y) for k in path.keyframes}
        self.assertLessEqual(len(positions), 3)

    def test_a_still_clip_compresses_to_very_few_keyframes(self):
        # This is what makes the path executable as a sendcmd script.
        track = SpeakerTrack(tuple(still_track(duration=20.0)))
        path = solve(track, 20.0, ASPECT)
        self.assertLess(len(path.keyframes), 20, "1200 frames should not "
                        "produce 1200 keyframes")


class TestMotion(unittest.TestCase):
    def drifting(self, distance: float = 220.0, duration: float = 8.0):
        samples = []
        for index in range(int(duration * 10)):
            t = index / 10.0
            samples.append(
                FaceSample(t, Box(700 + distance * (t / duration), 300, 187, 240),
                           0.9, "host")
            )
        return SpeakerTrack(tuple(samples))

    def test_a_real_move_is_followed(self):
        path = solve(self.drifting(), 8.0, ASPECT)
        xs = [k.x for k in path.keyframes]
        self.assertGreater(max(xs) - min(xs), 120)

    def test_movement_is_smooth_rather_than_a_lurch(self):
        # The regression: a deadband that engages and then sprints at the
        # slew ceiling produces exactly the jerk the deadband exists to avoid.
        path = solve(self.drifting(), 8.0, ASPECT)
        steps = path_steps(path)
        self.assertTrue(steps)
        ceiling = MAX_PAN_SPEED * path.width
        self.assertLess(max(steps), ceiling * 0.55,
                        "the follower, not the slew limit, should shape motion")

    def test_the_first_move_after_a_hold_starts_gently(self):
        # The specific shape of that regression. The camera sits still while
        # the speaker drifts inside the deadband, so by the time it engages
        # the target is already a deadband-width away. Closing that gap must
        # ease in, not snap.
        path = solve(self.drifting(distance=300.0, duration=10.0), 10.0, ASPECT)
        moves = [
            (a, b) for a, b in zip(path.keyframes, path.keyframes[1:])
            if b.motion is Motion.PAN and a.motion is not Motion.PAN
        ]
        self.assertTrue(moves, "expected the camera to engage at least once")

        for before, after in moves:
            jump = math.hypot(after.x - before.x, after.y - before.y)
            self.assertLess(
                jump, DEADBAND * path.width * 0.5,
                f"engaging moved {jump:.0f}px in one step — that is a lurch",
            )

    def test_pan_speed_never_exceeds_the_slew_limit(self):
        # A detector glitch throwing the box across the frame must not throw
        # the camera with it.
        samples = still_track(duration=3.0)
        samples.append(FaceSample(3.0, Box(1600, 300, 187, 240), 0.95, "host"))
        samples += [
            FaceSample(3.0 + i / 10.0, Box(800, 300, 187, 240), 0.9, "host")
            for i in range(1, 30)
        ]
        path = solve(SpeakerTrack(tuple(samples)), 6.0, ASPECT)
        ceiling = MAX_PAN_SPEED * path.width
        for speed in path_steps(path):
            self.assertLessEqual(speed, ceiling * 1.02)

    def test_keyframes_are_monotonic_in_time(self):
        path = solve(self.drifting(), 8.0, ASPECT)
        times = [k.t for k in path.keyframes]
        self.assertEqual(times, sorted(times))


class TestCuts(unittest.TestCase):
    def two_speakers(self, switch_at: float = 5.0, duration: float = 10.0):
        samples = []
        for index in range(int(duration * 10)):
            t = index / 10.0
            if t < switch_at:
                samples.append(
                    FaceSample(t, Box(400, 300, 187, 240), 0.9, "host"))
            else:
                samples.append(
                    FaceSample(t, Box(1350, 320, 180, 230), 0.9, "guest"))
        return SpeakerTrack(tuple(samples))

    def test_a_speaker_change_cuts_rather_than_pans(self):
        path = solve(self.two_speakers(), 10.0, ASPECT)
        self.assertEqual(len(path.cuts), 1)
        self.assertAlmostEqual(path.cuts[0], 5.0, delta=0.2)

    def test_the_cut_is_instantaneous(self):
        path = solve(self.two_speakers(), 10.0, ASPECT)
        cut = next(k for k in path.keyframes if k.motion is Motion.CUT and k.t > 0)
        previous = max(
            (k for k in path.keyframes if k.t < cut.t), key=lambda k: k.t
        )
        self.assertGreater(abs(cut.x - previous.x), 400)

    def test_rapid_turn_taking_does_not_strobe(self):
        # Two people interrupting each other must not flip the frame on every
        # exchange.
        samples = []
        for index in range(200):
            t = index / 10.0
            speaker = "host" if int(t * 4) % 2 == 0 else "guest"
            box = Box(400 if speaker == "host" else 1350, 300, 187, 240)
            samples.append(FaceSample(t, box, 0.9, speaker))

        path = solve(SpeakerTrack(tuple(samples)), 20.0, ASPECT)
        for first, second in zip(path.cuts, path.cuts[1:]):
            self.assertGreaterEqual(second - first, MIN_SHOT_S - 0.05)

    def test_a_large_jump_by_one_speaker_also_cuts(self):
        samples = still_track(duration=4.0, x=300)
        samples += [
            FaceSample(4.0 + i / 10.0, Box(1500, 300, 187, 240), 0.9, "host")
            for i in range(40)
        ]
        path = solve(SpeakerTrack(tuple(samples)), 8.0, ASPECT)
        self.assertGreaterEqual(len(path.cuts), 1)


class TestGaps(unittest.TestCase):
    def test_camera_holds_through_a_dropout(self):
        # Recentring during a dropout and coming back is the single most
        # obvious artefact an auto-framer can produce.
        samples = [s for s in still_track(duration=8.0, x=1400)
                   if not 3.0 <= s.t < 4.2]
        path = solve(SpeakerTrack(tuple(samples)), 8.0, ASPECT)

        during = path.at(3.6)
        before = path.at(2.9)
        self.assertEqual((during.x, during.y), (before.x, before.y))

    def test_a_long_gap_is_reported(self):
        samples = [s for s in still_track(duration=10.0) if not 3.0 <= s.t < 6.0]
        path = solve(SpeakerTrack(tuple(samples)), 10.0, ASPECT)
        self.assertTrue(any("gap" in note for note in path.notes), path.notes)

    def test_low_confidence_detections_are_ignored(self):
        samples = [
            FaceSample(index / 10.0, Box(300, 300, 187, 240), 0.05, "host")
            for index in range(60)
        ]
        path = solve(SpeakerTrack(tuple(samples)), 6.0, ASPECT)
        self.assertEqual(path.tracking, "static")

    def test_tiny_detections_are_ignored(self):
        samples = [
            FaceSample(index / 10.0, Box(300, 300, 8, 10), 0.95, "host")
            for index in range(60)
        ]
        path = solve(SpeakerTrack(tuple(samples)), 6.0, ASPECT)
        self.assertEqual(path.tracking, "static")


class TestDegradation(unittest.TestCase):
    def test_empty_track_gives_a_centred_static_crop(self):
        path = solve(SpeakerTrack(), 10.0, ASPECT)
        self.assertEqual(path.tracking, "static")
        self.assertEqual(len(path.keyframes), 1)
        self.assertEqual(path.hold_ratio, 1.0)
        self.assertTrue(path.notes)

    def test_always_emits_at_least_one_keyframe(self):
        for track in (SpeakerTrack(), SpeakerTrack(tuple(still_track(0.1)))):
            self.assertGreaterEqual(len(solve(track, 5.0, ASPECT).keyframes), 1)

    def test_samples_are_sorted_on_construction(self):
        out_of_order = (
            FaceSample(2.0, Box(0, 0, 100, 100)),
            FaceSample(0.5, Box(0, 0, 100, 100)),
            FaceSample(1.0, Box(0, 0, 100, 100)),
        )
        track = SpeakerTrack(out_of_order)
        self.assertEqual([s.t for s in track.samples], [0.5, 1.0, 2.0])

    def test_speaker_ids_are_reported_in_order_of_appearance(self):
        track = SpeakerTrack((
            FaceSample(0.0, Box(0, 0, 100, 100), speaker_id="host"),
            FaceSample(1.0, Box(0, 0, 100, 100), speaker_id="guest"),
            FaceSample(2.0, Box(0, 0, 100, 100), speaker_id="host"),
        ))
        self.assertEqual(track.speaker_ids, ("host", "guest"))

    def test_path_at_resolves_the_active_keyframe(self):
        track = SpeakerTrack(tuple(still_track(duration=6.0)))
        path = solve(track, 6.0, ASPECT)
        self.assertEqual(path.at(-1.0), path.keyframes[0])
        self.assertEqual(path.at(999.0), path.keyframes[-1])


if __name__ == "__main__":
    unittest.main()
