"""Per-platform upload state machines.

Each adapter is a pure state machine: given where it is and what the platform
just said, it returns the next `Step` — make this request, wait and poll again,
you are done, or this failed. It never performs I/O. A `Transport` does that,
which means every branch of every platform's protocol is exercisable offline
with a scripted transport, and the credentials never reach the layer that
formats logs.

The three protocols have nothing in common, which is most of the work:

**YouTube** is a Google resumable upload. Initiate to get a session URI, then
PUT byte ranges; a `308` means "still going, here is how much I have", which
is also how you resume after a crash — send `Content-Range: bytes *​/total` and
the server tells you where it got to. It is the only one of the three that
accepts a future publish time.

**TikTok** initialises with the exact file size, chunk size and chunk count
declared up front, PUTs the chunks, and then makes you *poll* — the upload
finishing is not the post existing. The chunk arithmetic is fussy: the final
chunk absorbs the remainder rather than being a short chunk of its own.

**Instagram** never receives bytes at all. You hand it a public URL, it fetches
and transcodes asynchronously, you poll a container until `FINISHED`, and then
a *second* call publishes it. The container is discarded after 24 hours, so a
system that creates containers early and publishes later has a bug waiting for
its first slow day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from . import limits as limits_mod
from .limits import (
    INSTAGRAM_CONTAINER_TTL_S,
    google_chunk_size,
    is_short_form,
    tiktok_chunking,
)
from .oauth import TokenSet
from .types import (
    Account,
    Action,
    Platform,
    PostSpec,
    Request,
    Response,
    Step,
    Visibility,
)

GOOGLE_UPLOAD_URL = (
    "https://www.googleapis.com/upload/youtube/v3/videos"
    "?uploadType=resumable&part=snippet,status"
)
GOOGLE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
TIKTOK_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
TIKTOK_INBOX_URL = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
TIKTOK_STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
GRAPH_URL = "https://graph.facebook.com/v21.0"


class Adapter(Protocol):
    platform: Platform

    def begin(
        self, spec: PostSpec, account: Account, tokens: TokenSet,
        run_at: datetime, idempotency_key: str,
    ) -> Step: ...

    def advance(self, context: dict[str, Any], response: Response) -> Step: ...

    def reconcile(
        self, context: dict[str, Any], tokens: TokenSet
    ) -> Request | None: ...


def _bearer(tokens: TokenSet) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens.access_token}"}


# --- YouTube -----------------------------------------------------------------


class YouTubeAdapter:
    platform = Platform.YOUTUBE

    def begin(
        self, spec: PostSpec, account: Account, tokens: TokenSet,
        run_at: datetime, idempotency_key: str,
    ) -> Step:
        asset = spec.asset
        gap = limits_mod.automation_gap(account)

        status: dict[str, Any] = {
            "selfDeclaredMadeForKids": spec.made_for_kids,
        }

        # Server-side scheduling: YouTube holds the post itself. Requires the
        # video to start private, and is the only genuine hand-off of the three
        # platforms — the job can be considered done the moment it uploads.
        wants_public = spec.visibility is Visibility.PUBLIC
        if wants_public and not gap:
            status["privacyStatus"] = "private"
            status["publishAt"] = run_at.isoformat().replace("+00:00", "Z")
        else:
            status["privacyStatus"] = (
                "private" if gap else spec.visibility.value
            )

        body = {
            "snippet": {
                "title": spec.title[:100],
                "description": spec.caption_for(Platform.YOUTUBE),
                "categoryId": spec.category_id,
                "tags": list(spec.hashtags),
            },
            "status": status,
        }

        request = Request(
            method="POST",
            url=GOOGLE_UPLOAD_URL,
            headers={
                **_bearer(tokens),
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(asset.size_bytes),
                "X-Upload-Content-Type": "video/mp4",
            },
            json_body=body,
            description="youtube: open a resumable upload session",
        )

        return Step(
            Action.REQUEST,
            request=request,
            context={
                "phase": "init",
                "size": asset.size_bytes,
                "offset": 0,
                "chunk": google_chunk_size(),
                "access_token": tokens.access_token,
                "asset_path": asset.path,
                "scheduled": "publishAt" in status,
                "short_form": is_short_form(asset, Platform.YOUTUBE),
                "idempotency_key": idempotency_key,
            },
        )

    def advance(self, context: dict[str, Any], response: Response) -> Step:
        phase = context.get("phase")

        if phase == "init":
            session = response.headers.get("Location") or response.headers.get(
                "location", ""
            )
            if not session:
                return Step(
                    Action.ERROR,
                    error_code="no_session_uri",
                    error_message="resumable init returned no Location header",
                    context=context,
                )
            context = {**context, "phase": "upload", "session_uri": session}
            return self._chunk(context)

        if phase == "upload":
            if response.status in (200, 201):
                video_id = str(response.body.get("id", ""))
                if not video_id:
                    return Step(
                        Action.ERROR,
                        error_code="no_video_id",
                        error_message="upload completed with no video id",
                        context=context,
                    )
                return Step(Action.DONE, remote_post_id=video_id,
                            context={**context, "phase": "done"})

            if response.status == 308:
                # "Resume Incomplete". The Range header is authoritative about
                # how much the server actually holds — trusting the local
                # offset instead is how a resumed upload corrupts itself.
                confirmed = _google_confirmed_bytes(response)
                offset = confirmed if confirmed is not None else context["offset"]
                return self._chunk({**context, "offset": offset})

            return Step(
                Action.ERROR,
                error_code=f"http_{response.status}",
                error_message="unexpected status during resumable upload",
                context=context,
            )

        return Step(Action.ERROR, error_code="bad_phase",
                    error_message=f"unknown phase {phase!r}", context=context)

    def _chunk(self, context: dict[str, Any]) -> Step:
        size = context["size"]
        offset = context["offset"]
        chunk = context["chunk"]

        if offset >= size:
            # Everything is sent; ask the server to confirm completion.
            request = Request(
                method="PUT",
                url=context["session_uri"],
                headers={"Content-Range": f"bytes */{size}"},
                description="youtube: query upload status",
            )
            return Step(Action.REQUEST, request=request, context=context)

        end = min(offset + chunk, size) - 1
        request = Request(
            method="PUT",
            url=context["session_uri"],
            headers={
                "Content-Length": str(end - offset + 1),
                "Content-Range": f"bytes {offset}-{end}/{size}",
            },
            byte_range=(offset, end),
            asset_path=context.get("asset_path", ""),
            description=(
                f"youtube: upload bytes {offset}-{end} of {size}"
            ),
        )
        return Step(
            Action.REQUEST,
            request=request,
            context={**context, "offset": end + 1},
        )

    def reconcile(
        self, context: dict[str, Any], tokens: TokenSet
    ) -> Request | None:
        """Ask the session how much exists, without sending anything.

        A zero-length `bytes *​/total` probe is the whole reason Google's
        resumable protocol is safe to retry: it is a question, not a write.
        """
        session = context.get("session_uri")
        if not session:
            return None
        return Request(
            method="PUT",
            url=session,
            headers={"Content-Range": f"bytes */{context['size']}"},
            description="youtube: reconcile — how much of this upload exists?",
        )


def _google_confirmed_bytes(response: Response) -> int | None:
    """Bytes the server holds, from a 308's Range header."""
    raw = response.headers.get("Range") or response.headers.get("range", "")
    if not raw or "-" not in raw:
        return 0 if raw == "" else None
    try:
        return int(raw.split("-")[-1]) + 1
    except ValueError:
        return None


