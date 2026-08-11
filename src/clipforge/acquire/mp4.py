"""An ISO base media file format reader: duration, geometry, cover art.

MP4, M4A, M4V and MOV are all the same box-structured container, and the few
facts acquisition needs — how long is it, how big is the picture, is there
audio, is there artwork — are in fixed fields near the front of it.

## Why not just shell out to ffprobe

Three reasons, in order of how much they matter here.

**It answers on a partial file.** `moov` is often at the front (anything
served for progressive playback puts it there), so a duration can be read from
the first megabyte of a download that is still running. That turns "is this
worth finishing?" into a question answerable before spending the bandwidth.

**It cannot be absent.** ffprobe ships separately from ffmpeg and plenty of
static builds omit it. A duration extractor that silently degrades to "unknown"
because a binary is missing is one that reports zero-length videos in
production and works perfectly in development.

**It is exact.** `mvhd` carries a timescale and a duration as integers, and
`duration / timescale` is the real number. Parsing it out of ffprobe's text
output means rounding to whatever precision the formatter chose.

ffmpeg is still used, in `probe.py`, for containers this does not read
(WebM, Matroska) and for grabbing a frame — which needs a decoder, not a
parser.

## Reading defensively

Every read is bounded by the box it is inside, and a box that claims a size
larger than its parent stops the walk. Media files are attacker-controlled
input in any product that accepts uploads: a `moov` claiming four gigabytes
inside a two-kilobyte file must produce a parse error, not an allocation.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import BinaryIO

__all__ = ["Mp4Info", "read_mp4", "looks_like_mp4"]

#: Boxes that contain other boxes. Anything else is a leaf and gets skipped.
_CONTAINERS = frozenset({
    b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"meta", b"ilst",
    b"edts", b"moof", b"traf", b"mvex", b"\xa9nam", b"\xa9ART", b"covr",
})

#: The epoch for MP4 timestamps: 1904-01-01, not 1970.
_MP4_EPOCH = datetime(1904, 1, 1, tzinfo=UTC)

#: Refuse to walk deeper than this. A crafted file can nest boxes until the
#: recursion limit; a real one is four or five deep.
_MAX_DEPTH = 12

#: Cap on how much of a leaf box is read into memory (cover art, mostly).
_MAX_LEAF_BYTES = 32 << 20  # 32 MiB


@dataclass
class Mp4Info:
    duration_s: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    #: Major brand from `ftyp` — "isom", "mp42", "M4A ", "qt  ".
    brand: str = ""
    has_video: bool = False
    has_audio: bool = False
    video_codec: str = ""
    audio_codec: str = ""
    title: str = ""
    creator: str = ""
    created_at: datetime | None = None
    #: Embedded artwork, as bytes, with the image type ("jpeg" or "png").
    cover_art: bytes | None = field(default=None, repr=False)
    cover_type: str = ""
    #: True when `moov` was found before `mdat` — the file can be probed from
    #: its first bytes, which is what makes probing a partial download work.
    fast_start: bool = False
    #: An image track carrying artwork rather than footage. A podcast with
    #: embedded cover art has one, and counting it as video would tell the
    #: pipeline the episode has footage — so the gameplay bed it actually
    #: needs would never be composed.
    has_cover_track: bool = False

    #: Scratch for the track currently being walked. Merged into the fields
    #: above when the `trak` box closes, because whether a track is footage or
    #: artwork is only decidable once its handler *and* its codec are known,
    #: and those live in different boxes.
    _track_handler: str = field(default="", repr=False)
    _track_codec: str = field(default="", repr=False)


def looks_like_mp4(head: bytes) -> bool:
    """Cheap sniff on the first bytes: is the second box-word a known brand?"""

    return len(head) >= 12 and head[4:8] in (b"ftyp", b"moov", b"free", b"skip")


def read_mp4(handle: BinaryIO, size: int | None = None) -> Mp4Info:
    """Parse what is reachable. Never raises on a truncated file.

    Truncation is the normal case, not the exceptional one: this is pointed at
    downloads in progress on purpose. A file whose `moov` has not arrived
    returns an `Mp4Info` with `duration_s` still None, which is the honest
    answer — as opposed to zero, which reads as "a video of no length".
    """

    info = Mp4Info()
    if size is None:
        handle.seek(0, 2)
        size = handle.tell()
    handle.seek(0)
    _walk(handle, 0, size, info, depth=0, seen_mdat=[False])
    return info


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


def _walk(
    handle: BinaryIO,
    start: int,
    end: int,
    info: Mp4Info,
    depth: int,
    seen_mdat: list[bool],
) -> None:
    if depth > _MAX_DEPTH:
        return
    offset = start
    while offset + 8 <= end:
        handle.seek(offset)
        header = handle.read(8)
        if len(header) < 8:
            return
        box_size, kind = struct.unpack(">I4s", header)
        body = offset + 8

        if box_size == 1:
            # 64-bit size, in the eight bytes after the type.
            extended = handle.read(8)
            if len(extended) < 8:
                return
            box_size = struct.unpack(">Q", extended)[0]
            body = offset + 16
        elif box_size == 0:
            # "to end of file", per the spec.
            box_size = end - offset

        # A box claiming more than its parent holds is either truncation or a
        # malformed file. Either way the walk stops rather than seeking past
        # the end and reading whatever is there.
        if box_size < 8 or offset + box_size > end:
            _leaf(handle, kind, body, end, info)
            return

        stop = offset + box_size
        if kind == b"mdat":
            seen_mdat[0] = True
        elif kind == b"moov" and not seen_mdat[0]:
            info.fast_start = True

        if kind == b"trak":
            _walk(handle, body, stop, info, depth + 1, seen_mdat)
            _close_track(info)
        elif kind in _CONTAINERS:
            if kind == b"meta":
                # `meta` is a FullBox: four bytes of version and flags before
                # the children. Walking from the wrong offset finds nothing
                # and looks exactly like a file with no metadata.
                _walk(handle, body + 4, stop, info, depth + 1, seen_mdat)
            else:
                _walk(handle, body, stop, info, depth + 1, seen_mdat)
        else:
            _leaf(handle, kind, body, stop, info)

        offset = stop


def _leaf(handle: BinaryIO, kind: bytes, start: int, end: int, info: Mp4Info) -> None:
    reader = _LEAVES.get(kind)
    if reader is None:
        return
    length = end - start
    if length <= 0 or length > _MAX_LEAF_BYTES:
        return
    handle.seek(start)
    payload = handle.read(length)
    if len(payload) < length:
        return  # truncated mid-box; take nothing rather than half a field
    try:
        reader(payload, info)
    except (struct.error, UnicodeDecodeError, ValueError):
        # A malformed box loses that one field, not the whole parse. A file
        # with a corrupt title still has a usable duration.
        return


# ---------------------------------------------------------------------------
# Leaf readers
# ---------------------------------------------------------------------------


def _ftyp(payload: bytes, info: Mp4Info) -> None:
    if len(payload) >= 4:
        info.brand = payload[:4].decode("ascii", "replace").strip()


def _mvhd(payload: bytes, info: Mp4Info) -> None:
    """Movie header: the authoritative duration."""

    version = payload[0]
    if version == 1:
        created, _modified, timescale, duration = struct.unpack(
            ">QQIQ", payload[4:32]
        )
    else:
        created, _modified, timescale, duration = struct.unpack(
            ">IIII", payload[4:20]
        )
    if timescale:
        # A duration of all-ones means "unknown" in the spec, and dividing it
        # by the timescale produces a plausible-looking number of years.
        unknown = 0xFFFFFFFFFFFFFFFF if version == 1 else 0xFFFFFFFF
        if duration != unknown:
            info.duration_s = duration / timescale
    if created:
        info.created_at = _MP4_EPOCH + timedelta(seconds=created)


def _tkhd(payload: bytes, info: Mp4Info) -> None:
    """Track header. Its width and height are the *display* size."""

    # Offsets are from the start of the FullBox. Version 0:
    #   4 version+flags, 4 created, 4 modified, 4 track_id, 4 reserved,
    #   4 duration, 8 reserved, 2 layer, 2 alternate_group, 2 volume,
    #   2 reserved, 36 matrix  ->  width at 76, height at 80.
    # Version 1 widens created/modified/duration by 4 bytes each: +12.
    version = payload[0]
    at = 88 if version == 1 else 76
    tail = payload[at:at + 8]
    if len(tail) < 8:
        return
    width, height = struct.unpack(">II", tail)
    # 16.16 fixed point.
    width, height = width >> 16, height >> 16
    if width and height:
        # Widest track wins: a file can carry a thumbnail track alongside the
        # real one, and taking the last would report the wrong geometry.
        if not info.width or width > info.width:
            info.width, info.height = width, height


def _hdlr(payload: bytes, info: Mp4Info) -> None:
    if len(payload) < 12:
        return
    handler = payload[8:12]
    if handler in (b"vide", b"soun"):
        info._track_handler = handler.decode("ascii")


#: Still-image codecs. A "video" track using one of these in an audio
#: container is cover art, not footage.
_IMAGE_CODECS = frozenset({"mjpg", "jpeg", "png ", "avc1-still"})


def _stsd(payload: bytes, info: Mp4Info) -> None:
    """Sample description: the codec four-cc lives here."""

    if len(payload) < 16:
        return
    info._track_codec = payload[12:16].decode("ascii", "replace")


def _close_track(info: Mp4Info) -> None:
    """Decide what the track just walked actually was."""

    handler, codec = info._track_handler, info._track_codec
    info._track_handler = info._track_codec = ""
    if handler == "soun":
        info.has_audio = True
        if codec:
            info.audio_codec = codec
    elif handler == "vide":
        if codec in _IMAGE_CODECS:
            info.has_cover_track = True
        else:
            info.has_video = True
            if codec:
                info.video_codec = codec


def _mdhd_free(payload: bytes, info: Mp4Info) -> None:
    return


def _stts(payload: bytes, info: Mp4Info) -> None:
    """Time-to-sample. Frame rate, when the track is video and evenly paced.

    Only trusted for a single-entry table. A variable-frame-rate video has
    several entries and no single meaningful rate, and averaging them produces
    a number that is wrong in a way nothing downstream can detect.
    """

    if len(payload) < 8 or info._track_handler != "vide" or info.fps:
        return
    count = struct.unpack(">I", payload[4:8])[0]
    if count != 1 or len(payload) < 16:
        return
    samples, delta = struct.unpack(">II", payload[8:16])
    if delta and samples and info.duration_s:
        # samples / duration is the rate, and it avoids needing the media
        # timescale from mdhd — which may not have been reached yet.
        rate = samples / info.duration_s
        if 1.0 <= rate <= 480.0:
            info.fps = round(rate, 3)


def _covr(payload: bytes, info: Mp4Info) -> None:
    """Cover art, inside an iTunes-style `data` atom.

    Real artwork, already in the file. Extracting it costs a read and no
    decoding, which is the difference between a thumbnail for every podcast
    episode and a thumbnail only when ffmpeg happens to be installed.
    """

    if len(payload) < 16 or payload[4:8] != b"data":
        return
    type_flag = struct.unpack(">I", payload[8:12])[0] & 0x00FFFFFF
    blob = payload[16:]
    if not blob:
        return
    if type_flag == 13 or blob[:3] == b"\xff\xd8\xff":
        info.cover_art, info.cover_type = blob, "jpeg"
    elif type_flag == 14 or blob[:8] == b"\x89PNG\r\n\x1a\n":
        info.cover_art, info.cover_type = blob, "png"


def _text_atom(payload: bytes) -> str:
    if len(payload) < 16 or payload[4:8] != b"data":
        return ""
    return payload[16:].decode("utf-8", "replace").strip()


def _nam(payload: bytes, info: Mp4Info) -> None:
    if text := _text_atom(payload):
        info.title = text


def _art(payload: bytes, info: Mp4Info) -> None:
    if text := _text_atom(payload):
        info.creator = text


_LEAVES = {
    b"ftyp": _ftyp,
    b"mvhd": _mvhd,
    b"tkhd": _tkhd,
    b"hdlr": _hdlr,
    b"stsd": _stsd,
    b"stts": _stts,
    b"mdhd": _mdhd_free,
    b"data": lambda payload, info: None,
    b"\xa9nam": _nam,
    b"\xa9ART": _art,
}


# `covr`, `©nam` and `©ART` are containers of a single `data` atom, so they are
# in `_CONTAINERS` for the walk and handled here for their payload. Registering
# them in both places would double-read; instead the walk treats them as leaves
# by keeping them out of the container set at read time.
for _name, _reader in ((b"covr", _covr), (b"\xa9nam", _nam), (b"\xa9ART", _art)):
    _LEAVES[_name] = _reader
_CONTAINERS = _CONTAINERS - {b"covr", b"\xa9nam", b"\xa9ART"}
