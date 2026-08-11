"""Postgres implementation of the store.

Three things here are load-bearing and worth reading before changing anything.

**The tenant is set on the transaction, not on the connection.** Every unit of
work opens with `SET LOCAL app.tenant_id = ...`, and `SET LOCAL` is undone when
the transaction ends. That is what makes this safe behind a connection pool: a
connection handed back to the pool cannot carry one customer's tenant into the
next customer's query. A plain `SET` would, and the resulting bug — one tenant
occasionally seeing another's clips, depending on pool scheduling — is close to
undiagnosable from a log.

**The application connects as `clipforge_app`,** which is neither superuser nor
the table owner. Postgres lets both bypass row-level security, so connecting as
either would turn every policy into a no-op while the isolation tests kept
passing.

**SQL is composed from the table descriptors, never from caller input.**
Identifiers go through `psycopg.sql.Identifier`; values are always parameters.
The column lists come from the dataclasses, so a query cannot name a column
that does not exist.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Sequence

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Json

from ..publish.types import ensure_utc, utcnow
from .errors import Conflict, NotFound, TenantScopeError
from .records import ChannelSourceUseRecord
from .schema import TABLES, Table

__all__ = ["PostgresDatabase", "PostgresUnitOfWork"]

#: Postgres SQLSTATEs this layer translates. Everything else propagates: an
#: undiagnosed database error should not be flattened into a domain error that
#: a caller might reasonably retry.
_UNIQUE_VIOLATION = "23505"
_RLS_VIOLATION = "42501"


def _translate(error: psycopg.Error) -> Exception:
    code = getattr(error, "sqlstate", None)
    if code == _UNIQUE_VIOLATION:
        return Conflict(str(error).strip())
    if code == _RLS_VIOLATION:
        return TenantScopeError(str(error).strip())
    return error


class _PgRepository:
    """Generic CRUD over one table.

    Written once rather than thirteen times. Thirteen hand-written CRUD blocks
    is thirteen chances to get the column list subtly wrong in one of them, and
    the symptom is a field that silently stops persisting.
    """

    def __init__(self, uow: PostgresUnitOfWork, table: Table) -> None:
        self._uow = uow
        self._table = table
        self._ident = sql.Identifier(table.name)

    # -- plumbing ----------------------------------------------------------

    def _execute(self, statement: sql.Composed | sql.SQL, params: Sequence[Any] = ()):
        return self._uow._execute(statement, params)

    def _hydrate(self, row: dict[str, Any] | None) -> Any:
        if row is None:
            return None
        return self._table.record(**row)

    def _hydrate_all(self, rows: list[dict[str, Any]]) -> tuple[Any, ...]:
        return tuple(self._table.record(**row) for row in rows)

    def _values(self, record: Any, columns: Sequence[str]) -> list[Any]:
        out = []
        for column in columns:
            value = getattr(record, column)
            if column in self._table.json_columns and value is not None:
                value = Json(value)
            elif isinstance(value, datetime):
                value = ensure_utc(value)
            out.append(value)
        return out

    def _columns(self, columns: Sequence[str]) -> sql.Composed:
        return sql.SQL(", ").join(sql.Identifier(c) for c in columns)

    def _select(self) -> sql.Composed:
        return sql.SQL("SELECT {cols} FROM {table}").format(
            cols=self._columns(self._table.columns), table=self._ident
        )

    def _where_key(self) -> sql.Composed:
        return sql.SQL(" AND ").join(
            sql.SQL("{} = %s").format(sql.Identifier(c)) for c in self._table.key
        )

    def _check_tenant(self, record: Any) -> None:
        value = getattr(record, self._table.tenant_column)
        if value != self._uow.tenant_id:
            # Row-level security would refuse this too, but its message names a
            # policy, not the mistake. Catching it here says which record and
            # which tenant, at the call site that mixed them.
            raise TenantScopeError(
                f"{self._table.name} record {value!r} written under "
                f"tenant {self._uow.tenant_id!r}"
            )

    # -- Repository --------------------------------------------------------

    def get(self, key: str) -> Any:
        statement = self._select() + sql.SQL(" WHERE ") + self._where_key()
        cursor = self._execute(statement, [key])
        return self._hydrate(cursor.fetchone())

    def require(self, key: str) -> Any:
        row = self.get(key)
        if row is None:
            raise NotFound(f"{self._table.name}: no {key!r}")
        return row

    def add(self, record: Any) -> Any:
        self._check_tenant(record)
        columns = self._table.writable
        statement = sql.SQL(
            "INSERT INTO {table} ({cols}) VALUES ({marks}) RETURNING {out}"
        ).format(
            table=self._ident,
            cols=self._columns(columns),
            marks=sql.SQL(", ").join(sql.Placeholder() * len(columns)),
            out=self._columns(self._table.columns),
        )
        cursor = self._execute(statement, self._values(record, columns))
        return self._hydrate(cursor.fetchone())

    def save(self, record: Any) -> Any:
        self._check_tenant(record)
        columns = self._table.writable
        updatable = self._table.updatable
        assignments = sql.SQL(", ").join(
            sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c))
            for c in updatable
        )
        statement = sql.SQL(
            "INSERT INTO {table} ({cols}) VALUES ({marks}) "
            "ON CONFLICT ({key}) DO UPDATE SET {assign} RETURNING {out}"
        ).format(
            table=self._ident,
            cols=self._columns(columns),
            marks=sql.SQL(", ").join(sql.Placeholder() * len(columns)),
            key=self._columns(self._table.key),
            assign=assignments,
            out=self._columns(self._table.columns),
        )
        cursor = self._execute(statement, self._values(record, columns))
        return self._hydrate(cursor.fetchone())

    def delete(self, key: str) -> bool:
        statement = (
            sql.SQL("DELETE FROM {table} WHERE ").format(table=self._ident)
            + self._where_key()
        )
        cursor = self._execute(statement, [key])
        return cursor.rowcount > 0

    def all(self) -> tuple[Any, ...]:
        cursor = self._execute(self._select())
        return self._hydrate_all(cursor.fetchall())

    def count(self) -> int:
        statement = sql.SQL("SELECT count(*) AS n FROM {table}").format(
            table=self._ident
        )
        return int(self._execute(statement).fetchone()["n"])

    # -- shared query shapes ----------------------------------------------

    def _where(
        self, clause: str, params: Sequence[Any], order: str = "", limit: int = 0
    ) -> tuple[Any, ...]:
        statement = self._select() + sql.SQL(" WHERE ") + sql.SQL(clause)  # noqa: S608
        if order:
            statement += sql.SQL(" ORDER BY ") + sql.SQL(order)  # noqa: S608
        if limit > 0:
            statement += sql.SQL(" LIMIT {}").format(sql.Literal(limit))
        return self._hydrate_all(self._execute(statement, list(params)).fetchall())

    def _one_where(self, clause: str, params: Sequence[Any]) -> Any:
        rows = self._where(clause, params, limit=1)
        return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Per-entity repositories
#
# The clause strings below are literals in this module — never caller input —
# and every value in them is a parameter.
# ---------------------------------------------------------------------------


class _PgTenants(_PgRepository):
    pass


class _PgUsers(_PgRepository):
    def by_email(self, email: str) -> Any:
        return self._one_where("email = %s", [email])

    def active(self) -> tuple[Any, ...]:
        return self._where("active", [], order="email")


class _PgProjects(_PgRepository):
    def live(self) -> tuple[Any, ...]:
        return self._where("NOT archived", [], order="name")


class _PgChannels(_PgRepository):
    def for_project(self, project_id: str) -> tuple[Any, ...]:
        return self._where("project_id = %s", [project_id], order="name")

    def in_state(self, *states: str) -> tuple[Any, ...]:
        return self._where("state = ANY(%s)", [list(states)], order="name")


class _PgAccounts(_PgRepository):
    def for_channel(self, channel_id: str) -> tuple[Any, ...]:
        return self._where("channel_id = %s", [channel_id], order="platform, handle")

    def on_platform(self, platform: str) -> tuple[Any, ...]:
        return self._where("platform = %s", [platform], order="handle")

    def refresh_expiring_before(self, moment: datetime) -> tuple[Any, ...]:
        return self._where(
            "refresh_valid_until IS NOT NULL AND refresh_valid_until < %s",
            [ensure_utc(moment)],
            order="refresh_valid_until",
        )


class _PgSources(_PgRepository):
    def by_fingerprint(self, fingerprint: str) -> Any:
        return self._one_where("fingerprint = %s", [fingerprint])

    def rights_expiring_before(self, moment: datetime) -> tuple[Any, ...]:
        return self._where(
            "rights_expires_at IS NOT NULL AND rights_expires_at < %s",
            [ensure_utc(moment)],
            order="rights_expires_at",
        )

    def mark_used(self, channel_id: str, source_id: str, used_at: datetime) -> None:
        self._execute(
            sql.SQL(
                "INSERT INTO channel_source_uses "
                "(tenant_id, channel_id, source_id, used_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (channel_id, source_id) "
                "DO NOTHING"
            ),
            [self._uow.tenant_id, channel_id, source_id, ensure_utc(used_at)],
        )

    def used_by(self, channel_id: str) -> tuple[ChannelSourceUseRecord, ...]:
        cursor = self._execute(
            sql.SQL(
                "SELECT tenant_id, channel_id, source_id, used_at "
                "FROM channel_source_uses WHERE channel_id = %s ORDER BY used_at"
            ),
            [channel_id],
        )
        return tuple(ChannelSourceUseRecord(**row) for row in cursor.fetchall())

    def unused_by(self, channel_id: str) -> tuple[Any, ...]:
        # NOT EXISTS rather than NOT IN: `source_id` is not nullable here, but
        # NOT IN against a subquery that can yield NULL returns nothing at all,
        # and that failure is silent — an empty backlog reads as "no material
        # left" rather than as a bug.
        return self._where(
            "NOT EXISTS (SELECT 1 FROM channel_source_uses u "
            "WHERE u.source_id = sources.id AND u.channel_id = %s)",
            [channel_id],
            order="created_at",
        )


class _PgVideos(_PgRepository):
    def for_clip(self, clip_id: str) -> tuple[Any, ...]:
        return self._where("clip_id = %s", [clip_id], order="created_at")

    def in_state(self, *states: str) -> tuple[Any, ...]:
        return self._where("state = ANY(%s)", [list(states)], order="created_at")


class _PgClips(_PgRepository):
    def for_channel(self, channel_id: str, limit: int = 0) -> tuple[Any, ...]:
        return self._where(
            "channel_id = %s", [channel_id], order="created_at DESC", limit=limit
        )

    def for_source(self, source_id: str) -> tuple[Any, ...]:
        return self._where("source_id = %s", [source_id], order="start_ms")


class _PgSchedules(_PgRepository):
    def enabled_for_channel(self, channel_id: str) -> tuple[Any, ...]:
        return self._where("channel_id = %s AND enabled", [channel_id], order="id")


class _PgUploads(_PgRepository):
    def by_idempotency_key(self, key: str) -> Any:
        return self._one_where("idempotency_key = %s", [key])

    def due(self, now: datetime, limit: int = 100) -> tuple[Any, ...]:
        return self._where(
            "state IN ('scheduled', 'retrying') AND run_at <= %s",
            [ensure_utc(now)],
            order="run_at",
            limit=limit,
        )

    def for_account_between(
        self, account_id: str, start: datetime, end: datetime
    ) -> tuple[Any, ...]:
        return self._where(
            "account_id = %s AND run_at >= %s AND run_at < %s",
            [account_id, ensure_utc(start), ensure_utc(end)],
            order="run_at",
        )

    def for_channel_between(
        self, channel_id: str, start: datetime, end: datetime
    ) -> tuple[Any, ...]:
        return self._where(
            "channel_id = %s AND run_at >= %s AND run_at < %s",
            [channel_id, ensure_utc(start), ensure_utc(end)],
            order="run_at",
        )

    def in_state(self, *states: str) -> tuple[Any, ...]:
        return self._where("state = ANY(%s)", [list(states)], order="run_at")


class _PgMetrics(_PgRepository):
    def append(self, record: Any) -> Any:
        return self.add(record)

    def for_upload(self, upload_id: str) -> tuple[Any, ...]:
        return self._where("upload_id = %s", [upload_id], order="age_hours")

    def latest_for_upload(self, upload_id: str) -> Any:
        return self._one_where_ordered("upload_id = %s", [upload_id], "age_hours DESC")

    def between(self, start: datetime, end: datetime) -> tuple[Any, ...]:
        return self._where(
            "taken_at >= %s AND taken_at < %s",
            [ensure_utc(start), ensure_utc(end)],
            order="taken_at",
        )

    def at_age(self, age_hours: float, tolerance: float = 0.5) -> tuple[Any, ...]:
        return self._where(
            "abs(age_hours - %s) <= %s", [age_hours, tolerance], order="taken_at"
        )

    def _one_where_ordered(
        self, clause: str, params: Sequence[Any], order: str
    ) -> Any:
        rows = self._where(clause, params, order=order, limit=1)
        return rows[0] if rows else None


class _PgAcquisitions(_PgRepository):
    def for_ref(self, kind: str, ref_key: str, channel_id: str | None) -> Any:
        # `IS NOT DISTINCT FROM` rather than `=`: channel_id is nullable, and
        # `= NULL` is never true, so an unattached acquisition would never be
        # found and every pass would insert another one.
        return self._one_where(
            "kind = %s AND ref_key = %s AND channel_id IS NOT DISTINCT FROM %s",
            [kind, ref_key, channel_id],
        )

    def in_state(self, *states: str) -> tuple[Any, ...]:
        return self._where("state = ANY(%s)", [list(states)], order="created_at")

    def for_source(self, source_id: str) -> tuple[Any, ...]:
        return self._where("source_id = %s", [source_id], order="created_at")


class _PgRevenue(_PgRepository):
    def for_period(self, period: str) -> tuple[Any, ...]:
        return self._where("period = %s", [period], order="project_id")

    def for_project(self, project_id: str, period: str) -> Any:
        return self._one_where(
            "project_id = %s AND period = %s", [project_id, period]
        )


class _PgPools(_PgRepository):
    def on_platform(self, platform: str) -> tuple[Any, ...]:
        return self._where("platform = %s", [platform], order="id")


class _PgJobs(_PgRepository):
    def enqueue(self, record: Any) -> Any:
        if not record.dedupe_key:
            return self.add(record)
        # ON CONFLICT DO NOTHING returns no row, so the existing job has to be
        # read back. Both statements are in the caller's transaction, so a
        # concurrent producer either lost the insert (and this read finds its
        # row) or won it (and this read finds ours) — there is no window where
        # neither is visible.
        columns = self._table.writable
        statement = sql.SQL(
            "INSERT INTO jobs ({cols}) VALUES ({marks}) "
            "ON CONFLICT (tenant_id, dedupe_key) DO NOTHING RETURNING {out}"
        ).format(
            cols=self._columns(columns),
            marks=sql.SQL(", ").join(sql.Placeholder() * len(columns)),
            out=self._columns(self._table.columns),
        )
        cursor = self._execute(statement, self._values(record, columns))
        row = cursor.fetchone()
        if row is not None:
            return self._hydrate(row)
        return self._one_where("dedupe_key = %s", [record.dedupe_key])

    def claim(
        self,
        owner: str,
        now: datetime,
        lease_s: int = 300,
        kinds: tuple[str, ...] = (),
        limit: int = 1,
    ) -> tuple[Any, ...]:
        """Take work atomically.

        `FOR UPDATE SKIP LOCKED` is what lets several workers poll the same
        queue without coordinating: each skips rows another has already locked
        rather than blocking behind them. Without SKIP LOCKED, N workers
        serialise on the oldest job and the queue drains at single-worker
        speed while looking busy.
        """

        now = ensure_utc(now)
        clause = sql.SQL(" AND kind = ANY(%s)") if kinds else sql.SQL("")
        statement = sql.SQL(
            "WITH taken AS ("
            "  SELECT id FROM jobs"
            "  WHERE state = 'queued' AND run_after <= %s{kinds}"
            "  ORDER BY priority, run_after"
            "  LIMIT %s FOR UPDATE SKIP LOCKED"
            ") "
            "UPDATE jobs SET state = 'leased', lease_owner = %s, lease_until = %s "
            "WHERE id IN (SELECT id FROM taken) RETURNING {out}"
        ).format(kinds=clause, out=self._columns(self._table.columns))
        # The parameters appear in statement order: run_after, [kinds], limit,
        # owner, lease_until. Reordered here to match, because the LIMIT sits
        # inside the CTE and therefore before the UPDATE's own placeholders.
        ordered = [now]
        if kinds:
            ordered.append(list(kinds))
        ordered.extend([limit, owner, now + timedelta(seconds=lease_s)])
        cursor = self._execute(statement, ordered)
        # The CTE's ORDER BY decides *which* rows are claimed; it does not
        # survive into RETURNING, whose order is unspecified. Sorting here is
        # what makes a batch claim arrive in the same order it would in memory
        # — otherwise a worker that processes its batch in order would work
        # highest-priority-first on one implementation and not the other.
        rows = sorted(
            cursor.fetchall(), key=lambda r: (r["priority"], r["run_after"])
        )
        return self._hydrate_all(rows)

    def heartbeat(self, job_id: str, owner: str, until: datetime) -> bool:
        cursor = self._execute(
            sql.SQL(
                "UPDATE jobs SET lease_until = %s WHERE id = %s AND lease_owner = %s "
                "AND state IN ('leased', 'running')"
            ),
            [ensure_utc(until), job_id, owner],
        )
        return cursor.rowcount > 0

    def succeed(self, job_id: str, result: dict | None, now: datetime) -> Any:
        now = ensure_utc(now)
        cursor = self._execute(
            sql.SQL(
                "UPDATE jobs SET state = 'succeeded', result = %s, finished_at = %s, "
                "lease_owner = '', lease_until = NULL WHERE id = %s RETURNING {out}"
            ).format(out=self._columns(self._table.columns)),
            [Json(result) if result is not None else None, now, job_id],
        )
        row = cursor.fetchone()
        if row is None:
            raise NotFound(f"jobs: no {job_id!r}")
        return self._hydrate(row)

    def fail(
        self, job_id: str, error: str, retry_at: datetime | None, now: datetime
    ) -> Any:
        now = ensure_utc(now)
        # The retry decision is made in SQL against the row's own attempt
        # count. Read-then-write would let two workers each see "attempt 7 of
        # 8" and between them retry a ninth time.
        cursor = self._execute(
            sql.SQL(
                "UPDATE jobs SET "
                "  attempts = attempts + 1, "
                "  last_error = %s, "
                "  lease_owner = '', "
                "  lease_until = NULL, "
                "  state = CASE WHEN %s::timestamptz IS NOT NULL "
                "                 AND attempts + 1 < max_attempts "
                "               THEN 'queued'::\"JobState\" "
                "               ELSE 'dead'::\"JobState\" END, "
                "  run_after = CASE WHEN %s::timestamptz IS NOT NULL "
                "                     AND attempts + 1 < max_attempts "
                "                   THEN %s::timestamptz ELSE run_after END, "
                "  finished_at = CASE WHEN %s::timestamptz IS NOT NULL "
                "                       AND attempts + 1 < max_attempts "
                "                     THEN NULL ELSE %s END "
                "WHERE id = %s RETURNING {out}"
            ).format(out=self._columns(self._table.columns)),
            [
                error,
                retry_at, retry_at, retry_at, retry_at,
                now,
                job_id,
            ],
        )
        row = cursor.fetchone()
        if row is None:
            raise NotFound(f"jobs: no {job_id!r}")
        return self._hydrate(row)

    def reap(self, now: datetime, limit: int = 100) -> int:
        cursor = self._execute(
            sql.SQL(
                "WITH expired AS ("
                "  SELECT id FROM jobs WHERE state IN ('leased', 'running') "
                "    AND lease_until IS NOT NULL AND lease_until < %s "
                "  ORDER BY lease_until LIMIT %s FOR UPDATE SKIP LOCKED"
                ") "
                "UPDATE jobs SET state = 'queued', lease_owner = '', "
                "  lease_until = NULL WHERE id IN (SELECT id FROM expired)"
            ),
            [ensure_utc(now), limit],
        )
        return cursor.rowcount

    def pending(self, limit: int = 100) -> tuple[Any, ...]:
        return self._where(
            "state = 'queued'", [], order="priority, run_after", limit=limit
        )

    def in_state(self, *states: str) -> tuple[Any, ...]:
        return self._where("state = ANY(%s)", [list(states)], order="created_at")


# ---------------------------------------------------------------------------
# Unit of work
# ---------------------------------------------------------------------------


class PostgresUnitOfWork:
    """One transaction, one tenant, one connection borrowed from the pool.

    Entering opens a transaction and pins the tenant with `SET LOCAL`. Leaving
    normally commits; leaving by exception rolls back. There is no `commit()`
    to forget, which is deliberate — the failure mode of an explicit commit is
    work that looked saved and was not, and that is precisely what this layer
    is here to make impossible.
    """

    def __init__(self, database: PostgresDatabase, tenant_id: str) -> None:
        if not tenant_id:
            raise ValueError("a unit of work needs a tenant")
        self.tenant_id = tenant_id
        self._database = database
        self._connection: psycopg.Connection | None = None
        self._release: Callable[[psycopg.Connection], None] | None = None
        self._cursor: psycopg.Cursor | None = None
        self._rolled_back = False

        self.tenants = _PgTenants(self, TABLES["tenants"])
        self.users = _PgUsers(self, TABLES["users"])
        self.projects = _PgProjects(self, TABLES["projects"])
        self.channels = _PgChannels(self, TABLES["channels"])
        self.accounts = _PgAccounts(self, TABLES["social_accounts"])
        self.sources = _PgSources(self, TABLES["sources"])
        self.videos = _PgVideos(self, TABLES["videos"])
        self.clips = _PgClips(self, TABLES["clips"])
        self.schedules = _PgSchedules(self, TABLES["schedules"])
        self.uploads = _PgUploads(self, TABLES["uploads"])
        self.metrics = _PgMetrics(self, TABLES["metric_snapshots"])
        self.jobs = _PgJobs(self, TABLES["jobs"])
        self.revenue = _PgRevenue(self, TABLES["revenue_entries"])
        self.pools = _PgPools(self, TABLES["quota_pools"])
        self.acquisitions = _PgAcquisitions(self, TABLES["acquisition_runs"])

    # -- plumbing ----------------------------------------------------------

    def _execute(self, statement: Any, params: Sequence[Any] = ()):
        if self._cursor is None:
            raise RuntimeError("unit of work is not open — use it as a context manager")
        try:
            self._cursor.execute(statement, list(params))
        except psycopg.Error as error:
            raise _translate(error) from error
        return self._cursor

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> PostgresUnitOfWork:
        if self._connection is not None:
            raise RuntimeError("unit of work is not re-entrant")
        self._connection, self._release = self._database._acquire()
        self._connection.autocommit = False
        self._cursor = self._connection.cursor(row_factory=dict_row)
        # `set_config(..., true)` is `SET LOCAL` with a parameter. `SET LOCAL`
        # itself takes no parameters, and building the statement by string
        # interpolation is the one place a tenant id would reach the parser as
        # text rather than as a value.
        self._cursor.execute(
            "SELECT set_config('app.tenant_id', %s, true)", [self.tenant_id]
        )
        return self

    def __exit__(self, exc_type: object, *_: object) -> bool:
        connection, release = self._connection, self._release
        cursor = self._cursor
        self._connection = self._release = self._cursor = None
        if connection is None:
            return False
        try:
            if cursor is not None:
                cursor.close()
            if exc_type is not None or self._rolled_back:
                connection.rollback()
            else:
                connection.commit()
        finally:
            if release is not None:
                release(connection)
        return False

    def rollback(self) -> None:
        """Abandon the work. The block will not commit on exit."""

        self._rolled_back = True
        if self._connection is not None:
            self._connection.rollback()
            # The transaction that held `app.tenant_id` is gone, so a further
            # statement on this unit of work would run unscoped and be refused
            # by `app.current_tenant()`. Setting it again keeps the failure
            # about the rollback rather than about a missing GUC.
            if self._cursor is not None:
                self._cursor.execute(
                    "SELECT set_config('app.tenant_id', %s, true)", [self.tenant_id]
                )


class PostgresDatabase:
    """Owns the connection pool and hands out units of work.

    The pool matters at empire scale: fifty channels' workers each opening a
    connection per job is a thousand backends, and Postgres does not have a
    thousand backends. Connections are reused; the tenant is not, because it
    lives on the transaction.
    """

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 16,
        application_name: str = "clipforge",
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.dsn = dsn
        self.clock = clock
        self._pool: Any = None
        try:
            from psycopg_pool import ConnectionPool
        except ImportError:  # pragma: no cover - exercised only without the extra
            ConnectionPool = None  # type: ignore[assignment]

        self._kwargs = {"application_name": application_name}
        if ConnectionPool is not None:
            self._pool = ConnectionPool(
                dsn,
                min_size=min_size,
                max_size=max_size,
                kwargs=self._kwargs,
                open=True,
            )
            self._pool.wait(timeout=10)

    def _acquire(self) -> tuple[psycopg.Connection, Callable[[psycopg.Connection], None]]:
        if self._pool is not None:
            connection = self._pool.getconn()
            return connection, self._pool.putconn
        # No pool available. Correct, just slower — one connection per unit of
        # work. Kept as a path rather than a hard failure so the store works in
        # a minimal environment, and loud enough in the docs that nobody ships
        # it.
        connection = psycopg.connect(self.dsn, **self._kwargs)
        return connection, lambda c: c.close()

    def unit_of_work(self, tenant_id: str) -> PostgresUnitOfWork:
        return PostgresUnitOfWork(self, tenant_id)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None
