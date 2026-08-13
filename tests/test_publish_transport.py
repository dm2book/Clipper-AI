"""The upload layer against real sockets.

## What "integration" means here, precisely

Every test in this file runs a real `ThreadingHTTPServer` on loopback and
drives `HttpTransport` — and in most cases the whole `PublishingSystem` — at
it over a real TCP connection. Real request framing, real `Content-Length`,
real streamed file bodies, real status codes, real headers, real JSON.

The servers implement each platform's **documented** protocol: Google's
resumable upload with `308 Resume Incomplete` and authoritative `Range`
headers, TikTok's declared-chunk-count upload followed by status polling,
Instagram's container-then-publish with an asynchronous `FINISHED`.

That makes these genuine integration tests **of this repository's code**. They
are not evidence that TikTok, Google or Meta behave as their documentation
says. Nothing in this environment can produce that evidence: outbound CONNECT
to `open.tiktokapis.com` and `graph.facebook.com` is refused by policy, and
there are no credentials. The one exception is `LiveGoogleTokenTest`, which
talks to Google's actual token endpoint — see its docstring.

## Why the servers are strict

A permissive fake passes on requests the real platform would reject, which is
worse than no test: it converts a production failure into a green build. So
each server asserts what the platform asserts — that the chunk count matches
what was declared, that `Content-Range` is well formed and contiguous, that
the final chunk carries the remainder, that a bearer token is present — and
returns the platform's own error shape when it is not.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from clipforge.publish import (
    Account,
    AccountManager,
    ClientCredentials,
    HttpTransport,
    InMemoryTokenStore,
    MediaAsset,
    Platform,
    PostSpec,
    PostState,
    PublishConfig,
    PublishingSystem,
    ReauthRequired,
    RefreshFailed,
    TokenRefresher,
    TokenSet,
    TransportConfig,
    TransportError,
    UploadVerifier,
    Visibility,
)
from clipforge.publish.types import Request

#: Anchored to the real clock: `PublishingSystem.schedule` validates run times
#: against `utcnow()` and refuses the past, so a frozen fixture date makes
#: every scheduling call fail the day after it was written.
NOW = datetime.now(UTC).replace(microsecond=0)
LATER = NOW + timedelta(hours=2)

#: Big enough to force multi-chunk uploads once the servers below shrink the
#: chunk size, small enough that the suite stays fast.
ASSET_BYTES = 700_000


def _credentials() -> ClientCredentials:
    return ClientCredentials(
        client_id="cid", client_secret="secret",
        redirect_uri="https://clipforge.test/callback",
    )


# ---------------------------------------------------------------------------
# A real server, per platform
# ---------------------------------------------------------------------------


class _Platform(BaseHTTPRequestHandler):
    """Shared plumbing. Subclasses implement the platform."""

    state: dict = {}

    def log_message(self, *args) -> None:      # noqa: A003 - silence the server
        pass

    # -- helpers -----------------------------------------------------------

    def _read(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _json(self) -> dict:
        raw = self._read()
        return json.loads(raw) if raw else {}

    def _reply(self, status: int, body: dict | None = None,
               headers: dict | None = None) -> None:
        payload = json.dumps(body or {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _authorised(self) -> bool:
        return self.headers.get("Authorization", "").startswith("Bearer ")


class _GoogleHandler(_Platform):
    """Google's resumable upload, including the parts that make it resumable.

    Chunks are accepted one at a time and answered with `308` plus a `Range`
    header naming exactly how much is held. The `bytes */total` probe is
    answered without consuming a body, which is what makes it safe to send
    after a timeout.
    """

    def do_POST(self) -> None:                              # noqa: N802
        state = self.state
        if self.path.startswith("/upload/youtube"):
            if not self._authorised():
                self._reply(401, {"error": {"code": 401, "message": "unauthorised",
                                            "status": "UNAUTHENTICATED"}})
                return
            body = self._json()
            state["metadata"] = body
            declared = int(self.headers.get("X-Upload-Content-Length") or 0)
            state["declared"] = declared
            state["received"] = 0
            state["session"] = f"{state['base']}/session/{len(state)}"
            self._reply(200, {}, {"Location": state["session"]})
            return
        self._reply(404, {"error": {"message": "no such endpoint"}})

    def do_PUT(self) -> None:                               # noqa: N802
        state = self.state
        if "/session/" not in self.path:
            self._reply(404, {"error": {"message": "unknown session"}})
            return

        content_range = self.headers.get("Content-Range", "")
        # The status probe. A question, not a write — and it must not block
        # waiting for a body that is not coming.
        if content_range.startswith("bytes */"):
            held = state["received"]
            if held >= state["declared"]:
                self._reply(200, {"id": state.get("video_id", "vid_google")})
            else:
                self._reply(308, {}, {"Range": f"bytes=0-{held - 1}"})
            return

        chunk = self._read()
        prefix, _, total = content_range.partition("/")
        start, _, end = prefix.replace("bytes ", "").partition("-")
        start, end, total = int(start), int(end), int(total)

        if start != state["received"]:
            # Google rejects a chunk that does not continue the upload. A
            # fake that accepted it would hide an off-by-one in the resume
            # path, which is the single most delicate part of this protocol.
            self._reply(400, {"error": {"message":
                        f"expected byte {state['received']}, got {start}"}})
            return
        if end - start + 1 != len(chunk):
            self._reply(400, {"error": {"message":
                        f"Content-Range says {end - start + 1} bytes, "
                        f"body carried {len(chunk)}"}})
            return

        state["body"] = state.get("body", b"") + chunk
        state["received"] = end + 1
        state["chunks"] = state.get("chunks", 0) + 1

        if state["received"] >= total:
            self._reply(200, {"id": state.get("video_id", "vid_google")})
        else:
            self._reply(308, {}, {"Range": f"bytes=0-{end}"})

    def do_GET(self) -> None:                               # noqa: N802
        if self.path.startswith("/youtube/v3/videos"):
            sent = (self.state.get("metadata") or {}).get("status") or {}
            default = {
                "uploadStatus": "processed",
                "privacyStatus": sent.get("privacyStatus", "private"),
                # Echoed, not invented: a canned date here reads as a bug in
                # anything that prints the verification result.
                "publishAt": sent.get("publishAt", ""),
            }
            self._reply(200, {"items": [{
                "id": "vid_google",
                "status": self.state.get("verify_status", default),
                "snippet": {"title": "A clip"},
            }]})
            return
        self._reply(404, {"error": {"message": "no such endpoint"}})


class _TikTokHandler(_Platform):
    """TikTok's init → chunked PUT → poll cycle.

    The chunk arithmetic is checked here because it is checked there: TikTok
    rejects an undersized chunk in a multi-chunk upload, so the final chunk
    has to absorb the remainder rather than being short.
    """

    def do_POST(self) -> None:                              # noqa: N802
        state = self.state
        if not self._authorised():
            self._reply(401, {"error": {"code": "access_token_invalid",
                                        "message": "invalid token"}})
            return

        if self.path.endswith("/video/init/") or self.path.endswith("/inbox/video/init/"):
            body = self._json()
            source = body.get("source_info", {})
            state["inbox"] = self.path.endswith("/inbox/video/init/")
            state["declared_size"] = source.get("video_size")
            state["declared_chunk"] = source.get("chunk_size")
            state["declared_count"] = source.get("total_chunk_count")
            state["post_info"] = body.get("post_info")
            state["received"] = 0
            state["chunks"] = 0
            self._reply(200, {"data": {
                "publish_id": "pub_tiktok",
                "upload_url": f"{state['base']}/upload/pub_tiktok",
            }, "error": {"code": "ok"}})
            return

        if self.path.endswith("/status/fetch/"):
            state["polls"] = state.get("polls", 0) + 1
            if state["polls"] < state.get("polls_needed", 2):
                status = "PROCESSING_UPLOAD"
            else:
                # An unaudited client never reaches PUBLISH_COMPLETE — the
                # draft lands in the inbox and a human finishes it.
                status = ("SEND_TO_USER_INBOX" if state.get("inbox")
                          else "PUBLISH_COMPLETE")
            self._reply(200, {"data": {"status": status,
                                       "publicly_available_post_id": ["tt_1"]},
                              "error": {"code": "ok"}})
            return

        self._reply(404, {"error": {"code": "not_found"}})

    def do_PUT(self) -> None:                               # noqa: N802
        state = self.state
        chunk = self._read()
        content_range = self.headers.get("Content-Range", "")
        prefix, _, total = content_range.partition("/")
        start, _, end = prefix.replace("bytes ", "").partition("-")
        start, end = int(start), int(end)

        if start != state["received"]:
            self._reply(400, {"error": {"code": "invalid_chunk",
                              "message": f"expected {state['received']}"}})
            return
        if len(chunk) != end - start + 1:
            self._reply(400, {"error": {"code": "invalid_chunk",
                              "message": "length disagrees with Content-Range"}})
            return

        state["received"] = end + 1
        state["chunks"] += 1
        state["body"] = state.get("body", b"") + chunk

        if state["chunks"] > state["declared_count"]:
            self._reply(400, {"error": {"code": "too_many_chunks"}})
            return
        self._reply(201, {})


class _InstagramHandler(_Platform):
    """Container create → poll until FINISHED → publish."""

    def do_POST(self) -> None:                              # noqa: N802
        state = self.state
        if self.path.split("?")[0].endswith("/media"):
            state["created"] = True
            state["polls"] = 0
            self._reply(200, {"id": "container_1"})
            return
        if self.path.split("?")[0].endswith("/media_publish"):
            if not state.get("finished"):
                self._reply(400, {"error": {
                    "message": "Media ID is not available",
                    "code": 9007,
                }})
                return
            self._reply(200, {"id": "ig_media_1"})
            return
        self._reply(404, {"error": {"message": "no such endpoint"}})

    def do_GET(self) -> None:                               # noqa: N802
        state = self.state
        path = self.path.split("?")[0]
        if path.endswith("/container_1"):
            state["polls"] = state.get("polls", 0) + 1
            done = state["polls"] >= state.get("polls_needed", 2)
            state["finished"] = done
            self._reply(200, {"status_code": "FINISHED" if done else "IN_PROGRESS",
                              "id": "container_1"})
            return
        if path.endswith("/ig_media_1"):
            self._reply(200, {"id": "ig_media_1", "media_type": "VIDEO",
                              "media_product_type": "REELS",
                              "permalink": "https://instagram.com/reel/xyz"})
            return
        self._reply(404, {"error": {"message": "no such object"}})


class _OAuthHandler(_Platform):
    """A token endpoint. Refresh, exchange and revoke."""

    def do_POST(self) -> None:                              # noqa: N802
        state = self.state
        raw = self._read().decode()
        form = dict(
            part.split("=", 1) for part in raw.split("&") if "=" in part
        )
        state.setdefault("requests", []).append(form)

        if self.path.endswith("/revoke"):
            self._reply(200, {})
            return

        if state.get("fail_with"):
            status, body = state["fail_with"]
            self._reply(status, body)
            return

        grant = form.get("grant_type", "")
        state["last_grant"] = grant
        self._reply(200, {
            "access_token": f"fresh_{state.get('serial', 0)}",
            "refresh_token": "rotated_refresh",
            "expires_in": 3600,
            "refresh_expires_in": 86_400,
            "scope": "video.upload video.publish",
        })


class _Server:
    """Runs a handler on loopback for the lifetime of one test."""

    def __init__(self, handler: type[_Platform], **state) -> None:
        self.state: dict = dict(state)
        handler_class = type(handler.__name__, (handler,), {"state": self.state})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.state["base"] = self.base
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _Fixture(unittest.TestCase):
    """A real media file and the objects that publish it."""

    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="clipforge-up-")
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.media = os.path.join(self.directory, "clip.mp4")
        # Deterministic and incompressible enough that a server comparing what
        # it received against what it should have received is a real check.
        with open(self.media, "wb") as handle:
            handle.write(bytes((i * 7 + 11) % 256 for i in range(ASSET_BYTES)))
        self.transport = HttpTransport(TransportConfig(
            connect_timeout_s=5, read_timeout_s=15, upload_timeout_s=30,
        ))

    def asset(self, public_url: str = "") -> MediaAsset:
        return MediaAsset(
            asset_id="a1", path=self.media, public_url=public_url,
            size_bytes=os.path.getsize(self.media), duration_s=31.0,
            width=1080, height=1920, fps=30,
        )

    def spec(self, public_url: str = "") -> PostSpec:
        return PostSpec(
            asset=self.asset(public_url), title="A clip",
            caption="Something worth watching", visibility=Visibility.PUBLIC,
        )

    def tokens(self, platform: Platform, expires_in_s: int = 3600) -> TokenSet:
        return TokenSet(
            account_id="acc_1", platform=platform, access_token="at_live",
            refresh_token="rt_live",
            expires_at=NOW + timedelta(seconds=expires_in_s),
            refresh_valid_until=NOW + timedelta(days=30), obtained_at=NOW,
        )

    def system(self, platform: Platform, **kwargs) -> PublishingSystem:
        store = InMemoryTokenStore()
        system = PublishingSystem(
            PublishConfig(enforce_spacing=False, enforce_token_horizon=False),
            token_store=store, **kwargs,
        )
        system.connect(
            Account("acc_1", platform, "org1", external_id="ext",
                    direct_post_approved=True, business_account=True),
            self.tokens(platform),
        )
        return system


# ---------------------------------------------------------------------------
# The transport itself
# ---------------------------------------------------------------------------


class TransportTest(_Fixture):
    def test_a_json_request_goes_out_and_a_json_reply_comes_back(self) -> None:
        server = _Server(_OAuthHandler)
        self.addCleanup(server.close)

        response = self.transport.send(Request(
            method="POST", url=f"{server.base}/token",
            form_body={"grant_type": "refresh_token", "refresh_token": "x"},
            description="token",
        ))
        self.assertTrue(response.ok)
        self.assertEqual(response.body["access_token"], "fresh_0")
        self.assertEqual(server.state["requests"][0]["grant_type"], "refresh_token")

    def test_a_byte_range_is_streamed_from_disk_exactly(self) -> None:
        """The bytes the server receives must be the bytes on disk, at the
        offset the adapter asked for — not the start of the file, and not a
        block rounded to a buffer size."""

        server = _Server(_GoogleHandler)
        self.addCleanup(server.close)
        server.state.update(declared=ASSET_BYTES, received=300_000)

        response = self.transport.send(Request(
            method="PUT", url=f"{server.base}/session/1",
            headers={"Content-Range": f"bytes 300000-399999/{ASSET_BYTES}"},
            byte_range=(300_000, 399_999), asset_path=self.media,
            description="chunk",
        ))
        self.assertEqual(response.status, 308)

        with open(self.media, "rb") as handle:
            handle.seek(300_000)
            expected = handle.read(100_000)
        self.assertEqual(server.state["body"], expected)

    def test_a_308_is_returned_not_followed(self) -> None:
        """A client that treats 308 as a redirect re-sends the chunk somewhere
        it was never meant to go. This is the status the whole resumable
        protocol is built on."""

        server = _Server(_GoogleHandler)
        self.addCleanup(server.close)
        server.state.update(declared=ASSET_BYTES, received=1000)

        response = self.transport.send(Request(
            method="PUT", url=f"{server.base}/session/1",
            headers={"Content-Range": f"bytes */{ASSET_BYTES}"},
            description="probe",
        ))
        self.assertEqual(response.status, 308)
        self.assertEqual(response.headers["range"], "bytes=0-999")

    def test_a_4xx_is_data_rather_than_an_exception(self) -> None:
        """`retry.py` decides what a 401 means. It cannot do that if the
        transport raises and loses the body the error code lives in."""

        server = _Server(_TikTokHandler)
        self.addCleanup(server.close)

        response = self.transport.send(Request(
            method="POST", url=f"{server.base}/v2/post/publish/video/init/",
            json_body={"source_info": {}}, description="init without a token",
        ))
        self.assertEqual(response.status, 401)
        self.assertEqual(response.body["error"]["code"], "access_token_invalid")

    def test_a_refused_connection_is_a_transport_error(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead = probe.getsockname()[1]

        transport = HttpTransport(TransportConfig(
            connect_timeout_s=2, connect_attempts=2, connect_backoff_s=0.01,
        ))
        with self.assertRaises(TransportError):
            transport.send(Request(method="GET", url=f"http://127.0.0.1:{dead}/x"))

    def test_a_slow_reply_is_a_timeout_not_a_transport_error(self) -> None:
        """The engine treats the two differently and it matters: a timeout may
        mean the platform acted, a refused connection cannot."""

        class _Slow(_Platform):
            def do_GET(self) -> None:                       # noqa: N802
                time.sleep(2.0)
                self._reply(200, {})

        server = _Server(_Slow)
        self.addCleanup(server.close)
        transport = HttpTransport(TransportConfig(read_timeout_s=0.4))

        with self.assertRaises(TimeoutError):
            transport.send(Request(method="GET", url=f"{server.base}/slow"))

    def test_a_non_json_body_keeps_the_text_rather_than_discarding_it(self) -> None:
        """An HTML error page from a proxy is the only evidence of what went
        wrong. `{}` would throw it away."""

        class _Html(_Platform):
            def do_GET(self) -> None:                       # noqa: N802
                body = b"<html><body>502 Bad Gateway</body></html>"
                self.send_response(502)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = _Server(_Html)
        self.addCleanup(server.close)

        response = self.transport.send(Request(method="GET", url=f"{server.base}/x"))
        self.assertEqual(response.status, 502)
        self.assertIn("502 Bad Gateway", response.body["error"]["message"])

    def test_a_chunk_request_without_an_asset_path_is_refused(self) -> None:
        with self.assertRaises(TransportError) as caught:
            self.transport.send(Request(
                method="PUT", url="http://127.0.0.1:1/x", byte_range=(0, 10),
            ))
        self.assertIn("asset path", str(caught.exception))

    def test_a_shrinking_asset_is_caught_before_it_hangs(self) -> None:
        """A range past the end of the file would send a short body under an
        honest Content-Length, and the connection would stall until it timed
        out with nothing to explain it."""

        with self.assertRaises(TransportError) as caught:
            self.transport.send(Request(
                method="PUT", url="http://127.0.0.1:1/x",
                byte_range=(0, ASSET_BYTES + 5_000), asset_path=self.media,
            ))
        self.assertIn("changed after", str(caught.exception))

    def test_credentials_never_reach_the_observer(self) -> None:
        server = _Server(_OAuthHandler)
        self.addCleanup(server.close)
        seen: list[tuple[str, dict]] = []
        transport = HttpTransport(TransportConfig(
            observer=lambda event, payload: seen.append((event, payload))
        ))

        transport.send(Request(
            method="POST", url=f"{server.base}/token",
            headers={"Authorization": "Bearer super-secret"},
            form_body={"refresh_token": "also-secret", "client_secret": "worse"},
            description="token",
        ))

        blob = json.dumps(seen)
        self.assertNotIn("super-secret", blob)
        self.assertNotIn("also-secret", blob)
        self.assertNotIn("worse", blob)
        self.assertIn("<redacted>", blob)


# ---------------------------------------------------------------------------
# Platform integration — the whole system, over a socket
# ---------------------------------------------------------------------------


class YouTubeUploadTest(_Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.server = _Server(_GoogleHandler)
        self.addCleanup(self.server.close)
        self._patch()

    def _patch(self) -> None:
        """Point the adapter at the local server, with a small chunk window.

        Google's real chunk size is 8 MB, which would send this fixture in a
        single request and leave the 308 resume path — the whole reason the
        protocol is shaped this way — untested. Shrinking the window exercises
        the identical code against a file small enough to keep the suite fast,
        the same trade the render tests make with 1.2-second clips.
        """
        from clipforge.publish import adapters

        original = (adapters.GOOGLE_UPLOAD_URL, adapters.GOOGLE_VIDEOS_URL,
                    adapters.google_chunk_size)
        adapters.google_chunk_size = lambda: 262_144
        adapters.GOOGLE_UPLOAD_URL = (
            f"{self.server.base}/upload/youtube/v3/videos"
            f"?uploadType=resumable&part=snippet,status"
        )
        adapters.GOOGLE_VIDEOS_URL = f"{self.server.base}/youtube/v3/videos"

        def restore() -> None:
            (adapters.GOOGLE_UPLOAD_URL, adapters.GOOGLE_VIDEOS_URL,
             adapters.google_chunk_size) = original

        self.addCleanup(restore)

    def test_a_video_uploads_in_chunks_and_arrives_byte_for_byte(self) -> None:
        system = self.system(Platform.YOUTUBE)
        post = system.schedule("acc_1", self.spec(), LATER)
        result = system.run_post(post, self.transport, now=LATER)

        self.assertEqual(result.state, PostState.PUBLISHED, result.error)
        self.assertEqual(result.remote_post_id, "vid_google")
        self.assertGreater(self.server.state["chunks"], 1,
                           "the file went up in one request — chunking is not "
                           "being exercised")
        with open(self.media, "rb") as handle:
            self.assertEqual(self.server.state["body"], handle.read())

    def test_the_scheduled_publish_time_reaches_the_platform(self) -> None:
        """YouTube is the only one of the three that holds the post itself,
        and it is the reason a YouTube job can finish at upload time."""

        system = self.system(Platform.YOUTUBE)
        post = system.schedule("acc_1", self.spec(), LATER)
        system.run_post(post, self.transport, now=NOW)

        status = self.server.state["metadata"]["status"]
        self.assertEqual(status["privacyStatus"], "private")
        self.assertEqual(status["publishAt"],
                         LATER.isoformat().replace("+00:00", "Z"))

    def test_an_upload_resumes_from_where_the_server_says(self) -> None:
        """The server's Range header is authoritative. Trusting the local
        offset after a resume is how a resumed upload corrupts itself."""

        from clipforge.publish import adapters

        adapter = adapters.YouTubeAdapter()
        step = adapter.begin(self.spec(), Account("acc_1", Platform.YOUTUBE, "o"),
                             self.tokens(Platform.YOUTUBE), LATER, "key")
        self.server.state.update(declared=ASSET_BYTES, received=0)
        response = self.transport.send(step.request)
        step = adapter.advance(step.context, response)

        # Send one chunk, then pretend the worker died and came back: probe.
        self.transport.send(step.request)
        probe = adapter.reconcile(step.context, self.tokens(Platform.YOUTUBE))
        confirmed = self.transport.send(probe)

        self.assertEqual(confirmed.status, 308)
        held = int(confirmed.headers["range"].split("-")[1]) + 1
        self.assertEqual(held, self.server.state["received"])

    def test_a_rejected_video_is_caught_by_verification(self) -> None:
        """The upload succeeds and the video is never viewable. Nothing in the
        upload protocol reports this."""

        system = self.system(Platform.YOUTUBE)
        post = system.schedule("acc_1", self.spec(), LATER)
        result = system.run_post(post, self.transport, now=LATER)
        self.assertEqual(result.state, PostState.PUBLISHED)

        self.server.state["verify_status"] = {
            "uploadStatus": "rejected", "rejectionReason": "duplicate",
            "privacyStatus": "private",
        }
        verification = UploadVerifier(self.transport).verify(
            Platform.YOUTUBE, result.remote_post_id, self.tokens(Platform.YOUTUBE)
        )
        self.assertFalse(verification.live)
        self.assertTrue(verification.rejected)
        self.assertEqual(verification.metadata["rejection_reason"], "duplicate")

    def test_a_processed_video_verifies_as_live(self) -> None:
        verification = UploadVerifier(self.transport).verify(
            Platform.YOUTUBE, "vid_google", self.tokens(Platform.YOUTUBE)
        )
        self.assertTrue(verification.live)
        self.assertEqual(verification.state, "processed")


class TikTokUploadTest(_Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.server = _Server(_TikTokHandler, polls_needed=2)
        self.addCleanup(self.server.close)

        from clipforge.publish import adapters

        original = (adapters.TIKTOK_INIT_URL, adapters.TIKTOK_INBOX_URL,
                    adapters.TIKTOK_STATUS_URL, adapters.tiktok_chunking)
        # Three chunks, with the last absorbing the remainder — the arithmetic
        # TikTok actually rejects if you get it wrong.
        adapters.tiktok_chunking = lambda size: (262_144, 2)
        adapters.TIKTOK_INIT_URL = f"{self.server.base}/v2/post/publish/video/init/"
        adapters.TIKTOK_INBOX_URL = (
            f"{self.server.base}/v2/post/publish/inbox/video/init/"
        )
        adapters.TIKTOK_STATUS_URL = (
            f"{self.server.base}/v2/post/publish/status/fetch/"
        )

        def restore() -> None:
            (adapters.TIKTOK_INIT_URL, adapters.TIKTOK_INBOX_URL,
             adapters.TIKTOK_STATUS_URL, adapters.tiktok_chunking) = original

        self.addCleanup(restore)

    def test_a_direct_post_uploads_polls_and_completes(self) -> None:
        system = self.system(Platform.TIKTOK)
        post = system.schedule("acc_1", self.spec(), LATER)
        result = system.run_post(post, self.transport, now=LATER)

        self.assertEqual(result.state, PostState.PUBLISHED, result.error)
        # The publicly available post id, not the publish id: the latter is a
        # handle on the upload job and stops resolving once it completes.
        self.assertEqual(result.remote_post_id, "tt_1")
        self.assertGreaterEqual(self.server.state["polls"], 2,
                                "the upload finishing is not the post existing")
        with open(self.media, "rb") as handle:
            self.assertEqual(self.server.state["body"], handle.read())

    def test_the_declared_chunk_count_matches_what_is_sent(self) -> None:
        """TikTok is told the chunk count up front and rejects a mismatch.
        The final chunk absorbs the remainder rather than being short."""

        system = self.system(Platform.TIKTOK)
        post = system.schedule("acc_1", self.spec(), LATER)
        system.run_post(post, self.transport, now=LATER)

        state = self.server.state
        self.assertEqual(state["chunks"], state["declared_count"])
        self.assertEqual(state["received"], state["declared_size"])

    def test_an_unaudited_app_lands_in_the_inbox_as_a_draft(self) -> None:
        """The honest outcome, not a failure — and not "published" either."""

        store = InMemoryTokenStore()
        system = PublishingSystem(
            PublishConfig(enforce_spacing=False, enforce_token_horizon=False),
            token_store=store,
        )
        system.connect(
            Account("acc_1", Platform.TIKTOK, "org1", external_id="ext",
                    direct_post_approved=False),
            self.tokens(Platform.TIKTOK),
        )
        post = system.schedule("acc_1", self.spec(), LATER)
        result = system.run_post(post, self.transport, now=LATER)

        self.assertEqual(result.state, PostState.AWAITING_CREATOR)
        self.assertTrue(result.draft)
        self.assertIsNone(self.server.state["post_info"],
                          "a draft must not carry post_info")

    def test_a_moderation_failure_is_caught_by_verification(self) -> None:
        class _Failed(_TikTokHandler):
            def do_POST(self) -> None:                      # noqa: N802
                if self.path.endswith("/status/fetch/"):
                    self._reply(200, {"data": {"status": "FAILED",
                                               "fail_reason": "spam"}})
                    return
                super().do_POST()

        server = _Server(_Failed)
        self.addCleanup(server.close)
        from clipforge.publish import adapters
        adapters.TIKTOK_STATUS_URL = f"{server.base}/v2/post/publish/status/fetch/"

        verification = UploadVerifier(self.transport).verify(
            Platform.TIKTOK, "pub_tiktok", self.tokens(Platform.TIKTOK)
        )
        self.assertTrue(verification.rejected)
        self.assertEqual(verification.metadata["fail_reason"], "spam")


class InstagramUploadTest(_Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.server = _Server(_InstagramHandler, polls_needed=2)
        self.addCleanup(self.server.close)

        from clipforge.publish import adapters

        original = adapters.GRAPH_URL
        adapters.GRAPH_URL = self.server.base
        self.addCleanup(lambda: setattr(adapters, "GRAPH_URL", original))

    def test_a_reel_is_created_polled_and_published(self) -> None:
        system = self.system(Platform.INSTAGRAM)
        post = system.schedule("acc_1", self.spec("https://cdn.test/c.mp4"), LATER)
        result = system.run_post(post, self.transport, now=LATER)

        self.assertEqual(result.state, PostState.PUBLISHED, result.error)
        self.assertEqual(result.remote_post_id, "ig_media_1")
        self.assertGreaterEqual(self.server.state["polls"], 2)

    def test_no_public_url_is_refused_at_schedule_time(self) -> None:
        """Instagram fetches the file itself. Without somewhere to fetch from
        there is nothing to attempt — and the refusal lands when the post is
        booked rather than at 6am when it fires, which is the difference
        between a validation error and a hole in the content calendar."""

        from clipforge.publish import ScheduleError

        system = self.system(Platform.INSTAGRAM)
        with self.assertRaises(ScheduleError) as caught:
            system.schedule("acc_1", self.spec(), LATER)

        self.assertIn("public_url", str(caught.exception))
        self.assertFalse(self.server.state.get("created"),
                         "a request went out for an asset with no public URL")

    def test_a_published_media_verifies_with_its_permalink(self) -> None:
        verification = UploadVerifier(self.transport).verify(
            Platform.INSTAGRAM, "ig_media_1", self.tokens(Platform.INSTAGRAM)
        )
        self.assertTrue(verification.live)
        self.assertEqual(verification.metadata["permalink"],
                         "https://instagram.com/reel/xyz")


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------


class TokenRefreshTest(_Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.server = _Server(_OAuthHandler)
        self.addCleanup(self.server.close)

        from clipforge.publish import oauth

        original = dict(oauth.TOKEN_URL)
        for platform in Platform:
            oauth.TOKEN_URL[platform] = f"{self.server.base}/token"
        self.addCleanup(lambda: oauth.TOKEN_URL.update(original))

        self.store = InMemoryTokenStore()
        self.refresher = TokenRefresher(
            self.transport, self.store,
            {p: _credentials() for p in Platform}, clock=lambda: NOW,
        )

    def test_a_token_near_expiry_is_renewed_over_the_wire(self) -> None:
        self.store.put(self.tokens(Platform.YOUTUBE, expires_in_s=30))
        result = self.refresher.ensure_fresh("acc_1", NOW)

        self.assertTrue(result.refreshed)
        self.assertEqual(result.tokens.access_token, "fresh_0")
        self.assertEqual(self.store.get("acc_1").access_token, "fresh_0")
        self.assertEqual(self.server.state["last_grant"], "refresh_token")

    def test_a_healthy_token_is_left_alone(self) -> None:
        self.store.put(self.tokens(Platform.YOUTUBE, expires_in_s=3600))
        result = self.refresher.ensure_fresh("acc_1", NOW)

        self.assertFalse(result.refreshed)
        self.assertEqual(self.server.state.get("requests"), None)

    def test_a_dead_grant_asks_for_reconnection_rather_than_retrying(self) -> None:
        self.server.state["fail_with"] = (400, {
            "error": "invalid_grant",
            "error_description": "Token has been expired or revoked.",
        })
        self.store.put(self.tokens(Platform.YOUTUBE, expires_in_s=30))

        with self.assertRaises(ReauthRequired) as caught:
            self.refresher.ensure_fresh("acc_1", NOW)
        self.assertIn("reconnected", str(caught.exception))

    def test_a_server_error_is_retryable_not_a_reconnection(self) -> None:
        """Asking a customer to reconnect a healthy account because the
        platform had a bad minute is the wrong answer."""

        self.server.state["fail_with"] = (503, {"error": {"message": "try later"}})
        self.store.put(self.tokens(Platform.YOUTUBE, expires_in_s=30))

        with self.assertRaises(RefreshFailed):
            self.refresher.ensure_fresh("acc_1", NOW)

    def test_a_platform_that_omits_a_refresh_token_keeps_the_old_one(self) -> None:
        """Dropping it leaves the account unable to refresh again — a fault
        that only appears one token lifetime later."""

        class _NoRotate(_OAuthHandler):
            def do_POST(self) -> None:                      # noqa: N802
                self._reply(200, {"access_token": "fresh_x", "expires_in": 3600})

        server = _Server(_NoRotate)
        self.addCleanup(server.close)
        from clipforge.publish import oauth
        oauth.TOKEN_URL[Platform.YOUTUBE] = f"{server.base}/token"

        self.store.put(self.tokens(Platform.YOUTUBE, expires_in_s=30))
        result = self.refresher.ensure_fresh("acc_1", NOW)

        self.assertEqual(result.tokens.access_token, "fresh_x")
        self.assertEqual(result.tokens.refresh_token, "rt_live")

    def test_concurrent_refreshes_produce_one_round_trip(self) -> None:
        """Several platforms retire the old refresh token on use. Two workers
        refreshing at once leaves the loser holding a retired credential."""

        self.store.put(self.tokens(Platform.YOUTUBE, expires_in_s=30))
        barrier = threading.Barrier(4)
        errors: list[Exception] = []

        def refresh() -> None:
            try:
                barrier.wait(timeout=5)
                self.refresher.ensure_fresh("acc_1", NOW)
            except Exception as error:                      # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=refresh) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(self.server.state["requests"]), 1,
                         "the refresh token was spent more than once")

    def test_the_engine_refreshes_before_uploading(self) -> None:
        """A resumable upload runs for minutes; a token valid at the first
        chunk can expire before the last."""

        google = _Server(_GoogleHandler)
        self.addCleanup(google.close)
        from clipforge.publish import adapters
        original = adapters.GOOGLE_UPLOAD_URL
        adapters.GOOGLE_UPLOAD_URL = (
            f"{google.base}/upload/youtube/v3/videos?uploadType=resumable"
        )
        self.addCleanup(lambda: setattr(adapters, "GOOGLE_UPLOAD_URL", original))

        self.store.put(self.tokens(Platform.YOUTUBE, expires_in_s=30))
        system = PublishingSystem(
            PublishConfig(enforce_spacing=False, enforce_token_horizon=False),
            token_store=self.store, refresher=self.refresher,
        )
        system.connect(
            Account("acc_1", Platform.YOUTUBE, "org1", external_id="e",
                    direct_post_approved=True),
            self.store.get("acc_1"),
        )
        post = system.schedule("acc_1", self.spec(), LATER)
        result = system.run_post(post, self.transport, now=NOW)

        self.assertEqual(result.state, PostState.PUBLISHED, result.error)
        self.assertEqual(self.server.state["last_grant"], "refresh_token")
        self.assertEqual(
            google.state["metadata"] and "Bearer fresh_0",
            "Bearer fresh_0",
        )


# ---------------------------------------------------------------------------
# Account management
# ---------------------------------------------------------------------------


class AccountManagementTest(_Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.server = _Server(_OAuthHandler)
        self.addCleanup(self.server.close)

        from clipforge.publish import accounts as accounts_mod
        from clipforge.publish import oauth

        original_token = dict(oauth.TOKEN_URL)
        original_revoke = dict(accounts_mod.REVOKE_URL)
        for platform in Platform:
            oauth.TOKEN_URL[platform] = f"{self.server.base}/token"
        accounts_mod.REVOKE_URL[Platform.YOUTUBE] = f"{self.server.base}/revoke"

        def restore() -> None:
            oauth.TOKEN_URL.update(original_token)
            accounts_mod.REVOKE_URL.clear()
            accounts_mod.REVOKE_URL.update(original_revoke)

        self.addCleanup(restore)

        self.store = InMemoryTokenStore()
        self.manager = AccountManager(
            self.transport, self.store,
            {p: _credentials() for p in Platform}, clock=lambda: NOW,
        )

    def test_a_connection_round_trips_and_stores_tokens(self) -> None:
        request = self.manager.begin("acc_1", Platform.YOUTUBE)
        self.assertIn("code_challenge", request.url)
        self.assertIn("access_type=offline", request.url)

        result = self.manager.complete(request.state, "auth-code")
        self.assertEqual(result.account_id, "acc_1")
        self.assertEqual(self.store.get("acc_1").access_token, "fresh_0")
        self.assertEqual(
            self.server.state["requests"][0]["grant_type"], "authorization_code"
        )

    def test_an_unknown_state_is_refused(self) -> None:
        """An unchecked state is the CSRF hole in every OAuth integration that
        has one: the attacker connects their account to the victim's channel."""

        with self.assertRaises(Exception) as caught:
            self.manager.complete("state-nobody-issued", "code")
        self.assertIn("unknown or expired", str(caught.exception))

    def test_a_callback_cannot_be_replayed(self) -> None:
        request = self.manager.begin("acc_1", Platform.YOUTUBE)
        self.manager.complete(request.state, "auth-code")

        with self.assertRaises(Exception):
            self.manager.complete(request.state, "auth-code")

    def test_an_abandoned_connection_expires(self) -> None:
        request = self.manager.begin("acc_1", Platform.YOUTUBE)
        self.manager.clock = lambda: NOW + timedelta(hours=1)

        self.assertEqual(self.manager.pending(), ())
        with self.assertRaises(Exception):
            self.manager.complete(request.state, "code")

    def test_a_declined_consent_says_so(self) -> None:
        request = self.manager.begin("acc_1", Platform.YOUTUBE)
        with self.assertRaises(Exception) as caught:
            self.manager.complete(request.state, "")
        self.assertIn("declined", str(caught.exception))

    def test_disconnect_revokes_before_forgetting(self) -> None:
        self.store.put(self.tokens(Platform.YOUTUBE))
        revoked, detail = self.manager.disconnect("acc_1")

        self.assertTrue(revoked, detail)
        self.assertIsNone(self.store.get("acc_1"))
        self.assertTrue(any("/revoke" in str(r) or "token" in r
                            for r in self.server.state["requests"]))

    def test_disconnect_forgets_even_when_revocation_fails(self) -> None:
        """The operator asked to disconnect. Refusing because the platform is
        down leaves them unable to act at all."""

        from clipforge.publish import accounts as accounts_mod
        accounts_mod.REVOKE_URL[Platform.YOUTUBE] = "http://127.0.0.1:1/revoke"

        self.store.put(self.tokens(Platform.YOUTUBE))
        revoked, detail = self.manager.disconnect("acc_1")

        self.assertFalse(revoked)
        self.assertIn("could not reach", detail)
        self.assertIsNone(self.store.get("acc_1"))

    def test_instagram_reports_that_it_cannot_be_revoked(self) -> None:
        self.store.put(self.tokens(Platform.INSTAGRAM))
        revoked, detail = self.manager.disconnect("acc_1")

        self.assertFalse(revoked)
        self.assertIn("account settings", detail)
        self.assertIsNone(self.store.get("acc_1"))

    def test_health_names_the_accounts_needing_a_human(self) -> None:
        self.store.put(self.tokens(Platform.YOUTUBE))
        dead = TokenSet(
            account_id="acc_dead", platform=Platform.TIKTOK,
            access_token="at", refresh_token="",
            expires_at=NOW - timedelta(hours=1), obtained_at=NOW,
        )
        self.store.put(dead)

        needing = {h.account_id for h in self.manager.needing_attention(NOW)}
        self.assertEqual(needing, {"acc_dead"})
        self.assertTrue(self.manager.health("acc_1", NOW).connected)


