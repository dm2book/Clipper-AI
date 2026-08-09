"""Composition of the 1080x1920 canvas.

Two panels share a fixed canvas, and every decision here is about how much of
it the person talking gets. The split is not a style preference: it is the
retention trade the whole format exists to make. Too little speaker and the
face stops reading at phone size; too little gameplay and the bed cannot do
the job it is there for.

Platform safe zones are imported from the stream clipper rather than
redeclared. TikTok's right rail and Reels' bottom band are facts about the
platforms, not about either engine, and two copies of a fact is one copy too
many.
"""

from __future__ import annotations

from .catalog import CROP_SAFE_SPREAD, profile
from ..stream.layout import SAFE_ZONES, Destination
from .types import (
    OUTPUT_HEIGHT,
    OUTPUT_WIDTH,
    Game,
    GameplayAsset,
    LayoutStyle,
    Panel,
    clamp,
    even,
)

#: Share of canvas height given to the speaker, per style. The gameplay panel
#: takes the remainder.
SPEAKER_SHARE: dict[LayoutStyle, float] = {
    LayoutStyle.SPLIT: 0.60,
    LayoutStyle.SPEAKER_DOMINANT: 0.72,
    LayoutStyle.GAMEPLAY_DOMINANT: 0.40,
    LayoutStyle.INSET: 0.34,
    LayoutStyle.SPEAKER_ONLY: 1.0,
}

MIN_SPEAKER_SHARE = 0.34
MAX_SPEAKER_SHARE = 0.80

#: The speaker PIP in the inset layout, as a fraction of canvas width.
INSET_WIDTH = 0.52
INSET_MARGIN = 0.04
INSET_CORNER_RADIUS = 28

#: Height of the caption band, as a fraction of the canvas.
CAPTION_BAND = 0.155


def speaker_share(style: LayoutStyle, game: Game | None) -> float:
    """How much height the speaker gets, adjusted for the bed's salience.

    A high-salience bed is given *less* room, not more. It does not need the
    space to do its job, and every pixel it takes comes out of the only part
    of the frame carrying information.
    """
    share = SPEAKER_SHARE[style]
    if game is None or style in (LayoutStyle.SPEAKER_ONLY, LayoutStyle.INSET):
        return share

    entry = profile(game)
    # `band` is the profile's own recommendation for the gameplay panel.
    blended = (share + (1.0 - entry.band)) / 2.0
    return clamp(blended, MIN_SPEAKER_SHARE, MAX_SPEAKER_SHARE)


def cover_source(
    source_w: int, source_h: int, dest_w: int, dest_h: int,
    center_x: float = 0.5, center_y: float = 0.5,
) -> tuple[int, int, int, int]:
    """Largest sub-rectangle of the source with the destination's aspect.

    Biased toward the action rather than the geometric centre: Minecraft
    parkour has its subject slightly below centre, and a centred crop cuts the
    player's feet off at exactly the wrong moment.
    """
    if source_h <= 0 or dest_h <= 0:
        return 0, 0, source_w, source_h

    target = dest_w / dest_h
    if source_w / source_h > target:
        width = even(source_h * target)
        height = even(source_h)
    else:
        width = even(source_w)
        height = even(source_w / target)

    width = min(width, source_w)
    height = min(height, source_h)

    x = even(clamp(center_x * source_w - width / 2.0, 0, source_w - width))
    y = even(clamp(center_y * source_h - height / 2.0, 0, source_h - height))
    return x, y, width, height


def gameplay_scale_mode(game: Game, asset: GameplayAsset) -> str:
    """Whether the bed can be cropped to the panel or must be fitted in.

    Rocket League is the clear case. The ball is the subject and it crosses the
    full width of the pitch; a fixed crop loses it several times a minute, and
    a crop that chases it is a second moving camera fighting the first. Fitting
    the whole frame into the band keeps the footage legible, and the band is
    small enough that the letterbox is not the thing you notice.
    """
    if asset.is_vertical:
        return "cover"
    return "cover" if profile(game).action_spread <= CROP_SAFE_SPREAD else "fit"


def caption_zone(
    destination: Destination, seam_y: int
) -> tuple[int, int, int, int]:
    """The band captions may occupy, straddling the panel seam.

    The seam is where this format puts captions, and it is the right place:
    it sits in the lower-middle of the frame where the eye already is, it does
    not cover the face, and it does not compete with the bed. The safe-zone
    clamp is what stops it landing under a platform's own chrome.
    """
    safe = SAFE_ZONES[destination]
    left = int(OUTPUT_WIDTH * safe.left)
    right = int(OUTPUT_WIDTH * (1.0 - safe.right))
    width = max(1, right - left)

    height = int(OUTPUT_HEIGHT * CAPTION_BAND)
    top_limit = int(OUTPUT_HEIGHT * safe.top)
    bottom_limit = int(OUTPUT_HEIGHT * (1.0 - safe.bottom)) - height

    # Centre the band on the seam, then pull it inside the safe area.
    y = seam_y - height // 2
    if bottom_limit < top_limit:
        y = top_limit
    else:
        y = int(clamp(y, top_limit, bottom_limit))
    return left, y, width, height


