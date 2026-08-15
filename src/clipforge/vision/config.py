"""Configuration, model resolution and device selection.

```sh
CLIPFORGE_FACE_DEVICE=auto        # auto | cpu | cuda | opencl
CLIPFORGE_FACE_SAMPLE_FPS=10      # detector rate, not video rate
CLIPFORGE_FACE_MIN_CONFIDENCE=0.6
CLIPFORGE_FACE_MAX_SIDE=640       # detect on a downscale this big
CLIPFORGE_FACE_MODEL=/path/to.onnx
```

## Why detection does not run at video rate

A 60-second 1080p clip at 30fps is 1800 frames. Detecting on all of them buys
nothing: faces do not move meaningfully between consecutive frames, the camera
solver smooths and deadbands whatever it is given, and `SpeakerTrack` already
describes itself as arriving at around 10fps. Sampling at 10fps is a 3x
reduction in decode-and-detect work for a track the camera cannot tell apart.

## Why detection runs on a downscale

YuNet is trained around small inputs and detects a 1080p face perfectly well at
640px wide. The boxes are scaled back up afterwards, so the track is still in
source pixels — which is what the camera needs, since its crop rectangle is in
source pixels too.

The one thing this costs is very small faces: a face 40px tall in a 1920-wide
frame is 13px tall at 640, which is under YuNet's floor. That is a real limit
and `MIN_FACE_FRACTION` is where it is written down.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .types import DetectorUnavailable

__all__ = [
    "FaceDetectionConfig",
    "config_from_env",
    "resolve_model",
    "BUNDLED_MODEL",
    "MIN_FACE_FRACTION",
]

#: Shipped alongside this package — see `models/NOTICE.md`.
BUNDLED_MODEL = "face_detection_yunet_2023mar.onnx"

#: A face shorter than this fraction of the frame height is below what the
#: downscaled detector can see. Not a preference — a measurement of the input
#: size the model runs at.
MIN_FACE_FRACTION = 0.035


@dataclass(frozen=True, slots=True)
class FaceDetectionConfig:
    """Everything the detector and tracker read.

    Frozen and passed explicitly rather than read from the environment deep in
    the call stack, so a test configures a detector by constructing one rather
    than by mutating `os.environ` and hoping nothing else in the process
    noticed.
    """

    model_path: str = ""
    device: str = "auto"
    sample_fps: float = 10.0
    min_confidence: float = 0.6
    #: Non-maximum suppression threshold, passed to YuNet.
    nms_threshold: float = 0.3
    max_side: int = 640
    top_k: int = 50

    # -- tracking ----------------------------------------------------------

    #: Minimum IoU for a detection to continue an existing track.
    min_iou: float = 0.25
    #: A detection whose centre is within this many face-widths of a track's
    #: last centre may continue it even at zero IoU. Fast head movement at
    #: 10fps routinely produces non-overlapping boxes for the same person.
    max_centre_drift: float = 1.6
    #: Sampled frames a track survives without a detection before it ends.
    #: At 10fps this is 1.2s, which covers a head turn or someone walking in
    #: front, and is short enough that a person who left does not hold an id
    #: that the next arrival then inherits.
    max_age: int = 12
    #: Hits before a track is believed and its buffered samples are emitted.
    min_hits: int = 2


def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"CLIPFORGE_FACE_{name}", default).strip()


def _float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def config_from_env() -> FaceDetectionConfig:
    """Build a config from the environment, falling back to the defaults.

    A malformed number is ignored rather than fatal. The alternative is a
    render pipeline that refuses to start because someone typed
    `SAMPLE_FPS=ten`, and a detector running at its default rate is a much
    better outcome than a channel that stops publishing.
    """

    return FaceDetectionConfig(
        model_path=_env("MODEL"),
        device=(_env("DEVICE", "auto") or "auto").lower(),
        sample_fps=max(0.5, _float("SAMPLE_FPS", 10.0)),
        min_confidence=min(1.0, max(0.05, _float("MIN_CONFIDENCE", 0.6))),
        nms_threshold=_float("NMS_THRESHOLD", 0.3),
        max_side=max(160, _int("MAX_SIDE", 640)),
        top_k=max(1, _int("TOP_K", 50)),
    )


def resolve_model(config: FaceDetectionConfig | None = None) -> str:
    """Find the ONNX weights, or say precisely what is missing.

    Order: an explicit path, then the environment, then the copy shipped
    inside this package. The explicit path wins so a deployment can pin a
    newer model without a release here.
    """

    candidates: list[tuple[str, str]] = []
    if config is not None and config.model_path:
        candidates.append((config.model_path, "CLIPFORGE_FACE_MODEL / config"))
    env_path = os.environ.get("CLIPFORGE_FACE_MODEL", "").strip()
    if env_path:
        candidates.append((env_path, "CLIPFORGE_FACE_MODEL"))
    candidates.append(
        (str(Path(__file__).parent / "models" / BUNDLED_MODEL), "bundled")
    )

    tried: list[str] = []
    for path, origin in candidates:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
        tried.append(f"{path} ({origin})")

    raise DetectorUnavailable(
        "no face detection model found. Tried: " + "; ".join(tried) + ". The "
        "model normally ships with this package; set CLIPFORGE_FACE_MODEL to "
        "an ONNX face detector if it has been stripped from the install."
    )


def select_device(requested: str = "auto") -> tuple[int, int, str, str]:
    """Pick an OpenCV DNN backend and target.

    Returns `(backend_id, target_id, device_name, note)`, where `device_name`
    is what will actually run — never what was asked for. `note` is empty when
    the request was honoured and carries the reason when it was not.

    A request for CUDA on a box with no CUDA is downgraded rather than raised.
    Refusing would take a working CPU deployment offline over an aspiration in
    a config file, and framing on CPU is slower but identical. The downgrade is
    reported all the way out to `DetectorInfo.device_note`, so nobody has to
    infer from a timing graph that the GPU never engaged.
    """

    try:
        import cv2
    except ImportError as error:                            # pragma: no cover
        raise DetectorUnavailable(
            "opencv-python-headless is not installed"
        ) from error

    dnn = cv2.dnn
    cpu = (dnn.DNN_BACKEND_OPENCV, dnn.DNN_TARGET_CPU)
    requested = (requested or "auto").lower()

    def cuda_ready() -> tuple[bool, str]:
        try:
            count = cv2.cuda.getCudaEnabledDeviceCount()
        except Exception as error:                          # noqa: BLE001
            return False, f"OpenCV has no CUDA module ({type(error).__name__})"
        if count <= 0:
            return False, (
                "this OpenCV build reports no CUDA devices — the usual cause "
                "is the stock opencv-python wheel, which is built without "
                "CUDA regardless of the driver on the host"
            )
        return True, ""

    #: OpenCV 5.0 routes DNN inference through a new graph engine that does not
    #: yet honour a preferred target — it logs "Targets are not supported by
    #: the new graph engine for now" and runs on CPU regardless. A caller who
    #: asked for CUDA and got a `device` of "cuda" on OpenCV 5 is therefore
    #: being told what was *selected*, not necessarily what executed, and that
    #: distinction is the whole contract of this function. So it is carried in
    #: the note rather than left in a log line nobody correlates.
    graph_engine_caveat = ""
    try:
        # int(), not a string compare: "10" < "5" lexically, so a string
        # comparison would silently stop warning at OpenCV 10.
        major = int(cv2.__version__.split(".")[0])
    except (ValueError, IndexError):                        # pragma: no cover
        major = 0
    if major >= 5:
        graph_engine_caveat = (
            f"OpenCV {cv2.__version__} may ignore the target and execute on "
            f"CPU anyway — its new DNN graph engine does not yet support "
            f"non-CPU targets"
        )

    if requested in ("cuda", "gpu"):
        ready, why = cuda_ready()
        if ready:
            return (dnn.DNN_BACKEND_CUDA, dnn.DNN_TARGET_CUDA, "cuda",
                    graph_engine_caveat)
        return (*cpu, "cpu", f"cuda requested but unavailable: {why}")

    if requested == "opencl":
        try:
            if cv2.ocl.haveOpenCL():
                cv2.ocl.setUseOpenCL(True)
                if cv2.ocl.useOpenCL():
                    return (dnn.DNN_BACKEND_OPENCV, dnn.DNN_TARGET_OPENCL,
                            "opencl", "")
        except Exception as error:                          # noqa: BLE001
            return (*cpu, "cpu", f"opencl unavailable: {type(error).__name__}")
        return (*cpu, "cpu", "opencl requested but not available")

    if requested == "cpu":
        return (*cpu, "cpu", "")

    # auto: take CUDA if it is genuinely there, otherwise CPU without
    # complaint. `auto` asking for something it cannot have is not a problem
    # worth a note on every single result.
    ready, _ = cuda_ready()
    if ready:
        return (dnn.DNN_BACKEND_CUDA, dnn.DNN_TARGET_CUDA, "cuda",
                graph_engine_caveat)
    return (*cpu, "cpu", "")
