"""Face detection, tracking, and the framing it produces.

Three layers, deliberately separated by what they need:

* **Tracker unit tests** feed synthetic detections straight into `FaceTracker`.
  No video, no model, microseconds each. Association, occlusion tolerance and
  the false-positive filter are pure logic and are tested as such.
* **Detector and decode tests** run the real YuNet model over a real
  photograph and real MP4 files.
* **Integration tests** run the whole thing — decode, detect, track, solve —
  over constructed videos whose ground truth is known exactly, and then check
  what the camera actually does with the result.

The fixtures are built once for the module. See `fixtures/faces.py` for what
they contain and `fixtures/README.md` for where the photograph came from.
"""

from __future__ import annotations

import math
import os
import shutil
import tempfile
import unittest

import _support  # noqa: F401  (puts src/ on the path)

from clipforge.gameplay import camera as camera_mod
from clipforge.gameplay.types import FaceSample, Motion, SpeakerTrack
from clipforge.vision import (
    DecodeError,
    Detection,
    FaceDetectionConfig,
    FaceTrackEngine,
    FaceTracker,
    MIN_FACE_FRACTION,
    TrackState,
    activity,
    iou,
    probe_video,
    salience,
    sample_frames,
    select_device,
)
from clipforge.vision.types import Box

FFMPEG = os.environ.get("CLIPFORGE_FFMPEG", "") or shutil.which("ffmpeg") or ""

try:
    import cv2  # noqa: F401

    HAVE_CV2 = True
except ImportError:                                         # pragma: no cover
    HAVE_CV2 = False

needs_cv2 = unittest.skipUnless(
    HAVE_CV2, "face detection needs opencv-python-headless"
)
needs_ffmpeg = unittest.skipUnless(
    FFMPEG, "video fixtures need ffmpeg — set CLIPFORGE_FFMPEG"
)

_TMP: str = ""
_CACHE = None


def setUpModule() -> None:
    """Build the video fixtures once. Each is a few seconds of encoding."""
    global _TMP, _CACHE
    if not (HAVE_CV2 and FFMPEG):
        return
    from fixtures.faces import FixtureCache

    _TMP = tempfile.mkdtemp(prefix="clipforge-vision-")
    _CACHE = FixtureCache(_TMP)


def tearDownModule() -> None:
    if _TMP:
        shutil.rmtree(_TMP, ignore_errors=True)


def fixture(name: str):
    assert _CACHE is not None
    return _CACHE.get(name)


def box(x: float, y: float, w: float = 100.0, h: float = 120.0) -> Box:
    return Box(x, y, w, h)


def detection(x: float, y: float, w: float = 100.0, h: float = 120.0,
              confidence: float = 0.9) -> Detection:
    return Detection(box=box(x, y, w, h), confidence=confidence)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


class GeometryTest(unittest.TestCase):
    def test_iou_of_identical_boxes_is_one(self) -> None:
        self.assertAlmostEqual(iou(box(0, 0), box(0, 0)), 1.0)

    def test_disjoint_boxes_score_zero(self) -> None:
        self.assertEqual(iou(box(0, 0), box(500, 500)), 0.0)

    def test_touching_edges_do_not_overlap(self) -> None:
        # Shared edge, zero area. A `>=` in the intersection test makes this
        # 0.0 anyway but by accident; the assertion pins the intent.
        self.assertEqual(iou(box(0, 0, 100, 100), box(100, 0, 100, 100)), 0.0)

    def test_half_overlap(self) -> None:
        # Two 100x100 boxes offset by 50 in x: intersection 50x100 = 5000,
        # union 20000 - 5000 = 15000.
        self.assertAlmostEqual(
            iou(box(0, 0, 100, 100), box(50, 0, 100, 100)), 5000 / 15000
        )


# ---------------------------------------------------------------------------
# The tracker, on synthetic detections
# ---------------------------------------------------------------------------