# --- TikTok ------------------------------------------------------------------


class TikTokAdapter:
    platform = Platform.TIKTOK

    def begin(
        self, spec: PostSpec, account: Account, tokens: TokenSet,
        run_at: datetime, idempotency_key: str,
    ) -> Step:
        asset = spec.asset
        chunk, count = tiktok_chunking(asset.size_bytes)
        gap = limits_mod.automation_gap(account)

        source = {
            "source": "FILE_UPLOAD",
            "video_size": asset.size_bytes,
            "chunk_size": chunk,
            "total_chunk_count": count,
        }

        if gap:
            # Unaudited: the only thing available is dropping a draft into the
            # creator's inbox. There is no post_info — a human writes the
            # caption and taps publish. Calling this "scheduled publishing"
            # would be a lie, so the plan reports it as a draft.
            request = Request(
                method="POST",
                url=TIKTOK_INBOX_URL,
                headers={**_bearer(tokens),
                         "Content-Type": "application/json; charset=UTF-8"},
                json_body={"source_info": source},
                description="tiktok: upload draft to creator inbox (unaudited)",
            )
        else:
            visibility = {
                Visibility.PUBLIC: "PUBLIC_TO_EVERYONE",
                Visibility.PRIVATE: "SELF_ONLY",
                Visibility.FOLLOWERS: "FOLLOWER_OF_CREATOR",
                Visibility.UNLISTED: "SELF_ONLY",
            }[spec.visibility]
            request = Request(
                method="POST",
                url=TIKTOK_INIT_URL,
                headers={**_bearer(tokens),
                         "Content-Type": "application/json; charset=UTF-8"},
                json_body={
                    "post_info": {
                        "title": spec.caption_for(Platform.TIKTOK)[:2200],
                        "privacy_level": visibility,
                        "disable_duet": False,
                        "disable_comment": False,
                        "disable_stitch": False,
                    },
                    "source_info": source,
                },
                description="tiktok: initialise a direct post",
            )

        return Step(
            Action.REQUEST,
            request=request,
            context={
                "phase": "init",
                "size": asset.size_bytes,
                "chunk": chunk,
                "chunk_count": count,
                "chunk_index": 0,
                "access_token": tokens.access_token,
                "asset_path": asset.path,
                "draft_only": bool(gap),
                "idempotency_key": idempotency_key,
                "polls": 0,
            },
        )

    def advance(self, context: dict[str, Any], response: Response) -> Step:
        phase = context.get("phase")
        data = response.body.get("data") or {}

        if phase == "init":
            publish_id = data.get("publish_id", "")
            upload_url = data.get("upload_url", "")
            if not publish_id or not upload_url:
                return Step(
                    Action.ERROR,
                    error_code="init_incomplete",
                    error_message="init returned no publish_id or upload_url",
                    context=context,
                )
            return self._chunk({
                **context, "phase": "upload",
                "publish_id": publish_id, "upload_url": upload_url,
            })

        if phase == "upload":
            index = context["chunk_index"]
            if index < context["chunk_count"]:
                return self._chunk(context)
            # Bytes are in. The post does not exist yet.
            return Step(
                Action.WAIT, wait_s=5.0,
                context={**context, "phase": "poll"},
            )

        if phase == "poll":
            status = data.get("status", "")
            if status == "PUBLISH_COMPLETE":
                remote = (
                    data.get("publicaly_available_post_id")
                    or data.get("publicly_available_post_id")
                    or [context.get("publish_id", "")]
                )
                if isinstance(remote, list):
                    remote = remote[0] if remote else context.get("publish_id", "")
                return Step(Action.DONE, remote_post_id=str(remote),
                            context={**context, "phase": "done"})
            if status == "SEND_TO_USER_INBOX":
                # Terminal for an unaudited client: the draft has landed and a
                # human now has to finish it.
                return Step(
                    Action.DONE,
                    remote_post_id=str(context.get("publish_id", "")),
                    context={**context, "phase": "done", "draft": True},
                )
            if status == "FAILED":
                return Step(
                    Action.ERROR,
                    error_code=str(data.get("fail_reason", "publish_failed")),
                    error_message="TikTok rejected the post after upload",
                    context=context,
                )
            polls = context.get("polls", 0) + 1
            if polls > 60:
                return Step(
                    Action.ERROR,
                    error_code="processing_timeout",
                    error_message=f"still {status!r} after {polls} polls",
                    context=context,
                )
            return Step(Action.WAIT, wait_s=min(30.0, 5.0 + polls),
                        context={**context, "polls": polls})

        return Step(Action.ERROR, error_code="bad_phase",
                    error_message=f"unknown phase {phase!r}", context=context)

    def _chunk(self, context: dict[str, Any]) -> Step:
        index = context["chunk_index"]
        size = context["size"]
        chunk = context["chunk"]
        count = context["chunk_count"]

        start = index * chunk
        # The last chunk takes the remainder rather than being a short chunk of
        # its own — TikTok rejects an undersized chunk in a multi-chunk upload,
        # so an exact ceil-division split fails on almost every real file.
        end = size - 1 if index == count - 1 else min(start + chunk, size) - 1

        request = Request(
            method="PUT",
            url=context["upload_url"],
            headers={
                "Content-Type": "video/mp4",
                "Content-Length": str(end - start + 1),
                "Content-Range": f"bytes {start}-{end}/{size}",
            },
            byte_range=(start, end),
            asset_path=context.get("asset_path", ""),
            description=f"tiktok: chunk {index + 1}/{count}",
        )
        return Step(
            Action.REQUEST, request=request,
            context={**context, "chunk_index": index + 1},
        )

    def poll_request(self, context: dict[str, Any], tokens: TokenSet) -> Request:
        return Request(
            method="POST",
            url=TIKTOK_STATUS_URL,
            headers={**_bearer(tokens),
                     "Content-Type": "application/json; charset=UTF-8"},
            json_body={"publish_id": context.get("publish_id", "")},
            description="tiktok: poll publish status",
        )

    def reconcile(
        self, context: dict[str, Any], tokens: TokenSet
    ) -> Request | None:
        if not context.get("publish_id"):
            return None
        return self.poll_request(context, tokens)


