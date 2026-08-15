"""Builds real video files with faces in known places.

Every fixture here is an actual H.264 MP4, encoded by ffmpeg from raw frames
and decoded back through OpenCV by the code under test. Nothing is mocked: the
detector sees pixels that went through a real codec, with the compression
artefacts that implies.

## Why the videos are constructed rather than filmed

Because the interesting assertions are about *where* a face is and *when* it is
gone, and a stock clip of two people talking supplies neither. Hand-labelling
one gives approximate boxes on a handful of frames; constructing one gives the
exact centre of every face in every frame, plus the ability to hide a face for
precisely 1.2 seconds and see whether the track survives it.

The trade is that a rendered face is easier than a filmed one. That is why
`face_astronaut.jpg` — a real NASA photograph — is the sprite for the primary
speaker in most scenarios, so the detector is looking at real skin, real
shading and real hair, moved along a path this module controls. See
`README.md` for provenance.

## Mouth motion is simulated, and that is the point

The active-speaker signal in `vision.tracking` measures pixel change in the
mouth region. To test it, a face has to have a mouth that moves. `talking`
windows warp the mouth region of the sprite vertically, frame by frame, which
produces genuine motion of genuine pixels in exactly the region the signal
reads. It is a jaw, not a phoneme; it is enough to distinguish a talking face
from a still one, which is all the signal itself claims to do.
"""

from __future__ import annotations

import math
import os
import subprocess
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

FIXTURE_DIR = Path(__file__).resolve().parent
REAL_FACE = FIXTURE_DIR / "face_astronaut.jpg"

FFMPEG = os.environ.get("CLIPFORGE_FFMPEG", "") or shutil.which("ffmpeg") or ""

#: Deliberately not 1920x1080. `SpeakerTrack` defaults to that, so a fixture at
#: that size would let a track that never read the file pass every geometry
#: assertion in this suite.
DEFAULT_SIZE = (1280, 720)
DEFAULT_FPS = 30


# ---------------------------------------------------------------------------
# Sprites
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sprite:
    """A face image, plus where the face actually is inside it."""

    image: np.ndarray
    #: (x, y, w, h) of the face within `image`.
    face: tuple[float, float, float, float]
    #: Mouth centre within `image`, and roughly how wide the mouth region is.
    mouth: tuple[float, float]
    mouth_span: float

    @property
    def face_height(self) -> float:
        return self.face[3]

    @property
    def face_centre(self) -> tuple[float, float]:
        x, y, w, h = self.face
        return (x + w / 2.0, y + h / 2.0)


def _detect_once(image: np.ndarray):
    """Locate the face and mouth inside a sprite, using the real detector.

    The fixtures ask the same model the tests exercise where the face is. That
    is circular for "is there a face here" and exactly right for everything
    else: the ground truth this module publishes is about *where a known face
    was placed on the canvas*, and that needs the sprite's own geometry to be
    measured rather than assumed.
    """

    import sys

    sys.path.insert(0, str(FIXTURE_DIR.parent.parent / "src"))
    from clipforge.vision.yunet import YuNetDetector

    with YuNetDetector() as detector:
        found = detector.detect(image)
    if not found:
        raise RuntimeError(
            "the bundled face fixture no longer detects — the image or the "
            "model changed"
        )
    return found[0]


def real_sprite() -> Sprite:
    """The photograph, measured."""
    import cv2

    image = cv2.imread(str(REAL_FACE))
    if image is None:
        raise RuntimeError(f"missing fixture {REAL_FACE}")
    detection = _detect_once(image)
    box = detection.box
    marks = detection.landmarks
    mouth = marks.mouth_centre if marks else (box.cx, box.y + box.height * 0.72)
    span = marks.eye_distance if marks else box.width * 0.4
    return Sprite(
        image=image,
        face=(box.x, box.y, box.width, box.height),
        mouth=mouth,
        mouth_span=span,
    )


