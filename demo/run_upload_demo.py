#!/usr/bin/env python3
"""A real upload, over a real socket, end to end.

    python demo/run_upload_demo.py                     # YouTube, chunked
    python demo/run_upload_demo.py --platform tiktok
    python demo/run_upload_demo.py --platform instagram
    python demo/run_upload_demo.py --all --verify
    python demo/run_upload_demo.py --refresh           # the token lifecycle
    python demo/run_upload_demo.py --failures          # 401, 429, 500, timeout

Needs no credentials and reaches no platform. It runs a local server that
speaks the platform's documented protocol, then drives the *production*
client — `HttpTransport`, `PublishingSystem`, `TokenRefresher`,
`UploadVerifier` — at it over TCP. The bytes are streamed off disk, the chunk
arithmetic is real, the `308` resume is real, and the file the server ends up
holding is compared against the file on disk.

What it therefore demonstrates: this repository's upload layer works against
something that behaves the way the documentation says a platform behaves.
What it cannot demonstrate: that TikTok, Google and Meta behave that way.
Point `--base-url` at a real sandbox with real credentials to find out.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))

from clipforge.publish import (  # noqa: E402
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
    UploadVerifier,
    Visibility,
)

# The demo reuses the integration suite's platform servers rather than keeping
# a second, subtly different copy of three protocols in sync.
from test_publish_transport import (  # noqa: E402
    _GoogleHandler,
    _InstagramHandler,
    _OAuthHandler,
    _Server,
    _TikTokHandler,
)

ASSET_BYTES = 900_000
NOW = datetime.now(UTC).replace(microsecond=0)
LATER = NOW + timedelta(hours=2)

CREDENTIALS = ClientCredentials(
    client_id="demo-client", client_secret="demo-secret",
    redirect_uri="https://clipforge.test/callback",
)


def _media(directory: str) -> str:
    path = os.path.join(directory, "clip.mp4")
    with open(path, "wb") as handle:
        handle.write(bytes((i * 7 + 11) % 256 for i in range(ASSET_BYTES)))
    return path


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()[:16]


def _spec(path: str, public_url: str = "") -> PostSpec:
    return PostSpec(
        asset=MediaAsset(
            asset_id="a1", path=path, public_url=public_url,
            size_bytes=os.path.getsize(path), duration_s=31.0,
            width=1080, height=1920, fps=30,
        ),
        title="He lost the deal in one sentence",
        caption="The moment the room turned. #business #startups",
        visibility=Visibility.PUBLIC,
    )


def _tokens(platform: Platform, expires_in_s: int = 3600) -> TokenSet:
    return TokenSet(
        account_id="acc_demo", platform=platform, access_token="at_demo",
        refresh_token="rt_demo",
        expires_at=NOW + timedelta(seconds=expires_in_s),
        refresh_valid_until=NOW + timedelta(days=30), obtained_at=NOW,
    )


def _point_at(platform: Platform, base: str) -> None:
    """Aim the adapter at the local server, with a small chunk window."""
    from clipforge.publish import adapters

    if platform is Platform.YOUTUBE:
        adapters.GOOGLE_UPLOAD_URL = (
            f"{base}/upload/youtube/v3/videos?uploadType=resumable"
            f"&part=snippet,status"
        )
        adapters.GOOGLE_VIDEOS_URL = f"{base}/youtube/v3/videos"
        adapters.google_chunk_size = lambda: 262_144
    elif platform is Platform.TIKTOK:
        adapters.TIKTOK_INIT_URL = f"{base}/v2/post/publish/video/init/"
        adapters.TIKTOK_INBOX_URL = f"{base}/v2/post/publish/inbox/video/init/"
        adapters.TIKTOK_STATUS_URL = f"{base}/v2/post/publish/status/fetch/"
        adapters.tiktok_chunking = lambda size: (262_144, 3)
    else:
        adapters.GRAPH_URL = base


HANDLERS = {
    Platform.YOUTUBE: _GoogleHandler,
    Platform.TIKTOK: _TikTokHandler,
    Platform.INSTAGRAM: _InstagramHandler,
}


def upload(platform: Platform, path: str, verify: bool) -> bool:
    server = _Server(HANDLERS[platform], polls_needed=2)
    try:
        _point_at(platform, server.base)
        transport = HttpTransport(TransportConfig(
            read_timeout_s=15, upload_timeout_s=30,
            observer=lambda event, payload: _trace(event, payload),
        ))
        store = InMemoryTokenStore()
        system = PublishingSystem(
            PublishConfig(enforce_spacing=False, enforce_token_horizon=False),
            token_store=store,
        )
        system.connect(
            Account("acc_demo", platform, "org1", external_id="ext",
                    direct_post_approved=True, business_account=True),
            _tokens(platform),
        )

        public = "https://cdn.clipforge.test/clip.mp4" if (
            platform is Platform.INSTAGRAM) else ""
        print(f"\n  ── {platform.value} " + "─" * (58 - len(platform.value)))
        post = system.schedule("acc_demo", _spec(path, public), LATER)
        result = system.run_post(post, transport, now=LATER)

        print(f"\n  state      {result.state.value}")
        print(f"  remote id  {result.remote_post_id or '—'}")
        print(f"  requests   {result.requests}")
        if result.error:
            print(f"  error      {result.error}")

        received = server.state.get("body")
        if received is not None:
            with open(path, "rb") as handle:
                on_disk = handle.read()
            match = received == on_disk
            print(f"  bytes      {len(received):,} received, "
                  f"sha {_digest(received)} "
                  f"{'== ' + _digest(on_disk) if match else '!= MISMATCH'}")
            print(f"  chunks     {server.state.get('chunks', 0)}")
            if not match:
                return False
        else:
            print("  bytes      none — this platform fetches the file itself")

        if verify and result.remote_post_id:
            verification = UploadVerifier(transport).verify(
                platform, result.remote_post_id, _tokens(platform)
            )
            flag = ("live" if verification.live
                    else "pending" if verification.pending
                    else "unknown" if verification.unknown else "REJECTED")
            print(f"  verified   {flag} ({verification.state}) — "
                  f"{verification.detail}")

        return result.state in (PostState.PUBLISHED, PostState.AWAITING_CREATOR)
    finally:
        server.close()


_TRACE: list[str] = []


def _trace(event: str, payload: dict) -> None:
    if event == "request":
        _TRACE.append(f"    → {payload['method']:5} {payload['description']}")
    else:
        _TRACE.append(
            f"    ← {payload['status']} in {payload['elapsed_s']}s"
            + (f", {payload['bytes_sent']:,}B sent" if payload["bytes_sent"] else "")
        )


def refresh_demo() -> bool:
    """The token lifecycle: connect, renew, and refuse a dead grant."""

    from clipforge.publish import oauth

    server = _Server(_OAuthHandler)
    try:
        for platform in Platform:
            oauth.TOKEN_URL[platform] = f"{server.base}/token"
        transport = HttpTransport(TransportConfig(read_timeout_s=10))
        store = InMemoryTokenStore()

        print("\n  ── token lifecycle " + "─" * 43)

        manager = AccountManager(transport, store,
                                 {p: CREDENTIALS for p in Platform})
        request = manager.begin("acc_demo", Platform.YOUTUBE)
        print(f"\n  1. consent URL issued, state {request.state[:12]}…")
        result = manager.complete(request.state, "code-from-the-redirect")
        print(f"  2. code exchanged → access token "
              f"{result.tokens.access_token}, "
              f"expires {result.tokens.expires_at:%H:%M:%S}")

        store.put(_tokens(Platform.YOUTUBE, expires_in_s=30))
        refresher = TokenRefresher(transport, store,
                                   {p: CREDENTIALS for p in Platform})
        outcome = refresher.ensure_fresh("acc_demo", NOW)
        print(f"  3. near expiry → refreshed={outcome.refreshed}, "
              f"new token {outcome.tokens.access_token}")

        healthy = refresher.ensure_fresh("acc_demo", NOW)
        print(f"  4. still valid → refreshed={healthy.refreshed} "
              f"({healthy.reason})")

        server.state["fail_with"] = (400, {
            "error": "invalid_grant",
            "error_description": "Token has been expired or revoked.",
        })
        store.put(_tokens(Platform.YOUTUBE, expires_in_s=30))
        try:
            refresher.ensure_fresh("acc_demo", NOW)
            print("  5. a dead grant was NOT detected — that is a bug")
            return False
        except ReauthRequired as error:
            print(f"  5. dead grant → reauth required: {str(error)[:80]}…")
        except RefreshFailed:
            print("  5. dead grant misclassified as transient — that is a bug")
            return False

        health = manager.health("acc_demo", NOW)
        print(f"  6. health: connected={health.connected}, "
              f"needs_reauth={health.needs_reauth}")
        return True
    finally:
        server.close()


def failure_demo() -> bool:
    """What each kind of failure turns into."""

    from clipforge.publish import classify
    from clipforge.publish.types import Response

    print("\n  ── failure handling " + "─" * 42)
    print(f"\n  {'situation':<34} {'disposition':<12} next")
    print(f"  {'-' * 34} {'-' * 12} {'-' * 22}")

    cases = [
        ("401 invalid credentials", Response(401, body={
            "error": {"message": "unauthorised"}}), False, False),
        ("429 rate limited (Retry-After 90)", Response(
            429, headers={"retry-after": "90"}), False, False),
        ("500 platform error", Response(500), False, False),
        ("400 bad request", Response(400, body={
            "error": {"message": "title too long"}}), False, False),
        ("timeout, nothing sent yet", None, True, False),
        ("timeout, upload in flight", None, True, True),
    ]
    ok = True
    for label, response, timed_out, in_flight in cases:
        decision = classify(response, 1, Platform.YOUTUBE, NOW,
                            key="demo", timed_out=timed_out,
                            already_in_flight=in_flight)
        delay = (f"in {decision.delay_s:.0f}s" if decision.delay_s
                 else "no retry")
        print(f"  {label:<34} {decision.disposition.value:<12} {delay}")
        if label.startswith("timeout, upload") and not decision.unsafe_to_repeat:
            ok = False
    print("\n  A timeout mid-upload is the one that matters: the platform may")
    print("  already have the post, so it reconciles before sending anything.")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", default="youtube",
                        choices=[p.value for p in Platform])
    parser.add_argument("--all", action="store_true", help="every platform")
    parser.add_argument("--verify", action="store_true",
                        help="read the post back after publishing")
    parser.add_argument("--refresh", action="store_true",
                        help="the token lifecycle instead of an upload")
    parser.add_argument("--failures", action="store_true",
                        help="how each failure is classified")
    parser.add_argument("--trace", action="store_true",
                        help="print every request and reply")
    args = parser.parse_args()

    print("\n  Local servers speaking each platform's documented protocol.")
    print("  No credentials, no network egress, real sockets throughout.")

    ok = True
    if args.refresh:
        ok = refresh_demo() and ok
    if args.failures:
        ok = failure_demo() and ok

    if not args.refresh and not args.failures:
        directory = tempfile.mkdtemp(prefix="clipforge-updemo-")
        try:
            path = _media(directory)
            platforms = (list(Platform) if args.all
                         else [Platform(args.platform)])
            for platform in platforms:
                _TRACE.clear()
                ok = upload(platform, path, args.verify) and ok
                if args.trace:
                    print()
                    for line in _TRACE:
                        print(line)
        finally:
            import shutil
            shutil.rmtree(directory, ignore_errors=True)

    print("\n  " + ("all good\n" if ok else "something failed above\n"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
