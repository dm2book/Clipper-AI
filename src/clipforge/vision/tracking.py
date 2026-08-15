"""Turning per-frame detections into per-person tracks.

A detector answers "where are the faces in this frame". The camera needs "where
is *this person* over time", and the gap between those two questions is this
file. Without it, two people in shot produce a stream of boxes with no identity,
`speaker_id` is a constant, and the camera's whole cut-on-speaker-change path is
dead code.

## Association is IoU plus a centroid gate, not IoU alone

At ten samples a second a head can travel further than its own width, and two
boxes for the same person then have zero overlap. An IoU-only matcher ends the
track and starts a new one, which the camera reads as a *different speaker* and
cuts to — so the visible symptom of getting this wrong is not a lost track, it
is a hard cut every time somebody leans forward.

So a detection continues a track if it overlaps it, or if its centre is within
`max_centre_drift` face-widths of the track's last centre. The gate scales with
face size because a drift of 80px is nothing for a face filling the frame and
is a different person entirely for a face at the back of a room.

## Occlusion produces silence, not guesses

When a track loses its detection the tracker emits nothing for those frames.
It does not interpolate, extrapolate, or hold the last box forward into the
output. This is not laziness — it is the correct handoff, because
`camera.solve` already treats a gap as "hold the camera exactly still", which
is the right behaviour and is better than any position this module could
invent. A predicted box that drifts is strictly worse than no box: the camera
would follow the prediction and then snap back when the real face returned.

The track itself survives the gap (up to `max_age`), so the person keeps their
`speaker_id` and the camera does not cut when they reappear.

## Confirmation is retroactive

A track emits nothing until it has `min_hits` detections, which is what stops a
one-frame false positive from becoming a speaker. But once confirmed it emits
its *buffered* history too, so a real face loses no samples for having been
provisional during its first two frames. Discarding them instead would make
every entrance start late, which is exactly when the framing matters most.

## Who is talking

`FaceSample.confidence` is the channel the camera reads to pick between two
faces visible in the same frame — see `camera._observe`. This module fills it
with a *salience* score, not the detector's score, and the distinction is the
whole reason `Detection.confidence` is kept separate: a person who is silent is
not a poor detection, and a camera that treated them as one would cut away from
somebody standing perfectly still in good light.

Salience combines mouth motion (`activity`), face size, and centrality. Mouth
motion is doing most of the work and it is a genuinely weak signal — see
`activity()` for exactly how weak. Every emitted sample is floored at
`_SALIENCE_FLOOR`, comfortably above the camera's `MIN_CONFIDENCE`, so a quiet
person is always still a *detected* person.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

from .config import FaceDetectionConfig
from .types import (
    Box,
    Detection,
    PersonSummary,
    PersonTrack,
    TrackState,
    iou,
)

__all__ = ["FaceTracker", "activity", "salience", "smooth_activity",
           "size_score", "centre_score"]

#: The lowest salience an emitted sample can carry. Above `camera.MIN_CONFIDENCE`
#: (0.35) with room to spare, because everything reaching this point has already
#: passed the detector's own threshold and is a real face.
_SALIENCE_FLOOR = 0.50

#: How much of the salience range above the floor each signal controls. Mouth
#: motion dominates because it is the only one that tracks *speech*; size and
#: centrality are priors that break ties when nobody is moving their mouth.
_W_ACTIVITY = 0.62
_W_SIZE = 0.23
_W_CENTRE = 0.15

#: Mouth-crop side, as a multiple of inter-eye distance. Wide enough to hold
#: the whole mouth through moderate head yaw, tight enough that cheek and chin
#: pixels do not dilute the motion signal.
_MOUTH_SPAN = 0.95

#: Frame-to-frame mouth difference, in 0-255 units, treated as "fully active".
#: Calibrated against the fixtures in `tests/fixtures/faces.py`: a rendered
#: talking mouth moves 12-30 units between samples, a still one under 3.
_ACTIVITY_FULL_SCALE = 14.0

#: Activity is averaged over a window this long so a closed mouth between two
#: syllables does not read as silence.
_ACTIVITY_WINDOW_S = 0.6


def _centre_distance(a: Box, b: Box) -> float:
    return math.hypot(a.cx - b.cx, a.cy - b.cy)


def _mouth_patch(image: np.ndarray, detection: Detection) -> np.ndarray | None:
    """The mouth region of a face, normalised to a fixed size.

    Fixed size because the comparison is between two frames in which the face
    may have changed scale, and differencing a 40px patch against a 44px one
    measures the resize rather than the mouth.
    """

    marks = detection.landmarks
    if marks is None:
        return None
    eye_span = marks.eye_distance
    if eye_span <= 1.0:
        return None

    cx, cy = marks.mouth_centre
    half = max(4.0, eye_span * _MOUTH_SPAN / 2.0)
    height, width = image.shape[:2]
    x0 = int(max(0, round(cx - half)))
    x1 = int(min(width, round(cx + half)))
    y0 = int(max(0, round(cy - half * 0.75)))
    y1 = int(min(height, round(cy + half * 0.75)))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None

    patch = image[y0:y1, x0:x1]
    if patch.size == 0:
        return None

    try:
        import cv2

        grey = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        return cv2.resize(grey, (24, 18), interpolation=cv2.INTER_AREA)
    except Exception:                                       # noqa: BLE001
        return None


def activity(previous: np.ndarray | None, current: np.ndarray | None) -> float:
    """How much the mouth moved between two samples, in [0, 1].

    This is a visual voice-activity estimate and it is worth being precise
    about what it is not. It does not hear anything. It cannot tell speech from
    chewing, laughing, yawning, or a head turning far enough to change what the
    mouth crop contains. It goes to zero for someone speaking with a still
    mouth on a low-resolution face, and it fires on a person who is silent and
    animated.

    It is used anyway because the alternative for choosing between two visible
    faces is face area, which is a fixed property of where people sat, and
    because a wrong choice costs a camera cut rather than a wrong transcript.

    **There is no diarisation override, and there should be.** Audio would
    answer this properly, and `viral.types.Utterance` already carries a
    `speaker` label — but nothing maps those labels onto visual tracks, and
    inventing that mapping from turn timings alone would be a guess dressed as
    a measurement. Until it exists, this is what decides.
    """

    if previous is None or current is None:
        return 0.0
    if previous.shape != current.shape:
        return 0.0
    difference = np.abs(current.astype(np.int16) - previous.astype(np.int16))
    return float(min(1.0, difference.mean() / _ACTIVITY_FULL_SCALE))


def size_score(box: Box, largest_area: float) -> float:
    """This face's area against the biggest face in the same frame."""
    if largest_area <= 0:
        return 0.0
    return min(1.0, box.area / largest_area)


