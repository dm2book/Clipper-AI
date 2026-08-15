"""Sampled frame reading.

The detector wants roughly ten frames a second. The video has thirty or sixty.
This module is the part that bridges those two numbers without decoding work
nobody uses, and without lying about what time a frame is at.

## Sequential decode, not seeking

The obvious implementation seeks to each wanted timestamp. It is wrong for
inter-frame codecs: a seek in H.264 lands on the nearest keyframe, which can be
seconds away, so the frames come back at the wrong times *and* the decoder
re-decodes the whole GOP to get there — slower than reading straight through
and far less accurate. `VideoCapture.grab()` decodes without converting to a
numpy array, which is the expensive half, so skipping a frame costs a fraction
of using one.

Seeking is used for exactly one thing: skipping to `start_s` when a caller only
wants a window late in a long file. Even then the position is read back from
the decoder rather than assumed, because that seek is keyframe-aligned too.

## Timestamps come from the frame index

Not from `CAP_PROP_POS_MSEC`, which is unreliable across containers and returns
zero on some. `index / fps` is exact for constant-rate video and consistently
wrong in a way that cancels out for variable-rate — every consumer downstream
cares about intervals between samples, not about absolute presentation time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterator

import numpy as np

from .types import DecodeError

__all__ = ["VideoInfo", "SampledFrame", "probe_video", "sample_frames"]

#: Believed only if the container reports something in this range. Files with
#: a broken header report 0, 1000, or nan, and a stride computed from nan is a
#: stride of zero.
_MIN_FPS = 1.0
_MAX_FPS = 480.0
#: Used when the container has no usable rate. Chosen because it is the most
#: common real value, so the sample stride lands close for typical footage.
_ASSUMED_FPS = 30.0


@dataclass(frozen=True, slots=True)
class VideoInfo:
    """What the container claims about the file."""

    path: str
    width: int
    height: int
    fps: float
    frame_count: int
    #: True when `fps` is `_ASSUMED_FPS` rather than something the file said.
    fps_assumed: bool = False

    @property
    def duration_s(self) -> float:
        if self.frame_count > 0 and self.fps > 0:
            return self.frame_count / self.fps
        return 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "width": self.width, "height": self.height,
            "fps": round(self.fps, 3), "frame_count": self.frame_count,
            "duration_s": round(self.duration_s, 3),
            "fps_assumed": self.fps_assumed,
        }


@dataclass(frozen=True, slots=True)
class SampledFrame:
    """One frame handed to the detector."""

    t: float
    index: int
    image: np.ndarray


def _open(path: str):
    try:
        import cv2
    except ImportError as error:                            # pragma: no cover
        raise DecodeError(
            "opencv-python-headless is not installed, so no video can be read"
        ) from error

    if not path:
        raise DecodeError("no video path given")
    if not os.path.isfile(path):
        raise DecodeError(f"{path} does not exist")
    if os.path.getsize(path) == 0:
        raise DecodeError(f"{path} is empty")

    capture = cv2.VideoCapture(path)
    if not capture.isOpened():
        capture.release()
        raise DecodeError(
            f"OpenCV could not open {os.path.basename(path)} — it is either "
            f"not a video, uses a codec this build lacks, or is truncated"
        )
    return cv2, capture


def _describe(cv2, capture, path: str) -> VideoInfo:
    raw_fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    usable = raw_fps == raw_fps and _MIN_FPS <= raw_fps <= _MAX_FPS
    return VideoInfo(
        path=path,
        width=int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
        height=int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0),
        fps=raw_fps if usable else _ASSUMED_FPS,
        frame_count=max(0, int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)),
        fps_assumed=not usable,
    )


def probe_video(path: str) -> VideoInfo:
    """Geometry and rate, without decoding the whole file.

    Used before detection so the resulting `SpeakerTrack` carries the real
    source dimensions. That single fact is what stops the camera path from
    being solved against `SpeakerTrack`'s 1920x1080 default and then rejected
    by the renderer's preflight against 1280x720 media.
    """

    cv2, capture = _open(path)
    try:
        info = _describe(cv2, capture, path)
    finally:
        capture.release()
    if not info.width or not info.height:
        raise DecodeError(
            f"{os.path.basename(path)} reports a {info.width}x{info.height} "
            f"frame size, which cannot be used for framing"
        )
    return info


#: Seeking below this is slower than reading through it, and less accurate.
_SEEK_THRESHOLD_S = 2.0


def sample_frames(
    path: str,
    *,
    sample_fps: float = 10.0,
    start_s: float = 0.0,
    duration_s: float = 0.0,
    max_frames: int = 0,
) -> Iterator[SampledFrame]:
    """Yield frames at approximately `sample_fps`, in order.

    `duration_s` of zero means "to the end". `max_frames` of zero means no
    ceiling — a caller processing a two-hour source should set one, because the
    cost here is linear in the file and not in the clip.

    The generator owns the capture and releases it on exhaustion *or* on an
    early exit, which is why the body is wrapped rather than trusting the
    caller to drain it. A `break` in a `for` over this used to leak a decoder
    per clip.
    """

    cv2, capture = _open(path)
    try:
        info = _describe(cv2, capture, path)
        if not info.width or not info.height:
            raise DecodeError(
                f"{os.path.basename(path)} reports no frame size"
            )

        rate = max(0.1, float(sample_fps))
        stride = max(1, int(round(info.fps / rate)))

        index = 0
        if start_s > _SEEK_THRESHOLD_S:
            wanted = int(start_s * info.fps)
            capture.set(cv2.CAP_PROP_POS_FRAMES, float(wanted))
            # Read back rather than assume: the seek is keyframe-aligned and
            # can land well before what was asked for.
            landed = int(capture.get(cv2.CAP_PROP_POS_FRAMES) or 0)
            index = max(0, landed)

        end_index = (
            int((start_s + duration_s) * info.fps)
            if duration_s > 0 else 0
        )
        start_index = int(start_s * info.fps)
        # Keep the sampling grid anchored to the file rather than to wherever
        # the seek landed, so two overlapping windows sample the same frames.
        emitted = 0

        while True:
            if end_index and index >= end_index:
                break
            if max_frames and emitted >= max_frames:
                break

            if index % stride != 0 or index < start_index:
                if not capture.grab():
                    break
                index += 1
                continue

            ok, image = capture.read()
            if not ok or image is None:
                break
            yield SampledFrame(t=index / info.fps, index=index, image=image)
            emitted += 1
            index += 1
    finally:
        capture.release()
