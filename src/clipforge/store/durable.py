"""Durable replacements for the engines' in-memory stores.

Before this module the decision layer held everything in dictionaries: the
calendar's posts, the publishing system's accounts, its OAuth tokens, the
factory's channels, the analytics store's readings. All of it survived exactly
as long as the process did.

Each class here is a drop-in for one of those dictionaries, keeping the same
interface so the engines did not have to be rewritten around a database.

## Write-through, not write-behind

Every mutation reaches Postgres before the call returns. Batching writes would
be faster and would reintroduce precisely the failure being removed: work that
looked saved and was not.

## Why the calendar keeps a working set in memory

`PersistentCalendar` is the one that is not purely a pass-through. The calendar
answers questions — day occupancy, spacing conflicts, per-account windows,
month views — that are cheap over a sorted in-memory index and would be a
round trip each over SQL. So it keeps its index, and Postgres holds the truth:
every write goes through, and `load()` rebuilds the index at startup.

The important consequence is that the index is a **cache of a window**, not of
the table. `load()` takes a horizon and pulls the posts inside it, because a
year of an empire's uploads is millions of rows and none of them belong in a
worker's heap. Posts outside the window are still in the database and still
publish — they are simply not in this process's view until the window moves.
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from datetime import datetime, timedelta
from typing import Any, Callable

from ..publish.calendar import ContentCalendar
from ..publish.oauth import TokenSet
from ..publish.types import Account, Platform, ScheduledPost, ensure_utc, utcnow
from .errors import NotFound
from .mappers import (
    apply_tokens,
    to_account,
    to_account_record,
    to_scheduled_post,
    to_token_set,
    to_upload_record,
)
from .records import SocialAccountRecord

__all__ = [
    "DurableTokenStore",
    "DurableAccountBook",
    "PersistentCalendar",
    "DEFAULT_HORIZON_DAYS",
]

#: How far ahead `PersistentCalendar.load` pulls by default. Ninety days covers
#: the "weeks and months ahead" the scheduler supports, without loading a
#: portfolio's entire history into a worker.
DEFAULT_HORIZON_DAYS = 90

#: How far *back* to load. Far enough to see recent failures and retries;
#: not so far that a year of published posts rides along in memory.
DEFAULT_LOOKBACK_DAYS = 14


class _TenantBound:
    """Shared plumbing: a database and the one tenant this object speaks for."""

    def __init__(self, database: Any, tenant_id: str) -> None:
        if not tenant_id:
            raise ValueError("a durable store needs a tenant")
        self._database = database
        self.tenant_id = tenant_id

    def _uow(self):
        return self._database.unit_of_work(self.tenant_id)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


class DurableTokenStore(_TenantBound):
    """`TokenStore` backed by the `social_accounts` table.

    Tokens live on the account row rather than in a table of their own: there
    is exactly one credential set per account, and a separate table would only
    add a join and a way for the two to disagree about which accounts exist.

    `seal` and `unseal` are required. Refresh tokens are long-lived credentials
    to other people's audiences, and what is written here is ciphertext, so a
    database dump is not a set of working logins. The key belongs in a KMS held
    by something other than the process that publishes — which is why this
    takes functions rather than implementing any crypto itself.

    One transaction per call. That is a round trip the in-memory store did not
    make, and it is the correct default: a token read inside a larger unit of
    work would otherwise see whatever that transaction had already written,
    which is not what a credential lookup should mean.
    """

    def __init__(
        self,
        database: Any,
        tenant_id: str,
        *,
        seal: Callable[[str], str],
        unseal: Callable[[str], str],
    ) -> None:
        super().__init__(database, tenant_id)
        self._seal = seal
        self._unseal = unseal

    def get(self, account_id: str) -> TokenSet | None:
        with self._uow() as uow:
            record = uow.accounts.get(account_id)
        if record is None or not record.access_token_sealed:
            return None
        return to_token_set(record, unseal=self._unseal)

    def put(self, tokens: TokenSet) -> None:
        with self._uow() as uow:
            record = uow.accounts.get(tokens.account_id)
            if record is None:
                # An account can be authorised before it is otherwise
                # configured. Creating a minimal row beats refusing the
                # credential and losing an OAuth callback that will not repeat.
                record = SocialAccountRecord(
                    id=tokens.account_id,
                    tenant_id=self.tenant_id,
                    platform=tokens.platform.value,
                )
            uow.accounts.save(apply_tokens(record, tokens, seal=self._seal))

    def delete(self, account_id: str) -> None:
        """Forget the credentials, keep the account.

        Disconnecting is not deleting: the account's posts, metrics and history
        stay, and a row removed here would cascade them away.
        """

        with self._uow() as uow:
            record = uow.accounts.get(account_id)
            if record is None:
                return
            record.access_token_sealed = ""
            record.refresh_token_sealed = ""
            record.token_expires_at = None
            record.refresh_valid_until = None
            record.token_obtained_at = None
            record.scopes = []
            uow.accounts.save(record)

    def all_accounts(self) -> tuple[str, ...]:
        with self._uow() as uow:
            return tuple(
                r.id for r in uow.accounts.all() if r.access_token_sealed
            )

    def expiring_before(self, moment: datetime) -> tuple[str, ...]:
        """Accounts whose refresh token lapses before `moment`.

        Not on the `TokenStore` protocol; available because the index exists
        and because a refresh token that lapses unnoticed is a channel that
        silently stops posting, with the customer finding out first.
        """

        with self._uow() as uow:
            return tuple(a.id for a in uow.accounts.refresh_expiring_before(moment))


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


class DurableAccountBook(_TenantBound, MutableMapping[str, Account]):
    """`dict[str, Account]` that happens to be a table.

    A `MutableMapping` rather than a new interface so `PublishingSystem` keeps
    working unchanged: `self.accounts[id] = account`, `.values()`, `in`, `len`
    all mean what they meant.

    Reads are not cached. An account's `enabled` flag and audit status are
    edited from the dashboard, by a different process, and a cached copy is how
    a system keeps posting to an account somebody just disconnected.
    """

    def __init__(
        self, database: Any, tenant_id: str, *, channel_id: str | None = None
    ) -> None:
        super().__init__(database, tenant_id)
        #: Attached to accounts created through this book. The publishing
        #: engine has no notion of channels; the factory does.
        self.channel_id = channel_id

    def __getitem__(self, account_id: str) -> Account:
        with self._uow() as uow:
            record = uow.accounts.get(account_id)
        if record is None:
            raise KeyError(account_id)
        return to_account(record)

    def __setitem__(self, account_id: str, account: Account) -> None:
        if account_id != account.account_id:
            raise ValueError(
                f"key {account_id!r} does not match account {account.account_id!r}"
            )
        with self._uow() as uow:
            existing = uow.accounts.get(account_id)
            record = to_account_record(
                account,
                tenant_id=self.tenant_id,
                channel_id=(existing.channel_id if existing else self.channel_id),
            )
            if existing is not None:
                # Credentials are not the caller's to overwrite here. An
                # `accounts[id] = account` that silently wiped the tokens would
                # disconnect a live account as a side effect of renaming it.
                record.access_token_sealed = existing.access_token_sealed
                record.refresh_token_sealed = existing.refresh_token_sealed
                record.token_expires_at = existing.token_expires_at
                record.refresh_valid_until = existing.refresh_valid_until
                record.token_obtained_at = existing.token_obtained_at
                record.scopes = existing.scopes
            uow.accounts.save(record)

    def __delitem__(self, account_id: str) -> None:
        with self._uow() as uow:
            if not uow.accounts.delete(account_id):
                raise KeyError(account_id)

    def __iter__(self) -> Iterator[str]:
        with self._uow() as uow:
            return iter([r.id for r in uow.accounts.all()])

    def __len__(self) -> int:
        with self._uow() as uow:
            return uow.accounts.count()

    def values(self):  # type: ignore[override]
        """Overridden so listing every account is one query, not one per key.

        `MutableMapping` would derive this from `__iter__` and `__getitem__`,
        which is N+1 round trips — fine at three accounts, not at five hundred.
        """

        with self._uow() as uow:
            return [to_account(r) for r in uow.accounts.all()]

    def items(self):  # type: ignore[override]
        return [(a.account_id, a) for a in self.values()]


# ---------------------------------------------------------------------------
# The calendar
# ---------------------------------------------------------------------------


class PersistentCalendar(ContentCalendar, _TenantBound):
    """`ContentCalendar` whose posts are rows in `uploads`.

    Every `add`, `move`, `remove` and `persist` writes through before
    returning. The in-memory index stays, because that is what makes the
    spacing check a binary search instead of a query — see the comment on
    `ContentCalendar._by_account` for what it cost when it was linear.

    Restart behaviour is the whole point: `load()` rebuilds the index from the
    table, so a process that comes back finds the same calendar it left.
    """

    def __init__(
        self,
        database: Any,
        tenant_id: str,
        *,
        channel_id: str,
        tz: str = "UTC",
    ) -> None:
        # Guard first: `ContentCalendar.__init__` calls `self.add`, which this
        # class overrides to write through. Without the flag, rehydrating a
        # calendar would write every post it just read straight back.
        self._writing_through = False
        ContentCalendar.__init__(self, tz=tz)
        _TenantBound.__init__(self, database, tenant_id)
        self.channel_id = channel_id
        self._writing_through = True

    # -- write-through -----------------------------------------------------

    def _save(self, *posts: ScheduledPost) -> None:
        if not self._writing_through or not posts:
            return
        with self._uow() as uow:
            for post in posts:
                uow.uploads.save(
                    to_upload_record(
                        post, tenant_id=self.tenant_id, channel_id=self.channel_id
                    )
                )

    def add(self, post: ScheduledPost) -> None:
        super().add(post)
        self._save(post)

    def remove(self, post_id: str) -> None:
        super().remove(post_id)
        if self._writing_through:
            with self._uow() as uow:
                uow.uploads.delete(post_id)

    def move(self, post_id: str, run_at: datetime) -> ScheduledPost:
        # `super().move` routes through `remove` then `add`, so the row would
        # be deleted and reinserted — losing `created_at`, and briefly losing
        # the post entirely if the process died between the two. Reorder the
        # index without touching the database, then save once.
        was = self._writing_through
        self._writing_through = False
        try:
            post = super().move(post_id, run_at)
        finally:
            self._writing_through = was
        self._save(post)
        return post

    def persist(self, *posts: ScheduledPost) -> None:
        """Write the given posts' current state through.

        The engine mutates posts in place — state, attempts, lease, error — and
        this is how those transitions reach the table. Called with no arguments
        it flushes everything in the index, which is the safe answer when a
        caller is not sure what it touched.
        """

        self._save(*(posts or self.posts))

    # -- rehydration -------------------------------------------------------

    def load(
        self,
        now: datetime | None = None,
        *,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        horizon_days: int = DEFAULT_HORIZON_DAYS,
    ) -> int:
        """Rebuild the index from the table. Returns how many posts came back.

        Bounded by a window on purpose. An empire scheduling 500 uploads a day
        ninety days out is 45,000 rows, which is a reasonable working set; the
        same query without a horizon is every post the account has ever made,
        which is not. Posts outside the window are not lost — they are in the
        table, and they publish when the window reaches them.
        """

        now = ensure_utc(now or utcnow())
        start = now - timedelta(days=lookback_days)
        end = now + timedelta(days=horizon_days)

        with self._uow() as uow:
            records = uow.uploads.for_channel_between(self.channel_id, start, end)

        was = self._writing_through
        self._writing_through = False
        try:
            for record in records:
                self.add(to_scheduled_post(record))
        finally:
            self._writing_through = was
        return len(records)

    @classmethod
    def restore(
        cls,
        database: Any,
        tenant_id: str,
        *,
        channel_id: str,
        tz: str = "UTC",
        now: datetime | None = None,
        **window: int,
    ) -> PersistentCalendar:
        """Open a calendar and fill it from the table in one step."""

        calendar = cls(database, tenant_id, channel_id=channel_id, tz=tz)
        calendar.load(now, **window)
        return calendar
