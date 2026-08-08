"""Vertical (9:16) composition planning.

Stream clips cannot be centre-cropped. A 16:9 gameplay frame with a facecam in
one corner, cropped to 9:16, throws away either the gameplay or the streamer —
and the streamer's reaction is usually the reason the clip works. So the
planner composes: facecam stacked above cropped gameplay, which is the format
the entire stream-clip genre uses.

Output is a declarative spec, not pixels. It is deterministic and hashable, so
the render layer caches on it exactly as the architecture's edit decision list
does.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from .types import Crop, VerticalLayout, VideoRegion

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920


class Destination(str, enum.Enum):
    TIKTOK = "tiktok"
    SHORTS = "youtube_shorts"
    REELS = "instagram_reels"


@dataclass(frozen=True, slots=True)
class SafeZone:
    """Fractions of the frame covered by each platform's own UI chrome.

    Captions placed inside these bands get hidden behind the like button, the
    caption text, or the progress bar. This is the detail that separates a
    real product from a demo: it is invisible in preview and wrong on every
    single published clip.
    """

    top: float
    bottom: float
    left: float
    right: float


SAFE_ZONES: dict[Destination, SafeZone] = {
    # Right rail of action buttons, caption block and progress bar at the base.
    Destination.TIKTOK: SafeZone(top=0.06, bottom=0.15, left=0.03, right=0.12),
    # Lighter chrome than TikTok; title overlays the top.
    Destination.SHORTS: SafeZone(top=0.10, bottom=0.12, left=0.03, right=0.10),
    # Heaviest bottom chrome of the three.
    Destination.REELS: SafeZone(top=0.06, bottom=0.20, left=0.03, right=0.13),
}


class LayoutStyle(str, enum.Enum):
    FACECAM_OVER_GAMEPLAY = "facecam_over_gameplay"
    GAMEPLAY_ONLY = "gameplay_only"
    FACECAM_ONLY = "facecam_only"
    FULL_FRAME = "full_frame"


# Share of frame height given to the facecam in the stacked layout. Enough for
# the reaction to read at phone size without starving the gameplay.
FACECAM_BAND = 0.36
MIN_FACECAM_BAND = 0.22
MAX_FACECAM_BAND = 0.45


def cover_crop(region: VideoRegion, dest_width: int, dest_height: int) -> VideoRegion:
    """Largest centred sub-rectangle of `region` matching the destination aspect.

    Cover-fit rather than contain-fit: filling the band and losing some edge
    pixels beats letterboxing, which wastes the scarcest resource in vertical
    video, which is height.
    """
    if region.height <= 0 or dest_height <= 0:
        return region

    target = dest_width / dest_height
    source = region.width / region.height

    if source > target:
        # Source is too wide — trim the sides.
        width = max(1, int(round(region.height * target)))
        height = region.height
        x = region.x + (region.width - width) // 2
        y = region.y
    else:
        # Source is too tall — trim top and bottom.
        width = region.width
        height = max(1, int(round(region.width / target)))
        x = region.x
        y = region.y + (region.height - height) // 2

    return VideoRegion(name=region.name, x=x, y=y, width=width, height=height)


def _caption_zone(destination: Destination) -> tuple[int, int, int, int]:
    """The band captions may occupy, given the destination's chrome.

    Sits in the lower third — where viewers look — but strictly above the
    bottom safe inset.
    """
    safe = SAFE_ZONES[destination]
    left = int(OUTPUT_WIDTH * safe.left)
    right = int(OUTPUT_WIDTH * (1.0 - safe.right))
    width = max(1, right - left)

    bottom_limit = int(OUTPUT_HEIGHT * (1.0 - safe.bottom))
    height = int(OUTPUT_HEIGHT * 0.16)
    y = max(int(OUTPUT_HEIGHT * safe.top), bottom_limit - height)
    return left, y, width, height


def _full_frame(session_width: int, session_height: int) -> VideoRegion:
    return VideoRegion("fullscreen", 0, 0, session_width, session_height)


def plan(
    regions: tuple[VideoRegion, ...],
    source_width: int,
    source_height: int,
    destination: Destination = Destination.TIKTOK,
    style: LayoutStyle | None = None,
    include_chat: bool = False,
) -> VerticalLayout:
    """Build a 9:16 composition plan.

    `style` is inferred from the available regions when not given: a facecam
    means the stacked layout, no facecam means a gameplay crop.
    """
    by_name = {r.name: r for r in regions}
    facecam = by_name.get("facecam")
    gameplay = by_name.get("gameplay") or _full_frame(source_width, source_height)

    if style is None:
        style = (
            LayoutStyle.FACECAM_OVER_GAMEPLAY if facecam else LayoutStyle.GAMEPLAY_ONLY
        )
    if style is LayoutStyle.FACECAM_OVER_GAMEPLAY and facecam is None:
        # Asked for the stacked layout with no facecam to stack. Degrade rather
        # than raise: a slightly wrong layout still ships a clip.
        style = LayoutStyle.GAMEPLAY_ONLY

    caption = _caption_zone(destination)
    chat_overlay = None
    crops: list[Crop] = []

    if style is LayoutStyle.FACECAM_OVER_GAMEPLAY:
        assert facecam is not None
        # Size the facecam band to its natural aspect so the streamer is not
        # stretched, clamped so it can never dominate or vanish.
        natural = OUTPUT_WIDTH / facecam.aspect if facecam.aspect else OUTPUT_HEIGHT
        band = int(
            min(
                max(natural, OUTPUT_HEIGHT * MIN_FACECAM_BAND),
                OUTPUT_HEIGHT * MAX_FACECAM_BAND,
            )
        )
        crops.append(
            Crop(
                source=cover_crop(facecam, OUTPUT_WIDTH, band),
                dest_x=0,
                dest_y=0,
                dest_width=OUTPUT_WIDTH,
                dest_height=band,
            )
        )
        remaining = OUTPUT_HEIGHT - band
        crops.append(
            Crop(
                source=cover_crop(gameplay, OUTPUT_WIDTH, remaining),
                dest_x=0,
                dest_y=band,
                dest_width=OUTPUT_WIDTH,
                dest_height=remaining,
            )
        )

    elif style is LayoutStyle.FACECAM_ONLY and facecam is not None:
        crops.append(
            Crop(
                source=cover_crop(facecam, OUTPUT_WIDTH, OUTPUT_HEIGHT),
                dest_x=0,
                dest_y=0,
                dest_width=OUTPUT_WIDTH,
                dest_height=OUTPUT_HEIGHT,
            )
        )

    else:
        source = gameplay if style is LayoutStyle.GAMEPLAY_ONLY else _full_frame(
            source_width, source_height
        )
        crops.append(
            Crop(
                source=cover_crop(source, OUTPUT_WIDTH, OUTPUT_HEIGHT),
                dest_x=0,
                dest_y=0,
                dest_width=OUTPUT_WIDTH,
                dest_height=OUTPUT_HEIGHT,
            )
        )

    if include_chat:
        # Chat sits directly above the caption band. Showing chat react is a
        # large part of why stream clips land — the crowd is the punchline.
        height = int(OUTPUT_HEIGHT * 0.14)
        chat_overlay = (
            caption[0],
            max(0, caption[1] - height - 16),
            caption[2],
            height,
        )

    return VerticalLayout(
        name=style.value,
        width=OUTPUT_WIDTH,
        height=OUTPUT_HEIGHT,
        crops=tuple(crops),
        background="blurred_source",
        caption_zone=caption,
        chat_overlay=chat_overlay,
    )
