"""Account, sessions, connected platforms, and what this build can actually do.

The capability list is the unusual part and the most useful. Most of its
answers are negative — no object storage, no live metric source, no email
transport, no acquisition worker — and every one of them explains a way the
product will appear broken to someone using it. A dashboard that hides them
shows an upload queue that never drains and gives no clue why.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..deps import ContextDep, LivePrincipalDep, PrincipalDep, _services
from ..schemas import (
    CapabilityOut,
    SessionOut,
    SettingsResponse,
    SocialAccountOut,
    StorageUsageOut,
)

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_model=SettingsResponse)
async def settings(context: ContextDep, principal: PrincipalDep) -> SettingsResponse:
    auth = context.services.auth
    identity = auth.store.identity(principal.identity_id)

    sessions = [
        SessionOut(
            session_id=s.session_id,
            ip=s.ip,
            user_agent=s.user_agent,
            issued_at=s.issued_at,
            expires_at=s.expires_at,
            revoked=s.revoked_at is not None,
            rotations=s.rotations,
            current=s.session_id == principal.session_id,
        )
        for s in auth.sessions(principal.identity_id)
        if s.revoked_at is None
    ]
    sessions.sort(key=lambda s: (not s.current, s.issued_at), reverse=False)

    with context.unit_of_work() as uow:
        accounts = list(uow.accounts.all())

    return SettingsResponse(
        identity_id=principal.identity_id,
        email=principal.email,
        verified=bool(identity and identity.verified),
        tenant_id=principal.tenant_id,
        role=principal.role,
        memberships=context.memberships(),
        sessions=sessions,
        accounts=[
            SocialAccountOut(
                id=a.id,
                platform=a.platform,
                handle=getattr(a, "handle", "") or "",
                channel_id=a.channel_id,
                # The token store is the authority on whether an account can
                # actually post; a row in `social_accounts` only says someone
                # once connected one.
                connected=_has_credentials(auth, a.id),
                needs_reauth=not _has_credentials(auth, a.id),
                detail=(
                    "" if _has_credentials(auth, a.id)
                    else "No stored credentials — reconnect to publish."
                ),
            )
            for a in accounts
        ],
        capabilities=_capabilities(context),
    )


def _has_credentials(auth, account_id: str) -> bool:
    """Whether the auth layer holds a usable token for this account.

    Returns False rather than raising when no token store is wired up: an
    unconfigured deployment should read as "cannot publish", which is true.
    """

    store = getattr(auth, "token_store", None)
    if store is None:
        return False
    try:
        return store.get(account_id) is not None
    except Exception:                                       # noqa: BLE001
        return False


def _capabilities(context: ContextDep) -> list[CapabilityOut]:
    services = context.services
    checks: list[CapabilityOut] = []

    database = type(services.database).__name__
    checks.append(CapabilityOut(
        key="persistence", label="Durable storage",
        available=database.startswith("Postgres"),
        detail=(
            "PostgreSQL" if database.startswith("Postgres")
            else "In-memory — everything is lost when the API restarts"
        ),
    ))

    auth_store = type(getattr(services.auth, "store", None)).__name__
    checks.append(CapabilityOut(
        key="auth_persistence", label="Durable accounts",
        available=auth_store.startswith("Postgres"),
        detail=(
            "PostgreSQL, on its own role" if auth_store.startswith("Postgres")
            else "In-memory — accounts do not survive a restart"
        ),
    ))

    sender = type(getattr(services.auth, "sender", None)).__name__
    sends_mail = sender not in ("RecordingEmailSender", "NoneType",
                                "ConsoleEmailSender")
    checks.append(CapabilityOut(
        key="email", label="Email delivery", available=sends_mail,
        detail=(
            "configured" if sends_mail else
            "No transport wired up. Verification and reset links are recorded, "
            "not delivered, so a new user never receives one."
        ),
    ))

    checks.append(CapabilityOut(
        key="acquisition", label="Source acquisition",
        available=services.acquisition_factory is not None,
        detail=(
            "worker configured" if services.acquisition_factory is not None
            else "No worker configured — submitting a URL is refused rather "
                 "than queued into something that never drains."
        ),
    ))

    checks.append(_storage_capability(services))
    checks.append(_public_url_capability(services))
    checks.append(CapabilityOut(
        key="metrics", label="Live platform metrics", available=False,
        detail=(
            "`RecordedSource` is the only metric source in this build. "
            "Analytics reports what has been collected, and nothing is "
            "collecting."
        ),
    ))
    checks.append(CapabilityOut(
        key="publishing", label="Live publishing", available=False,
        detail=(
            "The HTTP transport is real and no upload has reached a live "
            "platform from this build — no credentials are configured."
        ),
    ))
    return checks


@router.get("/storage", response_model=StorageUsageOut)
async def storage_usage(context: ContextDep) -> StorageUsageOut:
    """How much this workspace is storing, and how the backend is behaving.

    Scoped to the caller's own prefix — `usage()` is a full listing, so an
    unscoped call would walk every tenant's objects and bill the caller for
    the privilege. Operation counters are process-wide because that is what
    they measure; they say nothing about any one tenant's data.
    """

    storage = context.services.storage
    if storage is None:
        return StorageUsageOut(
            backend="none",
            note=(
                "No storage backend is configured, so media stays on whichever "
                "worker downloaded it and is lost when that container is "
                "replaced."
            ),
        )

    metrics = storage.metrics.snapshot()
    report = StorageUsageOut(
        backend=storage.backend,
        operations={
            name: {k: float(v) for k, v in stats.items()}
            for name, stats in metrics["operations"].items()
        },
        total_calls=int(metrics["total"]["calls"]),
        total_failures=int(metrics["total"]["failures"]),
        total_retries=int(metrics["total"]["retries"]),
    )

    try:
        usage = storage.usage(f"{context.tenant_id}/")
    except Exception as error:                              # noqa: BLE001
        # Null rather than zero. "Could not measure" and "you are storing
        # nothing" are different answers and the second is a lie.
        report.note = f"Usage could not be measured: {error}"
        return report

    largest = usage.get("largest") or {}
    report.objects = usage["objects"]
    report.bytes = usage["bytes"]
    report.gigabytes = usage["gigabytes"]
    report.largest_key = largest.get("key", "")
    report.largest_bytes = largest.get("size_bytes")
    return report


def _storage_capability(services) -> CapabilityOut:
    """Whether media survives the container it was downloaded onto."""

    storage = getattr(services, "storage", None)
    backend = getattr(storage, "backend", "")
    if backend == "r2":
        bucket = getattr(getattr(storage, "config", None), "bucket", "")
        return CapabilityOut(
            key="object_storage", label="Object storage", available=True,
            detail=f"Cloudflare R2, bucket {bucket or 'unnamed'}",
        )
    if backend == "local":
        return CapabilityOut(
            key="object_storage", label="Object storage", available=False,
            detail=(
                "Media is on this machine's disk and is lost when the "
                "container is replaced. Set CLIPFORGE_STORAGE_BACKEND=r2."
            ),
        )
    return CapabilityOut(
        key="object_storage", label="Object storage", available=False,
        detail=(
            "No storage backend configured, so acquired media stays wherever "
            "the worker downloaded it."
        ),
    )


def _public_url_capability(services) -> CapabilityOut:
    """Instagram fetches media itself, so this decides whether Reels work.

    Broken out from object storage because the two fail independently: R2 can
    be configured and working while the bucket has no public domain, and that
    combination publishes to TikTok and YouTube perfectly while every Reel
    fails at Meta's fetcher with an error that names neither.
    """

    storage = getattr(services, "storage", None)
    if storage is None:
        return CapabilityOut(
            key="public_media_urls", label="Public media URLs",
            available=False, detail="No storage backend configured.",
        )
    try:
        storage.public_url("probe/capability-check")
    except Exception as error:                              # noqa: BLE001
        # Whole message, not the first sentence. The reason this matters —
        # that Instagram fetches media itself and cannot present a signature —
        # is in the second one, and truncating leaves an operator with
        # "no public URL" and nothing to act on.
        return CapabilityOut(
            key="public_media_urls", label="Public media URLs",
            available=False, detail=" ".join(str(error).split()),
        )
    return CapabilityOut(
        key="public_media_urls", label="Public media URLs", available=True,
        detail="Configured — Instagram can fetch rendered clips.",
    )


@router.post("/sessions/{session_id}/revoke", status_code=204)
async def revoke_session(
    session_id: str, principal: LivePrincipalDep, request: Request
):
    """End one session from the "where am I signed in" list."""

    from fastapi import HTTPException, Response

    services = _services(request)
    held = services.auth.store.session(session_id)
    # Scoped to the caller's own identity: a session id is not a secret, and
    # without this check anyone could revoke anyone's session by guessing one.
    if held is None or held.identity_id != principal.identity_id:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "No such session."},
        )
    if held.revoked_at is None:
        from ...auth.types import utcnow

        held.revoked_at = utcnow()
        held.revoked_reason = "revoked from settings"
        services.auth.store.save_session(held)
    return Response(status_code=204)
