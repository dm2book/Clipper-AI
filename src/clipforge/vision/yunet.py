"""YuNet: a 227 KB CNN face detector, run through OpenCV's DNN module.

## Why this model

The alternatives were all worse for this workload. Haar cascades are free and
ship with nothing useful in OpenCV 5 — the bundled XML files were dropped — and
they miss any face not looking straight at the camera, which is most of a
podcast. MediaPipe is excellent and is a 60 MB dependency with its own
threading model. The large detectors (RetinaFace, SCRFD via insightface) are
better on tiny faces in crowds, which is not this problem: a clip has one to
three people filling a good part of the frame.

YuNet detects that case at parity with far larger models, runs at hundreds of
frames a second on a CPU core, and — the part that decided it — returns five
landmarks, which is what makes the mouth-motion activity signal in
`tracking.py` possible at all.

## The downscale is a contract, not an optimisation

Detection runs on a frame scaled so its longest side is `max_side`, and every
box is scaled back into source pixels before it leaves this class. Callers get
source coordinates always. That matters because the camera's crop rectangle is
in source pixels, and a track in detector pixels would produce framing that is
silently wrong by a factor of three on 1080p and correct on 640p footage —
which is exactly the kind of bug that survives a test suite built on small
fixtures.
"""

from __future__ import annotations

import os
from typing import Any

import numpy as np

from .config import FaceDetectionConfig, resolve_model, select_device
from .types import (
    Availability,
    Box,
    Detection,
    DetectorInfo,
    DetectorUnavailable,
    Landmarks,
)

__all__ = ["YuNetDetector"]

#: YuNet returns one row per face: x, y, w, h, then five (x, y) landmarks,
#: then the score. Named because `row[14]` at the call site is unreadable and
#: wrong-by-one is silent — it would read a landmark coordinate as a
#: confidence, which lands in a plausible range and never raises.
_ROW_WIDTH = 15
_SCORE = 14
_LANDMARK_START = 4


