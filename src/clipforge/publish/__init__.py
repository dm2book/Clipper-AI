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

Requests are built, never performed: adapters are state machines over a
`Transport`, so the whole system runs offline and secrets never reach the layer
that formats logs.
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
from .retry import Decision, Disposition, backoff_delay, classify
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
    "ADAPTERS",
    "Account",
    "Action",
    "Adapter",
    "AmbiguousTime",
    "Attempt",
    "AuthorizationRequest",
    "ClientCredentials",
    "Conflict",
    "ContentCalendar",
    "DaySlot",
    "Decision",
    "Disposition",
    "DstReport",
    "EVERY_DAY",
    "Frequency",
    "InMemoryTokenStore",
    "InstagramAdapter",
    "LIMITS",
    "LIMITS_VERSION",
    "MIN_SPACING_S",
    "MediaAsset",
    "NonexistentTime",
    "PkceChallenge",
    "Platform",
    "PlatformLimits",
    "PostSpec",
    "PostState",
    "PublishConfig",
    "PublishResult",
    "PublishingSystem",
    "Readiness",
    "RecordingTransport",
    "Recurrence",
    "Request",
    "Response",
    "ScheduleError",
    "ScheduledPost",
    "SealedTokenStore",
    "Step",
    "TikTokAdapter",
    "TokenSet",
    "TokenStore",
    "Transport",
    "Visibility",
    "WEEKDAYS",
    "WEEKEND",
    "YouTubeAdapter",
    "accounts_needing_attention",
    "adapter_for",
    "authorization_url",
    "automation_gap",
    "backoff_delay",
    "classify",
    "daily",
    "dst_report",
    "effective_visibility",
    "exchange_request",
    "is_short_form",
    "limits_for",
    "make_pkce",
    "monthly_on",
    "parse_token_response",
    "readiness",
    "refresh_request",
    "utcnow",
    "validate",
    "weekdays_at",
    "weekly_on",
]
