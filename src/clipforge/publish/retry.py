"""Failure classification: what to do about it, not just how long to wait.

Retry logic in a publisher is not a backoff curve. It is a decision about which
of five different things went wrong, because the right response to each is
different and three of them are actively harmed by retrying.

**A retry that double-posts is worse than a post that failed.** A failed post
is a notification; a duplicate is a creator's audience seeing the same video
twice and the creator losing trust in the tool. So the single most important
case here is the ambiguous one: the request timed out *after* the platform may
have accepted it. That is not a retry — it is a reconciliation. Ask the
platform what exists before creating anything.

**Exponential backoff against a daily quota is pointless.** Doubling from 30
seconds to 8 minutes does not help when the limit resets at midnight; it just
burns the retry budget and then marks the post failed while the real answer was
"tomorrow". Quota exhaustion is a *rescheduling* problem.

**A dead token is not a transient error.** No amount of waiting reconnects an
account. It needs a human, and the post should say so rather than retrying
eleven times and then reporting a generic failure.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from .types import Platform, Response, UTC


class Disposition(str, enum.Enum):
    """What to do about a failure."""

    RETRY = "retry"              # transient; back off and try again
    RESCHEDULE = "reschedule"    # quota or rate limit; move to a later window
    RECONCILE = "reconcile"      # may have succeeded; ask before re-sending
    REAUTH = "reauth"            # credentials dead; needs a human
    FAIL = "fail"                # permanently invalid; retrying cannot help


#: Attempts before a retryable failure is given up on. Deliberately generous:
#: the failures that reach this many attempts are nearly always platform-side
#: incidents, which resolve on their own within the hour.
MAX_ATTEMPTS = 8

BASE_BACKOFF_S = 20.0
MAX_BACKOFF_S = 30 * 60.0
BACKOFF_FACTOR = 2.0

#: Deterministic jitter fraction. Jitter matters here: a platform outage fails
#: every queued post at once, and without it they all retry in lockstep and
#: hammer the platform the moment it recovers.
JITTER_FRACTION = 0.25


@dataclass(frozen=True, slots=True)
class Decision:
    disposition: Disposition
    delay_s: float = 0.0
    reason: str = ""
    error_code: str = ""
    #: True when the post must not be re-sent without checking platform state.
    unsafe_to_repeat: bool = False

    @property
    def retryable(self) -> bool:
        return self.disposition in (Disposition.RETRY, Disposition.RESCHEDULE)

    def to_dict(self) -> dict[str, object]:
        return {
            "disposition": self.disposition.value,
            "delay_s": round(self.delay_s, 1),
            "reason": self.reason,
            "error_code": self.error_code,
            "unsafe_to_repeat": self.unsafe_to_repeat,
        }


def backoff_delay(attempt: int, key: str = "") -> float:
    """Exponential backoff with deterministic jitter.

    Jitter is derived from the post's own key rather than drawn randomly, so a
    replayed queue produces the same schedule and a test can assert on it —
    while still spreading a thundering herd, because different posts hash
    differently.
    """
    raw = min(BASE_BACKOFF_S * (BACKOFF_FACTOR ** max(0, attempt - 1)),
              MAX_BACKOFF_S)
    if not key:
        return raw

    digest = hashlib.blake2b(f"{key}|{attempt}".encode(), digest_size=4).digest()
    unit = int.from_bytes(digest, "big") / float(1 << 32)   # [0, 1)
    # Symmetric around the nominal delay.
    return raw * (1.0 + JITTER_FRACTION * (unit * 2.0 - 1.0))


#: Platform error codes that mean "this will never work". Retrying any of
#: these is a guaranteed waste of a slot.
_PERMANENT_CODES = frozenset({
    # TikTok
    "spam_risk_too_many_posts", "spam_risk_user_banned_from_posting",
    "reached_active_user_cap", "unaudited_client_can_only_post_to_private_accounts",
    "url_ownership_unverified", "privacy_level_option_mismatch",
    "invalid_file_upload", "video_pull_failed",
    # YouTube
    "invalidVideoMetadata", "invalidTitle", "invalidDescription",
    "invalidCategoryId", "mediaBodyRequired", "forbidden",
    "uploadLimitExceeded",
    # Instagram / Graph
    "media_type_mismatch", "unsupported_format", "aspect_ratio_not_supported",
    "video_too_long", "permission_denied",
})

#: Codes meaning the credential is gone rather than momentarily unhappy.
_AUTH_CODES = frozenset({
    "access_token_invalid", "invalid_grant", "invalid_token",
    "token_expired", "unauthorized_client", "authenticationFailure",
    "scope_not_authorized", "OAuthException",
})

#: Codes meaning a budget is exhausted rather than a request being too fast.
_QUOTA_CODES = frozenset({
    "quotaExceeded", "dailyLimitExceeded", "application_rate_limit_exceeded",
    "rate_limit_exceeded", "spam_risk_too_many_pending_share",
})


def _quota_reset_delay(now: datetime, platform: Platform) -> float:
    """Seconds until the quota window most likely reopens.

    YouTube's quota resets at midnight Pacific. TikTok and Instagram enforce a
    *rolling* 24 hours, so there is no reset moment — the pragmatic move is to
    come back in an hour and see whether the oldest post has aged out.
    """
    if platform is Platform.YOUTUBE:
        from zoneinfo import ZoneInfo

        pacific = ZoneInfo("America/Los_Angeles")
        local = now.astimezone(pacific)
        midnight = (local + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return max(60.0, (midnight - local).total_seconds())
    return 3600.0


def classify(
    response: Response | None,
    attempt: int,
    platform: Platform,
    now: datetime,
    key: str = "",
    timed_out: bool = False,
    already_in_flight: bool = False,
) -> Decision:
    """Decide what to do about one failed attempt.

    `already_in_flight` is the caller saying "the platform had already been
    told to create something when this failed". Combined with a timeout it is
    the ambiguous case, and the answer is never a blind retry.
    """
    if timed_out:
        if already_in_flight:
            return Decision(
                Disposition.RECONCILE,
                delay_s=30.0,
                reason=(
                    "request timed out after the platform may have accepted "
                    "the post — check what exists before sending anything else"
                ),
                error_code="timeout_in_flight",
                unsafe_to_repeat=True,
            )
        return Decision(
            Disposition.RETRY,
            delay_s=backoff_delay(attempt, key),
            reason="request timed out before the platform was told anything",
            error_code="timeout",
        )

    if response is None:
        return Decision(
            Disposition.RETRY,
            delay_s=backoff_delay(attempt, key),
            reason="no response — network or DNS failure",
            error_code="no_response",
        )

    code = _extract_code(response)
    message = _extract_message(response)
    status = response.status

    if code in _AUTH_CODES or status == 401:
        return Decision(
            Disposition.REAUTH,
            reason=(
                f"credentials rejected ({code or status}) — the account must "
                f"be reconnected; retrying cannot fix this"
            ),
            error_code=code or "unauthorized",
        )

    if code in _QUOTA_CODES or status == 429:
        delay = _retry_after(response)
        if delay is None:
            delay = (
                _quota_reset_delay(now, platform)
                if code in _QUOTA_CODES
                else backoff_delay(attempt, key)
            )
        return Decision(
            Disposition.RESCHEDULE,
            delay_s=delay,
            reason=(
                f"rate limited or out of quota ({code or status}) — moved to "
                f"the next window rather than retried, because backing off "
                f"inside an exhausted budget accomplishes nothing"
            ),
            error_code=code or "rate_limited",
        )

    if code in _PERMANENT_CODES:
        return Decision(
            Disposition.FAIL,
            reason=f"{code}: {message or 'permanently rejected'}",
            error_code=code,
        )

    if status == 403:
        return Decision(
            Disposition.FAIL,
            reason=f"forbidden ({code or 'no code'}): {message}",
            error_code=code or "forbidden",
        )

    if 400 <= status < 500 and status not in (408, 409, 425, 429):
        # 409 is included above as retryable-ish: a conflict usually means the
        # resource is already being created, which is a reconciliation.
        return Decision(
            Disposition.FAIL,
            reason=f"rejected with {status} ({code or 'no code'}): {message}",
            error_code=code or f"http_{status}",
        )

    if status == 409:
        return Decision(
            Disposition.RECONCILE,
            delay_s=15.0,
            reason="conflict — the platform may already hold this post",
            error_code=code or "conflict",
            unsafe_to_repeat=True,
        )

    if status >= 500 or status in (408, 425):
        if already_in_flight and status >= 500:
            # A 5xx after the platform was told to publish is ambiguous in
            # exactly the same way a timeout is.
            return Decision(
                Disposition.RECONCILE,
                delay_s=30.0,
                reason=(
                    f"platform returned {status} after being asked to create "
                    f"the post — the outcome is unknown, so reconcile"
                ),
                error_code=code or f"http_{status}",
                unsafe_to_repeat=True,
            )
        return Decision(
            Disposition.RETRY,
            delay_s=backoff_delay(attempt, key),
            reason=f"platform error {status} — transient",
            error_code=code or f"http_{status}",
        )

    return Decision(
        Disposition.RETRY,
        delay_s=backoff_delay(attempt, key),
        reason=f"unclassified response {status}",
        error_code=code or f"http_{status}",
    )


def _extract_code(response: Response) -> str:
    """Pull an error code out of three quite different envelope shapes."""
    body = response.body

    # TikTok: {"error": {"code": "...", "message": "..."}}
    error = body.get("error")
    if isinstance(error, dict):
        for field in ("code", "type", "error_type"):
            value = error.get(field)
            if isinstance(value, str) and value and value != "ok":
                return value
    elif isinstance(error, str) and error:
        return error

    # Google: {"error": {"errors": [{"reason": "..."}]}}
    if isinstance(error, dict):
        errors = error.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            reason = errors[0].get("reason")
            if isinstance(reason, str):
                return reason

    for field in ("error_code", "code"):
        value = body.get(field)
        if isinstance(value, str) and value:
            return value

    return ""


def _extract_message(response: Response) -> str:
    body = response.body
    error = body.get("error")
    if isinstance(error, dict):
        for field in ("message", "error_user_msg", "error_description"):
            value = error.get(field)
            if isinstance(value, str) and value:
                return value
    for field in ("message", "error_description"):
        value = body.get(field)
        if isinstance(value, str) and value:
            return value
    return ""


def _retry_after(response: Response) -> float | None:
    """Honour the platform's own advice about when to come back."""
    for header in ("Retry-After", "retry-after"):
        value = response.headers.get(header)
        if value:
            try:
                return float(value)
            except ValueError:
                continue
    return None


def exhausted(attempt: int) -> bool:
    return attempt >= MAX_ATTEMPTS
