"""Source acquisition: URLs and files in, rights-annotated material out.

    from clipforge.acquire import AcquisitionEngine, resolve

    engine = AcquisitionEngine(db, "ten_acme", config=AcquisitionConfig(
        workspace="/var/lib/clipforge/media"))
    engine.submit("https://youtu.be/dQw4w9WgXcQ")
    engine.submit("https://feeds.example.com/show.xml")   # expands to episodes
    engine.submit("/uploads/interview.mp4")
    engine.run(limit=10)                                   # a worker turn

Four inputs — a YouTube video, a YouTube channel, a podcast feed, an uploaded
file — behind one resolver, one queue and one persistence path.

Acquisition establishes that material *exists* and what it is. It takes no
position on whether it may be republished: everything it creates is
`RightsBasis.UNVERIFIED`, which the channel gate refuses by default. That is
deliberate — republishing is a licensing decision a person records against a
source, and a layer that could grant it would turn that into a technical one.
"""

from __future__ import annotations

from .engine import ACQUIRE_JOB, AcquisitionConfig, AcquisitionEngine
from .finder import AcquiringSourceFinder, WatchedInput
from .http import DownloadConfig, HttpDownloader, sha256_file
from .mp4 import Mp4Info, looks_like_mp4, read_mp4
from .probe import MediaProber, ProbeConfig, find_ffmpeg, find_ffprobe
from .resolve import normalise_url, resolve
from .rss import Feed, FeedItem, parse_duration, parse_feed
from .types import (
    Acquisition,
    AcquisitionError,
    Download,
    DownloadFailed,
    DownloadState,
    InputKind,
    MediaProbe,
    PermanentDownloadFailed,
    PermanentError,
    ProbeFailed,
    RetryableError,
    SourceRef,
    Thumbnail,
    UnsupportedInput,
)
from .youtube import YouTubeAdapter, YouTubeConfig, yt_dlp_available

__all__ = [
    "AcquisitionEngine",
    "AcquisitionConfig",
    "AcquiringSourceFinder",
    "WatchedInput",
    "ACQUIRE_JOB",
    "resolve",
    "normalise_url",
    "HttpDownloader",
    "DownloadConfig",
    "sha256_file",
    "MediaProber",
    "ProbeConfig",
    "find_ffmpeg",
    "find_ffprobe",
    "read_mp4",
    "looks_like_mp4",
    "Mp4Info",
    "parse_feed",
    "parse_duration",
    "Feed",
    "FeedItem",
    "YouTubeAdapter",
    "YouTubeConfig",
    "yt_dlp_available",
    "InputKind",
    "SourceRef",
    "Download",
    "DownloadState",
    "MediaProbe",
    "Thumbnail",
    "Acquisition",
    "AcquisitionError",
    "UnsupportedInput",
    "DownloadFailed",
    "PermanentDownloadFailed",
    "ProbeFailed",
    "RetryableError",
    "PermanentError",
]
