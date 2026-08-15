"""Face detection and speaker tracking for automatic framing.

The camera solver in `gameplay.camera` was written against a `SpeakerTrack` it
was never given: the factory passed an empty one, so every clip was framed by a
static centred crop and the deadband, the 1€ filter, the slew limit and the
cut-on-speaker-change logic were all unreachable. This package is what produces
the track those parts were built for.

```python
from clipforge.vision import FaceTrackEngine

with FaceTrackEngine() as engine:
    result = engine.track_video("interview.mp4")

if result.ok:
    plan = GameplayEngine().compose(duration_s=59.0, track=result.track)
else:
    log.info("static framing: %s", result.fallback)
```

Nothing here raises for content reasons. A video with no faces, a detector that
will not load, an unexpected failure inside OpenCV — all three come back as a
`FaceTrackResult` whose `track` is empty, whose `fallback` says which happened,
and whose source dimensions are still real. Only a file that cannot be decoded
raises, because that file was never going to render either.
"""

from .config import (
    MIN_FACE_FRACTION,
    FaceDetectionConfig,
    config_from_env,
    resolve_model,
    select_device,
)
from .decode import SampledFrame, VideoInfo, probe_video, sample_frames
from .detector import FaceDetector
from .engine import FaceTrackEngine, track_video
from .tracking import FaceTracker, activity, salience
from .types import (
    Availability,
    Box,
    DecodeError,
    Detection,
    DetectorInfo,
    DetectorUnavailable,
    FaceTrackResult,
    Landmarks,
    PersonSummary,
    PersonTrack,
    TrackState,
    VisionError,
    iou,
)

__all__ = [
    "Availability",
    "Box",
    "DecodeError",
    "Detection",
    "DetectorInfo",
    "DetectorUnavailable",
    "FaceDetectionConfig",
    "FaceDetector",
    "FaceTrackEngine",
    "FaceTrackResult",
    "FaceTracker",
    "Landmarks",
    "MIN_FACE_FRACTION",
    "PersonSummary",
    "PersonTrack",
    "SampledFrame",
    "TrackState",
    "VideoInfo",
    "VisionError",
    "activity",
    "config_from_env",
    "iou",
    "probe_video",
    "resolve_model",
    "salience",
    "sample_frames",
    "select_device",
    "track_video",
]
