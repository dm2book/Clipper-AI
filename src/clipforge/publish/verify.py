"""Asking the platform whether the post really exists.

The state machine reports DONE when the protocol says the upload finished. That
is not the same claim as "the video is on the account", and the gap between
them is where the expensive failures live:

* **YouTube** accepts the bytes and then transcodes. `uploadStatus` can become
  `rejected` — duplicate, claimed audio, terms violation — minutes after a
  perfectly successful upload, and nothing pushes that back to the uploader.
* **TikTok** returns `PUBLISH_COMPLETE`, but a moderation failure arrives as
  `FAILED` with a reason, and the post never appears.
* **Instagram** publishes a container into a media id, and the media id is the
  only proof; a container that expired produces a plausible-looking error and
  no post.

So verification is a separate read, deliberately after the fact, and it is the
only thing in this package that can downgrade a `PUBLISHED` post to
`NEEDS_ATTENTION`.

## It is a read, and reads are safe

Every request here is a GET or a status POST that creates nothing. That is what
makes verification safe to run on a timer, safe to retry, and safe to run
against a post whose state is uncertain — which is exactly the situation after
a timeout, and why `reconcile` on each adapter uses the same shape.

## An unverifiable post is not a failed post

`Verification.unknown` exists because "we could not reach the platform" and
"the platform says this does not exist" must not be the same outcome. Treating
an outage as a missing post would have the system re-upload videos that are
already live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Imported as a module, not as names. Binding the endpoint constants at
# import time freezes them, which makes pointing a deployment at a sandbox or
# a pinned API version impossible without editing source — and silently
# decouples this module from `adapters`, so the two could disagree about where
# a platform lives.
from . import adapters
from .oauth import TokenSet
from .types import Platform, Request, Response

__all__ = ["Verification", "UploadVerifier", "verification_request", "interpret"]


@dataclass(slots=True)
class Verification:
    """What the platform says about a post that was reported as published."""

    platform: Platform
    remote_post_id: str
    #: True only when the platform confirmed the post exists and is usable.
    live: bool = False
    #: True when the platform could not be asked. Not a failure.
    unknown: bool = False
    #: True when the platform says the post exists but is not viewable yet —
    #: still transcoding, or scheduled to go public later.
    pending: bool = False
    state: str = ""
    detail: str = ""
    #: Anything worth keeping for a human: view counts at publish, the
    #: permalink, the rejection reason.
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def rejected(self) -> bool:
        """Confirmed absent or refused. The only case worth alarming on."""
        return not self.live and not self.unknown and not self.pending

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform.value,
            "remote_post_id": self.remote_post_id,
            "live": self.live,
            "pending": self.pending,
            "unknown": self.unknown,
            "state": self.state,
            "detail": self.detail,
            "metadata": self.metadata,
        }


def verification_request(
    platform: Platform, remote_post_id: str, tokens: TokenSet
) -> Request:
    """The read that proves a post exists. Creates nothing."""

    bearer = {"Authorization": f"Bearer {tokens.access_token}"}

    if platform is Platform.YOUTUBE:
        return Request(
            method="GET",
            url=(
                f"{adapters.GOOGLE_VIDEOS_URL}?id={remote_post_id}"
                f"&part=status,processingDetails,snippet"
            ),
            headers=bearer,
            description="youtube: verify the video exists and was accepted",
        )

    if platform is Platform.TIKTOK:
        return Request(
            method="POST",
            url=adapters.TIKTOK_STATUS_URL,
            headers={**bearer, "Content-Type": "application/json; charset=UTF-8"},
            json_body={"publish_id": remote_post_id},
            description="tiktok: verify the publish completed",
        )

    return Request(
        method="GET",
        url=f"{adapters.GRAPH_URL}/{remote_post_id}?fields=id,media_type,media_product_type,permalink",
        headers=bearer,
        description="instagram: verify the media exists",
    )


def interpret(
    platform: Platform, remote_post_id: str, response: Response
) -> Verification:
    """Turn a verification reply into a verdict."""

    result = Verification(platform=platform, remote_post_id=remote_post_id)

    if not response.ok:
        # A 404 is the platform saying it does not exist. Anything else is the
        # platform failing to answer, which says nothing about the post.
        if response.status == 404:
            result.state = "not_found"
            result.detail = f"{platform.value} has no record of {remote_post_id}"
            return result
        result.unknown = True
        result.state = f"http_{response.status}"
        result.detail = (
            f"could not verify {remote_post_id}: {platform.value} answered "
            f"{response.status}"
        )
        return result

    if platform is Platform.YOUTUBE:
        return _youtube(result, response)
    if platform is Platform.TIKTOK:
        return _tiktok(result, response)
    return _instagram(result, response)


def _youtube(result: Verification, response: Response) -> Verification:
    items = response.body.get("items") or []
    if not items:
        result.state = "not_found"
        result.detail = "the video id returned no items — it does not exist"
        return result

    item = items[0] if isinstance(items[0], dict) else {}
    status = item.get("status") or {}
    upload_status = str(status.get("uploadStatus", "")).lower()
    privacy = str(status.get("privacyStatus", ""))
    result.state = upload_status or "unknown"
    result.metadata = {
        "privacy_status": privacy,
        "publish_at": status.get("publishAt", ""),
        "title": (item.get("snippet") or {}).get("title", ""),
    }

    if upload_status == "rejected":
        reason = str(status.get("rejectionReason", "unspecified"))
        result.metadata["rejection_reason"] = reason
        result.detail = (
            f"YouTube rejected the video after upload: {reason}. The upload "
            f"succeeded and the video will never be viewable."
        )
        return result

    if upload_status in ("uploaded", "processing"):
        result.pending = True
        result.detail = "uploaded; YouTube is still processing it"
        return result

    if upload_status == "processed":
        # A scheduled video is private until `publishAt`. That is success, not
        # a pending state — the hand-off to YouTube is what was wanted.
        result.live = True
        result.detail = (
            f"processed and {privacy}"
            + (f", publishing at {status['publishAt']}" if status.get("publishAt") else "")
        )
        return result

    result.unknown = True
    result.detail = f"unrecognised uploadStatus {upload_status!r}"
    return result


def _tiktok(result: Verification, response: Response) -> Verification:
    data = response.body.get("data") or {}
    status = str(data.get("status", "")).upper()
    result.state = status or "unknown"
    result.metadata = {
        k: data[k] for k in ("publicaly_available_post_id", "publicly_available_post_id")
        if k in data
    }

    if status == "PUBLISH_COMPLETE":
        result.live = True
        result.detail = "TikTok confirms the post is published"
        return result
    if status in ("PROCESSING_UPLOAD", "PROCESSING_DOWNLOAD", "SEND_TO_USER_INBOX"):
        result.pending = True
        result.detail = f"TikTok is still working: {status}"
        return result
    if status == "FAILED":
        reason = str(data.get("fail_reason", "unspecified"))
        result.metadata["fail_reason"] = reason
        result.detail = f"TikTok failed to publish: {reason}"
        return result

    result.unknown = True
    result.detail = f"unrecognised TikTok status {status!r}"
    return result


def _instagram(result: Verification, response: Response) -> Verification:
    media_id = str(response.body.get("id", ""))
    if not media_id:
        result.state = "not_found"
        result.detail = "the media id returned no object"
        return result
    result.live = True
    result.state = "published"
    result.metadata = {
        "permalink": response.body.get("permalink", ""),
        "media_type": response.body.get("media_type", ""),
        "media_product_type": response.body.get("media_product_type", ""),
    }
    result.detail = "Instagram confirms the media exists"
    return result


@dataclass
class UploadVerifier:
    """Performs the read and reports the verdict.

    Separate from `PublishingSystem` because verification runs on its own
    schedule: minutes after publishing for YouTube's transcode, and again
    hours later for the moderation decisions that arrive late.
    """

    transport: Any

    def verify(
        self, platform: Platform, remote_post_id: str, tokens: TokenSet
    ) -> Verification:
        if not remote_post_id:
            return Verification(
                platform=platform, remote_post_id="",
                state="no_remote_id",
                detail="the post has no remote id — it was never published",
            )

        request = verification_request(platform, remote_post_id, tokens)
        try:
            response = self.transport.send(request)
        except Exception as error:                              # noqa: BLE001
            # Verification never raises. A failure to check is a fact about
            # the network, and a caller looping over a hundred posts should
            # not stop at the first flaky one.
            return Verification(
                platform=platform, remote_post_id=remote_post_id,
                unknown=True, state="unreachable",
                detail=f"could not verify: {type(error).__name__}: {error}",
            )
        return interpret(platform, remote_post_id, response)
