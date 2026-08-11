# Database

PostgreSQL 16. Schema and migrations owned by Prisma; the application talks to
Postgres through psycopg from `src/clipforge/store/`.

## Why Prisma, given this is a Python project

Prisma is two halves. The client is a TypeScript library and is not used here —
there is no `generator` block in `schema.prisma`, so nothing is generated. The
migration engine is used, and it is the reason Prisma is here at all: one
declarative schema, a versioned and checksummed migration history, and plain
SQL on disk that a reviewer can read before it touches production.

`prisma-client-py` was considered and rejected. It is a third-party port rather
than a Prisma product, and betting the data layer of a system whose whole
promise is "nothing important is lost" on an unofficial client is not a trade
worth making.

## Layout

```
db/
  schema.prisma                     the models — the source of truth
  roles.sql                         cluster roles; run once, as a superuser
  migrations/
    20260810143555_init/            tables, indexes, foreign keys
    20260810143700_row_level_security/
                                    policies, grants, updated_at triggers
    20260810205229_empire_revenue_and_quota_pools/
                                    two tables, and the hand-written half a
                                    new model always needs
    20260811010500_source_acquisition/
                                    acquisition_runs
```

## Setting one up

```sh
# 1. Roles. Once per cluster, as a superuser. Passwords come from the
#    environment; they are never written into the file.
psql -v ON_ERROR_STOP=1 \
     -v owner_password="$PGOWNER_PASSWORD" \
     -v app_password="$PGAPP_PASSWORD" \
     -v worker_password="$PGWORKER_PASSWORD" \
     -f db/roles.sql

# 2. Database, owned by the migration role.
createdb clipforge -O clipforge_owner

# 3. Schema. `migrate deploy` applies what is committed and nothing else —
#    it never generates, never resets, never prompts.
cd db
DATABASE_URL="postgresql://clipforge_owner:$PGOWNER_PASSWORD@host/clipforge" \
  npx prisma migrate deploy
```

The application then connects as `clipforge_app`, never as `clipforge_owner`.

## Changing the schema

```sh
cd db
export DATABASE_URL="postgresql://clipforge_owner:...@localhost/clipforge"
export SHADOW_DATABASE_URL="postgresql://clipforge_owner:...@localhost/clipforge_shadow"
npx prisma migrate dev --name what_changed
```

`migrate dev` diffs against a throwaway "shadow" database. Prisma will create
one itself if the migration role holds CREATEDB — this one deliberately does
not, for the same reason the app role is not superuser — so provision it once:

```sh
createdb clipforge_shadow -O clipforge_owner
```

**A new model needs a hand-written follow-up migration.** Prisma emits tables,
indexes and foreign keys. It does not emit `ENABLE ROW LEVEL SECURITY`, does
not emit policies, and does not emit the `updated_at` trigger. A model added
without them is a table with no tenant isolation, and nothing in the Prisma
tooling will say so. `tests/test_row_level_security.py` will —
`test_every_tenant_scoped_table_has_a_policy` enumerates `pg_class` and fails
on any public table that is unprotected. Copy the shape from
`20260810143700_row_level_security/migration.sql`.

## The three roles

| Role | Used by | RLS | Reach |
| --- | --- | --- | --- |
| `clipforge_owner` | migrations only | subject to it (`FORCE`) | owns the schema |
| `clipforge_app` | the application | enforced | every table, one tenant at a time |
| `clipforge_worker` | the job dispatcher | `BYPASSRLS` | `jobs`, and nothing else |

`clipforge_app` is deliberately neither superuser nor the table owner: Postgres
lets both bypass row-level security, so an application connected as either
turns every policy into a no-op — while the isolation tests keep passing.

`clipforge_worker` is the one role that sees across tenants, because the queue
is necessarily cross-tenant: a worker picking up the oldest due job cannot know
whose job it is until it has read one. Its grants stop at `jobs`. The contract
is: claim a job, read its `tenant_id`, then open a `clipforge_app` connection
scoped to that tenant and do the actual work there.

`FORCE ROW LEVEL SECURITY` applies to the owner too. That is not defence
against an attacker — an owner has DDL rights and could drop the policies. It
is defence against the likeliest real failure: an application pointed at
`clipforge_owner` by a copy-pasted `DATABASE_URL`. With `FORCE`, that mistake
raises on the first query instead of quietly serving every tenant's rows to
everyone.

A migration that genuinely needs to backfill across tenants says so explicitly
and puts it back:

```sql
ALTER TABLE public.clips NO FORCE ROW LEVEL SECURITY;
-- ... backfill ...
ALTER TABLE public.clips FORCE ROW LEVEL SECURITY;
```

## The tenant scope

Every policy is `tenant_id = app.current_tenant()`. `app.current_tenant()`
reads `app.tenant_id`, a per-transaction setting the store writes with
`set_config(..., true)` — the parameterised form of `SET LOCAL`.

`SET LOCAL` is what makes the connection pool safe. It is undone when the
transaction ends, so a connection handed back to the pool cannot carry one
customer's tenant into the next customer's query. A plain `SET` would, and the
resulting bug — one tenant occasionally seeing another's clips, depending on
pool scheduling — is close to undiagnosable from a log.

`app.current_tenant()` **raises** when the setting is missing, rather than
returning NULL. A NULL would make every policy fail closed, which sounds safer
and is not: a forgotten scope would look like a tenant with no data, and "the
dashboard is empty" gets triaged as a product bug for a week. An error names
the mistake at the first query.

## Conventions worth knowing before adding a model

* **`tenant_id` on the row, never via a join.** A policy has to be a predicate
  over the row it protects. A policy that needs a join is a policy that will
  one day be written wrong, and no test will notice.
* **Every index leads with `tenant_id`.** The policy predicate is ANDed into
  every query, so an index that does not start with the tenant is an index the
  planner will not use.
* **`@db.Timestamptz(6)` on every `DateTime`.** Prisma's default is
  `timestamp(3)` — no time zone, millisecond precision. In a system that
  schedules across IANA zones, a naive timestamp is a post at the wrong hour
  twice a year, and the truncation silently changes values on round-trip.
* **`@map` every camelCase field to snake_case.** Hand-written SQL is the norm
  here; `"tenantId"` needs quoting everywhere and unquoted `tenantId` folds to
  `tenantid` and fails.
* **`@updatedAt @default(now())`.** The `@updatedAt` half is maintained by the
  Prisma client, which is not used, so the trigger in migration 002 does it.
  The default covers the INSERT, which the trigger does not.

## Running the tests against a real database

```sh
createdb clipforge_test -O clipforge_owner
(cd db && DATABASE_URL=postgresql://clipforge_owner:...@localhost/clipforge_test \
   npx prisma migrate deploy)

CLIPFORGE_TEST_DSN=postgresql://clipforge_app:...@localhost/clipforge_test \
CLIPFORGE_TEST_ADMIN_DSN=postgresql://clipforge_owner:...@localhost/clipforge_test \
PYTHONPATH=src python -m unittest discover -s tests
```

Without `CLIPFORGE_TEST_DSN` the Postgres cases skip and say so. The in-memory
contract tests still run — but they are only evidence about the Postgres path
because the same assertions pass against it, so a green suite full of skips is
not a green suite.

## The cost of FORCE, and where you will meet it

`ADD CONSTRAINT ... FOREIGN KEY` makes Postgres run a validation query against
the *referenced* table. If that table is FORCEd, the read goes through
`tenant_isolation`, which calls `app.current_tenant()`, which raises — because a
migration has no tenant scope. The migration then dies, on an empty table it was
about to create.

Setting a sentinel scope instead would be worse: the validation would run
against one tenant's rows and quietly conclude the constraint holds. Lift FORCE
for the length of the statement and put it back:

```sql
ALTER TABLE public.tenants NO FORCE ROW LEVEL SECURITY;
ALTER TABLE public.projects NO FORCE ROW LEVEL SECURITY;

-- ... Prisma's AddForeignKey statements ...

ALTER TABLE public.tenants FORCE ROW LEVEL SECURITY;
ALTER TABLE public.projects FORCE ROW LEVEL SECURITY;
```

`20260810205229_empire_revenue_and_quota_pools` is the worked example. The
failure is loud rather than silent, and
`test_every_tenant_scoped_table_has_a_policy` catches a migration that lifts
FORCE and forgets to restore it — so this is an annoyance rather than a hazard.
It is still the main ongoing cost of the FORCE decision, and worth knowing
before the first FK migration rather than during it.

One related trap: a migration must apply to an **empty** database as well as to
a live one, because `migrate dev` replays the whole history into a throwaway
shadow database. Migration 002 originally revoked privileges on
`_prisma_migrations`, which the shadow database does not have; that one
unguarded statement made every subsequent `migrate dev` impossible. Anything
touching a table Prisma itself manages needs an existence check.