def centre_score(box: Box, frame_width: int, frame_height: int) -> float:
    """How near the frame's centre a face sits, 1.0 dead centre."""
    if not frame_width or not frame_height:
        return 0.0
    dx = (box.cx - frame_width / 2.0) / (frame_width / 2.0)
    dy = (box.cy - frame_height / 2.0) / (frame_height / 2.0)
    return max(0.0, 1.0 - math.hypot(dx, dy) / math.sqrt(2.0))


def salience(activity_score: float, size: float, centre: float) -> float:
    """Blend the three signals into the camera's tie-breaker.

    The floor is what keeps this honest: a silent, small, off-centre face
    still scores `_SALIENCE_FLOOR`, which the camera reads as a solid
    detection. Nothing here can push a real face below the camera's
    confidence gate and make it disappear.
    """

    blended = (
        _W_ACTIVITY * max(0.0, min(1.0, activity_score))
        + _W_SIZE * max(0.0, min(1.0, size))
        + _W_CENTRE * max(0.0, min(1.0, centre))
    )
    return _SALIENCE_FLOOR + (1.0 - _SALIENCE_FLOOR) * blended


@dataclass(slots=True)
class _Live:
    """Bookkeeping the tracker keeps that the public track does not."""

    track: PersonTrack
    last_box: Box
    last_patch: np.ndarray | None = None
    #: Sampled-frame gaps, for the summary.
    longest_gap_s: float = 0.0
    gap_started_t: float | None = None