class YuNetDetector:
    """A loaded YuNet model, reused across frames.

    The model is loaded once and the input size is reset per frame size rather
    than per frame — `setInputSize` is cheap but reallocating the network is
    not, and a 60-second clip is several hundred detections.

    Not thread-safe: `cv2.FaceDetectorYN` holds internal buffers sized to the
    last `setInputSize`, so two threads sharing one instance race on the
    geometry rather than on the pixels. The engine keeps one per job.
    """

    def __init__(self, config: FaceDetectionConfig | None = None) -> None:
        self.config = config or FaceDetectionConfig()
        self._model: Any = None
        self._model_path = ""
        self._input_size: tuple[int, int] = (0, 0)
        self._device = "cpu"
        self._device_note = ""
        self._version = ""

    # -- lifecycle ---------------------------------------------------------

    @property
    def info(self) -> DetectorInfo:
        return DetectorInfo(
            name="yunet",
            version=self._version,
            model=os.path.basename(self._model_path) if self._model_path else "",
            device=self._device,
            device_note=self._device_note,
        )

    def availability(self) -> Availability:
        """Check OpenCV, the model file and the model's loadability.

        Deliberately loads the network rather than stopping at
        `os.path.isfile`. A truncated or corrupt ONNX file passes every cheap
        check and then throws inside `detect` on the first frame of a real
        job — by which point a clip has already been downloaded and cut.
        """

        try:
            import cv2
        except ImportError:
            return Availability(
                False,
                "opencv-python-headless is not installed — no face detection, "
                "so framing falls back to a static centred crop",
            )
        if not hasattr(cv2, "FaceDetectorYN"):
            return Availability(
                False,
                f"OpenCV {cv2.__version__} has no FaceDetectorYN (needs 4.5.4+)",
            )
        try:
            path = resolve_model(self.config)
        except DetectorUnavailable as error:
            return Availability(False, str(error))
        try:
            self._load(path)
        except DetectorUnavailable as error:
            return Availability(False, str(error))
        return Availability(
            True, f"yunet on {self._device}" + (
                f" ({self._device_note})" if self._device_note else ""
            )
        )

    def _load(self, path: str = "") -> Any:
        if self._model is not None:
            return self._model

        import cv2

        self._version = cv2.__version__
        path = path or resolve_model(self.config)
        backend, target, device, note = select_device(self.config.device)
        try:
            model = cv2.FaceDetectorYN.create(
                model=path,
                config="",
                input_size=(320, 320),
                score_threshold=float(self.config.min_confidence),
                nms_threshold=float(self.config.nms_threshold),
                top_k=int(self.config.top_k),
                backend_id=backend,
                target_id=target,
            )
        except Exception as error:                          # noqa: BLE001
            raise DetectorUnavailable(
                f"could not load the face model at {path}: {error}"
            ) from error
        if model is None:                                   # pragma: no cover
            raise DetectorUnavailable(f"OpenCV returned no model for {path}")

        self._model = model
        self._model_path = path
        self._device = device
        self._device_note = note
        self._input_size = (0, 0)
        return model

    def close(self) -> None:
        self._model = None
        self._input_size = (0, 0)

    def __enter__(self) -> "YuNetDetector":
        self._load()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- detection ---------------------------------------------------------

    def _scale_for(self, width: int, height: int) -> float:
        longest = max(width, height)
        if longest <= self.config.max_side:
            return 1.0
        return self.config.max_side / float(longest)

    def detect(self, frame: np.ndarray) -> tuple[Detection, ...]:
        """Every face in one BGR frame, in that frame's own pixels.

        Returns strongest first. An empty tuple is a normal answer — most
        videos contain frames with nobody in them — and is distinguished from
        a failure by the fact that a failure raises.
        """

        import cv2

        if frame is None or frame.size == 0:
            return ()
        if frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError(
                f"expected a BGR frame with three channels, got shape "
                f"{getattr(frame, 'shape', None)}"
            )

        model = self._load()
        source_h, source_w = frame.shape[:2]
        scale = self._scale_for(source_w, source_h)

        if scale < 1.0:
            work_w = max(1, int(round(source_w * scale)))
            work_h = max(1, int(round(source_h * scale)))
            work = cv2.resize(frame, (work_w, work_h),
                              interpolation=cv2.INTER_AREA)
        else:
            work_w, work_h = source_w, source_h
            work = frame

        if (work_w, work_h) != self._input_size:
            model.setInputSize((work_w, work_h))
            self._input_size = (work_w, work_h)

        # YuNet requires a contiguous uint8 buffer. A frame that has been
        # sliced (a crop, a flip) is a view with a stride the C++ side reads
        # as garbage rather than rejecting.
        if not work.flags["C_CONTIGUOUS"]:
            work = np.ascontiguousarray(work)

        _count, raw = model.detect(work)
        if raw is None or len(raw) == 0:
            return ()

        inverse = 1.0 / scale if scale else 1.0
        found: list[Detection] = []
        for row in raw:
            if len(row) < _ROW_WIDTH:                       # pragma: no cover
                continue
            x, y, w, h = (float(v) * inverse for v in row[:4])
            if w <= 0 or h <= 0:
                continue
            # Clamp into the frame. YuNet legitimately returns boxes that hang
            # off the edge for a partially visible face, and a negative origin
            # becomes a negative crop offset four layers downstream.
            x0 = max(0.0, min(x, source_w - 1.0))
            y0 = max(0.0, min(y, source_h - 1.0))
            w = min(w - (x0 - x), source_w - x0)
            h = min(h - (y0 - y), source_h - y0)
            if w <= 1 or h <= 1:
                continue
            found.append(Detection(
                box=Box(x0, y0, w, h),
                confidence=float(row[_SCORE]),
                landmarks=_landmarks(row, inverse),
            ))

        found.sort(key=lambda d: d.confidence, reverse=True)
        return tuple(found)


def _landmarks(row: np.ndarray, inverse: float) -> Landmarks | None:
    try:
        points = [
            (float(row[_LANDMARK_START + i * 2]) * inverse,
             float(row[_LANDMARK_START + i * 2 + 1]) * inverse)
            for i in range(5)
        ]
    except (IndexError, ValueError):                        # pragma: no cover
        return None
    return Landmarks(
        right_eye=points[0], left_eye=points[1], nose=points[2],
        right_mouth=points[3], left_mouth=points[4],
    )
