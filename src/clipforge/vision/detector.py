"""The detector contract.

One method — `detect(frame) -> tuple[Detection, ...]` — over a BGR numpy array
in source resolution. Detectors do not decode video, do not sample, do not
track and do not downscale: that work is identical whichever model runs, so a
second detector is an adapter rather than a second pipeline.

A detector is also asked whether it can run *before* a job is queued, for the
same reason the transcription providers are: "no model on disk" and "the model
found nothing" are different answers, and telling them apart from a stack trace
at three in the morning is avoidable.
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

import numpy as np

from .types import Availability, Detection, DetectorInfo

__all__ = ["FaceDetector"]


@runtime_checkable
class FaceDetector(Protocol):
    """Faces in a single frame.

    Implementations must return boxes in the coordinate space of the frame
    they were handed, and must not invent landmarks. `None` is the correct
    answer for a model that does not produce them — the tracker degrades to
    box-only association and says so, which is better than five points of
    fiction that the activity scorer would then read as a moving mouth.
    """

    @property
    def info(self) -> DetectorInfo: ...

    def availability(self) -> Availability:
        """Can this run? Called before work is queued, not after it fails."""

    def detect(self, frame: np.ndarray) -> tuple[Detection, ...]:
        """Find every face in one BGR frame, strongest first."""

    def close(self) -> None:
        """Release the model. Safe to call twice."""
