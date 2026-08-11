"""Resumable HTTP downloads, with retries that know what is worth retrying.

Built on `urllib` from the standard library rather than `requests`, to keep the
package's only hard dependency the one it cannot avoid (psycopg). The features
that matter here — `Range`, streaming reads, connection reuse being irrelevant
for one large file — are all available without a client library.

## What resumption actually requires

Three things, and skipping any one of them produces a corrupt file that passes
a size check:

1. **Bytes already on disk.** Kept in `<path>.part`, and the length of that
   file is the resume offset. Not a number remembered elsewhere — the file is
   the truth, because a crash between writing bytes and recording the count
   would otherwise resume from the wrong place.
2. **A validator.** `If-Range` with the original `ETag` or `Last-Modified`, so
   a file that changed on the server restarts instead of splicing the tail of
   a new encode onto the head of an old one. That splice decodes as a corrupt
   file hours later, in the renderer, with nothing pointing back to here.
3. **Proof the server honoured the Range.** A `206` means it did. A `200`
   means it is sending the whole file from the start, and appending that to
   the existing bytes produces a file of exactly the right size containing the
   first half twice. The response code is checked, and a `200` truncates and
   restarts.

## What is worth retrying

Timeouts, connection resets and 5xx are transient; 4xx are not — except 408,
425 and 429, which are the server asking for patience. Retrying a 404 eight
times with exponential backoff is a queue spending its afternoon on a video
that was deleted last week.
"""

from __future__ import annotations

import errno
import hashlib
import os
import random
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from ..publish.types import utcnow
from .types import (
    Download,
    DownloadFailed,
    DownloadState,
    PermanentDownloadFailed,
    PermanentError,
    RetryableError,
)

__all__ = ["DownloadConfig", "HttpDownloader", "sha256_file"]

#: Read size. Large enough that syscall overhead is noise against a 2GB
#: podcast, small enough that a cancelled download stops promptly.
CHUNK_BYTES = 1 << 18  # 256 KiB

#: 4xx codes that are the server asking for patience rather than refusing.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 507, 509})


def sha256_file(path: str, chunk: int = CHUNK_BYTES) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


@dataclass(slots=True)
class DownloadConfig:
    max_attempts: int = 6
    #: First backoff, doubling each attempt, capped at `max_backoff_s`.
    base_backoff_s: float = 1.0
    max_backoff_s: float = 60.0
    #: Full jitter. Without it a queue that fails as a group retries as a
    #: group, and the retry storm is what keeps the server down.
    jitter: bool = True
    connect_timeout_s: float = 20.0
    read_timeout_s: float = 120.0
    #: Refuse a file larger than this rather than filling the disk. Zero
    #: disables the check.
    max_bytes: int = 8 << 30  # 8 GiB
    user_agent: str = "ClipForge/0.1 (+acquisition)"
    #: Honour `Retry-After` when the server sends one. It knows better than
    #: the backoff curve does.
    honour_retry_after: bool = True