# --- Instagram ----------------------------------------------------------------


class InstagramAdapter:
    platform = Platform.INSTAGRAM

    def begin(
        self, spec: PostSpec, account: Account, tokens: TokenSet,
        run_at: datetime, idempotency_key: str,
    ) -> Step:
        asset = spec.asset
        if not asset.public_url:
            return Step(
                Action.ERROR,
                error_code="missing_public_url",
                error_message=(
                    "Instagram fetches the file itself; MediaAsset.public_url "
                    "is required and must outlive the transcode"
                ),
            )

        request = Request(
            method="POST",
            url=f"{GRAPH_URL}/{account.external_id}/media",
            form_body={
                "media_type": "REELS",
                "video_url": asset.public_url,
                "caption": spec.caption_for(Platform.INSTAGRAM)[:2200],
                "share_to_feed": "true",
                "access_token": tokens.access_token,
            },
            description="instagram: create a Reels container",
        )
        return Step(
            Action.REQUEST,
            request=request,
            context={
                "phase": "container",
                "ig_user_id": account.external_id,
                "access_token": tokens.access_token,
                "idempotency_key": idempotency_key,
                "polls": 0,
                "container_ttl_s": INSTAGRAM_CONTAINER_TTL_S,
            },
        )

    def advance(self, context: dict[str, Any], response: Response) -> Step:
        phase = context.get("phase")
        body = response.body

        if phase == "container":
            container = str(body.get("id", ""))
            if not container:
                return Step(
                    Action.ERROR, error_code="no_container_id",
                    error_message="container creation returned no id",
                    context=context,
                )
            return Step(
                Action.WAIT, wait_s=5.0,
                context={**context, "phase": "processing",
                         "container_id": container},
            )

        if phase == "processing":
            code = body.get("status_code", "")
            if code == "FINISHED":
                return Step(
                    Action.REQUEST,
                    request=Request(
                        method="POST",
                        url=f"{GRAPH_URL}/{context['ig_user_id']}/media_publish",
                        form_body={
                            "creation_id": context["container_id"],
                            "access_token": context["access_token"],
                        },
                        description="instagram: publish the container",
                    ),
                    context={**context, "phase": "publish"},
                )
            if code in ("ERROR", "EXPIRED"):
                return Step(
                    Action.ERROR,
                    error_code=code.lower(),
                    error_message=str(body.get("status", "container failed")),
                    context=context,
                )
            polls = context.get("polls", 0) + 1
            # A container is discarded 24h after creation, so there is a hard
            # ceiling on how long polling can usefully continue.
            if polls > 120:
                return Step(
                    Action.ERROR, error_code="processing_timeout",
                    error_message=f"container still {code!r} after {polls} polls",
                    context=context,
                )
            return Step(Action.WAIT, wait_s=min(30.0, 5.0 + polls),
                        context={**context, "polls": polls})

        if phase == "publish":
            media_id = str(body.get("id", ""))
            if not media_id:
                return Step(
                    Action.ERROR, error_code="no_media_id",
                    error_message="publish returned no media id",
                    context=context,
                )
            return Step(Action.DONE, remote_post_id=media_id,
                        context={**context, "phase": "done"})

        return Step(Action.ERROR, error_code="bad_phase",
                    error_message=f"unknown phase {phase!r}", context=context)

    def poll_request(self, context: dict[str, Any], tokens: TokenSet) -> Request:
        return Request(
            method="GET",
            url=f"{GRAPH_URL}/{context.get('container_id', '')}",
            form_body={"fields": "status_code,status",
                       "access_token": tokens.access_token},
            description="instagram: poll container status",
        )

    def reconcile(
        self, context: dict[str, Any], tokens: TokenSet
    ) -> Request | None:
        """Check the account's recent media for this container.

        Instagram gives no idempotency key, so the only way to answer "did my
        publish land?" is to look at what the account actually has. Anything
        else risks a duplicate Reel.
        """
        if not context.get("ig_user_id"):
            return None
        return Request(
            method="GET",
            url=f"{GRAPH_URL}/{context['ig_user_id']}/media",
            form_body={"fields": "id,timestamp", "limit": "10",
                       "access_token": tokens.access_token},
            description="instagram: reconcile — list recent media",
        )