class FaceTracker:
    """Associates detections into stable, named person tracks.

    One instance per video. Feed it frames in time order with `update`, then
    call `finish` for the tracks.
    """

    def __init__(self, config: FaceDetectionConfig | None = None) -> None:
        self.config = config or FaceDetectionConfig()
        self._live: list[_Live] = []
        self._done: list[PersonTrack] = []
        self._next_id = 1
        self._frames = 0
        self._frames_with_face = 0
        self._max_simultaneous = 0

    # -- ingest ------------------------------------------------------------

    def update(
        self,
        t: float,
        detections: Sequence[Detection],
        image: np.ndarray | None = None,
        frame_size: tuple[int, int] = (0, 0),
    ) -> None:
        """Fold one sampled frame into the tracks.

        `image` is optional and is used only for the mouth-motion signal. With
        no image every detection scores zero activity, salience falls back to
        size and centrality, and everything else works unchanged.
        """

        self._frames += 1
        if detections:
            self._frames_with_face += 1
        self._max_simultaneous = max(self._max_simultaneous, len(detections))

        width, height = frame_size
        if (not width or not height) and image is not None:
            height, width = image.shape[:2]

        largest = max((d.box.area for d in detections), default=0.0)
        pairs = self._match(detections)

        for slot, d_index in pairs.items():
            live = self._live[slot]
            detection = detections[d_index]

            patch = _mouth_patch(image, detection) if image is not None else None
            score = activity(live.last_patch, patch)
            if patch is not None:
                live.last_patch = patch

            if live.gap_started_t is not None:
                live.longest_gap_s = max(
                    live.longest_gap_s, t - live.gap_started_t
                )
                live.gap_started_t = None

            track = live.track
            self._record(track, t, detection, score, width, height, largest)
            track.hits += 1
            track.misses = 0
            live.last_box = detection.box
            if track.state is TrackState.TENTATIVE:
                if track.hits >= self.config.min_hits:
                    track.state = TrackState.CONFIRMED
            elif track.state is TrackState.LOST:
                track.state = TrackState.CONFIRMED

        # Unmatched tracks age. They keep their id through the gap so a person
        # who steps behind someone does not come back as a new speaker.
        for slot, live in enumerate(self._live):
            if slot in pairs:
                continue
            live.track.misses += 1
            if live.gap_started_t is None:
                live.gap_started_t = t
            live.longest_gap_s = max(live.longest_gap_s, t - live.gap_started_t)
            if live.track.state is TrackState.CONFIRMED:
                live.track.state = TrackState.LOST
            # Forget the mouth. Differencing the frame before an occlusion
            # against the frame after it measures the occlusion, not speech,
            # and would spike activity for the person who was *hidden* —
            # making the camera most likely to pick them at the exact moment
            # it has the least reason to.
            live.last_patch = None

        # Detections that matched nothing start tentative tracks.
        claimed = set(pairs.values())
        for d_index, detection in enumerate(detections):
            if d_index in claimed:
                continue
            self._birth(t, detection, image, width, height, largest)

        self._retire()

    def _record(
        self, track: PersonTrack, t: float, detection: Detection,
        score: float, width: int, height: int, largest: float,
    ) -> None:
        """Append one observation and the frame context salience will need."""
        track.observations.append((t, detection))
        track.activity.append(score)
        track.size_scores.append(size_score(detection.box, largest))
        track.centre_scores.append(centre_score(detection.box, width, height))
        track.last_t = t

    def _birth(
        self, t: float, detection: Detection, image: np.ndarray | None,
        width: int, height: int, largest: float,
    ) -> None:
        track = PersonTrack(
            track_id=f"spk_{self._next_id}",
            state=TrackState.TENTATIVE,
            first_t=t,
            hits=1,
        )
        self._next_id += 1
        patch = _mouth_patch(image, detection) if image is not None else None
        # A new track has nothing to difference against, so its first activity
        # is zero rather than a guess. Smoothing fills it in from the frames
        # that follow.
        self._record(track, t, detection, 0.0, width, height, largest)
        if track.hits >= self.config.min_hits:
            track.state = TrackState.CONFIRMED
        self._live.append(_Live(
            track=track, last_box=detection.box, last_patch=patch,
        ))

    def _match(self, detections: Sequence[Detection]) -> dict[int, int]:
        """Greedy best-first association of detections to live tracks.

        Greedy rather than Hungarian on purpose. The assignment is at most
        three faces against three tracks; optimal assignment differs from
        greedy only when two candidate pairs conflict, which needs two faces
        closer to each other than to their own previous positions — i.e. people
        overlapping — and in that case the tracker's identity is already a
        guess. A dependency on scipy to improve a guess is a poor trade.
        """

        if not detections or not self._live:
            return {}

        scored: list[tuple[float, int, int]] = []
        for slot, live in enumerate(self._live):
            for d_index, detection in enumerate(detections):
                overlap = iou(live.last_box, detection.box)
                reference = max(live.last_box.width, detection.box.width, 1.0)
                drift = _centre_distance(live.last_box, detection.box) / reference

                if overlap >= self.config.min_iou:
                    cost = 1.0 - overlap
                elif drift <= self.config.max_centre_drift:
                    # Behind every overlapping pair, so a real overlap always
                    # wins over a merely-nearby one.
                    cost = 1.0 + drift
                else:
                    continue
                scored.append((cost, slot, d_index))

        scored.sort()
        taken_dets: set[int] = set()
        pairs: dict[int, int] = {}
        for _cost, slot, d_index in scored:
            if slot in pairs or d_index in taken_dets:
                continue
            taken_dets.add(d_index)
            pairs[slot] = d_index
        return pairs

    def _retire(self) -> None:
        keep: list[_Live] = []
        for live in self._live:
            if live.track.misses > self.config.max_age:
                live.track.state = TrackState.ENDED
                self._done.append(live.track)
            else:
                keep.append(live)
        self._live = keep

    # -- results -----------------------------------------------------------

    def finish(self) -> list[PersonTrack]:
        """Close every open track and return the ones worth believing.

        A track that never reached `min_hits` is dropped entirely. That is the
        false-positive filter, and it is applied here rather than at ingest so
        a track that was tentative at the end of the video — someone walking in
        during the last half second — is judged on its whole life.
        """

        for live in self._live:
            live.track.state = TrackState.ENDED
            self._done.append(live.track)
        self._live = []

        believed = [
            track for track in self._done
            if track.hits >= self.config.min_hits and track.observations
        ]
        believed.sort(key=lambda track: (track.first_t, track.track_id))
        return believed

    def summaries(
        self, tracks: Iterable[PersonTrack], sample_interval_s: float = 0.0,
    ) -> tuple[PersonSummary, ...]:
        out: list[PersonSummary] = []
        for track in tracks:
            confidences = [d.confidence for _t, d in track.observations]
            gaps = _longest_gap(track, sample_interval_s)
            out.append(PersonSummary(
                speaker_id=track.track_id,
                first_t=track.first_t,
                last_t=track.last_t,
                samples=len(track.observations),
                mean_confidence=(
                    sum(confidences) / len(confidences) if confidences else 0.0
                ),
                mean_activity=(
                    sum(track.activity) / len(track.activity)
                    if track.activity else 0.0
                ),
                longest_gap_s=gaps,
            ))
        return tuple(out)

    @property
    def stats(self) -> tuple[int, int, int]:
        """`(frames, frames_with_face, max_simultaneous)`."""
        return self._frames, self._frames_with_face, self._max_simultaneous