class HttpDownloader:
    """Fetches one URL to one path, resumably.

    Stateless between calls except for what is on disk: hand it the same
    `Download` after a crash and it picks up from the bytes that survived.
    """

    def __init__(
        self,
        config: DownloadConfig | None = None,
        *,
        opener: urllib.request.OpenerDirector | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config or DownloadConfig()
        # Injected so tests can drive the backoff curve without waiting for it.
        # The curve is the thing under test, not the passage of time.
        self._sleep = sleep
        self._opener = opener or urllib.request.build_opener()

    # -- public ------------------------------------------------------------

    def fetch(
        self,
        download: Download,
        *,
        on_progress: Callable[[Download], None] | None = None,
    ) -> Download:
        """Drive a download to completion, resuming and retrying as needed.

        Returns the same `Download`, mutated. Raises `DownloadFailed` when the
        attempts run out — the caller decides whether that is fatal or whether
        the job goes back on the queue for later.
        """

        os.makedirs(os.path.dirname(os.path.abspath(download.path)) or ".",
                    exist_ok=True)

        if download.state is DownloadState.COMPLETE and os.path.exists(download.path):
            return download

        while download.attempts < self.config.max_attempts:
            download.attempts += 1
            download.state = DownloadState.RUNNING
            try:
                self._attempt(download, on_progress)
            except PermanentError as error:
                download.state = DownloadState.FAILED
                download.last_error = str(error)
                download.finished_at = utcnow()
                # Permanence has to survive the wrapping. A plain
                # `DownloadFailed` here reads as "worth another pass", and the
                # queue would retry a 404 until its attempt budget ran out.
                raise PermanentDownloadFailed(str(error)) from error
            except RetryableError as error:
                download.last_error = str(error)
                # PARTIAL rather than FAILED: bytes are on disk and the next
                # attempt continues from them. Calling that a failure would
                # invite a caller to delete them.
                download.state = (
                    DownloadState.PARTIAL if download.bytes_done
                    else DownloadState.QUEUED
                )
                if download.attempts >= self.config.max_attempts:
                    break
                self._sleep(self._backoff(download.attempts, error))
                continue
            else:
                download.state = DownloadState.COMPLETE
                download.finished_at = utcnow()
                download.checksum = sha256_file(download.path)
                download.last_error = ""
                if on_progress:
                    on_progress(download)
                return download

        download.state = DownloadState.FAILED
        download.finished_at = utcnow()
        raise DownloadFailed(
            f"{download.url}: gave up after {download.attempts} attempts "
            f"— {download.last_error}"
        )

    # -- one attempt -------------------------------------------------------

    def _attempt(
        self, download: Download, on_progress: Callable[[Download], None] | None
    ) -> None:
        # The file on disk is the truth about how far we got. A counter kept
        # anywhere else disagrees with it after a crash, and resuming from a
        # disagreeing counter is how the middle of a file goes missing.
        offset = (
            os.path.getsize(download.part_path)
            if os.path.exists(download.part_path)
            else 0
        )
        download.bytes_done = offset

        request = urllib.request.Request(download.url, method="GET")
        request.add_header("User-Agent", self.config.user_agent)
        request.add_header("Accept-Encoding", "identity")
        if offset:
            request.add_header("Range", f"bytes={offset}-")
            if download.validator:
                # If the file changed, the server sends 200 and the whole
                # thing, which is exactly what should happen — the bytes on
                # disk are from a different file and must be discarded.
                request.add_header("If-Range", download.validator)

        try:
            response = self._opener.open(request, timeout=self.config.read_timeout_s)
        except urllib.error.HTTPError as error:
            raise self._classify_status(error.code, download.url) from error
        except urllib.error.URLError as error:
            raise _classify_transport(error) from error
        except (TimeoutError, socket.timeout) as error:
            raise RetryableError(f"timed out connecting to {download.url}") from error

        with response:
            status = getattr(response, "status", response.getcode())
            headers = response.headers

            download.content_type = headers.get("Content-Type", "") or ""
            etag = headers.get("ETag", "") or ""
            modified = headers.get("Last-Modified", "") or ""
            if etag:
                download.etag = etag
            if modified:
                download.last_modified = modified

            if offset and status == 206:
                download.resumable = True
                mode = "ab"
                total = _total_from_content_range(headers.get("Content-Range", ""))
                if total is not None:
                    download.bytes_total = total
                elif (length := _int_or_none(headers.get("Content-Length"))) is not None:
                    download.bytes_total = offset + length
            elif offset and status == 200:
                # The server ignored the Range, or `If-Range` failed because
                # the file changed. Either way the bytes on disk belong to a
                # different response and appending to them would interleave
                # two files. Start again.
                download.resumable = False
                download.bytes_done = offset = 0
                mode = "wb"
                download.bytes_total = _int_or_none(headers.get("Content-Length"))
            else:
                mode = "wb"
                download.bytes_done = offset = 0
                download.resumable = "bytes" in (
                    headers.get("Accept-Ranges", "") or ""
                ).lower()
                download.bytes_total = _int_or_none(headers.get("Content-Length"))

            self._guard_size(download)
            self._stream(response, download, mode, on_progress)

        expected = download.bytes_total
        if expected is not None and download.bytes_done != expected:
            # Short read. Retryable, and the bytes stay on disk so the next
            # attempt resumes rather than starting over.
            raise RetryableError(
                f"{download.url}: got {download.bytes_done} of {expected} bytes"
            )

        os.replace(download.part_path, download.path)

    def _stream(
        self,
        response,
        download: Download,
        mode: str,
        on_progress: Callable[[Download], None] | None,
    ) -> None:
        try:
            with open(download.part_path, mode) as handle:
                while True:
                    try:
                        chunk = response.read(CHUNK_BYTES)
                    except (TimeoutError, socket.timeout) as error:
                        raise RetryableError(
                            f"{download.url}: read timed out at "
                            f"{download.bytes_done} bytes"
                        ) from error
                    except OSError as error:
                        raise RetryableError(
                            f"{download.url}: connection lost at "
                            f"{download.bytes_done} bytes"
                        ) from error
                    if not chunk:
                        break
                    handle.write(chunk)
                    download.bytes_done += len(chunk)
                    self._guard_size(download)
                    if on_progress:
                        on_progress(download)
                # Flush to the OS before the rename. Without this the rename
                # can land ahead of the data, and a crash in between leaves a
                # complete-looking file that is short.
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            if error.errno == errno.ENOSPC:
                # Not retryable in any useful sense: trying again fills the
                # disk again. The partial file stays, so freeing space and
                # re-running resumes rather than restarting.
                raise PermanentError(
                    f"{download.url}: out of disk space at "
                    f"{download.bytes_done} bytes"
                ) from error
            raise

    # -- policy ------------------------------------------------------------

    def _guard_size(self, download: Download) -> None:
        limit = self.config.max_bytes
        if not limit:
            return
        if download.bytes_done > limit or (download.bytes_total or 0) > limit:
            raise PermanentError(
                f"{download.url}: larger than the {limit} byte ceiling"
            )

    def _classify_status(self, status: int, url: str) -> Exception:
        if status in _RETRYABLE_STATUS:
            return RetryableError(f"{url}: HTTP {status}")
        if 400 <= status < 500:
            return PermanentError(f"{url}: HTTP {status}")
        return RetryableError(f"{url}: HTTP {status}")

    def _backoff(self, attempt: int, error: Exception) -> float:
        after = getattr(error, "retry_after", None)
        if self.config.honour_retry_after and after:
            return float(after)
        delay = min(
            self.config.base_backoff_s * (2 ** (attempt - 1)),
            self.config.max_backoff_s,
        )
        # Full jitter rather than delay ± a bit: with equal jitter a
        # thundering herd stays a herd, just a slightly blurrier one.
        return random.uniform(0, delay) if self.config.jitter else delay


def _classify_transport(error: urllib.error.URLError) -> Exception:
    reason = getattr(error, "reason", error)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return RetryableError(f"timed out: {reason}")
    if isinstance(reason, socket.gaierror):
        # DNS. Could be a typo or could be a resolver blip; treated as
        # retryable because the cost of a few retries is far below the cost of
        # dropping a legitimate source on a transient DNS failure.
        return RetryableError(f"cannot resolve host: {reason}")
    if isinstance(reason, ConnectionError):
        return RetryableError(f"connection failed: {reason}")
    return RetryableError(f"transport error: {reason}")


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _total_from_content_range(header: str) -> int | None:
    """`bytes 200-1023/1024` -> 1024. `bytes 200-1023/*` -> None."""

    _, _, total = header.partition("/")
    return _int_or_none(total.strip()) if total.strip() != "*" else None