class TrackerTest(unittest.TestCase):
    """Association logic, with no model and no video in sight."""

    def setUp(self) -> None:
        self.config = FaceDetectionConfig(min_hits=2, max_age=12)

    def feed(self, tracker: FaceTracker, frames, dt: float = 0.1) -> None:
        for index, detections in enumerate(frames):
            tracker.update(index * dt, detections, frame_size=(1280, 720))

    def test_a_still_face_is_one_track(self) -> None:
        tracker = FaceTracker(self.config)
        self.feed(tracker, [[detection(100, 100)] for _ in range(20)])
        tracks = tracker.finish()
        self.assertEqual(len(tracks), 1)
        self.assertEqual(len(tracks[0].observations), 20)

    def test_a_moving_face_stays_one_track(self) -> None:
        """The box moves further than its own width across the clip."""
        tracker = FaceTracker(self.config)
        self.feed(tracker, [[detection(100 + i * 22, 100)] for i in range(20)])
        tracks = tracker.finish()
        self.assertEqual(len(tracks), 1, "a walking person split into two ids")

    def test_a_jump_beyond_the_gate_starts_a_new_track(self) -> None:
        tracker = FaceTracker(self.config)
        self.feed(
            tracker,
            [[detection(100, 100)] for _ in range(5)]
            + [[detection(1000, 500)] for _ in range(5)],
        )
        tracks = tracker.finish()
        self.assertEqual(len(tracks), 2)

    def test_two_faces_get_two_ids(self) -> None:
        tracker = FaceTracker(self.config)
        self.feed(
            tracker,
            [[detection(200, 300), detection(900, 300)] for _ in range(15)],
        )
        tracks = tracker.finish()
        self.assertEqual(len(tracks), 2)
        self.assertEqual(
            {t.track_id for t in tracks}, {"spk_1", "spk_2"}
        )

    def test_two_faces_do_not_swap_identities(self) -> None:
        """The left track must stay on the left face for its whole life."""
        tracker = FaceTracker(self.config)
        frames = [
            [detection(200 + i, 300), detection(900 - i, 300)]
            for i in range(15)
        ]
        self.feed(tracker, frames)
        tracks = {t.track_id: t for t in tracker.finish()}
        for track in tracks.values():
            xs = [d.box.x for _t, d in track.observations]
            drift = max(xs) - min(xs)
            self.assertLess(
                drift, 100,
                f"{track.track_id} jumped between the two faces",
            )

    def test_a_single_frame_false_positive_is_dropped(self) -> None:
        """One spurious box must not become a speaker the camera cuts to."""
        tracker = FaceTracker(self.config)
        frames: list[list[Detection]] = [
            [detection(100, 100)] for _ in range(12)
        ]
        frames[6] = [detection(100, 100), detection(1100, 620)]
        self.feed(tracker, frames)
        tracks = tracker.finish()
        self.assertEqual(len(tracks), 1, "a one-frame blip became a speaker")

    def test_a_repeated_detection_is_believed(self) -> None:
        """The same filter must not discard a person who really arrives."""
        tracker = FaceTracker(self.config)
        frames: list[list[Detection]] = [
            [detection(100, 100)] for _ in range(12)
        ]
        for index in range(6, 12):
            frames[index] = [detection(100, 100), detection(1100, 620)]
        self.feed(tracker, frames)
        self.assertEqual(len(tracker.finish()), 2)

    def test_confirmation_emits_the_buffered_history(self) -> None:
        """A confirmed track keeps the samples from before it was believed.

        Otherwise every entrance starts `min_hits` frames late, which is
        exactly when framing matters most.
        """

        tracker = FaceTracker(self.config)
        self.feed(tracker, [[detection(100, 100)] for _ in range(4)])
        track = tracker.finish()[0]
        self.assertEqual(len(track.observations), 4)
        self.assertAlmostEqual(track.observations[0][0], 0.0)

    def test_a_track_survives_an_occlusion_and_keeps_its_id(self) -> None:
        tracker = FaceTracker(self.config)
        frames: list[list[Detection]] = []
        frames += [[detection(400, 300)] for _ in range(10)]
        frames += [[] for _ in range(8)]            # 0.8s hidden
        frames += [[detection(400, 300)] for _ in range(10)]
        self.feed(tracker, frames)
        tracks = tracker.finish()
        self.assertEqual(len(tracks), 1, "the person came back as a new id")
        self.assertEqual(tracks[0].track_id, "spk_1")

    def test_nothing_is_emitted_during_the_occlusion(self) -> None:
        """The tracker must not invent positions for a face it cannot see.

        `camera.solve` holds the shot through a gap, which is better than any
        interpolation available here. Filling the gap in would make the camera
        follow a guess and then snap back.
        """

        tracker = FaceTracker(self.config)
        frames: list[list[Detection]] = []
        frames += [[detection(400, 300)] for _ in range(10)]
        frames += [[] for _ in range(8)]
        frames += [[detection(400, 300)] for _ in range(10)]
        self.feed(tracker, frames)
        times = [round(t, 2) for t, _d in tracker.finish()[0].observations]
        hidden = [round(i * 0.1, 2) for i in range(10, 18)]
        self.assertFalse(
            set(times) & set(hidden),
            "the tracker emitted samples for frames with no detection",
        )

    def test_an_occlusion_longer_than_max_age_ends_the_track(self) -> None:
        tracker = FaceTracker(FaceDetectionConfig(min_hits=2, max_age=5))
        frames: list[list[Detection]] = []
        frames += [[detection(400, 300)] for _ in range(8)]
        frames += [[] for _ in range(12)]
        frames += [[detection(400, 300)] for _ in range(8)]
        self.feed(tracker, frames)
        self.assertEqual(len(tracker.finish()), 2)

    def test_returning_from_an_occlusion_does_not_spike_activity(self) -> None:
        """The mouth memory is dropped when a track is lost.

        Otherwise the first sample back differences a pre-occlusion mouth
        against a post-occlusion one, scores that as speech, and hands the
        camera its strongest reason to cut to the person who was hidden.
        """

        import numpy as np

        from clipforge.vision.types import Landmarks

        def with_mouth(shift: float) -> Detection:
            return Detection(
                box=box(400, 300, 120, 150),
                confidence=0.9,
                landmarks=Landmarks(
                    right_eye=(430.0, 350.0), left_eye=(470.0, 350.0),
                    nose=(450.0, 380.0),
                    right_mouth=(432.0, 400.0 + shift),
                    left_mouth=(468.0, 400.0 + shift),
                ),
            )

        rng = np.random.default_rng(3)
        tracker = FaceTracker(self.config)
        schedule = [1] * 6 + [0] * 5 + [1] * 6

        for index, present in enumerate(schedule):
            # A fresh noise frame each time, so a retained mouth memory would
            # produce a large, obvious difference.
            image = rng.integers(0, 255, (720, 1280, 3), dtype=np.uint8)
            dets = [with_mouth(index * 0.5)] if present else []
            tracker.update(index * 0.1, dets, image=image,
                           frame_size=(1280, 720))

        track = tracker.finish()[0]
        self.assertEqual(len(track.observations), 12)
        self.assertGreater(
            max(track.activity[1:6]), 0.0,
            "the fixture produced no activity at all; nothing is being measured",
        )
        self.assertEqual(
            track.activity[6], 0.0,
            "the first sample after the occlusion scored activity from a "
            "mouth it had not seen since before the gap",
        )

    def test_stats_count_frames_not_detections(self) -> None:
        tracker = FaceTracker(self.config)
        self.feed(
            tracker,
            [[detection(200, 300), detection(900, 300)]] * 4 + [[]] * 3,
        )
        frames, with_face, simultaneous = tracker.stats
        self.assertEqual((frames, with_face, simultaneous), (7, 4, 2))

    def test_summary_gap_excludes_the_sampling_interval(self) -> None:
        """A track seen in every frame has no gap, not a one-interval gap."""
        tracker = FaceTracker(self.config)
        self.feed(tracker, [[detection(400, 300)] for _ in range(10)])
        tracks = tracker.finish()
        summary = tracker.summaries(tracks, sample_interval_s=0.1)[0]
        self.assertAlmostEqual(summary.longest_gap_s, 0.0, places=6)