def _longest_gap(track: PersonTrack, sample_interval_s: float = 0.0) -> float:
    """The longest span a track went undetected while still alive.

    The nominal sampling interval is subtracted, so a track detected in every
    single sampled frame reports zero rather than reporting the sample period
    as though it were a dropout.
    """

    longest = 0.0
    times = [t for t, _d in track.observations]
    for earlier, later in zip(times, times[1:]):
        longest = max(longest, later - earlier)
    return max(0.0, longest - sample_interval_s)


def smooth_activity(
    tracks: Sequence[PersonTrack], window_s: float = _ACTIVITY_WINDOW_S
) -> None:
    """Average each track's activity over a short window, in place.

    Speech is not continuous at the ten-samples-a-second scale — the mouth
    closes between syllables and shuts entirely at a comma. Raw per-sample
    activity therefore flickers, and since activity drives the camera's choice
    of subject, flicker there means the camera keeps proposing a change of
    speaker. The window is longer than a syllable and shorter than a turn.
    """

    for track in tracks:
        times = [t for t, _d in track.observations]
        raw = list(track.activity)
        if len(raw) < 2:
            continue
        smoothed: list[float] = []
        for index, centre in enumerate(times):
            total = 0.0
            count = 0
            for other in range(index, -1, -1):
                if centre - times[other] > window_s:
                    break
                total += raw[other]
                count += 1
            for other in range(index + 1, len(times)):
                if times[other] - centre > window_s:
                    break
                total += raw[other]
                count += 1
            smoothed.append(total / count if count else raw[index])
        track.activity = smoothed
