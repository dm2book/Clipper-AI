"""The virtual camera: noisy face detections in, a watchable crop path out.

This is the hard part of the whole engine, and the part that separates output
a creator will publish from output they will not.

A face tracker gives you boxes that jitter by several pixels between frames,
that disappear whenever the speaker turns their head, and that arrive at 10fps
when the output is 60. Cropping straight to those boxes produces video that is
genuinely unpleasant to watch — the frame vibrates, snaps to centre every time
detection drops, and lurches whenever the tracker's idea of the face shifts.
None of that is visible while looking at boxes on a chart; all of it is visible
in the first second of playback.

Five things fix it, and all five are necessary:

1. **Deadband with hysteresis.** Small movements do not move the camera at all.
   A real operator holds the shot; they do not chase a head sway. The
   hysteresis (a smaller threshold to disengage than to engage) stops the
   camera chattering on and off at the boundary.
2. **Adaptive smoothing** — a 1€ filter rather than a fixed low-pass. Fixed
   smoothing forces a choice between visible jitter on slow movement and
   visible lag on fast movement. The 1€ filter loosens the cutoff in
   proportion to speed, so it is heavy when the subject is still and quick
   when they move.
3. **Slew limiting.** A hard ceiling on pan speed, so no residual detector
   glitch can throw the frame across the shot.
4. **Cut, do not pan, for large jumps.** When the target moves most of a frame
   width — a second speaker starts talking — panning across is nauseating and
   a hard cut is invisible. A minimum shot length stops two people from
   strobing the frame between them.
5. **Hold through gaps.** When detection drops, the camera stays exactly where
   it was. Recentring during a two-frame dropout and coming back is the single
   most obvious artefact an auto-framer can produce.

The crop **size** is constant for the entire clip. That is a deliberate
constraint with two independent justifications: zooming within a shot is
amateurish, and ffmpeg cannot change a filter's output dimensions mid-stream,
so a variable crop size could not be executed by the renderer anyway. The
camera pans; it never zooms.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .types import (
    Box,
    CameraPath,
    CropKeyframe,
    FaceSample,
    Motion,
    SpeakerTrack,
    clamp,
    even,
)

# --- Framing ----------------------------------------------------------------

#: Share of the crop's height the face should occupy. Tighter than this feels
#: claustrophobic and clips foreheads when the speaker moves; looser and the
#: face stops being readable at phone size, which defeats the crop.
TARGET_FACE_HEIGHT = 0.34

#: Where the eyes sit vertically in the finished frame. Centring the *face
#: box* is the naive choice and it leaves a visible slab of dead space above
#: the head — headroom is a composition rule, not a rounding error.
EYELINE = 0.40

#: Where the eyes sit within a face box, as returned by typical detectors.
EYES_IN_BOX = 0.42

MIN_FACE_HEIGHT_PX = 24.0

# --- Motion -----------------------------------------------------------------

#: Movement below this fraction of crop width does not move the camera.
DEADBAND = 0.055
#: Once moving, keep moving until inside this. Hysteresis: without the gap
#: between the two, the camera chatters on and off at the threshold.
DEADBAND_RELEASE = 0.018

#: Time constant of the follower that closes the gap once the deadband has
#: engaged. An exponential follow decelerates as it arrives, so a correction
#: eases out instead of stopping dead. Snapping to the smoothed target and
#: relying on the slew limit to slow it down produces a camera that sprints at
#: its speed ceiling and then halts — the exact lurch the deadband exists to
#: prevent.
FOLLOW_TAU_S = 0.45

#: Ceiling on pan speed, as a fraction of crop width per second. With the
#: follower doing the work this is a safety net against detector glitches,
#: not the thing shaping normal movement.
MAX_PAN_SPEED = 0.55

#: Target displacement beyond this fraction of crop width is a cut, not a pan.
CUT_DISTANCE = 0.42
#: No two cuts closer together than this. Two people in conversation will
#: otherwise strobe the frame between them on every "mm-hm".
MIN_SHOT_S = 1.2
#: How long the reason to cut must persist before the cut happens.
#:
#: Without this, one bad detection is enough. A single spurious box across the
#: frame reads as "the speaker moved a long way", the camera cuts to it, and
#: MIN_SHOT_S then strands the shot on empty background for over a second —
#: a one-frame detector error turned into a 1.2s mistake. Roughly two detector
#: samples at 10fps: long enough to reject an outlier, short enough that a
#: genuine speaker change still feels immediate.
CUT_CONFIRM_S = 0.15

#: Detections below this confidence are treated as no detection at all.
MIN_CONFIDENCE = 0.35
#: How far from an output frame a detection may be and still count.
MATCH_TOLERANCE_S = 0.20
#: A gap longer than this gets a note on the plan. The camera holds either
#: way — holding is always better than drifting — but a long hold is worth
#: surfacing, because it usually means the detector needs a look.
GAP_NOTE_S = 1.0

# --- 1€ filter --------------------------------------------------------------

MIN_CUTOFF = 0.8
#: Low for a 1€ filter. `beta` buys responsiveness to fast movement by letting
#: the cutoff open, and an open cutoff passes jitter straight through. This
#: camera does not need that trade: genuinely fast movement is handled by
#: cutting, not by panning faster, so the filter is free to prioritise
#: smoothness. At 0.25 a 16px jitter band survives as 6.4px of visible wobble;
#: at 0.10 it is 3.6px, which the deadband then absorbs entirely.
BETA = 0.10
D_CUTOFF = 1.0


def _alpha(cutoff: float, dt: float) -> float:
    tau = 1.0 / (2.0 * math.pi * cutoff)
    return 1.0 / (1.0 + tau / dt)


class OneEuroFilter:
    """Speed-adaptive low-pass filter.

    Heavy smoothing while the signal is slow (kills jitter), light smoothing
    while it is fast (kills lag). A fixed filter has to pick one and be wrong
    the rest of the time.

    The velocity estimate is taken from successive **raw inputs**, not from the
    gap between the input and the filter's own output. Using the output is a
    tempting simplification and it breaks the filter completely as soon as
    anything downstream can hold the camera still: after a deadband hold the
    output is far from the input, the filter reads that gap as enormous target
    velocity, opens its cutoff by two orders of magnitude and passes the raw
    signal straight through — turning every correction into a lurch.
    """

    __slots__ = ("min_cutoff", "beta", "d_cutoff", "_x", "_dx", "_prev",
                 "_started")

    def __init__(
        self,
        min_cutoff: float = MIN_CUTOFF,
        beta: float = BETA,
        d_cutoff: float = D_CUTOFF,
    ) -> None:
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self._x = 0.0
        self._dx = 0.0
        self._prev = 0.0
        self._started = False

    @property
    def value(self) -> float:
        return self._x

    def reset(self, value: float) -> None:
        """Jump the filter to `value` with no velocity — used on a cut."""
        self._x = value
        self._prev = value
        self._dx = 0.0
        self._started = True

    def __call__(self, value: float, dt: float) -> float:
        if not self._started:
            self.reset(value)
            return value
        if dt <= 0.0:
            return self._x

        raw_dx = (value - self._prev) / dt
        self._prev = value
        self._dx += _alpha(self.d_cutoff, dt) * (raw_dx - self._dx)

        cutoff = self.min_cutoff + self.beta * abs(self._dx)
        self._x += _alpha(cutoff, dt) * (value - self._x)
        return self._x


# --- Crop sizing -------------------------------------------------------------


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round(fraction * (len(ordered) - 1)))
    return ordered[max(0, min(len(ordered) - 1, index))]


def plan_crop_size(
    track: SpeakerTrack, aspect: float
) -> tuple[int, int]:
    """One crop size for the whole clip, matching the panel's aspect ratio.

    Sized from the 90th percentile of observed face height rather than the
    mean. The mean produces a crop that is correct on average and too tight
    exactly when the speaker leans into the camera — which is the moment the
    clip was chosen for.
    """
    source_w, source_h = track.source_width, track.source_height
    if aspect <= 0:
        aspect = source_w / source_h if source_h else 1.0

    heights = [
        s.box.height for s in track.samples
        if s.confidence >= MIN_CONFIDENCE and s.box.height >= MIN_FACE_HEIGHT_PX
    ]

    if heights:
        crop_h = _percentile(heights, 0.90) / TARGET_FACE_HEIGHT
    else:
        # No usable detections: fall back to the largest crop of the right
        # aspect that fits, i.e. a plain cover crop. Not framing — just a
        # sensible default that never looks broken.
        crop_h = float(source_h)

    crop_w = crop_h * aspect

    # Never ask for more than the source has.
    if crop_w > source_w:
        crop_w = float(source_w)
        crop_h = crop_w / aspect
    if crop_h > source_h:
        crop_h = float(source_h)
        crop_w = crop_h * aspect
    if crop_w > source_w:
        crop_w = float(source_w)

    return even(clamp(crop_w, 16, source_w)), even(clamp(crop_h, 16, source_h))


# --- Observation resampling ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Observation:
    box: Box
    speaker_id: str


#: Detections this close together are treated as belonging to the same
#: detector frame, and therefore as simultaneous.
SAME_FRAME_EPS_S = 0.005


def _observe(track: SpeakerTrack, t: float) -> _Observation | None:
    """The detection in effect at time `t`, or None for a gap.

    Nearest in time wins; confidence only breaks ties **within one detector
    frame**, which is the upstream tracker's channel for saying which of
    several visible people is talking. The engine deliberately does not try to
    infer that from geometry.

    Ranking by confidence across the whole match window instead is subtly
    wrong and bites hard: one confident false positive outranks every real
    detection within ±`MATCH_TOLERANCE_S`, so a single bad box wins a window
    twice the tolerance wide — long enough to look like a persistent, and
    therefore cut-worthy, change of subject.
    """
    candidates = [
        sample for sample in track.samples
        if abs(sample.t - t) <= MATCH_TOLERANCE_S
        and sample.confidence >= MIN_CONFIDENCE
        and sample.box.height >= MIN_FACE_HEIGHT_PX
    ]
    if not candidates:
        return None

    nearest = min(abs(sample.t - t) for sample in candidates)
    simultaneous = [
        sample for sample in candidates
        if abs(sample.t - t) <= nearest + SAME_FRAME_EPS_S
    ]
    best = max(simultaneous, key=lambda s: s.confidence)
    return _Observation(best.box, best.speaker_id)


def _desired(
    box: Box, crop_w: int, crop_h: int, source_w: int, source_h: int
) -> tuple[float, float]:
    """Top-left of the crop that frames `box` correctly, clamped in bounds."""
    x = box.cx - crop_w / 2.0
    eye_y = box.y + box.height * EYES_IN_BOX
    y = eye_y - crop_h * EYELINE
    return (
        clamp(x, 0.0, max(0.0, source_w - crop_w)),
        clamp(y, 0.0, max(0.0, source_h - crop_h)),
    )


# --- The solver ----------------------------------------------------------------


def solve(
    track: SpeakerTrack,
    duration_s: float,
    aspect: float,
    fps: int = 60,
) -> CameraPath:
    """Solve the camera path for one clip.

    Returns a piecewise path: a keyframe only where the crop actually changes.
    That compactness is not incidental — it is what makes the path executable
    as an ffmpeg `sendcmd` script, and it falls straight out of the deadband,
    which keeps the camera perfectly still for most of a typical clip.
    """
    crop_w, crop_h = plan_crop_size(track, aspect)
    source_w, source_h = track.source_width, track.source_height
    max_x = max(0.0, source_w - crop_w)
    max_y = max(0.0, source_h - crop_h)

    if track.is_empty:
        # No tracking data at all. A centred static crop is the honest
        # fallback: it is what an editor does with no information, and it
        # never looks broken. The plan says `tracking="static"` so the caller
        # can tell this apart from a solved path.
        return CameraPath(
            width=crop_w,
            height=crop_h,
            keyframes=(CropKeyframe(0.0, even(max_x / 2), even(max_y / 2),
                                    Motion.HOLD),),
            hold_ratio=1.0,
            tracking="static",
            notes=(
                "no speaker track — the crop is static and will not follow "
                "the speaker. Supply face detections to enable framing.",
            ),
        )

    frame_dt = 1.0 / fps
    frames = max(1, int(round(duration_s * fps)))

    filter_x = OneEuroFilter()
    filter_y = OneEuroFilter()

    keyframes: list[CropKeyframe] = []
    cuts: list[float] = []
    notes: list[str] = []

    current_x = current_y = 0.0
    started = False
    engaged = False
    active_speaker = ""
    last_cut_t = -math.inf
    cut_pending_since: float | None = None
    holds = 0
    gap_start: float | None = None
    longest_gap = 0.0
    emitted_x = emitted_y = None
    last_motion: Motion | None = None

    for index in range(frames):
        t = index * frame_dt
        observation = _observe(track, t)

        if observation is None:
            # Gap: hold position exactly. Never recentre.
            if started:
                if gap_start is None:
                    gap_start = t
                longest_gap = max(longest_gap, t - gap_start)
                holds += 1
                if last_motion is not Motion.HOLD:
                    keyframes.append(CropKeyframe(t, int(round(current_x)),
                                                  int(round(current_y)),
                                                  Motion.HOLD))
                    last_motion = Motion.HOLD
            continue

        if gap_start is not None:
            longest_gap = max(longest_gap, t - gap_start)
            gap_start = None

        raw_x, raw_y = _desired(observation.box, crop_w, crop_h,
                                source_w, source_h)

        if not started:
            current_x, current_y = raw_x, raw_y
            filter_x.reset(raw_x)
            filter_y.reset(raw_y)
            active_speaker = observation.speaker_id
            started = True
            keyframes.append(CropKeyframe(t, int(round(current_x)),
                                          int(round(current_y)), Motion.CUT))
            last_motion = Motion.CUT
            continue

        # Denoise the measurement every frame, whether or not the camera is
        # allowed to move. The filter's job is to describe where the speaker
        # actually is; the deadband and follower below decide what the camera
        # does about it. Conflating the two is what produced the lurch.
        target_x = filter_x(raw_x, frame_dt)
        target_y = filter_y(raw_y, frame_dt)

        distance = math.hypot(target_x - current_x, target_y - current_y)
        speaker_changed = observation.speaker_id != active_speaker
        far = distance > CUT_DISTANCE * crop_w
        wants_cut = speaker_changed or far

        # A cut has to earn itself over time, not on one frame.
        if wants_cut:
            if cut_pending_since is None:
                cut_pending_since = t
        else:
            cut_pending_since = None

        confirmed = (
            cut_pending_since is not None
            and (t - cut_pending_since) >= CUT_CONFIRM_S
        )
        can_cut = (t - last_cut_t) >= MIN_SHOT_S

        if wants_cut and confirmed and can_cut:
            # Hard cut. Panning a distance like this reads as a camera
            # operator losing control of a tripod.
            current_x, current_y = target_x, target_y
            filter_x.reset(target_x)
            filter_y.reset(target_y)
            active_speaker = observation.speaker_id
            last_cut_t = t
            cut_pending_since = None
            cuts.append(t)
            engaged = False
            keyframes.append(CropKeyframe(t, int(round(current_x)),
                                          int(round(current_y)), Motion.CUT))
            emitted_x, emitted_y = int(round(current_x)), int(round(current_y))
            last_motion = Motion.CUT
            continue

        if speaker_changed and confirmed and not can_cut:
            # Confirmed change of subject, but too soon to cut again. Stay on
            # the current framing rather than panning across to the new
            # speaker, which is worse than either option.
            holds += 1
            continue

        # Deadband with hysteresis.
        error = math.hypot(target_x - current_x, target_y - current_y)
        if engaged:
            if error < DEADBAND_RELEASE * crop_w:
                engaged = False
        elif error > DEADBAND * crop_w:
            engaged = True

        if not engaged:
            holds += 1
            if last_motion is not Motion.HOLD:
                keyframes.append(CropKeyframe(t, int(round(current_x)),
                                              int(round(current_y)),
                                              Motion.HOLD))
                last_motion = Motion.HOLD
            continue

        # Exponential follow toward the denoised target. Decelerates on
        # arrival, so a correction eases out rather than stopping dead.
        gain = 1.0 - math.exp(-frame_dt / FOLLOW_TAU_S)
        next_x = current_x + (target_x - current_x) * gain
        next_y = current_y + (target_y - current_y) * gain

        # Slew limit as a hard ceiling. In normal operation the follower stays
        # well underneath it; this only catches detector glitches.
        max_step = MAX_PAN_SPEED * crop_w * frame_dt
        step = math.hypot(next_x - current_x, next_y - current_y)
        if step > max_step and step > 0:
            scale = max_step / step
            next_x = current_x + (next_x - current_x) * scale
            next_y = current_y + (next_y - current_y) * scale

        current_x = clamp(next_x, 0.0, max_x)
        current_y = clamp(next_y, 0.0, max_y)

        rounded_x, rounded_y = int(round(current_x)), int(round(current_y))
        if (rounded_x, rounded_y) != (emitted_x, emitted_y):
            keyframes.append(CropKeyframe(t, rounded_x, rounded_y, Motion.PAN))
            emitted_x, emitted_y = rounded_x, rounded_y
            last_motion = Motion.PAN

    if not keyframes:
        keyframes.append(CropKeyframe(0.0, even(max_x / 2), even(max_y / 2),
                                      Motion.HOLD))
        notes.append("no detection cleared the confidence floor — static crop")

    if longest_gap >= GAP_NOTE_S:
        notes.append(
            f"held through a {longest_gap:.1f}s detection gap — "
            f"check the tracker before trusting the framing"
        )
    if len(cuts) > max(1, duration_s / MIN_SHOT_S / 3):
        notes.append(f"{len(cuts)} cuts in {duration_s:.0f}s — busy framing")

    tracking = "tracked" if any(k.motion is not Motion.HOLD for k in keyframes) \
        else "static"

    return CameraPath(
        width=crop_w,
        height=crop_h,
        keyframes=tuple(keyframes),
        cuts=tuple(cuts),
        hold_ratio=holds / frames if frames else 0.0,
        tracking=tracking,
        notes=tuple(notes),
    )