ADAPTERS: dict[Platform, Adapter] = {
    Platform.YOUTUBE: YouTubeAdapter(),
    Platform.TIKTOK: TikTokAdapter(),
    Platform.INSTAGRAM: InstagramAdapter(),
}


def adapter_for(platform: Platform) -> Adapter:
    return ADAPTERS[platform]


class Transport(Protocol):
    """Performs the I/O an adapter describes."""

    def send(self, request: Request) -> Response: ...


@dataclass
class RecordingTransport:
    """Records requests and replays scripted responses.

    **A test double. Never use it in a deployment** — it reaches no platform
    and reports success for posts that were never sent. `transport.HttpTransport`
    is the real client.

    It stays because it is the right tool for its job: driving every branch of
    every platform's state machine, including the ones a live platform will not
    produce on demand — a 429 with a specific `Retry-After`, a 308 that resumes
    from an awkward offset, a moderation rejection. A publisher whose test suite
    needs live credentials is a publisher whose test suite does not run.

    The protocols are exercised against real sockets too, in
    `tests/test_publish_transport.py`.
    """

    responses: list[Response]
    sent: list[Request] = None   # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.sent is None:
            self.sent = []

    def send(self, request: Request) -> Response:
        self.sent.append(request)
        if not self.responses:
            raise AssertionError(
                f"transport ran out of scripted responses at request "
                f"{len(self.sent)}: {request.description}"
            )
        return self.responses.pop(0)