def compose(
    style: LayoutStyle,
    camera_width: int,
    camera_height: int,
    asset: GameplayAsset | None,
    destination: Destination = Destination.TIKTOK,
) -> tuple[tuple[Panel, ...], tuple[int, int, int, int]]:
    """Build the panel set and caption zone for one clip.

    The speaker panel carries only its *source size* — the camera path
    supplies x and y per frame.
    """
    game = asset.game if asset else None

    if style is LayoutStyle.SPEAKER_ONLY or asset is None:
        panels = (
            Panel(
                name="speaker", x=0, y=0,
                width=OUTPUT_WIDTH, height=OUTPUT_HEIGHT,
                source_width=camera_width, source_height=camera_height, z=0,
            ),
        )
        return panels, caption_zone(destination, int(OUTPUT_HEIGHT * 0.72))

    if style is LayoutStyle.INSET:
        return _inset(camera_width, camera_height, asset, destination)

    share = speaker_share(style, game)
    speaker_h = even(OUTPUT_HEIGHT * share)
    gameplay_h = OUTPUT_HEIGHT - speaker_h

    mode = gameplay_scale_mode(asset.game, asset)
    if mode == "fit":
        # Fitted footage is scaled whole into the band — there is no crop
        # rectangle. Reporting a cover-crop the renderer will not use makes
        # the plan lie about what it is going to do.
        gx, gy, gw, gh = 0, 0, asset.width, asset.height
    else:
        gx, gy, gw, gh = cover_source(
            asset.width, asset.height, OUTPUT_WIDTH, gameplay_h,
            center_x=profile(asset.game).action_center_x,
            center_y=profile(asset.game).action_center_y,
        )

    panels = (
        Panel(
            name="speaker", x=0, y=0,
            width=OUTPUT_WIDTH, height=speaker_h,
            source_width=camera_width, source_height=camera_height, z=0,
        ),
        Panel(
            name="gameplay", x=0, y=speaker_h,
            width=OUTPUT_WIDTH, height=gameplay_h,
            source_x=gx, source_y=gy, source_width=gw, source_height=gh,
            scale_mode=mode, z=0,
        ),
    )
    return panels, caption_zone(destination, speaker_h)


def _inset(
    camera_width: int,
    camera_height: int,
    asset: GameplayAsset,
    destination: Destination,
) -> tuple[tuple[Panel, ...], tuple[int, int, int, int]]:
    """Full-bleed gameplay with the speaker as a rounded picture-in-picture.

    Only worth using when the bed is genuinely the point — a highlight reel
    with commentary over it. For a talking-head clip it inverts the hierarchy:
    the information is in the small box and the floor is fullscreen.
    """
    gx, gy, gw, gh = cover_source(
        asset.width, asset.height, OUTPUT_WIDTH, OUTPUT_HEIGHT,
        center_x=profile(asset.game).action_center_x,
        center_y=profile(asset.game).action_center_y,
    )

    inset_w = even(OUTPUT_WIDTH * INSET_WIDTH)
    inset_h = even(inset_w * camera_height / camera_width) if camera_width else inset_w
    margin = int(OUTPUT_WIDTH * INSET_MARGIN)
    inset_y = int(OUTPUT_HEIGHT * 0.13)

    panels = (
        Panel(
            name="gameplay", x=0, y=0,
            width=OUTPUT_WIDTH, height=OUTPUT_HEIGHT,
            source_x=gx, source_y=gy, source_width=gw, source_height=gh,
            scale_mode="cover", z=0,
        ),
        Panel(
            name="speaker", x=margin, y=inset_y,
            width=inset_w, height=inset_h,
            source_width=camera_width, source_height=camera_height,
            corner_radius=INSET_CORNER_RADIUS, z=1,
        ),
    )
    return panels, caption_zone(destination, inset_y + inset_h + margin)


def choose_style(
    words_per_second: float,
    has_track: bool,
    game: Game | None,
) -> LayoutStyle:
    """Pick a layout from what the clip actually is.

    Dense speech gets more speaker; sparse speech gets more bed. With no
    speaker track the crop is static, so a large speaker panel just means a
    large static shot — the bed earns more room in that case, not less.
    """
    if game is None:
        return LayoutStyle.SPEAKER_ONLY
    if not has_track:
        return LayoutStyle.SPLIT
    if words_per_second >= 3.0:
        return LayoutStyle.SPEAKER_DOMINANT
    if words_per_second <= 1.5:
        return LayoutStyle.GAMEPLAY_DOMINANT
    return LayoutStyle.SPLIT
