"""Storage errors.

Deliberately backend-neutral. A caller that catches `Conflict` should not have
to know whether the conflict came from a Postgres unique violation or from the
in-memory store's own check, or the in-memory tests stop being evidence about
the Postgres path.
"""

from __future__ import annotations

__all__ = ["StoreError", "NotFound", "Conflict", "TenantScopeError", "ReadOnly"]


class StoreError(Exception):
    """Base for everything this layer raises."""


class NotFound(StoreError):
    """No row with that key, in this tenant."""


class Conflict(StoreError):
    """A uniqueness constraint refused the write.

    Usually good news: the idempotency key on `uploads` and the dedupe key on
    `jobs` both exist to turn a duplicate into this, rather than into a second
    post on someone's feed or a second render of the same clip.
    """


class TenantScopeError(StoreError):
    """A record was written under a different tenant than the session holds.

    Caught in the repository rather than left to the database because the
    database's answer is a row-level security violation, which is a correct
    but unhelpful way to learn that a caller mixed two tenants' objects in one
    transaction.
    """


class ReadOnly(StoreError):
    """A write was attempted against an append-only or read-only table."""