def drawn_sprite(
    size: int = 256,
    skin: tuple[int, int, int] = (140, 172, 208),
    hair: tuple[int, int, int] = (38, 42, 58),
    seed: int = 0,
) -> Sprite:
    """A face drawn from primitives, for the second person in a two-shot.

    Visibly a different person from the photograph, so a two-speaker fixture
    is two people rather than one person twice.

    Getting this detected reliably took more than an oval with two dots. The
    first version scored 0.61-0.72 — straddling the 0.6 threshold, so it
    flickered in and out between frames and the tracker saw a person who kept
    leaving. Three things fixed it, and they are the three a detector is
    actually looking for: **radial shading**, so the face reads as a rounded
    surface rather than a flat patch of colour; **brow bars and lid shadows**,
    which are most of what an eye looks like at 30px; and a **neck and
    shoulders**, which give the head an outline to sit on. With those it scores
    0.92 at every scale these fixtures use.

    That is worth knowing beyond this file: it is also the difference between
    footage this detector handles and footage it does not.
    """

    import cv2

    rng = np.random.default_rng(seed)
    img = np.full((size, size, 3), 196, np.uint8)
    cx = cy = size // 2
    fh = int(size * 0.66)
    fw = int(fh * 0.74)

    # Neck and shoulders first — everything else is drawn over them.
    cv2.rectangle(img, (cx - int(fw * 0.20), cy + int(fh * 0.28)),
                  (cx + int(fw * 0.20), size),
                  tuple(int(c * 0.86) for c in skin), -1)
    cv2.ellipse(img, (cx, size), (int(fw * 0.95), int(fh * 0.34)),
                0, 180, 360, (96, 84, 74), -1)

    cv2.ellipse(img, (cx, cy), (fw // 2, fh // 2), 0, 0, 360, skin, -1)

    # Radial falloff, applied only inside the head.
    grid_y, grid_x = np.mgrid[0:size, 0:size]
    radius = np.sqrt(
        ((grid_x - cx) / (fw / 2)) ** 2 + ((grid_y - cy) / (fh / 2)) ** 2
    )
    shade = np.clip(1.06 - 0.30 * np.clip(radius, 0, 1.4) ** 2, 0.62, 1.06)
    inside = (radius <= 1.02)[:, :, None]
    img = np.where(
        inside, np.clip(img * shade[:, :, None], 0, 255), img
    ).astype(np.uint8)

    cv2.ellipse(img, (cx, cy - int(fh * 0.28)),
                (int(fw * 0.53), int(fh * 0.28)), 0, 185, 355, hair, -1)

    eye_w = int(fw * 0.135)
    eye_y = cy - int(fh * 0.07)
    eye_x = int(fw * 0.215)
    for side in (-1, 1):
        centre = (cx + side * eye_x, eye_y)
        cv2.ellipse(img, (centre[0], eye_y - int(eye_w * 1.05)),
                    (int(eye_w * 1.2), int(eye_w * 0.30)), 0, 180, 360, hair, -1)
        cv2.ellipse(img, centre, (eye_w, int(eye_w * 0.52)), 0, 0, 360,
                    (238, 240, 244), -1)
        cv2.circle(img, centre, int(eye_w * 0.46), (72, 56, 42), -1)
        cv2.circle(img, centre, int(eye_w * 0.21), (12, 12, 14), -1)
        cv2.ellipse(img, (centre[0], eye_y - int(eye_w * 0.30)),
                    (eye_w, int(eye_w * 0.34)), 0, 180, 360,
                    tuple(int(c * 0.72) for c in skin), -1)

    nose_y = cy + int(fh * 0.08)
    cv2.ellipse(img, (cx, nose_y), (int(fw * 0.085), int(fh * 0.055)), 0, 0, 360,
                tuple(int(c * 0.88) for c in skin), -1)
    cv2.ellipse(img, (cx, nose_y + int(fh * 0.012)),
                (int(fw * 0.10), int(fh * 0.03)), 0, 0, 180,
                tuple(int(c * 0.70) for c in skin), -1)

    mouth_y = cy + int(fh * 0.225)
    cv2.ellipse(img, (cx, mouth_y), (int(fw * 0.21), int(fh * 0.052)),
                0, 0, 360, (92, 86, 140), -1)
    cv2.ellipse(img, (cx, mouth_y - int(fh * 0.012)),
                (int(fw * 0.20), int(fh * 0.028)), 0, 0, 180, (70, 64, 116), -1)
    cv2.line(img, (cx - int(fw * 0.19), mouth_y), (cx + int(fw * 0.19), mouth_y),
             (56, 50, 92), max(1, size // 170))

    img = cv2.GaussianBlur(img, (5, 5), 0)
    img = np.clip(
        img.astype(np.float32) + rng.normal(0, 3.5, img.shape), 0, 255
    ).astype(np.uint8)

    detection = _detect_once(img)
    box = detection.box
    return Sprite(
        image=img,
        face=(box.x, box.y, box.width, box.height),
        mouth=(float(cx), float(mouth_y)),
        mouth_span=float(eye_x * 2),
    )


def _talking(sprite: Sprite, phase: float) -> np.ndarray:
    """A copy of the sprite with the mouth region stretched by `phase`.

    `phase` in [0, 1]; 0 is the sprite untouched. The warp is confined to a box
    around the mouth so nothing else in the frame moves — if the whole face
    moved, the activity signal would be measuring the head, which is the thing
    it is specifically meant not to do.
    """

    import cv2

    if phase <= 0.01:
        return sprite.image

    out = sprite.image.copy()
    mx, my = sprite.mouth
    half_w = max(6, int(sprite.mouth_span * 0.62))
    half_h = max(5, int(sprite.mouth_span * 0.48))
    x0, x1 = int(max(0, mx - half_w)), int(min(out.shape[1], mx + half_w))
    y0, y1 = int(max(0, my - half_h)), int(min(out.shape[0], my + half_h))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return out

    patch = out[y0:y1, x0:x1]
    height = y1 - y0
    # Squeeze vertically and pad, which reads as a jaw dropping rather than as
    # the mouth sliding down the face.
    squeezed = max(3, int(height * (1.0 - 0.45 * phase)))
    small = cv2.resize(patch, (x1 - x0, squeezed), interpolation=cv2.INTER_AREA)
    grown = cv2.resize(small, (x1 - x0, height), interpolation=cv2.INTER_LINEAR)
    shift = int(half_h * 0.35 * phase)
    dark = np.clip(grown.astype(np.int16) - int(38 * phase), 0, 255).astype(np.uint8)
    out[y0:y1, x0:x1] = dark
    if shift:
        out[y0 + shift:y1, x0:x1] = dark[: height - shift]
    return out


# ---------------------------------------------------------------------------
# Scenario description
# ---------------------------------------------------------------------------

Window = tuple[float, float]


def _inside(windows: Sequence[Window], t: float) -> bool:
    return any(start <= t < end for start, end in windows)


@dataclass
class Actor:
    """One person in a fixture, and everything they do."""

    name: str
    sprite: Sprite
    #: Face centre on the canvas at time `t`, in pixels.
    path: Callable[[float], tuple[float, float]]
    #: Face *height* on the canvas, in pixels. Sets the sprite's scale.
    face_height: float = 190.0
    #: When the actor is in the video at all. Empty means always.
    visible: tuple[Window, ...] = ()
    #: When their mouth is moving.
    talking: tuple[Window, ...] = ()
    talk_hz: float = 4.2

    def present(self, t: float) -> bool:
        return _inside(self.visible, t) if self.visible else True

    def phase(self, t: float) -> float:
        if not _inside(self.talking, t):
            return 0.0
        return 0.5 + 0.5 * math.sin(2 * math.pi * self.talk_hz * t)


@dataclass
class Occluder:
    """A solid shape that passes in front of the actors."""

    #: Centre-x of the bar at time `t`, in pixels.
    path: Callable[[float], float]
    width: float = 260.0
    windows: tuple[Window, ...] = ()
    colour: tuple[int, int, int] = (48, 52, 60)

    def active(self, t: float) -> bool:
        return _inside(self.windows, t) if self.windows else True


@dataclass
class Scenario:
    """A fixture: what to render, and what should be true about it."""

    name: str
    actors: tuple[Actor, ...] = ()
    occluders: tuple[Occluder, ...] = ()
    duration_s: float = 6.0
    size: tuple[int, int] = DEFAULT_SIZE
    fps: int = DEFAULT_FPS
    background: str = "studio"

    def expected(self, t: float) -> dict[str, tuple[float, float]]:
        """Where each actor's face centre should be at `t`.

        Only actors that are present *and* not behind an occluder, because the
        occlusion tests assert that the detector finds nothing there.
        """

        out: dict[str, tuple[float, float]] = {}
        for actor in self.actors:
            if not actor.present(t):
                continue
            cx, cy = actor.path(t)
            if self._hidden(t, cx):
                continue
            out[actor.name] = (cx, cy)
        return out

    def _hidden(self, t: float, cx: float) -> bool:
        for occluder in self.occluders:
            if not occluder.active(t):
                continue
            if abs(occluder.path(t) - cx) < occluder.width * 0.42:
                return True
        return False


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _background(kind: str, width: int, height: int, t: float) -> np.ndarray:
    """A non-flat background, because a flat one is not a fair test.

    A face on plain grey is easier to detect than a face in a room, and a
    detector validated only against plain grey would look better than it is.
    This is not a room, but it has texture, a gradient and moving content.
    """

    import cv2

    frame = np.zeros((height, width, 3), np.uint8)
    gradient = np.linspace(38, 96, height, dtype=np.uint8)[:, None]
    frame[:, :, 0] = gradient + 14
    frame[:, :, 1] = gradient
    frame[:, :, 2] = gradient - 8

    if kind == "shapes":
        # Stands in for a gameplay bed: plenty of motion, nothing face-shaped.
        #
        # The first version of this was per-frame random noise, which is
        # correct as content and terrible as a fixture — noise is
        # incompressible, so a four-second clip encoded to 13 MB. Moving solid
        # shapes give the detector just as little to find and encode to 40 KB.
        for index in range(6):
            phase = t * (0.6 + index * 0.21) + index
            x = int((math.sin(phase) * 0.5 + 0.5) * (width - 160)) + 40
            y = int((math.cos(phase * 0.8) * 0.5 + 0.5) * (height - 160)) + 40
            shade = (60 + index * 24, 130 - index * 12, 190 - index * 20)
            if index % 2:
                cv2.rectangle(frame, (x - 70, y - 50), (x + 70, y + 50), shade, -1)
            else:
                cv2.circle(frame, (x, y), 62, shade, -1)
        return cv2.GaussianBlur(frame, (7, 7), 0)

    step = max(48, width // 14)
    for x in range(0, width, step):
        shade = 58 + (x // step % 3) * 9
        cv2.rectangle(frame, (x, 0), (x + step // 2, height), (shade, shade - 6, shade - 12), -1)
    drift = int((math.sin(t * 0.7) * 0.5 + 0.5) * width * 0.6)
    cv2.circle(frame, (drift, int(height * 0.22)), int(height * 0.16),
               (74, 70, 64), -1)
    return cv2.GaussianBlur(frame, (21, 21), 0)


def _paste(canvas: np.ndarray, sprite_img: np.ndarray, sprite: Sprite,
           centre: tuple[float, float], face_height: float) -> None:
    """Composite a sprite so its *face* lands centred on `centre`."""

    import cv2

    scale = face_height / max(1.0, sprite.face_height)
    sh, sw = sprite_img.shape[:2]
    tw, th = max(8, int(round(sw * scale))), max(8, int(round(sh * scale)))
    scaled = cv2.resize(sprite_img, (tw, th), interpolation=cv2.INTER_LINEAR)

    fcx, fcy = sprite.face_centre
    ox = int(round(centre[0] - fcx * scale))
    oy = int(round(centre[1] - fcy * scale))

    # Feathered ellipse so the head does not arrive as a rectangle.
    mask = np.zeros((th, tw), np.float32)
    cv2.ellipse(mask, (tw // 2, th // 2), (int(tw * 0.46), int(th * 0.48)),
                0, 0, 360, 1.0, -1)
    mask = cv2.GaussianBlur(mask, (0, 0), max(1.5, tw * 0.02))[:, :, None]

    x0, y0 = max(0, ox), max(0, oy)
    x1, y1 = min(canvas.shape[1], ox + tw), min(canvas.shape[0], oy + th)
    if x1 <= x0 or y1 <= y0:
        return
    sx0, sy0 = x0 - ox, y0 - oy
    sub = scaled[sy0:sy0 + (y1 - y0), sx0:sx0 + (x1 - x0)]
    sub_mask = mask[sy0:sy0 + (y1 - y0), sx0:sx0 + (x1 - x0)]
    region = canvas[y0:y1, x0:x1]
    canvas[y0:y1, x0:x1] = (
        region * (1.0 - sub_mask) + sub * sub_mask
    ).astype(np.uint8)


def render(scenario: Scenario, path: str) -> str:
    """Encode the scenario to an H.264 MP4 at `path`."""

    import cv2

    if not FFMPEG:
        raise RuntimeError("building video fixtures needs ffmpeg")

    width, height = scenario.size
    frames = max(1, int(round(scenario.duration_s * scenario.fps)))
    argv = [
        FFMPEG, "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}", "-r", str(scenario.fps),
        "-i", "-",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", path,
    ]
    process = subprocess.Popen(argv, stdin=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    assert process.stdin is not None
    try:
        for index in range(frames):
            t = index / scenario.fps
            canvas = _background(scenario.background, width, height, t)
            for actor in scenario.actors:
                if not actor.present(t):
                    continue
                image = _talking(actor.sprite, actor.phase(t))
                _paste(canvas, image, actor.sprite, actor.path(t),
                       actor.face_height)
            for occluder in scenario.occluders:
                if not occluder.active(t):
                    continue
                cx = occluder.path(t)
                half = occluder.width / 2.0
                cv2.rectangle(
                    canvas,
                    (int(cx - half), 0), (int(cx + half), height),
                    occluder.colour, -1,
                )
            process.stdin.write(canvas.tobytes())
    finally:
        process.stdin.close()
        stderr = b""
        if process.stderr is not None:
            stderr = process.stderr.read()
            process.stderr.close()
        code = process.wait()
    if code != 0:
        raise RuntimeError(f"ffmpeg failed ({code}): {stderr.decode()[:400]}")
    return path


# ---------------------------------------------------------------------------
# The scenarios
# ---------------------------------------------------------------------------


def single_speaker(duration_s: float = 6.0) -> Scenario:
    """One person, talking, swaying gently. The base case."""
    sprite = real_sprite()
    width, height = DEFAULT_SIZE
    return Scenario(
        name="single_speaker",
        duration_s=duration_s,
        actors=(Actor(
            name="host",
            sprite=sprite,
            path=lambda t: (width * 0.42 + math.sin(t * 0.9) * 26.0,
                            height * 0.44 + math.sin(t * 1.4) * 12.0),
            face_height=210.0,
            talking=((0.4, duration_s),),
        ),),
    )


def two_speakers(duration_s: float = 8.0) -> Scenario:
    """Two people, taking turns. Only one mouth moves at a time."""
    width, height = DEFAULT_SIZE
    return Scenario(
        name="two_speakers",
        duration_s=duration_s,
        actors=(
            Actor(
                name="left",
                sprite=real_sprite(),
                path=lambda t: (width * 0.26, height * 0.46),
                face_height=190.0,
                talking=((0.3, 3.4), (6.2, duration_s)),
            ),
            Actor(
                name="right",
                sprite=drawn_sprite(seed=7),
                path=lambda t: (width * 0.74, height * 0.46),
                face_height=190.0,
                talking=((3.8, 6.0),),
            ),
        ),
    )


def enter_exit(duration_s: float = 7.0) -> Scenario:
    """A face that is not there, arrives, crosses, and leaves.

    The arrival and departure are off-canvas rather than a pop-in, because a
    face appearing instantly at full size is a cut, and a tracker can get that
    right while still mishandling somebody walking into shot.
    """

    width, height = DEFAULT_SIZE

    def walk(t: float) -> tuple[float, float]:
        # Enters from the right at 1.5s, reaches the left edge by 5.0s.
        progress = max(0.0, min(1.0, (t - 1.5) / 3.5))
        return (width * 1.12 - progress * width * 1.05, height * 0.46)

    return Scenario(
        name="enter_exit",
        duration_s=duration_s,
        actors=(Actor(
            name="walker",
            sprite=real_sprite(),
            path=walk,
            face_height=185.0,
            visible=((1.2, 5.6),),
            talking=((1.8, 5.0),),
        ),),
    )


def occlusion(duration_s: float = 7.0) -> Scenario:
    """A stationary talker, hidden by a pillar sweeping across the middle.

    The pillar fully covers the face for roughly a second — long enough that a
    tracker with no tolerance loses the person and gives their replacement a
    new id, which the camera would read as a new speaker and cut to.
    """

    width, height = DEFAULT_SIZE
    face_x = width * 0.44

    return Scenario(
        name="occlusion",
        duration_s=duration_s,
        actors=(Actor(
            name="host",
            sprite=real_sprite(),
            path=lambda t: (face_x, height * 0.45),
            face_height=205.0,
            talking=((0.3, duration_s),),
        ),),
        occluders=(Occluder(
            # Crosses the face between about 2.6s and 3.6s.
            path=lambda t: width * 1.15 - t * width * 0.24,
            width=300.0,
            windows=((0.0, duration_s),),
        ),),
    )


def no_faces(duration_s: float = 4.0) -> Scenario:
    """Moving content with nobody in it — a gameplay bed or a screencast."""
    return Scenario(
        name="no_faces",
        duration_s=duration_s,
        background="shapes",
        actors=(),
    )


def small_face(duration_s: float = 4.0) -> Scenario:
    """A face far below the detector's size floor.

    Not a failure case to be fixed — a documented limit, asserted so that a
    change to `MIN_FACE_FRACTION` or `max_side` shows up here rather than as a
    mysterious improvement in some other test.
    """

    width, height = DEFAULT_SIZE
    return Scenario(
        name="small_face",
        duration_s=duration_s,
        actors=(Actor(
            name="distant",
            sprite=real_sprite(),
            path=lambda t: (width * 0.5, height * 0.5),
            face_height=14.0,
        ),),
    )


ALL_SCENARIOS = {
    "single_speaker": single_speaker,
    "two_speakers": two_speakers,
    "enter_exit": enter_exit,
    "occlusion": occlusion,
    "no_faces": no_faces,
    "small_face": small_face,
}


class FixtureCache:
    """Builds each fixture once per process.

    Encoding six short videos costs a few seconds. Rebuilding them per test
    method costs a few minutes, which is the difference between a suite people
    run and one they skip.
    """

    def __init__(self, directory: str) -> None:
        self.directory = directory
        self._built: dict[str, tuple[str, Scenario]] = {}

    def get(self, name: str) -> tuple[str, Scenario]:
        if name not in self._built:
            scenario = ALL_SCENARIOS[name]()
            path = os.path.join(self.directory, f"{name}.mp4")
            render(scenario, path)
            self._built[name] = (path, scenario)
        return self._built[name]
