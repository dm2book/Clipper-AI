"""The resumable downloader, against a real HTTP server over a real socket.

Not a mocked transport. `_Server` below is `http.server` on a real port,
speaking real HTTP/1.1 with real `Range` handling, and every test here drives
bytes through a TCP connection. Resumption logic does not care whether the
origin is a CDN or localhost, so this is the same code path production takes —
which a stubbed `urlopen` would not be.

The server can also misbehave on purpose: drop a connection mid-body, return
503 then recover, ignore a `Range` header, or change its `ETag` between
attempts. Those are the cases that corrupt files, and they are difficult to
provoke against a well-behaved origin.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from clipforge.acquire.http import DownloadConfig, HttpDownloader, sha256_file
from clipforge.acquire.types import (
    Download,
    DownloadFailed,
    DownloadState,
)

#: Big enough to span many read chunks, so a mid-body drop lands in the middle
#: of the stream rather than between the header and the first byte.
BODY = bytes(range(256)) * 4096  # 1 MiB, and every byte position identifiable
DIGEST = hashlib.sha256(BODY).hexdigest()


class _Behaviour:
    """What the server should do next. Mutated by the tests."""

    def __init__(self) -> None:
        self.body = BODY
        self.etag = '"v1"'
        self.accept_ranges = True
        #: Close the connection after this many bytes of body, once.
        self.drop_after: int | None = None
        #: Status codes to return before serving properly, consumed in order.
        self.fail_with: list[int] = []
        self.retry_after: str | None = None
        #: Count of requests seen, and the Range headers they carried.
        self.requests: list[str | None] = []
        self.send_content_length = True


class _Handler(BaseHTTPRequestHandler):
    behaviour: _Behaviour

    def log_message(self, *args) -> None:  # noqa: A003 - silence the test log
        pass

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        behaviour = self.behaviour
        range_header = self.headers.get("Range")
        behaviour.requests.append(range_header)

        if behaviour.fail_with:
            status = behaviour.fail_with.pop(0)
            self.send_response(status)
            if behaviour.retry_after:
                self.send_header("Retry-After", behaviour.retry_after)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        body = behaviour.body
        start = 0
        partial = False

        if range_header and behaviour.accept_ranges:
            # `If-Range` that no longer matches means the file changed: answer
            # 200 with the whole thing, which is what a real origin does and
            # what the client must notice.
            if_range = self.headers.get("If-Range")
            if if_range and if_range != behaviour.etag:
                partial = False
            else:
                start = int(range_header.split("=")[1].split("-")[0])
                partial = True

        chunk = body[start:] if partial else body

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("ETag", behaviour.etag)
        if behaviour.accept_ranges:
            self.send_header("Accept-Ranges", "bytes")
        if behaviour.send_content_length:
            self.send_header("Content-Length", str(len(chunk)))
        if partial:
            self.send_header(
                "Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}"
            )
        self.end_headers()

        if behaviour.drop_after is not None:
            cut = behaviour.drop_after
            behaviour.drop_after = None  # once only; the retry should succeed
            self.wfile.write(chunk[:cut])
            self.wfile.flush()
            self.close_connection = True
            return

        self.wfile.write(chunk)


class _Server:
    def __init__(self) -> None:
        self.behaviour = _Behaviour()
        handler = type("_Bound", (_Handler,), {"behaviour": self.behaviour})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/video.mp4"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class DownloaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _Server()
        self.addCleanup(self.server.close)
        self.tmp = tempfile.mkdtemp(prefix="clipforge-dl-")
        self.slept: list[float] = []
        # The backoff curve is under test; waiting for it is not.
        self.downloader = HttpDownloader(
            DownloadConfig(base_backoff_s=1.0, jitter=False),
            sleep=self.slept.append,
        )

    def _download(self, name: str = "out.mp4") -> Download:
        return Download(
            download_id="dl_1",
            url=self.server.url,
            path=os.path.join(self.tmp, name),
        )

    # -- the happy path ----------------------------------------------------

    def test_a_file_arrives_intact(self) -> None:
        result = self.downloader.fetch(self._download())
        self.assertEqual(result.state, DownloadState.COMPLETE)
        self.assertEqual(result.bytes_done, len(BODY))
        self.assertEqual(result.checksum, DIGEST)
        with open(result.path, "rb") as handle:
            self.assertEqual(handle.read(), BODY)

    def test_the_part_file_is_gone_once_the_download_completes(self) -> None:
        """The rename is what makes the final path atomic. A `.part` left
        behind means a later run resumes a file that is already complete."""

        result = self.downloader.fetch(self._download())
        self.assertTrue(os.path.exists(result.path))
        self.assertFalse(os.path.exists(result.part_path))

    def test_progress_is_reported_as_bytes_land(self) -> None:
        seen: list[int] = []
        self.downloader.fetch(self._download(), on_progress=lambda d: seen.append(d.bytes_done))
        self.assertGreater(len(seen), 1, "one callback is not progress")
        self.assertEqual(seen[-1], len(BODY))
        self.assertEqual(seen, sorted(seen), "progress went backwards")

    # -- resumption --------------------------------------------------------

    def test_a_dropped_connection_resumes_from_the_bytes_on_disk(self) -> None:
        """The case the whole design exists for. The server hangs up halfway;
        the retry sends `Range` and the second half is appended to the first."""

        self.server.behaviour.drop_after = len(BODY) // 2
        result = self.downloader.fetch(self._download())

        self.assertEqual(result.state, DownloadState.COMPLETE)
        self.assertEqual(result.checksum, DIGEST, "the file is not the file")
        self.assertTrue(result.resumable)
        self.assertEqual(result.attempts, 2)
        # The second request asked for exactly what was missing.
        self.assertEqual(
            self.server.behaviour.requests[1], f"bytes={len(BODY) // 2}-"
        )

    def test_resuming_an_interrupted_download_in_a_later_process(self) -> None:
        """A crash between attempts. Nothing is carried in memory: the `.part`
        file's length is the resume offset, because a counter kept anywhere
        else disagrees with the disk after a crash."""

        download = self._download()
        head = len(BODY) // 3
        with open(download.part_path, "wb") as handle:
            handle.write(BODY[:head])

        # A brand-new downloader, as a restarted worker would have.
        result = HttpDownloader(DownloadConfig(jitter=False), sleep=self.slept.append).fetch(
            download
        )
        self.assertEqual(result.checksum, DIGEST)
        self.assertEqual(self.server.behaviour.requests[0], f"bytes={head}-")

    def test_a_changed_file_restarts_rather_than_splicing(self) -> None:
        """The corruption this prevents is the nastiest in the layer: the tail
        of a new encode appended to the head of an old one. It is exactly the
        right size, so every size check passes, and it fails to decode hours
        later in the renderer with nothing pointing back here."""

        download = self._download()
        head = len(BODY) // 3
        with open(download.part_path, "wb") as handle:
            handle.write(b"\x00" * head)
        download.bytes_done = head
        download.etag = '"stale"'

        result = self.downloader.fetch(download)
        self.assertEqual(result.checksum, DIGEST)
        self.assertEqual(result.bytes_done, len(BODY))

    def test_a_server_that_ignores_range_is_not_appended_to(self) -> None:
        """Some origins answer 200 to a Range request and send the whole file.
        Appending that to what is already there yields a file of exactly twice
        the length containing the first bytes twice."""

        self.server.behaviour.accept_ranges = False
        download = self._download()
        head = len(BODY) // 4
        with open(download.part_path, "wb") as handle:
            handle.write(BODY[:head])

        result = self.downloader.fetch(download)
        self.assertEqual(result.bytes_done, len(BODY))
        self.assertEqual(result.checksum, DIGEST)
        self.assertFalse(result.resumable)

    # -- retries -----------------------------------------------------------

    def test_a_5xx_is_retried_and_then_succeeds(self) -> None:
        self.server.behaviour.fail_with = [503, 502]
        result = self.downloader.fetch(self._download())
        self.assertEqual(result.state, DownloadState.COMPLETE)
        self.assertEqual(result.attempts, 3)

    def test_a_404_is_not_retried(self) -> None:
        """Retrying a deleted video eight times with exponential backoff is a
        queue spending its afternoon on something that will never arrive."""

        self.server.behaviour.fail_with = [404] * 10
        with self.assertRaises(DownloadFailed):
            self.downloader.fetch(self._download())
        self.assertEqual(len(self.server.behaviour.requests), 1)
        self.assertEqual(self.slept, [], "a permanent failure should not back off")

    def test_a_429_is_retried_because_it_is_a_request_for_patience(self) -> None:
        self.server.behaviour.fail_with = [429]
        result = self.downloader.fetch(self._download())
        self.assertEqual(result.state, DownloadState.COMPLETE)
        self.assertEqual(result.attempts, 2)

    def test_backoff_doubles_and_is_capped(self) -> None:
        downloader = HttpDownloader(
            DownloadConfig(base_backoff_s=1.0, max_backoff_s=8.0, jitter=False,
                           max_attempts=8),
            sleep=self.slept.append,
        )
        self.server.behaviour.fail_with = [503] * 6
        downloader.fetch(self._download())
        self.assertEqual(self.slept, [1.0, 2.0, 4.0, 8.0, 8.0, 8.0])

    def test_jitter_spreads_a_retry_storm(self) -> None:
        """Without it a batch that fails together retries together, and the
        retry storm is what keeps the server down."""

        delays = []
        for _ in range(12):
            server = _Server()
            self.addCleanup(server.close)
            server.behaviour.fail_with = [503]
            slept: list[float] = []
            HttpDownloader(
                DownloadConfig(base_backoff_s=8.0, jitter=True),
                sleep=slept.append,
            ).fetch(Download("d", server.url, os.path.join(self.tmp, "j.mp4")))
            delays.extend(slept)
        self.assertGreater(len(set(delays)), 1, "every delay was identical")
        self.assertTrue(all(0 <= d <= 8.0 for d in delays))

    def test_giving_up_leaves_the_partial_file_for_a_later_attempt(self) -> None:
        """`FAILED` here means "these attempts are spent", not "throw the bytes
        away". A 900 MB download that got to 890 MB should not start over."""

        downloader = HttpDownloader(
            DownloadConfig(max_attempts=2, base_backoff_s=0, jitter=False),
            sleep=self.slept.append,
        )
        download = self._download()
        self.server.behaviour.drop_after = 4096
        self.server.behaviour.fail_with = []
        # Drop once, then make every later attempt fail transiently.
        original = self.server.behaviour.body
        self.server.behaviour.body = original
        self.server.behaviour.fail_with = [503]

        with self.assertRaises(DownloadFailed):
            downloader.fetch(download)
        self.assertTrue(os.path.exists(download.part_path))
        self.assertGreater(os.path.getsize(download.part_path), 0)

    # -- guards ------------------------------------------------------------

    def test_a_file_over_the_ceiling_is_refused_before_it_fills_the_disk(self) -> None:
        downloader = HttpDownloader(
            DownloadConfig(max_bytes=1024, jitter=False), sleep=self.slept.append
        )
        with self.assertRaises(DownloadFailed):
            downloader.fetch(self._download())

    def test_a_short_read_is_retried_rather_than_accepted(self) -> None:
        """A body shorter than its own Content-Length is a truncated file. A
        downloader that renames it anyway hands the renderer half a video."""

        self.server.behaviour.drop_after = len(BODY) - 1024
        result = self.downloader.fetch(self._download())
        self.assertEqual(result.bytes_done, len(BODY))
        self.assertEqual(result.checksum, DIGEST)

    def test_a_response_with_no_content_length_still_completes(self) -> None:
        """Chunked responses are legal and common. Progress is unknowable, not
        zero — `bytes_total` stays None and `progress` says so."""

        self.server.behaviour.send_content_length = False
        result = self.downloader.fetch(self._download())
        self.assertEqual(result.state, DownloadState.COMPLETE)
        self.assertEqual(result.checksum, DIGEST)
        self.assertIsNone(result.bytes_total)
        self.assertIsNone(result.progress)

    def test_an_already_complete_download_is_not_fetched_again(self) -> None:
        first = self.downloader.fetch(self._download())
        seen = len(self.server.behaviour.requests)
        again = self.downloader.fetch(first)
        self.assertEqual(len(self.server.behaviour.requests), seen)
        self.assertEqual(again.checksum, DIGEST)

    def test_the_checksum_is_of_the_bytes_that_landed(self) -> None:
        result = self.downloader.fetch(self._download())
        self.assertEqual(result.checksum, sha256_file(result.path))


if __name__ == "__main__":
    unittest.main()
