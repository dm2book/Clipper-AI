"""ClipForge AI — scheduled multi-platform publishing.

TikTok, YouTube and Instagram. OAuth connection, recurring schedules, bulk
imports, a content calendar, and a retrying worker loop that can hold posts
months ahead.

    from clipforge.publish import PublishingSystem, weekdays_at

    system = PublishingSystem()
    system.connect(account, tokens)
    system.schedule_bulk(account.account_id, specs,
                         weekdays_at(17, 0, "Europe/Amsterdam"))

**Read `automation_report()` before believing the word "automated".** Two of
the three platforms put something between an app and unattended posting, and
the system reports it up front rather than discovering it at post time:

- TikTok needs an app audit. Until it clears, uploads land in the creator's
  inbox as a draft a human must finish.
- Instagram needs a Business or Creator account linked to a Facebook Page, and
  fetches the file from a public URL rather than accepting bytes.
- YouTube's 10,000-unit daily quota at 1,600 per upload is **six uploads a day
  per API project**, shared across every connected channel of every customer.

Only YouTube offers real server-side scheduling. For the other two, "scheduled"
means this system holds the job and fires it — which makes a post three months
out a *credentials* problem, since an Instagram token is good for sixty days.
`limits.readiness()` and `oauth.accounts_needing_attention()` exist to make
that visible before it costs a quarter of someone's content calendar.

Adapters build requests and interpret replies; they never perform I/O. A
`Transport` does that, and there are two:

* `HttpTransport` — the real client. TLS, streamed chunk uploads, exact status
  codes including Google's `308`, and typed failures.
* `RecordingTransport` — a scripted double, for exercising every branch of
  every platform's state machine offline.

Production wiring is `HttpTransport` plus a `TokenRefresher`; without the
latter a deployment publishes only until its first token expiry, which on
TikTok is 24 hours.

    from clipforge.publish import (
        AccountManager, HttpTransport, PublishingSystem, TokenRefresher,
    )

    transport = HttpTransport()
    refresher = TokenRefresher(transport, token_store, credentials)
    system = PublishingSystem(token_store=token_store, refresher=refresher)
    system.tick(transport)
"""

from .adapters import (
    ADAPTERS,
    Adapter,
    InstagramAdapter,
    RecordingTransport,
    TikTokAdapter,
    Transport,
    YouTubeAdapter,
    adapter_for,
)
from .calendar import Conflict, ContentCalendar, DaySlot, MIN_SPACING_S
from .engine import (
    PublishConfig,
    PublishResult,
    PublishingSystem,
    ScheduleError,
)
from .limits import (
    LIMITS,
    LIMITS_VERSION,
    PlatformLimits,
    Readiness,
    automation_gap,
    effective_visibility,
    is_short_form,
    limits_for,
    readiness,
    validate,
)
from .oauth import (
    AuthorizationRequest,
    ClientCredentials,
    InMemoryTokenStore,
    PkceChallenge,
    SealedTokenStore,
    TokenSet,
    TokenStore,
    accounts_needing_attention,
    authorization_url,
    exchange_request,
    make_pkce,
    parse_token_response,
    refresh_request,
)
from .accounts import (
    AccountHealth,
    AccountManager,
    ConnectionResult,
    PendingConnection,
)
from .refresh import ReauthRequired, RefreshFailed, RefreshResult, TokenRefresher
from .retry import Decision, Disposition, backoff_delay, classify
from .transport import HttpTransport, TransportConfig, TransportError
from .verify import UploadVerifier, Verification
from .schedule import (
    AmbiguousTime,
    DstReport,
    EVERY_DAY,
    Frequency,
    NonexistentTime,
    Recurrence,
    WEEKDAYS,
    WEEKEND,
    daily,
    dst_report,
    monthly_on,
    weekdays_at,
    weekly_on,
)
from .types import (
    Account,
    Action,
    Attempt,
    MediaAsset,
    Platform,
    PostSpec,
    PostState,
    Request,
    Response,
    ScheduledPost,
    Step,
    Visibility,
    utcnow,
)

__all__ = [
    "Account",
    "AccountHealth",
    "AccountManager",
    "accounts_needing_attention",
    "Action",
    "Adapter",
    "adapter_for",
    "ADAPTERS",
    "AmbiguousTime",
    "Attempt",
    "authorization_url",
    "AuthorizationRequest",
    "automation_gap",
    "backoff_delay",
    "classify",
    "ClientCredentials",
    "Conflict",
    "ConnectionResult",
    "ContentCalendar",
    "daily",
    "DaySlot",
    "Decision",
    "Disposition",
    "dst_report",
    "DstReport",
    "effective_visibility",
    "EVERY_DAY",
    "exchange_request",
    "Frequency",
    "HttpTransport",
    "InMemoryTokenStore",
    "InstagramAdapter",
    "is_short_form",
    "LIMITS",
    "limits_for",
    "LIMITS_VERSION",
    "make_pkce",
    "MediaAsset",
    "MIN_SPACING_S",
    "monthly_on",
    "NonexistentTime",
    "parse_token_response",
    "PendingConnection",
    "PkceChallenge",
    "Platform",
    "PlatformLimits",
    "PostSpec",
    "PostState",
    "PublishConfig",
    "PublishingSystem",
    "PublishResult",
    "readiness",
    "Readiness",
    "ReauthRequired",
    "RecordingTransport",
    "Recurrence",
    "refresh_request",
    "RefreshFailed",
    "RefreshResult",
    "Request",
    "Response",
    "ScheduledPost",
    "ScheduleError",
    "SealedTokenStore",
    "Step",
    "TikTokAdapter",
    "TokenRefresher",
    "TokenSet",
    "TokenStore",
    "Transport",
    "TransportConfig",
    "TransportError",
    "UploadVerifier",
    "utcnow",
    "validate",
    "Verification",
    "Visibility",
    "WEEKDAYS",
    "weekdays_at",
    "WEEKEND",
    "weekly_on",
    "YouTubeAdapter",
]
