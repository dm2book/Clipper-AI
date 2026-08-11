"""Row-level security, tested against the database rather than the repository.

`test_store_contract.py` shows that the repositories keep tenants apart. That
is not the same claim. The repositories also check tenancy in Python, so those
tests would pass with row-level security switched off entirely — and if it were
ever switched off, the first raw query written outside this layer would quietly
read everyone's rows.

So every test here goes around the repositories and issues plain SQL with no
WHERE clause, as the application role, and asserts the database refuses. If
these pass, the isolation survives a hand-written query, a reporting job, and a
psql session.

Skips unless `CLIPFORGE_TEST_DSN` names a migrated database.
"""

from __future__ import annotations

import os
import unittest

_DSN = os.environ.get("CLIPFORGE_TEST_DSN", "")
_ADMIN_DSN = os.environ.get("CLIPFORGE_TEST_ADMIN_DSN", _DSN)

A = "ten_rls_a"
B = "ten_rls_b"


@unittest.skipUnless(_DSN, "set CLIPFORGE_TEST_DSN to test row-level security")
class RowLevelSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        import psycopg

        self.psycopg = psycopg
        with psycopg.connect(_ADMIN_DSN, autocommit=True) as connection:
            connection.execute("TRUNCATE TABLE tenants CASCADE")
        for tenant, tag in ((A, "a"), (B, "b")):
            with self._scoped(tenant) as cursor:
                cursor.execute(
                    "INSERT INTO tenants (id, name) VALUES (%s, %s)", [tenant, tenant]
                )
                cursor.execute(
                    "INSERT INTO projects (id, tenant_id, name) VALUES (%s, %s, %s)",
                    [f"proj_{tag}", tenant, "Brand"],
                )
                cursor.execute(
                    "INSERT INTO channels (id, tenant_id, project_id, name, niche) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    [f"ch_{tag}", tenant, f"proj_{tag}", "Cars", "cars"],
                )
                cursor.execute(
                    "INSERT INTO clips (id, tenant_id, channel_id, hook_text, "
                    "start_ms, end_ms, duration_s) "
                    "VALUES (%s, %s, %s, %s, 0, 30000, 30)",
                    [f"cl_{tag}", tenant, f"ch_{tag}", f"secret of {tenant}"],
                )

    class _Scope:
        """A transaction with `app.tenant_id` set, yielding a raw cursor."""

        def __init__(self, dsn: str, tenant: str | None) -> None:
            import psycopg

            self.connection = psycopg.connect(dsn)
            self.tenant = tenant

        def __enter__(self):
            cursor = self.connection.cursor()
            if self.tenant is not None:
                cursor.execute(
                    "SELECT set_config('app.tenant_id', %s, true)", [self.tenant]
                )
            return cursor

        def __exit__(self, exc_type, *_):
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
            self.connection.close()
            return False

    def _scoped(self, tenant: str | None, dsn: str = ""):
        return self._Scope(dsn or _DSN, tenant)

    # -- reads -------------------------------------------------------------

    def test_an_unfiltered_select_returns_only_this_tenants_rows(self) -> None:
        with self._scoped(B) as cursor:
            cursor.execute("SELECT id, hook_text FROM clips")
            rows = cursor.fetchall()
        self.assertEqual(rows, [("cl_b", f"secret of {B}")])

    def test_naming_another_tenants_row_by_id_still_finds_nothing(self) -> None:
        """Not merely filtered out of a list — invisible even when asked for
        directly, which is what stops an enumerated id from confirming that a
        row exists."""

        with self._scoped(B) as cursor:
            cursor.execute("SELECT count(*) FROM clips WHERE id = 'cl_a'")
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_a_join_cannot_be_used_to_reach_across_the_boundary(self) -> None:
        with self._scoped(B) as cursor:
            cursor.execute(
                "SELECT count(*) FROM clips c "
                "JOIN channels ch ON ch.id = c.channel_id "
                "JOIN tenants t ON t.id = ch.tenant_id"
            )
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_an_aggregate_does_not_leak_a_count(self) -> None:
        """A policy applied to rows but not to aggregates would leak volumes:
        how many clips a competitor produced is itself commercial information."""

        with self._scoped(B) as cursor:
            cursor.execute("SELECT count(*) FROM clips")
            self.assertEqual(cursor.fetchone()[0], 1)

    # -- writes ------------------------------------------------------------

    def test_an_unfiltered_update_cannot_touch_another_tenants_row(self) -> None:
        with self._scoped(B) as cursor:
            cursor.execute("UPDATE clips SET hook_text = 'overwritten'")
            self.assertEqual(cursor.rowcount, 1)
        with self._scoped(A) as cursor:
            cursor.execute("SELECT hook_text FROM clips WHERE id = 'cl_a'")
            self.assertEqual(cursor.fetchone()[0], f"secret of {A}")

    def test_an_unfiltered_delete_cannot_remove_another_tenants_row(self) -> None:
        with self._scoped(B) as cursor:
            cursor.execute("DELETE FROM clips")
            self.assertEqual(cursor.rowcount, 1)
        with self._scoped(A) as cursor:
            cursor.execute("SELECT count(*) FROM clips")
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_inserting_under_another_tenants_id_is_refused(self) -> None:
        with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
            with self._scoped(B) as cursor:
                cursor.execute(
                    "INSERT INTO clips (id, tenant_id, channel_id) "
                    "VALUES ('cl_x', %s, 'ch_a')",
                    [A],
                )

    def test_relabelling_a_row_into_another_tenant_is_refused(self) -> None:
        """The WITH CHECK half of the policy. Without it a tenant could move
        its own row into someone else's account — the write equivalent of
        reading across the boundary."""

        with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
            with self._scoped(B) as cursor:
                cursor.execute("UPDATE clips SET tenant_id = %s WHERE id = 'cl_b'", [A])

    # -- the scope itself --------------------------------------------------

    def test_a_statement_with_no_tenant_set_is_an_error_not_an_empty_result(self) -> None:
        """Fail loud. A policy that quietly returns nothing turns a forgotten
        scope into "the dashboard is empty", which gets triaged as a product
        bug for a week."""

        with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
            with self._scoped(None) as cursor:
                cursor.execute("SELECT count(*) FROM clips")

    def test_the_scope_does_not_outlive_its_transaction(self) -> None:
        """`SET LOCAL`, not `SET`. This is what makes the connection pool safe:
        a connection handed back cannot carry one customer's tenant into the
        next customer's query."""

        connection = self.psycopg.connect(_DSN)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [A])
                cursor.execute("SELECT count(*) FROM clips")
                self.assertEqual(cursor.fetchone()[0], 1)
            connection.commit()
            with connection.cursor() as cursor:
                with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                    cursor.execute("SELECT count(*) FROM clips")
            connection.rollback()
        finally:
            connection.close()

    def test_the_application_role_cannot_switch_the_policy_off(self) -> None:
        """`SET row_security = off` is available to superusers and to
        BYPASSRLS roles. The application role is neither, which is the reason
        it is neither.

        Postgres refuses in one of two places — at the `SET` for a role that
        may not set it, or at the query, with "query would be affected by row
        level security policy". Either is correct. What must never happen is
        the third outcome: the query succeeding and returning both tenants.
        """

        with self._scoped(B) as cursor:
            try:
                cursor.execute("SET LOCAL row_security = off")
                cursor.execute("SELECT count(*) FROM clips")
            except self.psycopg.errors.InsufficientPrivilege:
                return
            self.fail(
                f"row_security = off bypassed the policy: saw "
                f"{cursor.fetchone()[0]} clips across tenants"
            )

    def test_the_application_role_is_neither_superuser_nor_owner(self) -> None:
        """The two ways row-level security silently becomes a no-op."""

        with self._scoped(B) as cursor:
            cursor.execute(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
            superuser, bypass = cursor.fetchone()
            self.assertFalse(superuser, "the application must not be superuser")
            self.assertFalse(bypass, "the application must not hold BYPASSRLS")
            cursor.execute(
                "SELECT tableowner = current_user FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename = 'clips'"
            )
            self.assertFalse(
                cursor.fetchone()[0], "the application must not own the tables"
            )

    def test_every_tenant_scoped_table_has_a_policy(self) -> None:
        """A table added without one is the failure this catches: `ALTER TABLE
        ... ENABLE ROW LEVEL SECURITY` is not something Prisma emits, so a new
        model is unprotected until someone remembers migration 002."""

        expected = {
            "tenants", "users", "projects", "channels", "social_accounts",
            "sources", "channel_source_uses", "videos", "clips", "schedules",
            "uploads", "metric_snapshots", "jobs", "revenue_entries",
            "quota_pools", "acquisition_runs",
        }
        with self.psycopg.connect(_ADMIN_DSN) as connection:
            rows = connection.execute(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
                "       count(p.polname) "
                "FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "LEFT JOIN pg_policy p ON p.polrelid = c.oid "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                "GROUP BY 1, 2, 3"
            ).fetchall()
        by_table = {name: (enabled, forced, count) for name, enabled, forced, count in rows}
        self.assertEqual(
            expected, expected & set(by_table), "a table from the schema is missing"
        )
        for table in sorted(expected):
            enabled, forced, policies = by_table[table]
            self.assertTrue(enabled, f"{table}: row-level security not enabled")
            self.assertTrue(forced, f"{table}: not FORCEd, so the owner is exempt")
            self.assertGreaterEqual(policies, 1, f"{table}: no policy")

    # -- append-only metrics ----------------------------------------------

    def test_metric_snapshots_cannot_be_rewritten(self) -> None:
        """Enforced as a privilege, not as a repository convention. Every
        finding the analytics engine reports rests on matched-age comparison,
        and a snapshot edited after the fact cannot be recovered."""

        with self._scoped(B) as cursor:
            cursor.execute(
                "INSERT INTO social_accounts (id, tenant_id, channel_id, platform) "
                "VALUES ('acc_b', %s, 'ch_b', 'tiktok')", [B]
            )
            cursor.execute(
                "INSERT INTO uploads (id, tenant_id, channel_id, account_id, "
                "platform, run_at, idempotency_key) "
                "VALUES ('up_b', %s, 'ch_b', 'acc_b', 'tiktok', now(), 'k')", [B]
            )
            cursor.execute(
                "INSERT INTO metric_snapshots (id, tenant_id, upload_id, taken_at, "
                "age_hours, views) VALUES ('ms_b', %s, 'up_b', now(), 24, 1000)", [B]
            )
        with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
            with self._scoped(B) as cursor:
                cursor.execute("UPDATE metric_snapshots SET views = 999999")
        with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
            with self._scoped(B) as cursor:
                cursor.execute("DELETE FROM metric_snapshots")


if __name__ == "__main__":
    unittest.main()