class SalienceTest(unittest.TestCase):
    def test_a_silent_face_stays_well_above_the_camera_floor(self) -> None:
        """Salience must never make a real face invisible to the camera."""
        quiet = salience(activity_score=0.0, size=0.0, centre=0.0)
        self.assertGreater(quiet, camera_mod.MIN_CONFIDENCE)

    def test_a_talking_face_outranks_a_silent_one(self) -> None:
        talking = salience(activity_score=0.9, size=0.5, centre=0.5)
        silent = salience(activity_score=0.0, size=0.5, centre=0.5)
        self.assertGreater(talking, silent)

    def test_salience_is_bounded(self) -> None:
        self.assertLessEqual(salience(2.0, 2.0, 2.0), 1.0)
        self.assertGreaterEqual(salience(-1.0, -1.0, -1.0), 0.0)

    def test_activity_of_a_still_patch_is_zero(self) -> None:
        import numpy as np

        patch = np.full((18, 24), 120, dtype=np.uint8)
        self.assertEqual(activity(patch, patch.copy()), 0.0)

    def test_activity_rises_with_change(self) -> None:
        import numpy as np

        a = np.full((18, 24), 100, dtype=np.uint8)
        b = np.full((18, 24), 140, dtype=np.uint8)
        self.assertGreater(activity(a, b), 0.5)

    def test_activity_without_a_previous_patch_is_zero(self) -> None:
        import numpy as np

        self.assertEqual(activity(None, np.zeros((18, 24), dtype="uint8")), 0.0)


# ---------------------------------------------------------------------------
# Device selection
# ---------------------------------------------------------------------------


@needs_cv2
class DeviceTest(unittest.TestCase):
    def test_cpu_is_always_available(self) -> None:
        _backend, _target, device, note = select_device("cpu")
        self.assertEqual(device, "cpu")
        self.assertEqual(note, "")

    def test_auto_resolves_to_something_real(self) -> None:
        _backend, _target, device, _note = select_device("auto")
        self.assertIn(device, ("cpu", "cuda"))

    def test_an_impossible_gpu_request_degrades_rather_than_raises(self) -> None:
        """A CUDA request on a CPU box must not take the deployment down."""
        _backend, _target, device, note = select_device("cuda")
        if device == "cpu":
            self.assertTrue(note, "the downgrade was silent")
            self.assertIn("cuda", note.lower())
        else:                                               # pragma: no cover
            self.assertEqual(device, "cuda")

    def test_auto_does_not_complain_about_what_it_did_not_ask_for(self) -> None:
        _backend, _target, device, note = select_device("auto")
        if device == "cpu":
            self.assertEqual(note, "")

    def test_opencv_5_warns_that_a_gpu_target_may_be_ignored(self) -> None:
        """OpenCV 5's DNN graph engine does not honour non-CPU targets yet.

        It logs that to stderr and runs on CPU. A `device` of "cuda" on
        OpenCV 5 therefore describes what was selected, not necessarily what
        executed — so the caveat has to travel with the result rather than
        sit in a log line nobody correlates.
        """

        major = int(cv2.__version__.split(".")[0])
        _backend, _target, device, note = select_device("cuda")
        if device == "cuda" and major >= 5:
            self.assertIn("graph engine", note)


# ---------------------------------------------------------------------------
# The detector, against a real photograph
# ---------------------------------------------------------------------------


