"""Giving a new account somewhere to work.

`AuthService.sign_up` creates an *identity* — an address and a password hash.
It does not create a workspace, and it must not: the auth store is reached
under `clipforge_auth`, a role deliberately scoped to the five `auth_*` tables
so the request path cannot read a password hash. Tenants and users live in the
application schema behind `clipforge_app`. Two roles, two connections, and no
single object that can write both.

So provisioning lives here, in the API layer, which is the first place that
holds a handle to each. That is the boundary working as designed rather than
being worked around.

## Why this exists at all

Without it a self-service signup produced an identity with no membership. The
token minted at login then carried an empty `tenant_id`, and
`deps.Context.unit_of_work` refuses to open an unscoped transaction — correctly,
because the alternative is a query that returns every tenant's rows. So every
page returned `409 NO_WORKSPACE` and the account was unusable from the moment
it was created. The signup endpoint worked; the product did not.

## There is no transaction across the two stores

The tenant row and the membership row are written over separate connections as
separate roles, so a crash between them leaves an identity with a workspace it
is not a member of. Rather than reach for two-phase commit, `ensure_workspace`
is **idempotent and called from two places**: after signup, and again at login
whenever an identity turns out to have no membership. A half-finished
provisioning heals on the next sign-in, and — the part that matters more —
accounts created before this module existed get a workspace the first time
their owner logs in.

The repair is keyed on "has no membership row at all", never on "has no
*active* membership". Someone suspended from the only workspace they belong to
keeps their row, so they get an empty session and a message — not a silent new
private tenant containing none of their data.

A membership that has been **hard-deleted** is a different matter: nothing
distinguishes it from an account that never had one, so that person is treated
as new and provisioned a fresh workspace. If revoking access needs to be
durable, deactivate the membership rather than deleting the row. That is a real
limitation of doing the repair here rather than in a store that keeps
tombstones, and it is stated rather than designed around.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from ..store.records import TenantRecord, UserRecord

log = logging.getLogger("clipforge.api.onboarding")

__all__ = ["Workspace", "ensure_workspace", "workspace_name"]

#: The role the first member of a new workspace gets. `owner` rather than
#: `admin` because `empire.tenancy` reserves billing and tenant deletion for
#: it, and the person who created the account is the person who will be asked
#: to pay for it.
FOUNDER_ROLE = "owner"


@dataclass(frozen=True, slots=True)
class Workspace:
    """What a new account was given."""

    tenant_id: str
    user_id: str
    name: str
    #: False when the identity already had a membership and nothing was made.
    created: bool = True


def workspace_name(email: str) -> str:
    """A first name for a workspace, from the address that signed up.

    `dana@acme.com` becomes `Acme` rather than `Dana`, because the domain is
    the better guess at what the workspace is for and it is the name the second
    person invited will recognise. Free-mail domains have no such meaning, so
    those fall back to the local part.

    Renaming is a settings concern. This only has to be better than a UUID.
    """

    local, _, domain = email.partition("@")
    free = {
        "gmail.com", "googlemail.com", "outlook.com", "hotmail.com",
        "live.com", "yahoo.com", "icloud.com", "me.com", "proton.me",
        "protonmail.com", "gmx.com", "mail.com", "aol.com", "zoho.com",
    }
    label = local if (not domain or domain.lower() in free) else domain.split(".")[0]
    label = "".join(ch for ch in label if ch.isalnum() or ch in " -_.")
    # `dana.smith` reads as a name once the separators are spaces, and as a
    # filename if they are left alone.
    for separator in ("_", "-", "."):
        label = label.replace(separator, " ")
    label = " ".join(label.split())
    if not label:
        return "My workspace"
    return label[:1].upper() + label[1:]


def ensure_workspace(services: Any, identity_id: str, email: str) -> Workspace | None:
    """Give this identity a workspace if it has none. Idempotent.

    Returns the workspace, `created=False` when one already existed, or `None`
    when provisioning failed. Failure is reported to the caller rather than
    raised, because both callers — signup and login — have something better to
    do with it than return a 500. Signup must answer identically whether or not
    the address was free, and a login that already has valid credentials should
    not be refused because a tenant row could not be written.

    The cost of that choice is an account that stays unusable until the next
    sign-in retries. It is logged at ERROR for exactly that reason.
    """

    existing = services.auth.store.memberships(identity_id)
    if existing:
        first = existing[0]
        return Workspace(
            tenant_id=first.tenant_id, user_id=first.user_id,
            name=first.tenant_name, created=False,
        )

    tenant_id = f"ten_{uuid.uuid4().hex[:16]}"
    user_id = f"usr_{uuid.uuid4().hex[:16]}"
    name = workspace_name(email)

    try:
        # Scoped to the tenant being created. The `tenants` policy checks
        # `id = app.current_tenant()`, so this insert is only legal inside a
        # transaction that has already claimed the id — which is also what
        # stops this from ever writing into somebody else's workspace.
        with services.database.unit_of_work(tenant_id) as uow:
            uow.tenants.save(TenantRecord(id=tenant_id, name=name))
            uow.users.save(UserRecord(
                id=user_id, tenant_id=tenant_id, email=email,
                role=FOUNDER_ROLE, active=True,
            ))
    except Exception:                                       # noqa: BLE001
        log.exception(
            "could not provision a workspace for identity %s; the account "
            "will have no workspace until the next sign-in retries",
            identity_id,
        )
        return None

    try:
        services.auth.add_membership(
            identity_id=identity_id, tenant_id=tenant_id, user_id=user_id,
            role=FOUNDER_ROLE, tenant_name=name,
        )
    except Exception:                                       # noqa: BLE001
        # The tenant row exists and the membership does not. Deliberately not
        # rolled back: the next call re-enters with no membership found, mints
        # a *new* tenant and links that one. An orphaned empty tenant is
        # cheap; deleting rows on an error path is how the wrong tenant gets
        # deleted when the error was something else.
        log.exception(
            "provisioned tenant %s but could not link identity %s to it",
            tenant_id, identity_id,
        )
        return None

    log.info("provisioned workspace %s (%s) for identity %s",
             tenant_id, name, identity_id)
    return Workspace(tenant_id=tenant_id, user_id=user_id, name=name)