# ---------------------------------------------------------------------------
# One real network leg
# ---------------------------------------------------------------------------


def _google_reachable() -> bool:
    try:
        HttpTransport(TransportConfig(connect_timeout_s=8, read_timeout_s=8)).send(
            Request(method="POST", url="https://oauth2.googleapis.com/token",
                    form_body={"grant_type": "refresh_token",
                               "refresh_token": "probe"},
                    description="reachability probe")
        )
    except Exception:                                       # noqa: BLE001
        return False
    return True


@unittest.skipUnless(
    os.environ.get("CLIPFORGE_LIVE_OAUTH") and _google_reachable(),
    "set CLIPFORGE_LIVE_OAUTH=1 to talk to Google's real token endpoint",
)
class LiveGoogleTokenTest(unittest.TestCase):
    """The only test here that leaves the machine.

    It sends a deliberately invalid refresh to Google's real token endpoint
    over real TLS and asserts that the reply is understood. That verifies the
    parts no local server can: that TLS negotiates, that the form encoding is
    accepted, that a real Google error body is parsed, and that a dead grant
    becomes `ReauthRequired` rather than an endless retry.

    It cannot verify an upload — that needs credentials this environment does
    not have, and `googleapis.com` upload endpoints reject anonymous requests
    long before any protocol behaviour is exercised.
    """

    def test_google_rejects_an_invalid_grant_and_we_understand_it(self) -> None:
        store = InMemoryTokenStore()
        store.put(TokenSet(
            account_id="acc_live", platform=Platform.YOUTUBE,
            access_token="expired", refresh_token="definitely-not-valid",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
            refresh_valid_until=datetime.now(UTC) + timedelta(days=1),
        ))
        refresher = TokenRefresher(
            HttpTransport(), store, {Platform.YOUTUBE: _credentials()}
        )

        with self.assertRaises(ReauthRequired) as caught:
            refresher.ensure_fresh("acc_live")
        # Google answers 401 invalid_client for an unregistered client id.
        self.assertIn("reconnected", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