@needs_cv2
class DetectorTest(unittest.TestCase):
    """Runs the shipped YuNet weights over the bundled NASA photograph."""

    @classmethod
    def setUpClass(cls) -> None:
        from clipforge.vision.yunet import YuNetDetector

        cls.detector = YuNetDetector()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.detector.close()

    def photo(self):
        from fixtures.faces import REAL_FACE

        image = cv2.imread(str(REAL_FACE))
        self.assertIsNotNone(image, "the bundled face fixture is missing")
        return image

    def test_the_bundled_model_loads(self) -> None:
        available = self.detector.availability()
        self.assertTrue(available.ready, available.detail)

    def test_it_finds_a_real_human_face(self) -> None:
        found = self.detector.detect(self.photo())
        self.assertEqual(len(found), 1)
        self.assertGreater(found[0].confidence, 0.8)

    def test_it_reports_five_landmarks_on_the_face(self) -> None:
        marks = self.detector.detect(self.photo())[0].landmarks
        self.assertIsNotNone(marks)
        assert marks is not None
        # Eyes above the mouth, nose between them. Weak assertions on purpose:
        # the point is that the points are real and ordered as documented,
        # not that they land on a particular pixel.
        self.assertLess(marks.eye_centre[1], marks.nose[1])
        self.assertLess(marks.nose[1], marks.mouth_centre[1])
        self.assertGreater(marks.eye_distance, 5.0)

    def test_a_blank_frame_yields_nothing(self) -> None:
        import numpy as np

        blank = np.full((480, 640, 3), 128, dtype=np.uint8)
        self.assertEqual(self.detector.detect(blank), ())

    def test_boxes_come_back_in_source_pixels_after_a_downscale(self) -> None:
        """The downscale must be invisible to the caller.

        A track in detector pixels would be silently wrong by a factor of
        three on 1080p and right on small fixtures — the exact bug a suite
        built on small videos never catches.
        """

        photo = self.photo()
        small = self.detector.detect(photo)
        self.assertTrue(small)

        big = cv2.resize(photo, (photo.shape[1] * 4, photo.shape[0] * 4))
        self.assertGreater(max(big.shape[:2]), self.detector.config.max_side)
        found = self.detector.detect(big)
        self.assertTrue(found)

        ratio = found[0].box.width / small[0].box.width
        self.assertAlmostEqual(ratio, 4.0, delta=0.35)

    def test_boxes_stay_inside_the_frame(self) -> None:
        photo = self.photo()
        height, width = photo.shape[:2]
        for found in self.detector.detect(photo):
            self.assertGreaterEqual(found.box.x, 0.0)
            self.assertGreaterEqual(found.box.y, 0.0)
            self.assertLessEqual(found.box.x + found.box.width, width + 1)
            self.assertLessEqual(found.box.y + found.box.height, height + 1)

    def test_a_greyscale_frame_is_refused_rather_than_misread(self) -> None:
        import numpy as np

        with self.assertRaises(ValueError):
            self.detector.detect(np.zeros((100, 100), dtype=np.uint8))

    def test_info_names_the_device_that_actually_ran(self) -> None:
        self.detector.availability()
        info = self.detector.info
        self.assertIn(info.device, ("cpu", "cuda", "opencl"))
        self.assertTrue(info.model.endswith(".onnx"))


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


@needs_cv2
@needs_ffmpeg
class DecodeTest(unittest.TestCase):
    def test_probe_reports_the_real_geometry(self) -> None:
        path, scenario = fixture("single_speaker")
        info = probe_video(path)
        self.assertEqual((info.width, info.height), scenario.size)
        self.assertAlmostEqual(info.fps, scenario.fps, delta=0.5)
        self.assertAlmostEqual(info.duration_s, scenario.duration_s, delta=0.2)

    def test_sampling_hits_the_requested_rate(self) -> None:
        path, scenario = fixture("single_speaker")
        frames = list(sample_frames(path, sample_fps=10.0))
        self.assertAlmostEqual(
            len(frames), scenario.duration_s * 10, delta=2,
        )

    def test_sample_times_are_ordered_and_evenly_spaced(self) -> None:
        path, _ = fixture("single_speaker")
        times = [f.t for f in sample_frames(path, sample_fps=10.0)]
        self.assertEqual(times, sorted(times))
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(all(abs(g - 0.1) < 0.02 for g in gaps), gaps[:5])

    def test_a_window_returns_only_that_window(self) -> None:
        path, _ = fixture("occlusion")
        frames = list(sample_frames(path, sample_fps=10.0, start_s=2.0,
                                    duration_s=2.0))
        self.assertTrue(frames)
        self.assertGreaterEqual(min(f.t for f in frames), 1.95)
        self.assertLessEqual(max(f.t for f in frames), 4.05)

    def test_max_frames_caps_the_work(self) -> None:
        path, _ = fixture("single_speaker")
        frames = list(sample_frames(path, sample_fps=10.0, max_frames=7))
        self.assertEqual(len(frames), 7)

    def test_frames_are_bgr_at_source_size(self) -> None:
        path, scenario = fixture("single_speaker")
        frame = next(iter(sample_frames(path, sample_fps=2.0)))
        self.assertEqual(frame.image.shape, (scenario.size[1], scenario.size[0], 3))

    def test_a_missing_file_is_a_decode_error(self) -> None:
        with self.assertRaises(DecodeError):
            probe_video(os.path.join(_TMP, "nope.mp4"))

    def test_a_non_video_is_a_decode_error(self) -> None:
        path = os.path.join(_TMP, "not-a-video.mp4")
        with open(path, "wb") as handle:
            handle.write(b"this is not an mp4")
        with self.assertRaises(DecodeError):
            probe_video(path)

    def test_an_empty_file_is_a_decode_error(self) -> None:
        path = os.path.join(_TMP, "empty.mp4")
        open(path, "wb").close()
        with self.assertRaises(DecodeError):
            probe_video(path)


# ---------------------------------------------------------------------------
# End to end over real video
# ---------------------------------------------------------------------------


@needs_cv2
@needs_ffmpeg
class FaceTrackEngineTest(unittest.TestCase):
    """Decode, detect, track — over videos whose truth is known."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = FaceTrackEngine()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.close()

    def track(self, name: str):
        path, scenario = fixture(name)
        return self.engine.track_video(path), scenario

    # -- the base case -----------------------------------------------------

    def test_a_single_speaker_produces_one_track(self) -> None:
        result, _ = self.track("single_speaker")
        self.assertTrue(result.ok, result.fallback)
        self.assertEqual(len(result.people), 1)

    def test_the_speaker_is_found_in_nearly_every_frame(self) -> None:
        result, _ = self.track("single_speaker")
        self.assertGreater(result.detection_rate, 0.9)

    def test_the_track_carries_the_real_source_size(self) -> None:
        """The whole reason the renderer's preflight exists.

        `SpeakerTrack` defaults to 1920x1080. These fixtures are 1280x720 on
        purpose, so a track that never read the file cannot pass this.
        """

        result, scenario = self.track("single_speaker")
        self.assertEqual(
            (result.track.source_width, result.track.source_height),
            scenario.size,
        )

    def test_detected_boxes_land_where_the_face_was_drawn(self) -> None:
        result, scenario = self.track("single_speaker")
        errors = []
        for sample in result.track.samples:
            truth = scenario.expected(sample.t)
            if not truth:
                continue
            cx, cy = next(iter(truth.values()))
            errors.append(math.hypot(cx - sample.box.cx, cy - sample.box.cy))
        self.assertTrue(errors)
        errors.sort()
        median = errors[len(errors) // 2]
        p90 = errors[int(len(errors) * 0.9)]
        # Measured at 6px median / 9px p90 on a 1280x720 frame with a ~210px
        # face. The thresholds are loose enough to survive a codec change and
        # tight enough that a coordinate-space bug cannot pass.
        self.assertLess(median, 15.0, f"median error {median:.1f}px")
        self.assertLess(p90, 25.0, f"p90 error {p90:.1f}px")

    def test_samples_are_time_ordered(self) -> None:
        result, _ = self.track("single_speaker")
        times = [s.t for s in result.track.samples]
        self.assertEqual(times, sorted(times))

    def test_every_sample_clears_the_cameras_confidence_floor(self) -> None:
        result, _ = self.track("single_speaker")
        lowest = min(s.confidence for s in result.track.samples)
        self.assertGreater(lowest, camera_mod.MIN_CONFIDENCE)

    # -- multiple speakers -------------------------------------------------

    def test_two_people_produce_two_speaker_ids(self) -> None:
        result, _ = self.track("two_speakers")
        self.assertEqual(len(result.people), 2, result.to_dict())
        self.assertEqual(result.max_simultaneous, 2)

    def test_both_speakers_are_tracked_for_the_whole_clip(self) -> None:
        result, scenario = self.track("two_speakers")
        for person in result.people:
            self.assertLess(person.first_t, 0.5)
            self.assertGreater(person.last_t, scenario.duration_s - 0.5)

    def test_the_two_tracks_stay_on_their_own_side(self) -> None:
        result, _ = self.track("two_speakers")
        sides: dict[str, list[float]] = {}
        for sample in result.track.samples:
            sides.setdefault(sample.speaker_id, []).append(sample.box.cx)
        self.assertEqual(len(sides), 2)
        for speaker, xs in sides.items():
            self.assertLess(
                max(xs) - min(xs), 120,
                f"{speaker} wandered between the two faces",
            )

    def test_the_talking_speaker_scores_highest_in_their_own_window(self) -> None:
        """The active-speaker signal, measured against the fixture's script.

        `left` talks 0.3-3.4s and 6.2-8.0s; `right` talks 3.8-6.0s. The gaps
        between those windows are excluded because nobody is talking in them
        and there is therefore no right answer.
        """

        result, _ = self.track("two_speakers")
        by_time: dict[float, dict[str, FaceSample]] = {}
        for sample in result.track.samples:
            by_time.setdefault(round(sample.t, 3), {})[sample.speaker_id] = sample

        # Identify the tracks by position rather than by id, so the assertion
        # does not depend on which one the tracker numbered first.
        centres: dict[str, float] = {}
        for sample in result.track.samples:
            centres.setdefault(sample.speaker_id, sample.box.cx)
        left = min(centres, key=lambda k: centres[k])
        right = max(centres, key=lambda k: centres[k])

        checked = wrong = 0
        for t, samples in sorted(by_time.items()):
            if len(samples) < 2:
                continue
            if 0.6 <= t < 3.2:
                expected = left
            elif 4.1 <= t < 5.8:
                expected = right
            elif 6.5 <= t < 7.8:
                expected = left
            else:
                continue
            checked += 1
            winner = max(samples, key=lambda k: samples[k].confidence)
            if winner != expected:
                wrong += 1

        self.assertGreater(checked, 30, "not enough two-face frames to judge")
        self.assertEqual(
            wrong, 0,
            f"{wrong}/{checked} frames picked the silent speaker",
        )

    # -- entering and leaving ---------------------------------------------

    def test_nothing_is_tracked_before_the_face_arrives(self) -> None:
        result, _ = self.track("enter_exit")
        self.assertTrue(result.ok, result.fallback)
        earliest = min(s.t for s in result.track.samples)
        # The walker is off-canvas until about 1.9s.
        self.assertGreater(earliest, 1.3)

    def test_nothing_is_tracked_after_the_face_leaves(self) -> None:
        result, scenario = self.track("enter_exit")
        latest = max(s.t for s in result.track.samples)
        self.assertLess(latest, scenario.duration_s - 0.8)

    def test_a_face_crossing_the_frame_stays_one_person(self) -> None:
        """It travels most of the frame width — the case IoU alone fails."""
        result, _ = self.track("enter_exit")
        self.assertEqual(len(result.people), 1, result.to_dict())
        xs = [s.box.cx for s in result.track.samples]
        self.assertGreater(max(xs) - min(xs), 400, "the walker barely moved")

    # -- occlusion ---------------------------------------------------------

    def test_an_occluded_face_keeps_its_identity(self) -> None:
        """A pillar crosses the face for about a second.

        If the track dies and restarts, the camera sees a new `speaker_id`,
        reads it as a change of subject and cuts — a visible artefact caused
        by something that merely walked in front of the lens.
        """

        result, _ = self.track("occlusion")
        self.assertTrue(result.ok, result.fallback)
        self.assertEqual(len(result.people), 1, result.to_dict())

    def test_the_occlusion_shows_up_as_a_gap_not_as_invented_samples(self) -> None:
        result, _ = self.track("occlusion")
        person = result.people[0]
        self.assertGreater(
            person.longest_gap_s, 0.25,
            "the pillar left no gap — the fixture is not occluding",
        )
        self.assertLess(result.detection_rate, 1.0)

    def test_the_track_resumes_after_the_occlusion(self) -> None:
        result, scenario = self.track("occlusion")
        latest = max(s.t for s in result.track.samples)
        self.assertGreater(latest, scenario.duration_s - 1.0)

    # -- fallbacks ---------------------------------------------------------

    def test_a_video_with_no_faces_falls_back_rather_than_failing(self) -> None:
        result, _ = self.track("no_faces")
        self.assertFalse(result.ok)
        self.assertEqual(result.track.samples, ())
        self.assertIn("no faces", result.fallback)

    def test_the_no_face_fallback_still_carries_real_dimensions(self) -> None:
        """The fallback has to be better than `SpeakerTrack()`, not equal to it."""
        result, scenario = self.track("no_faces")
        self.assertEqual(
            (result.track.source_width, result.track.source_height),
            scenario.size,
        )

    def test_no_false_positives_on_faceless_content(self) -> None:
        result, _ = self.track("no_faces")
        self.assertEqual(result.frames_with_face, 0)
        self.assertGreater(result.frames_sampled, 10)

    def test_a_face_below_the_size_floor_is_not_detected(self) -> None:
        """A documented limit, pinned so a change to it is visible here."""
        result, _ = self.track("small_face")
        self.assertFalse(result.ok)
        self.assertIn(f"{MIN_FACE_FRACTION:.1%}", result.fallback)

    def test_a_missing_video_raises_rather_than_falling_back(self) -> None:
        """A file that cannot be decoded is not going to render either."""
        with self.assertRaises(DecodeError):
            self.engine.track_video(os.path.join(_TMP, "absent.mp4"))

    def test_an_unavailable_detector_degrades_to_a_static_track(self) -> None:
        class Broken:
            info = None

            def availability(self):
                from clipforge.vision.types import Availability

                return Availability(False, "no model on this host")

            def detect(self, frame):               # pragma: no cover
                raise AssertionError("should not be reached")

            def close(self):
                pass

        path, scenario = fixture("single_speaker")
        engine = FaceTrackEngine(detector=Broken())
        result = engine.track_video(path)
        self.assertFalse(result.ok)
        self.assertIn("no model on this host", result.fallback)
        # Still useful: the geometry came from the file, not from a default.
        self.assertEqual(
            (result.track.source_width, result.track.source_height),
            scenario.size,
        )

    def test_a_detector_that_throws_degrades_rather_than_losing_the_clip(self) -> None:
        class Exploding:
            info = None

            def availability(self):
                from clipforge.vision.types import Availability

                return Availability(True, "fine")

            def detect(self, frame):
                raise RuntimeError("CUDA out of memory")

            def close(self):
                pass

        path, _ = fixture("single_speaker")
        with self.assertLogs("clipforge.vision", level="ERROR"):
            result = FaceTrackEngine(detector=Exploding()).track_video(path)
        self.assertFalse(result.ok)
        self.assertIn("CUDA out of memory", result.fallback)

    # -- windowing ---------------------------------------------------------

    def test_a_window_is_timed_from_the_start_of_the_window(self) -> None:
        """The camera solves from zero, so a clip's track must start at zero.

        A track timed from the start of a two-hour source would put every
        sample outside the camera's window and produce a static crop.
        """

        path, _ = fixture("occlusion")
        result = self.engine.track_video(path, start_s=1.0, duration_s=2.0)
        self.assertTrue(result.ok, result.fallback)
        self.assertLess(min(s.t for s in result.track.samples), 0.4)
        self.assertLess(max(s.t for s in result.track.samples), 2.1)

    def test_the_summaries_use_the_same_clock_as_the_samples(self) -> None:
        """One object must not carry two timelines.

        The tracker works in the source file's time because that is what the
        decoder gives it; everything leaving here is clip-relative. Shipping
        `samples[0].t == 0.0` beside `people[0].first_t == 612.3` would get a
        bug filed against the tracker by the first person to plot them.
        """

        path, _ = fixture("occlusion")
        result = self.engine.track_video(path, start_s=1.5, duration_s=2.5)
        self.assertTrue(result.ok, result.fallback)
        earliest_sample = min(s.t for s in result.track.samples)
        earliest_person = min(p.first_t for p in result.people)
        self.assertAlmostEqual(earliest_person, earliest_sample, delta=0.25)
        for person in result.people:
            self.assertLessEqual(person.last_t, 2.6)

    def test_max_frames_bounds_the_work(self) -> None:
        path, _ = fixture("single_speaker")
        result = self.engine.track_video(path, max_frames=5)
        self.assertLessEqual(result.frames_sampled, 5)

    def test_the_result_serialises(self) -> None:
        result, _ = self.track("two_speakers")
        payload = result.to_dict()
        self.assertEqual(len(payload["people"]), 2)
        self.assertEqual(payload["source"]["width"], 1280)
        self.assertIsNotNone(payload["detector"])


# ---------------------------------------------------------------------------
# What the camera does with it
# ---------------------------------------------------------------------------


@needs_cv2
@needs_ffmpeg
class CameraIntegrationTest(unittest.TestCase):
    """The point of all of it: framing that follows the speaker."""

    #: A 9:16 speaker panel filling the canvas, the common case.
    ASPECT = 1080 / 1920

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = FaceTrackEngine()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.close()

    def solve(self, name: str):
        path, scenario = fixture(name)
        result = self.engine.track_video(path)
        path_ = camera_mod.solve(
            result.track, scenario.duration_s, self.ASPECT, fps=30,
        )
        return result, scenario, path_

    def test_a_real_track_produces_a_tracked_camera_not_a_static_one(self) -> None:
        """Before this module existed, every clip took the static branch."""
        _result, _scenario, solved = self.solve("single_speaker")
        self.assertEqual(solved.tracking, "tracked")

    def test_the_crop_never_leaves_the_source_frame(self) -> None:
        _result, scenario, solved = self.solve("single_speaker")
        width, height = scenario.size
        for keyframe in solved.keyframes:
            self.assertGreaterEqual(keyframe.x, 0)
            self.assertGreaterEqual(keyframe.y, 0)
            self.assertLessEqual(keyframe.x + solved.width, width)
            self.assertLessEqual(keyframe.y + solved.height, height)

    def test_the_camera_holds_still_when_the_speaker_does(self) -> None:
        """A camera that is always moving is a camera nobody asked for.

        Asserted on the two-shot, where both faces are stationary, because
        that is the case where holding is unambiguously correct. Measured at
        0.95.
        """

        _result, _scenario, solved = self.solve("two_speakers")
        self.assertGreater(solved.hold_ratio, 0.7)

    def test_the_camera_absorbs_sway_rather_than_chasing_it(self) -> None:
        """The single-speaker fixture sways further than the deadband.

        Its `hold_ratio` is only 0.41 as a result, and that is correct — the
        subject really is moving, and a camera that ignored it would be
        broken rather than steady. What must still hold is that the camera
        travels much less far than the detected face does: the deadband and
        the 1€ filter exist to absorb motion, not to reproduce it.

        Measured: the face centre travels 156px, the crop travels 70px.
        """

        result, scenario, solved = self.solve("single_speaker")
        samples = sorted(result.track.samples, key=lambda s: s.t)
        raw = sum(
            abs(b.box.cx - a.box.cx) + abs(b.box.cy - a.box.cy)
            for a, b in zip(samples, samples[1:])
        )
        camera_travel = sum(
            abs(b.x - a.x) + abs(b.y - a.y)
            for a, b in zip(solved.keyframes, solved.keyframes[1:])
        )
        self.assertGreater(raw, 50, "the fixture barely moved; nothing to absorb")
        self.assertLess(
            camera_travel / raw, 0.6,
            f"the camera followed {camera_travel:.0f}px of {raw:.0f}px — "
            f"it is chasing the detector rather than smoothing it",
        )

    def test_movement_between_keyframes_is_bounded(self) -> None:
        """No lurches: consecutive pans stay under the slew ceiling."""
        _result, _scenario, solved = self.solve("single_speaker")
        limit = camera_mod.MAX_PAN_SPEED * solved.width
        worst = 0.0
        for before, after in zip(solved.keyframes, solved.keyframes[1:]):
            if after.motion is Motion.CUT:
                continue
            dt = after.t - before.t
            if dt <= 0:
                continue
            speed = math.hypot(after.x - before.x, after.y - before.y) / dt
            worst = max(worst, speed)
        self.assertLessEqual(worst, limit * 1.05, f"{worst:.0f}px/s")

    def test_a_still_speaker_provokes_no_cuts(self) -> None:
        _result, _scenario, solved = self.solve("single_speaker")
        self.assertEqual(len(solved.cuts), 0, solved.cuts)

    def test_the_camera_follows_a_walking_speaker(self) -> None:
        _result, _scenario, solved = self.solve("enter_exit")
        xs = [k.x for k in solved.keyframes]
        self.assertGreater(
            max(xs) - min(xs), 200,
            "the crop did not move while the speaker crossed the frame",
        )

    def test_two_speakers_make_the_camera_cut_between_them(self) -> None:
        _result, _scenario, solved = self.solve("two_speakers")
        self.assertGreater(len(solved.cuts), 0, "never changed subject")

    def test_cuts_respect_the_minimum_shot_length(self) -> None:
        """Two people in conversation must not strobe the frame."""
        _result, _scenario, solved = self.solve("two_speakers")
        for before, after in zip(solved.cuts, solved.cuts[1:]):
            self.assertGreaterEqual(
                after - before, camera_mod.MIN_SHOT_S - 1e-6,
            )

    def test_an_occlusion_does_not_make_the_camera_cut(self) -> None:
        """The tracker's whole job on this fixture, seen from the output."""
        _result, _scenario, solved = self.solve("occlusion")
        self.assertEqual(
            len(solved.cuts), 0,
            "a pillar walking past caused a change of subject",
        )

    def test_the_camera_holds_position_through_the_occlusion(self) -> None:
        """While the camera has no observation at all, it must not move.

        The window is derived from the track rather than hardcoded, and it is
        narrower than the detection gap by `MATCH_TOLERANCE_S` at each end —
        the camera legitimately keeps using a detection up to 0.2s either
        side, which is what stops it stuttering at every sample boundary. The
        interesting window is the part in the middle where it has nothing.
        """

        result, _scenario, solved = self.solve("occlusion")
        times = sorted(s.t for s in result.track.samples)
        gaps = [
            (a, b) for a, b in zip(times, times[1:])
            if b - a > 2.0 * camera_mod.MATCH_TOLERANCE_S
        ]
        self.assertTrue(gaps, "the pillar produced no detection gap")

        for start, end in gaps:
            blind_from = start + camera_mod.MATCH_TOLERANCE_S
            blind_to = end - camera_mod.MATCH_TOLERANCE_S
            moving = [
                k for k in solved.keyframes
                if blind_from < k.t < blind_to and k.motion is not Motion.HOLD
            ]
            self.assertEqual(
                moving, [],
                f"the camera moved between {blind_from:.2f}s and "
                f"{blind_to:.2f}s with nothing to follow: "
                f"{[k.to_dict() for k in moving]}",
            )
            # And the position in force across the blind window is unchanged.
            self.assertEqual(
                solved.at(blind_from).x, solved.at(blind_to).x,
            )

    def test_a_faceless_source_gets_a_static_crop_that_fits(self) -> None:
        """The regression the empty `SpeakerTrack()` used to cause.

        A bare `SpeakerTrack()` claims 1920x1080, so its crop is solved
        against a frame that does not exist and the renderer rejects the plan.
        The real fallback track carries 1280x720 and fits.
        """

        _result, scenario, solved = self.solve("no_faces")
        self.assertEqual(solved.tracking, "static")
        width, height = scenario.size
        self.assertLessEqual(solved.width, width)
        self.assertLessEqual(solved.height, height)
        for keyframe in solved.keyframes:
            self.assertLessEqual(keyframe.x + solved.width, width)
            self.assertLessEqual(keyframe.y + solved.height, height)

    def test_the_old_placeholder_would_not_have_fitted(self) -> None:
        """Proves the previous line is testing something real.

        This is the bug as it was: an empty default-sized track against
        1280x720 media asks for a crop taller than the frame.
        """

        _path, scenario = fixture("no_faces")
        placeholder = camera_mod.solve(
            SpeakerTrack(), scenario.duration_s, self.ASPECT, fps=30,
        )
        self.assertGreater(placeholder.height, scenario.size[1])


# ---------------------------------------------------------------------------
# Through the composer and the factory
# ---------------------------------------------------------------------------


@needs_cv2
@needs_ffmpeg
class ComposeIntegrationTest(unittest.TestCase):
    def test_a_real_track_reaches_the_gameplay_plan(self) -> None:
        from clipforge.gameplay import GameplayEngine

        path, scenario = fixture("single_speaker")
        with FaceTrackEngine() as engine:
            result = engine.track_video(path)

        plan = GameplayEngine().compose(
            duration_s=scenario.duration_s, track=result.track, word_count=90,
        )
        self.assertEqual(plan.camera.tracking, "tracked")
        # And the plan's crop fits the media it was composed against, which is
        # exactly what `render.engine._preflight` checks before encoding.
        reach_x = max(k.x for k in plan.camera.keyframes) + plan.camera.width
        reach_y = max(k.y for k in plan.camera.keyframes) + plan.camera.height
        self.assertLessEqual(reach_x, scenario.size[0])
        self.assertLessEqual(reach_y, scenario.size[1])


class PipelineWiringTest(unittest.TestCase):
    """The factory stage that used to pass an empty track unconditionally."""

    def _item(self, media_path: str = ""):
        from clipforge.factory.pipeline import WorkItem
        from clipforge.factory.sources import Source
        from clipforge.factory.niches import SourceKind

        source = Source(
            source_id="src_1", title="t", kind=SourceKind.PODCAST,
            media_path=media_path,
        )
        return WorkItem(item_id="it_1", channel_id="ch_1", source=source)

    def _moment(self, start_ms: int = 4000, duration_ms: int = 3000):
        class _Candidate:
            start_ms = 0
            end_ms = 0
            duration_ms = 0
            text = "a b c"

        class _Moment:
            candidate = _Candidate()

        moment = _Moment()
        moment.candidate.start_ms = start_ms
        moment.candidate.end_ms = start_ms + duration_ms
        moment.candidate.duration_ms = duration_ms
        return moment

    def test_no_media_means_a_static_track_and_a_stated_reason(self) -> None:
        from clipforge.factory.pipeline import Pipeline

        pipeline = Pipeline()
        track, note = pipeline._speaker_track(self._item(), self._moment(), 3.0)
        self.assertTrue(track.is_empty)
        self.assertIn("no media", note)

    def test_framing_can_be_turned_off(self) -> None:
        from clipforge.factory.pipeline import Pipeline, PipelineConfig

        pipeline = Pipeline(PipelineConfig(disable_face_tracking=True))
        track, note = pipeline._speaker_track(
            self._item("/anything.mp4"), self._moment(), 3.0,
        )
        self.assertTrue(track.is_empty)
        self.assertIn("off", note)

    def test_the_clip_window_is_what_gets_detected(self) -> None:
        """Not the whole source — a two-hour podcast is 72,000 detections."""

        seen: dict[str, object] = {}

        class Recording:
            def track_video(self, path, *, start_s=0.0, duration_s=0.0):
                seen.update(path=path, start_s=start_s, duration_s=duration_s)
                from clipforge.vision.types import FaceTrackResult

                return FaceTrackResult(
                    track=SpeakerTrack(
                        samples=(FaceSample(0.0, Box(10, 10, 80, 100)),),
                        source_width=1280, source_height=720,
                    ),
                    people=(),
                )

        from clipforge.factory.pipeline import Pipeline, PipelineConfig

        pipeline = Pipeline(PipelineConfig(face_tracker=Recording()))
        item = self._item("/tmp/source.mp4")
        track, _note = pipeline._speaker_track(
            item, self._moment(start_ms=4000, duration_ms=3000), 3.0,
        )
        self.assertEqual(seen["start_s"], 4.0)
        self.assertEqual(seen["duration_s"], 3.0)
        self.assertEqual(track.source_width, 1280)
        self.assertIsNotNone(item.face_track)

    def test_a_tracker_that_throws_does_not_lose_the_clip(self) -> None:
        class Broken:
            def track_video(self, path, **kwargs):
                raise OSError("disk went away")

        from clipforge.factory.pipeline import Pipeline, PipelineConfig

        pipeline = Pipeline(PipelineConfig(face_tracker=Broken()))
        track, note = pipeline._speaker_track(
            self._item("/tmp/source.mp4"), self._moment(), 3.0,
        )
        self.assertTrue(track.is_empty)
        self.assertIn("framing failed", note)


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main()
